// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useRef, useEffect, useId, KeyboardEvent } from 'react';
import { useChatSession } from './useChatSession';
import { DEFAULT_QUICK_PROMPTS, FAKE_MODEL_NAME, deliveryLabel, isTurnLimitCode } from './types';
import type { SessionListItem } from './types';
import { CitationFootnotes } from './CitationFootnotes';
import { extractCitations } from './citations';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { useCompactLayout } from '../../app/useCompactLayout';

/** Examples offered in an empty conversation; the full set stays in DEFAULT_QUICK_PROMPTS. */
const EXAMPLE_PROMPTS = DEFAULT_QUICK_PROMPTS.slice(0, 3);

function SessionList({
  sessions,
  currentSessionId,
  loading,
  busy,
  showTitle,
  onSelect,
  onDelete,
  onCreate,
}: {
  sessions: SessionListItem[];
  currentSessionId: string | null;
  loading: boolean;
  /** A reply is running: switching, creating or deleting would leave its turn behind. */
  busy: boolean;
  /** The drawer already names the list in its dialog title. */
  showTitle: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onCreate: () => void;
}) {
  const { t } = useI18n();
  const hintId = useId();
  return (
    // Named region rather than a heading, so the page h1 stays the first heading.
    <section className="chat-sessions" aria-label={t('ui.conversations')}>
      <div className="chat-sessions-header">
        {showTitle && <span className="chat-sessions-title" aria-hidden="true">{t('ui.conversations')}</span>}
        <Button
          size="sm"
          variant="ghost"
          className="chat-new-conversation"
          onClick={onCreate}
          disabled={busy}
          aria-describedby={busy ? hintId : undefined}
          data-testid="new-chat-button"
        >
          {t('chat.newConversation')}
        </Button>
      </div>
      {busy && <p id={hintId} className="resource-help" style={{ paddingBottom: 8 }}>{t('chat.busySessions')}</p>}
      {loading ? (
        <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '16px 0' }}>
          <Spinner size={16} /> <span className="resource-help">{t('common.loading')}</span>
        </div>
      ) : sessions.length === 0 ? (
        <p className="resource-help" style={{ padding: '16px 0' }}>{t('chat.noConversations')}</p>
      ) : (
        <ul className="chat-session-list">
          {sessions.map((sess) => (
            <li key={sess.session_id}>
              <button
                type="button"
                className="chat-session"
                aria-pressed={sess.session_id === currentSessionId}
                disabled={busy}
                aria-describedby={busy ? hintId : undefined}
                onClick={() => onSelect(sess.session_id)}
                data-testid={`session-item-${sess.session_id}`}
              >
                <span className="chat-session-id">{sess.session_id}</span>
                <span className="chat-session-meta">
                  {(sess.message_count === 1 ? t('chat.sessionMetaOne') : t('chat.sessionMeta'))
                    .replace('{count}', String(sess.message_count))
                    .replace('{revision}', String(sess.revision))}
                </span>
              </button>
              <button
                type="button"
                className="studio-icon-button"
                disabled={busy}
                onClick={() => onDelete(sess.session_id)}
                aria-label={`${t('ui.deleteSession')} ${sess.session_id}`}
                title={t('ui.deleteSession')}
                data-testid={`delete-session-${sess.session_id}`}
              >
                <Icon name="close" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function ChatPlayground() {
  const {
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
    error,
    setLanguage,
    selectSession,
    createSession,
    deleteSession,
    sendMessage,
    stopTurn,
    clearError,
  } = useChatSession();
  const { t } = useI18n();
  const ids = useId();
  const compact = useCompactLayout();
  const selectedAgent = agents.find((a) => a.agent_id === selectedAgentId) ?? null;
  // The model that answers: a saved agent runs on its own model.
  const effectiveModel = selectedAgent ? selectedAgent.model : selectedModel;
  const effectiveModelInfo = models.find((m) => m.name === effectiveModel);
  const isOfflineFake = effectiveModel === FAKE_MODEL_NAME || Boolean(effectiveModelInfo?.offline);
  // Before a reply arrives, describe what the chosen model can do; afterwards, what the service reported.
  const deliveryState = delivery ?? (effectiveModelInfo?.streaming ? 'streamed' : null);

  const [inputPrompt, setInputPrompt] = useState<string>('');
  const [expandedTraceIndex, setExpandedTraceIndex] = useState<number | null>(null);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  // Follow new text only while the reader is at (or near) the end of the transcript.
  const followRef = useRef(true);

  useEffect(() => {
    const el = transcriptRef.current;
    if (el && followRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, sending, streamingText, traces]);
  useEffect(() => {
    followRef.current = true; // a newly opened conversation starts at its end
  }, [currentSessionId]);

  const handleSend = () => {
    if (!inputPrompt.trim() || sending) return;
    const text = inputPrompt;
    setInputPrompt('');
    followRef.current = true;
    sendMessage(text);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleQuickPromptClick = (prompt: string, lang: 'en' | 'hi') => {
    setLanguage(lang);
    setInputPrompt(prompt);
    inputRef.current?.focus();
  };

  const sessionList = (inDrawer: boolean) => (
    <SessionList
      sessions={sessions}
      currentSessionId={currentSessionId}
      loading={loadingSessions}
      busy={sending}
      showTitle={!inDrawer}
      onSelect={(id) => {
        selectSession(id);
        if (inDrawer) setSessionsOpen(false);
      }}
      onDelete={deleteSession}
      onCreate={() => {
        createSession();
        if (inDrawer) setSessionsOpen(false);
      }}
    />
  );

  const citations = extractCitations([
    ...traces.map((tr) => tr.output),
    ...messages.filter((m) => m.role === 'tool').map((m) => m.content),
  ]);

  // Agent, model and reply language: in the strip on wider screens, in Settings on phones.
  const choiceControls = (
    <>
      <label className="chat-control" htmlFor={`${ids}-agent`}>
        {t('agents.playgroundAgent')}
        <select
          id={`${ids}-agent`}
          data-testid="agent-select"
          value={selectedAgentId}
          disabled={sending}
          onChange={(e) => setSelectedAgentId(e.target.value)}
        >
          <option value="">{t('agents.playgroundNoAgent')}</option>
          {agents.map((a) => (
            <option key={a.agent_id} value={a.agent_id}>
              {a.name} ({t('agents.revision').replace('{revision}', String(a.revision))})
            </option>
          ))}
        </select>
      </label>
      {!selectedAgent && (
        <label className="chat-control" htmlFor={`${ids}-model`}>
          {t('ui.chatModel')}
          <select
            id={`${ids}-model`}
            data-testid="model-select"
            value={selectedModel}
            disabled={sending}
            onChange={(e) => setSelectedModel(e.target.value)}
          >
            {models.map((m) => (
              <option key={m.name} value={m.name}>
                {m.name}
                {m.offline ? ' ' + t('ui.offline') : ''}
              </option>
            ))}
          </select>
        </label>
      )}
    </>
  );
  const languageControl = (
    <div className="chat-control chat-control--compact">
      <span id={`${ids}-reply-language`}>{t('chat.replyLanguage')}</span>
      <div className="segmented" role="group" aria-labelledby={`${ids}-reply-language`}>
        <button type="button" aria-pressed={language === 'en'} onClick={() => setLanguage('en')} data-testid="lang-en-btn">
          {t('ui.english')}
        </button>
        <button type="button" aria-pressed={language === 'hi'} onClick={() => setLanguage('hi')} data-testid="lang-hi-btn" lang="hi">
          हिन्दी
        </button>
      </div>
    </div>
  );
  const sessionDetails = currentSessionId && (
    <div className="chat-settings-body">
      <span>
        {t('ui.session') + ' '}
        <code>{currentSessionId}</code>
      </span>
      <Button
        size="sm"
        disabled={sending}
        onClick={() => selectSession(currentSessionId)}
        data-testid="reload-session-btn"
        title={t('ui.reloadTranscriptFromStore')}
      >
        <Icon name="refresh" /> {t('ui.reload')}
      </Button>
    </div>
  );
  const modelNote = (
    <div className="chat-model-note">
      <span data-testid="model-badge">
        {selectedAgent ? (
          <>
            <span className="chat-model-name">{selectedAgent.model}</span>{' '}
            ({t('agents.playgroundAgentModel').replace('{name}', selectedAgent.name)})
            {isOfflineFake && <> · {t('chat.fakeModelLabel')}</>}
          </>
        ) : isOfflineFake ? (
          <span className="chat-model-name">{t('chat.fakeModelLabel')}</span>
        ) : (
          <span className="chat-model-name">{effectiveModelInfo?.label ?? selectedModel}</span>
        )}
      </span>
      <span
        data-testid="delivery-badge"
        title={deliveryState === 'streamed' ? undefined : t('ui.turnProgressStreamsLiveTheReplyTextArrivesWholeNoTokenStreaming')}
      >
        {deliveryLabel(deliveryState, t)}
      </span>
    </div>
  );

  return (
    <div className="chat-layout" data-testid="chat-playground">
      {!compact && sessionList(false)}
      <section className="chat-main" aria-labelledby={`${ids}-title`}>
        {compact ? (
          <div className="chat-strip chat-strip--compact">
            <div className="chat-strip-row">
              <h1 id={`${ids}-title`}>{t('nav.playground')}</h1>
              <Button variant="ghost" onClick={() => setSessionsOpen(true)} aria-haspopup="dialog">
                {t('ui.conversations')}
              </Button>
              <details className="chat-settings">
                <summary>{t('chat.settings')}</summary>
                <div className="chat-settings-panel">
                  <div className="chat-settings-body chat-settings-body--stacked">
                    {choiceControls}
                    {languageControl}
                  </div>
                  {sessionDetails}
                </div>
              </details>
            </div>
            {modelNote}
          </div>
        ) : (
          <div className="chat-strip">
            <h1 id={`${ids}-title`}>{t('nav.playground')}</h1>
            {choiceControls}
            {modelNote}
            {languageControl}
            {currentSessionId && (
              <details className="chat-settings">
                <summary>{t('chat.settings')}</summary>
                {sessionDetails}
              </details>
            )}
          </div>
        )}

        <div
          ref={transcriptRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
          className="chat-transcript"
          data-testid="chat-transcript"
          role="region"
          aria-label={t('chat.transcript')}
          aria-busy={loadingSession || undefined}
          tabIndex={0}
        >
          <div className="chat-column">
            {loadingSession ? (
              <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Spinner size={16} /> <span className="resource-help">{t('common.loading')}</span>
              </div>
            ) : messages.length === 0 && !sending ? (
              <div className="chat-empty">
                <h2>{t('chat.emptyTitle')}</h2>
                <p className="resource-help" style={{ marginTop: 4 }}>{t('chat.emptyHelp')}</p>
                <ul className="chat-examples studio-quick-prompts">
                  {EXAMPLE_PROMPTS.map((qp) => (
                    <li key={qp.id}>
                      <Button
                        size="sm"
                        onClick={() => handleQuickPromptClick(qp.prompt, qp.language)}
                        data-testid={`quick-prompt-${qp.id}`}
                        lang={qp.language}
                      >
                        {t(`chat.quickPrompt.${qp.id}`, qp.label)}
                      </Button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              messages.map((msg, idx) => {
                if (msg.role === 'system') {
                  return (
                    <p key={idx} className="chat-system">
                      {t('ui.systemInstruction') + ' '}{msg.content}
                    </p>
                  );
                }
                const roleLabel =
                  msg.role === 'user'
                    ? t('ui.you')
                    : msg.role === 'assistant'
                    ? t('ui.agent')
                    : t('chat.toolLabel').replace('{name}', msg.name || t('ui.output'));
                return (
                  <div key={idx} data-testid={`message-bubble-${msg.role}`} className={`chat-message chat-message--${msg.role}`}>
                    <span className="chat-message-role">{roleLabel}</span>
                    <div className="chat-message-body">
                      {msg.content || ''}
                      {msg.tool_calls && msg.tool_calls.length > 0 && (
                        <code className="chat-tool-call">
                          {t('ui.invokingTool') + ' '}
                          {msg.tool_calls[0].name}({JSON.stringify(msg.tool_calls[0].arguments)})
                        </code>
                      )}
                    </div>
                  </div>
                );
              })
            )}

            {sending && streamingText && (
              <div data-testid="message-bubble-streaming" className="chat-message chat-message--assistant" aria-busy="true">
                <span className="chat-message-role">{t('ui.agent')} · {t('chat.replyInProgress')}</span>
                <div className="chat-message-body">{streamingText}</div>
              </div>
            )}

            {sending && (
              <div className="chat-activity">
                <Spinner size={14} />
                <span role="status" aria-live="polite" data-testid="turn-activity">
                  {activity ?? t('ui.agentIsThinkingExecutingTools')}
                </span>
              </div>
            )}

            {traces.length > 0 && (
              <section data-testid="tool-trace-container" className="chat-evidence" aria-labelledby={`${ids}-traces`}>
                <h2 id={`${ids}-traces`}>
                  {t('ui.toolExecutionTraces')}{traces.length})
                </h2>
                {traces.map((tr, tIdx) => {
                  const isExpanded = expandedTraceIndex === tIdx;
                  const payloadId = `${ids}-trace-${tIdx}`;
                  return (
                    <div key={tIdx} data-testid={`tool-trace-${tr.tool}`} className="chat-trace">
                      <button
                        type="button"
                        aria-expanded={isExpanded}
                        aria-controls={isExpanded ? payloadId : undefined}
                        onClick={() => setExpandedTraceIndex(isExpanded ? null : tIdx)}
                      >
                        <code>{tr.tool}</code>
                        <span className="chat-trace-toggle">{isExpanded ? t('ui.hide') : t('ui.viewPayload')}</span>
                      </button>
                      {isExpanded && (
                        <div id={payloadId} className="chat-trace-payload">
                          <strong className="resource-label">{t('ui.arguments')}</strong>
                          <pre>{JSON.stringify(tr.arguments, null, 2)}</pre>
                          <strong className="resource-label">{t('ui.output2')}</strong>
                          <pre>{typeof tr.output === 'object' ? JSON.stringify(tr.output, null, 2) : String(tr.output)}</pre>
                        </div>
                      )}
                    </div>
                  );
                })}
              </section>
            )}

            {/* Knowledge citations used by tool calls, shown as footnotes linking to source chunks */}
            {citations.length > 0 && (
              <div className="chat-evidence">
                <CitationFootnotes citations={citations} />
              </div>
            )}

          </div>
        </div>

        <div className="chat-composer">
          <div className="chat-column">
            {error && (
              <div role="alert" className="inline-notice inline-notice--danger">
                <span>{error}</span>
                <button type="button" className="studio-icon-button" onClick={clearError} aria-label={t('ui.dismissError')}>
                  <Icon name="close" />
                </button>
              </div>
            )}
            {interrupted && (
              <div role="alert" data-testid="turn-interrupted" className="inline-notice inline-notice--warning">
                {t('ui.turnInterrupted') + ' '}{interrupted} {t('ui.youCanSendItAgain')}
                {selectedAgentId && isTurnLimitCode(interruptedCode) && (
                  <div data-testid="agent-turn-limit" style={{ marginTop: 4 }}>
                    {t('agents.playgroundTurnLimit')}
                  </div>
                )}
              </div>
            )}
            {/* A saved agent's tool calls that needed approval were refused, never run, in chat */}
            {refusedToolCalls.length > 0 && (
              <div data-testid="refused-tool-calls" className="inline-notice inline-notice--warning chat-refused">
                <strong className="inline-notice-title">
                  {t('agents.playgroundRefusedTitle').replace('{count}', String(refusedToolCalls.length))}
                </strong>
                <span>{t('agents.playgroundRefusedNote')}</span>
                <ul>
                  {refusedToolCalls.map((c, i) => (
                    <li key={`${c.id}-${i}`} data-testid="refused-tool-call">
                      <code>{c.tool}</code>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="chat-composer-row">
              <label htmlFor={`${ids}-message`} className="studio-sr-only">{t('chat.messageLabel')}</label>
              <textarea
                id={`${ids}-message`}
                ref={inputRef}
                value={inputPrompt}
                onChange={(e) => setInputPrompt(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={compact ? t('chat.shortInputHint') : language === 'hi' ? t('chat.hindiInputHint') : t('chat.englishInputHint')}
                data-testid="chat-input-textarea"
                disabled={sending}
                rows={2}
              />
              {sending ? (
                <Button onClick={stopTurn} data-testid="stop-turn-button">
                  {t('ui.stop')}
                </Button>
              ) : (
                <Button variant="primary" onClick={handleSend} disabled={!inputPrompt.trim()} data-testid="send-message-button">
                  {t('ui.send')}
                </Button>
              )}
            </div>
            {usage && (
              <div data-testid="metrics-bar" className="chat-metrics">
                <span>
                  {t('ui.tokens') + ' '}
                  <strong>{usage.total_tokens ?? t('ui.unknown')}</strong> ({usage.prompt_tokens ?? t('ui.unknown')} {t('ui.prompt')}{' '}
                  {usage.completion_tokens ?? t('ui.unknown')} {t('ui.completion')}
                </span>
                {latencyMs !== null && (
                  <span>
                    {t('ui.latency') + ' '}
                    <strong>{latencyMs}ms</strong>
                  </span>
                )}
              </div>
            )}
          </div>
        </div>
      </section>

      {compact && (
        <Modal open={sessionsOpen} onClose={() => setSessionsOpen(false)} title={t('ui.conversations')} variant="drawer">
          {sessionList(true)}
        </Modal>
      )}
    </div>
  );
}
