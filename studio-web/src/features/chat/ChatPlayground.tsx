// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useRef, useEffect, KeyboardEvent } from 'react';
import { useChatSession } from './useChatSession';
import { DEFAULT_QUICK_PROMPTS, FAKE_MODEL_NAME, deliveryLabel, isTurnLimitCode } from './types';
import { CitationFootnotes } from './CitationFootnotes';
import { extractCitations } from './citations';
import { Button } from '../../components/Button';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';

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
  const selectedAgent = agents.find((a) => a.agent_id === selectedAgentId) ?? null;

  const [inputPrompt, setInputPrompt] = useState<string>('');
  const [expandedTraceIndex, setExpandedTraceIndex] = useState<number | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, sending]);

  const handleSend = () => {
    if (!inputPrompt.trim() || sending) return;
    const text = inputPrompt;
    setInputPrompt('');
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
  };

  return (
    <div
      className="studio-split"
      data-testid="chat-playground"
      style={{
        display: 'flex',
        height: 'calc(100vh - 120px)',
        gap: 16,
      }}
    >
      {/* Left Panel: Conversation Sessions */}
      <aside
        className="studio-split-secondary"
        style={{
          width: 260,
          display: 'flex',
          flexDirection: 'column',
          backgroundColor: 'var(--bg-surface)',
          borderRadius: 'var(--radius-lg)',
          border: '1px solid var(--border-subtle)',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            padding: '14px 16px',
            borderBottom: '1px solid var(--border-subtle)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
            {t('ui.conversations')}
          </span>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => createSession()}
            data-testid="new-chat-button"
          >
            {t('ui.new')}
          </Button>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: '8px' }}>
          {loadingSessions ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: 24 }}>
              <Spinner size={18} />
            </div>
          ) : sessions.length === 0 ? (
            <div
              style={{
                textAlign: 'center',
                padding: '24px 12px',
                color: 'var(--text-tertiary)',
                fontSize: 12,
              }}
            >
              {t('ui.noSessionsYetClickQuotNewQuotToStart')}
            </div>
          ) : (
            sessions.map((sess) => {
              const isSelected = sess.session_id === currentSessionId;
              return (
                <div
                  key={sess.session_id}
                  onClick={() => selectSession(sess.session_id)}
                  data-testid={`session-item-${sess.session_id}`}
                  style={{
                    padding: '8px 10px',
                    borderRadius: 'var(--radius-md)',
                    marginBottom: 4,
                    backgroundColor: isSelected ? 'var(--accent-subtle)' : 'transparent',
                    border: isSelected
                      ? '1px solid var(--accent-primary)'
                      : '1px solid transparent',
                    cursor: 'pointer',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 4,
                    position: 'relative',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                    }}
                  >
                    <span
                      style={{
                        fontWeight: isSelected ? 600 : 500,
                        fontSize: 12,
                        color: isSelected ? 'var(--accent-primary)' : 'var(--text-primary)',
                        fontFamily: 'monospace',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        maxWidth: 160,
                      }}
                    >
                      {sess.session_id}
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteSession(sess.session_id);
                      }}
                      title={t('ui.deleteSession')}
                      data-testid={`delete-session-${sess.session_id}`}
                      style={{
                        background: 'none',
                        border: 'none',
                        color: 'var(--text-tertiary)',
                        cursor: 'pointer',
                        fontSize: 11,
                        padding: '2px 4px',
                      }}
                    >
                      <Icon name="close" />
                    </button>
                  </div>
                  <div
                    style={{
                      display: 'flex',
                      gap: 6,
                      fontSize: 11,
                      color: 'var(--text-secondary)',
                    }}
                  >
                    <span>{sess.message_count} {t('ui.msgs')}</span>
                    <span>•</span>
                    <span>{t('ui.rev2') + " "}{sess.revision}</span>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </aside>

      {/* Main Panel: Active Playground */}
      <div
        className="studio-split-main"
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          backgroundColor: 'var(--bg-surface)',
          borderRadius: 'var(--radius-lg)',
          border: '1px solid var(--border-subtle)',
          overflow: 'hidden',
        }}
      >
        {/* Playground Header & Metadata */}
        <div
          style={{
            padding: '12px 16px',
            borderBottom: '1px solid var(--border-subtle)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: 12,
            backgroundColor: 'var(--bg-canvas)',
          }}
        >
          <div className="studio-mobile-wrap" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {/* Visibly labelled fake model fixture badge */}
            <span
              data-testid="model-badge"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                padding: '4px 10px',
                borderRadius: 'var(--radius-sm)',
                backgroundColor: 'rgba(59, 130, 246, 0.1)',
                border: '1px solid rgba(59, 130, 246, 0.3)',
                color: '#2563eb',
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              <span><Icon name="agents" /></span>
              <span>
                {t('ui.model')}{' '}
                {selectedAgent
                  ? `${selectedAgent.model} (${t('agents.playgroundAgentModel').replace('{name}', selectedAgent.name)})`
                  : selectedModel === FAKE_MODEL_NAME
                  ? t('chat.fakeModelLabel')
                  : (models.find((m) => m.name === selectedModel)?.label ?? selectedModel)}
              </span>
            </span>
            <select
              data-testid="agent-select"
              aria-label={t('agents.playgroundAgent')}
              value={selectedAgentId}
              disabled={sending}
              onChange={(e) => setSelectedAgentId(e.target.value)}
              style={{ fontSize: 12, padding: '4px 6px' }}
            >
              <option value="">{t('agents.playgroundNoAgent')}</option>
              {agents.map((a) => (
                <option key={a.agent_id} value={a.agent_id}>
                  {a.name} ({t('agents.revision').replace('{revision}', String(a.revision))})
                </option>
              ))}
            </select>
            {!selectedAgent && (
            <select
              data-testid="model-select"
              aria-label={t('ui.chatModel')}
              value={selectedModel}
              disabled={sending}
              onChange={(e) => setSelectedModel(e.target.value)}
              style={{ fontSize: 12, padding: '4px 6px' }}
            >
              {models.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                  {m.offline ? " " + t('ui.offline') : ''}
                </option>
              ))}
            </select>
            )}
            <span
              data-testid="delivery-badge"
              title={t('ui.turnProgressStreamsLiveTheReplyTextArrivesWholeNoTokenStreaming')}
              style={{
                padding: '4px 10px',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                color: 'var(--text-secondary)',
                fontSize: 12,
              }}
            >
              {deliveryLabel(delivery, t)}
            </span>

            {currentSessionId && (
              <span
                style={{
                  fontSize: 12,
                  fontFamily: 'monospace',
                  color: 'var(--text-secondary)',
                }}
              >
                {t('ui.session') + " "}{currentSessionId}
              </span>
            )}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {/* Language Switcher */}
            <div
              style={{
                display: 'inline-flex',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                overflow: 'hidden',
              }}
            >
              <button
                onClick={() => setLanguage('en')}
                data-testid="lang-en-btn"
                style={{
                  padding: '4px 10px',
                  fontSize: 12,
                  fontWeight: language === 'en' ? 600 : 400,
                  backgroundColor:
                    language === 'en' ? 'var(--accent-subtle)' : 'transparent',
                  color:
                    language === 'en' ? 'var(--accent-primary)' : 'var(--text-secondary)',
                  border: 'none',
                  cursor: 'pointer',
                }}
              >
                {t('ui.english')}
              </button>
              <button
                onClick={() => setLanguage('hi')}
                data-testid="lang-hi-btn"
                style={{
                  padding: '4px 10px',
                  fontSize: 12,
                  fontWeight: language === 'hi' ? 600 : 400,
                  backgroundColor:
                    language === 'hi' ? 'var(--accent-subtle)' : 'transparent',
                  color:
                    language === 'hi' ? 'var(--accent-primary)' : 'var(--text-secondary)',
                  border: 'none',
                  cursor: 'pointer',
                }}
              >
                हिन्दी
              </button>
            </div>

            {/* Reload Transcript Button */}
            {currentSessionId && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => selectSession(currentSessionId)}
                data-testid="reload-session-btn"
                title={t('ui.reloadTranscriptFromStore')}
              >
                <Icon name="refresh" /> {t('ui.reload')}
              </Button>
            )}
          </div>
        </div>

        {/* Interrupted turn notice */}
        {interrupted && (
          <div
            role="alert"
            data-testid="turn-interrupted"
            style={{
              padding: '8px 16px',
              backgroundColor: 'rgba(245, 158, 11, 0.1)',
              borderBottom: '1px solid rgba(245, 158, 11, 0.3)',
              color: 'var(--status-warning)',
              fontSize: 12,
            }}
          >
            {t('ui.turnInterrupted') + " "}{interrupted} {t('ui.youCanSendItAgain')}{selectedAgentId && isTurnLimitCode(interruptedCode) && (
              <div data-testid="agent-turn-limit" style={{ marginTop: 4 }}>
                {t('agents.playgroundTurnLimit')}
              </div>
            )}
          </div>
        )}

        {/* Error Alert */}
        {error && (
          <div
            style={{
              padding: '8px 16px',
              backgroundColor: 'rgba(239, 68, 68, 0.1)',
              borderBottom: '1px solid rgba(239, 68, 68, 0.3)',
              color: 'var(--status-danger)',
              fontSize: 12,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <span><Icon name="warning" /> {error}</span>
            <button
              onClick={clearError}
              aria-label={t('ui.dismissError')}
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--status-danger)',
                cursor: 'pointer',
                fontSize: 14,
              }}
            >
              <Icon name="close" />
            </button>
          </div>
        )}

        {/* Chat Transcript Area */}
        <div
          data-testid="chat-transcript"
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: 20,
            display: 'flex',
            flexDirection: 'column',
            gap: 14,
          }}
        >
          {loadingSession ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}>
              <Spinner size={24} />
            </div>
          ) : messages.length === 0 ? (
            <div
              style={{
                margin: 'auto',
                textAlign: 'center',
                maxWidth: 420,
                color: 'var(--text-secondary)',
              }}
            >
              <div style={{ fontSize: 36, marginBottom: 12 }}><Icon name="playground" /></div>
              <h3 style={{ fontSize: 16, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 6 }}>
                {t('ui.conversationPlayground')}
              </h3>
              <p style={{ fontSize: 13, lineHeight: 1.5, marginBottom: 16 }}>
                {t('ui.chatWithLocalDeterministicAgentsTestTypedToolCallsSuchAsMathCalculationOrOrd')}
              </p>
            </div>
          ) : (
            messages.map((msg, idx) => {
              const isUser = msg.role === 'user';
              const isAssistant = msg.role === 'assistant';
              const isTool = msg.role === 'tool';
              const isSystem = msg.role === 'system';

              if (isSystem) {
                return (
                  <div
                    key={idx}
                    style={{
                      alignSelf: 'center',
                      backgroundColor: 'var(--bg-canvas)',
                      border: '1px dashed var(--border-subtle)',
                      borderRadius: 'var(--radius-md)',
                      padding: '6px 12px',
                      fontSize: 11,
                      color: 'var(--text-tertiary)',
                      maxWidth: '85%',
                    }}
                  >
                    <Icon name="system" /> {t('ui.systemInstruction') + " "}{msg.content}
                  </div>
                );
              }

              return (
                <div
                  key={idx}
                  data-testid={`message-bubble-${msg.role}`}
                  style={{
                    alignSelf: isUser ? 'flex-end' : 'flex-start',
                    maxWidth: '80%',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 4,
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 6,
                      fontSize: 11,
                      color: 'var(--text-tertiary)',
                      alignSelf: isUser ? 'flex-end' : 'flex-start',
                    }}
                  >
                    <span>{isUser ? t('ui.you') : isAssistant ? t('ui.agent') : t('chat.toolLabel').replace('{name}', msg.name || t('ui.output'))}</span>
                  </div>

                  <div
                    style={{
                      padding: '10px 14px',
                      borderRadius: 'var(--radius-lg)',
                      fontSize: 13,
                      lineHeight: 1.5,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      backgroundColor: isUser
                        ? 'var(--accent-primary)'
                        : isTool
                        ? 'var(--bg-canvas)'
                        : 'var(--bg-surface-raised, #f8fafc)',
                      color: isUser ? '#ffffff' : 'var(--text-primary)',
                      border: isUser
                        ? 'none'
                        : isTool
                        ? '1px dashed var(--border-subtle)'
                        : '1px solid var(--border-subtle)',
                    }}
                  >
                    {msg.content || ''}

                    {msg.tool_calls && msg.tool_calls.length > 0 && (
                      <div
                        style={{
                          marginTop: 8,
                          padding: '6px 8px',
                          borderRadius: 'var(--radius-sm)',
                          backgroundColor: 'rgba(0,0,0,0.05)',
                          fontSize: 11,
                          fontFamily: 'monospace',
                        }}
                      >
                        <Icon name="workflows" /> {t('ui.invokingTool') + " "}{msg.tool_calls[0].name}({JSON.stringify(msg.tool_calls[0].arguments)})
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}

          {sending && (
            <div
              style={{
                alignSelf: 'flex-start',
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '8px 14px',
                borderRadius: 'var(--radius-lg)',
                backgroundColor: 'var(--bg-surface-raised, #f8fafc)',
                border: '1px solid var(--border-subtle)',
                fontSize: 12,
                color: 'var(--text-secondary)',
              }}
            >
              <Spinner size={14} />
              <span role="status" aria-live="polite" data-testid="turn-activity">
                {activity ?? t('ui.agentIsThinkingExecutingTools')}
              </span>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* A saved agent's tool calls that needed approval were refused, never run, in chat */}
        {refusedToolCalls.length > 0 && (
          <div
            data-testid="refused-tool-calls"
            style={{
              padding: '8px 16px',
              borderTop: '1px solid var(--border-subtle)',
              backgroundColor: 'rgba(245, 158, 11, 0.08)',
              fontSize: 12,
              color: 'var(--text-secondary)',
            }}
          >
            <div style={{ fontWeight: 600, color: 'var(--status-warning)', marginBottom: 4 }}>
              {t('agents.playgroundRefusedTitle').replace('{count}', String(refusedToolCalls.length))}
            </div>
            <div style={{ marginBottom: 4 }}>{t('agents.playgroundRefusedNote')}</div>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {refusedToolCalls.map((c, i) => (
                <li key={`${c.id}-${i}`} data-testid="refused-tool-call">
                  <code>{c.tool}</code>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Knowledge citations used by tool calls, shown as footnotes linking to source chunks */}
        <CitationFootnotes
          citations={extractCitations([
            ...traces.map((tr) => tr.output),
            ...messages.filter((m) => m.role === 'tool').map((m) => m.content),
          ])}
        />

        {/* Traces / Tool Executions Drawer (if any tool executed) */}
        {traces.length > 0 && (
          <div
            data-testid="tool-trace-container"
            style={{
              padding: '8px 16px',
              borderTop: '1px solid var(--border-subtle)',
              backgroundColor: 'var(--bg-canvas)',
              fontSize: 12,
            }}
          >
            <div
              style={{
                fontWeight: 600,
                color: 'var(--text-secondary)',
                marginBottom: 6,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <span><Icon name="tools" /> {t('ui.toolExecutionTraces')}{traces.length})</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 120, overflowY: 'auto' }}>
              {traces.map((tr, tIdx) => {
                const isExpanded = expandedTraceIndex === tIdx;
                return (
                  <div
                    key={tIdx}
                    data-testid={`tool-trace-${tr.tool}`}
                    style={{
                      border: '1px solid var(--border-subtle)',
                      borderRadius: 'var(--radius-sm)',
                      padding: '4px 8px',
                      backgroundColor: 'var(--bg-surface)',
                      fontSize: 11,
                    }}
                  >
                    <div
                      onClick={() => setExpandedTraceIndex(isExpanded ? null : tIdx)}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        cursor: 'pointer',
                      }}
                    >
                      <span style={{ fontFamily: 'monospace', fontWeight: 600, color: 'var(--accent-primary)' }}>
                        <Icon name="check" /> {tr.tool}
                      </span>
                      <span style={{ color: 'var(--text-tertiary)', fontSize: 10 }}>
                        {isExpanded ? <><Icon name="up" /> {t('ui.hide')}</> : <><Icon name="down" /> {t('ui.viewPayload')}</>}
                      </span>
                    </div>

                    {isExpanded && (
                      <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <div>
                          <strong style={{ color: 'var(--text-secondary)' }}>{t('ui.arguments')}</strong>
                          <pre
                            style={{
                              margin: '2px 0 0 0',
                              padding: 4,
                              backgroundColor: 'var(--bg-canvas)',
                              borderRadius: 4,
                              overflowX: 'auto',
                            }}
                          >
                            {JSON.stringify(tr.arguments, null, 2)}
                          </pre>
                        </div>
                        <div>
                          <strong style={{ color: 'var(--text-secondary)' }}>{t('ui.output2')}</strong>
                          <pre
                            style={{
                              margin: '2px 0 0 0',
                              padding: 4,
                              backgroundColor: 'var(--bg-canvas)',
                              borderRadius: 4,
                              overflowX: 'auto',
                            }}
                          >
                            {typeof tr.output === 'object' ? JSON.stringify(tr.output, null, 2) : String(tr.output)}
                          </pre>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Usage & Latency Metrics Bar */}
        {usage && (
          <div
            data-testid="metrics-bar"
            style={{
              padding: '6px 16px',
              borderTop: '1px solid var(--border-subtle)',
              backgroundColor: 'var(--bg-canvas)',
              display: 'flex',
              alignItems: 'center',
              gap: 16,
              fontSize: 11,
              color: 'var(--text-secondary)',
            }}
          >
            <span>
              {t('ui.tokens') + " "}<strong>{usage.total_tokens ?? t('ui.unknown')}</strong> ({usage.prompt_tokens ?? t('ui.unknown')} {t('ui.prompt')}{' '}
              {usage.completion_tokens ?? t('ui.unknown')} {t('ui.completion')}
            </span>
            {latencyMs !== null && (
              <span>
                {t('ui.latency') + " "}<strong>{latencyMs}ms</strong>
              </span>
            )}
          </div>
        )}

        {/* Quick Prompts Suggestions */}
        <div className="studio-mobile-wrap studio-quick-prompts"
          style={{
            padding: '8px 16px',
            borderTop: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-surface)',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            overflowX: 'auto',
          }}
        >
          <span style={{ fontSize: 11, color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
            {t('ui.quickPrompts')}
          </span>
          {DEFAULT_QUICK_PROMPTS.map((qp) => (
            <button
              key={qp.id}
              onClick={() => handleQuickPromptClick(qp.prompt, qp.language)}
              data-testid={`quick-prompt-${qp.id}`}
              style={{
                fontSize: 11,
                padding: '3px 8px',
                borderRadius: 'var(--radius-sm)',
                backgroundColor: 'var(--bg-canvas)',
                border: '1px solid var(--border-subtle)',
                color: 'var(--text-secondary)',
                cursor: 'pointer',
                whiteSpace: 'nowrap',
              }}
            >
              {t(`chat.quickPrompt.${qp.id}`, qp.label)}
            </button>
          ))}
        </div>

        {/* Prompt Input Form */}
        <div
          style={{
            padding: '12px 16px',
            borderTop: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-surface)',
            display: 'flex',
            gap: 10,
            alignItems: 'flex-end',
          }}
        >
          <textarea
            value={inputPrompt}
            onChange={(e) => setInputPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              language === 'hi' ? t('chat.hindiInputHint') : t('chat.englishInputHint')
            }
            data-testid="chat-input-textarea"
            disabled={sending}
            rows={2}
            style={{
              flex: 1,
              resize: 'none',
              padding: '8px 12px',
              borderRadius: 'var(--radius-md)',
              border: '1px solid var(--border-subtle)',
              backgroundColor: 'var(--bg-canvas)',
              color: 'var(--text-primary)',
              fontSize: 13,
              fontFamily: 'inherit',
              lineHeight: 1.4,
              outline: 'none',
            }}
          />
          {sending ? (
            <Button
              variant="secondary"
              onClick={stopTurn}
              data-testid="stop-turn-button"
              style={{ height: 42, padding: '0 16px' }}
            >
              {t('ui.stop')}
            </Button>
          ) : (
            <Button
              variant="primary"
              onClick={handleSend}
              disabled={!inputPrompt.trim()}
              data-testid="send-message-button"
              style={{ height: 42, padding: '0 16px' }}
            >
              {t('ui.send') + " "}<Icon name="send" />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
