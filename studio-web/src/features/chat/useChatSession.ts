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
  // Reply text of the current model turn, from the service's TEXT_DELTA events. Only a
  // model that really streams sends them; the saved transcript replaces it on completion.
  const [streamingText, setStreamingText] = useState<string>('');
  // P39 G4: the agent's output guardrails check the reply before any of it is shown
  // (MODEL_STARTED carries stream_guard "hold" or "chunked"). The live bubble then shows a
  // "checking reply" state instead of unchecked text, and only checked text afterwards.
  const [checkingReply, setCheckingReply] = useState<boolean>(false);

  const isMountedRef = useRef<boolean>(true);
  const turnAbortRef = useRef<AbortController | null>(null);
  // The session shown now. A turn only writes to the page while its session is still shown.
  const currentSessionRef = useRef<string | null>(null);
  const showSession = useCallback((id: string | null) => {
    currentSessionRef.current = id;
    setCurrentSessionId(id);
  }, []);
  // Leaving the session of a running turn stops it: the service saves a turn only when it
  // completes, so nothing half-done is kept and nothing lands in the next session.
  const leaveRunningTurn = useCallback(() => {
    turnAbortRef.current?.abort();
  }, []);

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
      if (currentSessionRef.current !== sessionId) leaveRunningTurn();
      showSession(sessionId);
      try {
        const detail: SessionDetail = await manager.client.sessions.get(sessionId);
        // A slower answer for a session the user already left must not replace the transcript.
        if (!isMountedRef.current || currentSessionRef.current !== sessionId) return;
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
    [manager, t, leaveRunningTurn, showSession]
  );

  const createSession = useCallback(
    async (customId?: string, title?: string) => {
      setError(null);
      leaveRunningTurn();
      try {
        const newSession = await manager.client.sessions.create({
          session_id: customId,
          title,
        });
        await refreshSessions();
        if (!isMountedRef.current) return;
        showSession(newSession.session_id);
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
    [manager, refreshSessions, t, leaveRunningTurn, showSession]
  );

  const deleteSession = useCallback(
    async (sessionId: string) => {
      setError(null);
      try {
        if (currentSessionRef.current === sessionId) leaveRunningTurn();
        await manager.client.sessions.delete(sessionId);
        if (currentSessionRef.current === sessionId) {
          showSession(null);
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
    [manager, refreshSessions, t, leaveRunningTurn, showSession]
  );

  const stopTurn = useCallback(() => {
    turnAbortRef.current?.abort();
  }, []);

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim() || sending) return;
      setError(null);
      setSending(true);

      let targetSessionId = currentSessionRef.current;
      if (!targetSessionId) {
        const created = await createSession();
        if (!created) {
          setSending(false);
          return;
        }
        targetSessionId = created.session_id;
      }
      const turnSession = targetSessionId;
      const stillShown = () => currentSessionRef.current === turnSession;

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
      let guardedTurn = false; // the current model turn's reply is checked before it is shown

      try {
        // A saved agent runs on its own model: send agent_id and never a model.
        const request = selectedAgentId
          ? { message: text, language, agent_id: selectedAgentId }
          : { message: text, language, model: selectedModel };
        const stream = manager.client.sessions.chatStream(targetSessionId, request, { signal: abort.signal });
        for await (const ev of stream) {
          if (!isMountedRef.current) return;
          if (!stillShown()) break;
          if (ev.kind === 'run_event') {
            if (ev.type === 'TEXT_DELTA') {
              const piece = typeof ev.data?.text === 'string' ? ev.data.text : '';
              if (piece) setStreamingText((prev) => prev + piece);
            } else if (ev.type === 'MODEL_STARTED') {
              setStreamingText(''); // a new model turn writes a new reply
              guardedTurn = isGuardedTurn(ev.data);
              setCheckingReply(guardedTurn);
            } else if (ev.type === 'TOOL_STARTED') {
              setCheckingReply(false); // the turn asked for tools; it was not the reply
            } else if (ev.type === 'FINAL_OUTPUT' && guardedTurn) {
              // FINAL_OUTPUT comes only after the output guardrails passed (rewrites applied).
              // The guardrails passed: show the checked reply until the transcript arrives.
              setStreamingText(typeof ev.data?.output === 'string' ? ev.data.output : '');
              setCheckingReply(false);
            } else if (ev.type === 'GUARDRAIL_TRIPPED') {
              // Blocked: discard anything shown from this reply.
              setStreamingText('');
              setCheckingReply(false);
            }
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
      setStreamingText('');
      setCheckingReply(false);

      if (!stillShown()) {
        // The user moved to another conversation: this turn's text, transcript and notices
        // belong to a session that is no longer shown.
        setActivity(null);
        setSending(false);
        await refreshSessions();
        return;
      }

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
    [sending, language, selectedModel, selectedAgentId, createSession, manager, refreshSessions, t]
  );

  // The delivery label describes the last reply of this session and model; a different
  // choice has not answered yet.
  useEffect(() => {
    setDelivery(null);
  }, [selectedModel, selectedAgentId, currentSessionId]);

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
    streamingText,
    checkingReply,
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

/** True when the model turn's reply is checked by output guardrails before it is shown. */
export function isGuardedTurn(data: Record<string, any> | undefined): boolean {
  const mode = data?.stream_guard;
  return mode === 'hold' || mode === 'chunked';
}

function describeRunEvent(type: string, data: Record<string, any>, t: Translate = englishT): string {
  switch (type) {
    case 'MODEL_STARTED':
      if (isGuardedTurn(data)) return t('chat.checkingReply');
      return t('chat.modelTurn').replace('{turn}', String(data.turn ?? '')).trim();
    case 'GUARDRAIL_TRIPPED':
      return t('chat.replyBlocked');
    case 'TOOL_STARTED':
      return t('chat.toolStarted').replace('{name}', String(data.tool_name ?? '')).trim();
    case 'TOOL_COMPLETED':
      return t('chat.toolCompleted').replace('{name}', String(data.tool_name ?? '')).replace('  ', ' ');
    case 'FINAL_OUTPUT':
      return t('chat.saving');
    case 'TEXT_DELTA':
      return t('chat.writing');
    default:
      return type.replace(/_/g, ' ').toLowerCase();
  }
}
