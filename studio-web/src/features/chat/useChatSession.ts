// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useLatestT, englishT, type Translate } from '../../i18n';
import { useState, useEffect, useCallback, useRef } from 'react';
import { useApi } from '../../api/context';
import type {
  SessionListItem,
  SessionDetail,
  ChatMessage,
  ToolExecutionTrace,
  TokenUsage,
  ChatResponse,
  ChatModelInfo,
  RefusedToolCall,
} from './types';
import { FAKE_MODEL_NAME, normalizeChatModels, defaultChatModel, chatFailureCode } from './types';
import type { AgentSummary } from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';

export function useChatSession() {
  const t = useLatestT();
  const { manager, connection } = useApi();
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [traces, setTraces] = useState<ToolExecutionTrace[]>([]);
  const [usage, setUsage] = useState<TokenUsage | null>(null);
  const [latencyMs, setLatencyMs] = useState<number | null>(null);
  const [language, setLanguage] = useState<'en' | 'hi'>('en');
  const [models, setModels] = useState<ChatModelInfo[]>(() => normalizeChatModels(null));
  const [selectedModel, setSelectedModel] = useState<string>(FAKE_MODEL_NAME);
  const [delivery, setDelivery] = useState<string | null>(null);
  // P26: chat with a saved agent instead of the demo (it runs on its own model).
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>('');
  const [refusedToolCalls, setRefusedToolCalls] = useState<RefusedToolCall[]>([]);

  const [loadingSessions, setLoadingSessions] = useState<boolean>(true);
  const [loadingSession, setLoadingSession] = useState<boolean>(false);
  const [sending, setSending] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  // Latest live step of the in-flight turn, e.g. "Running tool calculate".
  const [activity, setActivity] = useState<string | null>(null);
  // Set when a turn ended without completing; nothing from it was saved.
  const [interrupted, setInterrupted] = useState<string | null>(null);
  // Service code of the interrupted turn (e.g. AGENT_TURN_LIMIT), for a plain explanation.
  const [interruptedCode, setInterruptedCode] = useState<string | null>(null);

  const isMountedRef = useRef<boolean>(true);
  const turnAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const refreshSessions = useCallback(async () => {
    setLoadingSessions(true);
    try {
      const response = await manager.client.sessions.list({ limit: 50 });
      if (!isMountedRef.current) return;
      setSessions(response.items || []);
      return response.items;
    } catch (err: unknown) {
      if (!isMountedRef.current) return;
      const msg = formatApiError(err, t);
      setError(t('chat.listFailed').replace('{message}', msg));
      return [];
    } finally {
      if (isMountedRef.current) {
        setLoadingSessions(false);
      }
    }
  }, [manager, t]);

  const selectSession = useCallback(
    async (sessionId: string) => {
      setLoadingSession(true);
      setError(null);
      setRefusedToolCalls([]); // they belong to the previous session's last turn
      setCurrentSessionId(sessionId);
      try {
        const detail: SessionDetail = await manager.client.sessions.get(sessionId);
        if (!isMountedRef.current) return;
        setMessages(detail.messages || []);
      } catch (err: unknown) {
        if (!isMountedRef.current) return;
        const msg = formatApiError(err, t);
        setError(t('chat.loadFailed').replace('{id}', sessionId).replace('{message}', msg));
      } finally {
        if (isMountedRef.current) {
          setLoadingSession(false);
        }
      }
    },
    [manager, t]
  );

  const createSession = useCallback(
    async (customId?: string, title?: string) => {
      setError(null);
      try {
        const newSession = await manager.client.sessions.create({
          session_id: customId,
          title,
        });
        await refreshSessions();
        if (!isMountedRef.current) return;
        setCurrentSessionId(newSession.session_id);
        setMessages([]);
        setRefusedToolCalls([]);
        setTraces([]);
        setUsage(null);
        setLatencyMs(null);
        return newSession;
      } catch (err: unknown) {
        if (!isMountedRef.current) return;
        const msg = formatApiError(err, t);
        setError(t('chat.createFailed').replace('{message}', msg));
        return null;
      }
    },
    [manager, refreshSessions, t]
  );

  const deleteSession = useCallback(
    async (sessionId: string) => {
      setError(null);
      try {
        await manager.client.sessions.delete(sessionId);
        if (currentSessionId === sessionId) {
          setCurrentSessionId(null);
          setMessages([]);
          setRefusedToolCalls([]);
          setTraces([]);
          setUsage(null);
          setLatencyMs(null);
        }
        await refreshSessions();
      } catch (err: unknown) {
        if (!isMountedRef.current) return;
        const msg = formatApiError(err, t);
        setError(t('chat.deleteFailed').replace('{message}', msg));
      }
    },
    [manager, currentSessionId, refreshSessions, t]
  );

  const stopTurn = useCallback(() => {
    turnAbortRef.current?.abort();
  }, []);

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim() || sending) return;
      setError(null);
      setSending(true);

      let targetSessionId = currentSessionId;
      if (!targetSessionId) {
        const created = await createSession();
        if (!created) {
          setSending(false);
          return;
        }
        targetSessionId = created.session_id;
      }

      // Optimistically show user message
      const optimisticMsg: ChatMessage = {
        role: 'user',
        content: text,
      };
      setMessages((prev) => [...prev, optimisticMsg]);

      setInterrupted(null);
      setInterruptedCode(null);
      setActivity(t('chat.starting'));
      const abort = new AbortController();
      turnAbortRef.current = abort;
      let completed: ChatResponse | null = null;
      let failure: string | null = null;
      let failureCode: string | null = null;

      try {
        // A saved agent runs on its own model: send agent_id and never a model.
        const request = selectedAgentId
          ? { message: text, language, agent_id: selectedAgentId }
          : { message: text, language, model: selectedModel };
        const stream = manager.client.sessions.chatStream(targetSessionId, request, { signal: abort.signal });
        for await (const ev of stream) {
          if (!isMountedRef.current) return;
          if (ev.kind === 'run_event') {
            setActivity(describeRunEvent(ev.type, ev.data, t));
          } else if (ev.kind === 'chat_completed') {
            completed = ev.response;
          } else {
            failure = `${ev.code}: ${ev.message}`;
            failureCode = chatFailureCode(ev.code);
          }
        }
      } catch (err: unknown) {
        failure = abort.signal.aborted ? 'Stopped by you' : formatApiError(err, t);
        failureCode = abort.signal.aborted ? null : chatFailureCode(err);
      } finally {
        turnAbortRef.current = null;
      }
      if (!isMountedRef.current) return;

      if (completed) {
        setMessages(completed.messages || []);
        if (completed.tool_executions && completed.tool_executions.length > 0) {
          setTraces((prev) => [...prev, ...completed.tool_executions]);
        }
        setUsage(completed.usage);
        setLatencyMs(completed.latency_ms);
        setDelivery(completed.delivery ?? null);
        setRefusedToolCalls(completed.refused_tool_calls ?? []);
      } else {
        setRefusedToolCalls([]);
        // The turn is saved only on completion; reload the authoritative transcript.
        setInterrupted(t('chat.interrupted').replace('{reason}', failure ?? t('chat.streamEnded')));
        setInterruptedCode(failureCode);
        try {
          const detail = await manager.client.sessions.get(targetSessionId);
          if (isMountedRef.current) setMessages(detail.messages || []);
        } catch {
          if (isMountedRef.current) setMessages((prev) => prev.slice(0, -1));
        }
      }
      setActivity(null);
      setSending(false);
      await refreshSessions();
    },
    [currentSessionId, sending, language, selectedModel, selectedAgentId, createSession, manager, refreshSessions, t]
  );

  // Saved agents of the workspace for the agent selector (none: the demo only).
  useEffect(() => {
    let cancelled = false;
    setSelectedAgentId(''); // never send one workspace's agent to another
    setAgents([]);
    manager.client.agents
      .list()
      .then((res) => {
        if (cancelled) return;
        setAgents(res.items || []);
        setSelectedAgentId((cur) => (cur && !(res.items || []).some((a) => a.agent_id === cur) ? '' : cur));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // Reload on a workspace switch too: an agent of the previous workspace must not be sent.
  }, [manager, connection.workspace]);

  // Models the operator allowed; falls back to the offline fixture if unavailable.
  useEffect(() => {
    let cancelled = false;
    manager.client.sessions
      .models()
      .then((items) => {
        if (cancelled) return;
        const list = normalizeChatModels(items);
        setModels(list);
        setSelectedModel((cur) => (list.some((m) => m.name === cur) ? cur : defaultChatModel(list)));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [manager]);

  // Initial load
  useEffect(() => {
    refreshSessions().then((items) => {
      if (items && items.length > 0 && !currentSessionId) {
        selectSession(items[0].session_id);
      }
    });
  }, [refreshSessions]);

  return {
    sessions,
    currentSessionId,
    messages,
    traces,
    usage,
    latencyMs,
    language,
    models,
    selectedModel,
    setSelectedModel,
    delivery,
    agents,
    selectedAgentId,
    setSelectedAgentId,
    refusedToolCalls,
    loadingSessions,
    loadingSession,
    sending,
    activity,
    interrupted,
    interruptedCode,
    error,
    setLanguage,
    refreshSessions,
    selectSession,
    createSession,
    deleteSession,
    sendMessage,
    stopTurn,
    clearError: () => setError(null),
  };
}

function describeRunEvent(type: string, data: Record<string, any>, t: Translate = englishT): string {
  switch (type) {
    case 'MODEL_STARTED':
      return t('chat.modelTurn').replace('{turn}', String(data.turn ?? '')).trim();
    case 'TOOL_STARTED':
      return t('chat.toolStarted').replace('{name}', String(data.tool_name ?? '')).trim();
    case 'TOOL_COMPLETED':
      return t('chat.toolCompleted').replace('{name}', String(data.tool_name ?? '')).replace('  ', ' ');
    case 'FINAL_OUTPUT':
      return t('chat.saving');
    default:
      return type.replace(/_/g, ' ').toLowerCase();
  }
}
