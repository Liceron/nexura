// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useEffect, useRef, useState } from 'react';
import type { RunEvent, RunRecord, WorkflowSpec } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { formatApiError } from '../../api/errors';
import { useT } from '../../i18n';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { runHref } from '../../app/routes';
import { formatToolResult } from '../chat/formatToolResult';
import { defaultRunInput, parseRunInput, workflowMessageKey } from '../runs/runInput';
import { useRunStream } from '../runs/useRunStream';
import { PendingInputBanner } from '../runs/PendingInputBanner';
import { RunStatus } from '../runs/RunStatus';
import { formatEventTitle } from '../runs/events';

interface Props {
  open: boolean;
  wide: boolean;
  active: boolean;
  workflowId: string;
  spec: WorkflowSpec;
  dirty: boolean;
  draftPending: boolean;
  canRun: boolean;
  onSave: () => Promise<boolean>;
  onClose: () => void;
  onEvents: (events: RunEvent[], nodeId?: string | null) => void;
}

interface Turn { input: string; run: RunRecord; events: RunEvent[] }
const finished = new Set(['completed', 'failed', 'cancelled', 'rejected', 'interrupted']);

function TestTurn({ input, run, events }: Turn) {
  const t = useT();
  return <article className="builder-test-turn">
    <div className="builder-test-message"><strong>{t('builderTest.input')}</strong><pre>{input}</pre></div>
    <div className="builder-test-response">
      <RunStatus status={run.status} /> <a href={runHref(run.run_id)}>{t('builderTest.openRun')}</a>
      <ol className="builder-test-events">{events.map(event => <li key={event.seq}>
        <span>{t(`eventTitles.${event.type}`, formatEventTitle(event.type))}</span>{event.node_id && <> · <code>{event.node_id}</code></>}
        {event.type.includes('FAILED') && <pre className="inspection-error">{formatToolResult(event.data)}</pre>}
        {Object.keys(event.data ?? {}).length > 0 && <details><summary>{t('builderTest.eventDetails')}</summary><pre>{formatToolResult(event.data)}</pre></details>}
      </li>)}</ol>
      {run.status === 'completed' && <div><strong>{t('builderTest.output')}</strong><pre>{formatToolResult(run.current_state)}</pre></div>}
      {run.error && <p role="alert" className="inspection-error">{run.error}</p>}
      {run.status === 'pending_approval' && <p><a href={runHref(run.run_id)}>{t('builderTest.reviewApproval')}</a></p>}
      {run.status === 'paused' && <p>{run.pause_reason} · <a href={runHref(run.run_id)}>{t('builderTest.openRun')}</a></p>}
    </div>
  </article>;
}

/** A conversation is local presentation; each explicit send starts one normal saved-workflow run. */
export function WorkflowTestPanel({ open, wide, active, workflowId, spec, dirty, draftPending, canRun, onSave, onClose, onEvents }: Props) {
  const t = useT();
  const { manager, connection } = useApi();
  const messageKey = workflowMessageKey(spec);
  const [jsonMode, setJsonMode] = useState(false);
  const [message, setMessage] = useState('');
  const [json, setJson] = useState(() => JSON.stringify(defaultRunInput(connection.workspace, workflowId), null, 2));
  const [runId, setRunId] = useState<string | null>(null);
  const [startedRun, setStartedRun] = useState<RunRecord | null>(null);
  const [input, setInput] = useState('');
  const [history, setHistory] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const stream = useRunStream(runId);
  const run = stream.run?.run_id === runId ? stream.run : startedRun;
  const events = stream.run?.run_id === runId ? stream.events : startedRun?.events ?? [];
  const running = !!run && !finished.has(run.status);
  const useMessage = !!messageKey && !jsonMode;
  const parsed = parseRunInput(json, t);
  const invalid = useMessage ? !message.trim() : !!parsed.parseError;

  useEffect(() => { onEvents(stream.events, stream.run?.current_node_id); }, [stream.events, stream.run?.current_node_id, onEvents]);
  useEffect(() => { if (open && wide) composer.current?.focus(); }, [open, wide]);

  const send = async () => {
    if (inFlight.current || running || !canRun || invalid || draftPending) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      if (dirty && !(await onSave())) return;
      const initialState = useMessage ? { [messageKey!]: message } : parsed.parsedState!;
      const next = await manager.client.runs.start({ workflow_id: workflowId, initial_state: initialState, async_execution: true });
      if (run) setHistory(previous => [...previous, { input, run, events }]);
      setInput(useMessage ? message : JSON.stringify(initialState, null, 2));
      setStartedRun(next);
      setRunId(next.run_id);
      setMessage('');
    } catch (cause) { setError(formatApiError(cause, t)); }
    finally { setBusy(false); inFlight.current = false; }
  };

  const content = <section className="builder-test-content" aria-label={t('builderTest.title')} onKeyDown={event => {
    if (wide && event.key === 'Escape') { event.stopPropagation(); onClose(); }
  }}>
    <div className="builder-panel-heading">
      <strong>{t('builderTest.title')}</strong>
      <Button variant="ghost" size="sm" disabled={busy || running} onClick={() => {
        setHistory([]); setRunId(null); setStartedRun(null); setInput(''); setMessage(''); setError(null);
        setJson(JSON.stringify(defaultRunInput(connection.workspace, workflowId), null, 2)); setJsonMode(false); composer.current?.focus();
      }}>{t('builderTest.newConversation')}</Button>
      {wide && <Button variant="ghost" size="sm" onClick={onClose}>{t('common.close')}</Button>}
    </div>
    <p className="builder-panel-hint">{t('builderTest.costNote')}</p>
    <p className="builder-panel-hint">{t('builderTest.independentRuns')}</p>
    <div className="builder-test-conversation" role="log" aria-label={t('builderTest.conversation')} aria-live="polite">
      {history.map(turn => <TestTurn key={turn.run.run_id} {...turn} />)}
      {run && <TestTurn input={input} run={run} events={events} />}
    </div>
    {run && canRun && <PendingInputBanner run={run} onSubmit={async (id, value) => { await stream.provideInput(id, value); }} />}
    {(error || stream.error) && <p role="alert" className="inspection-error">{error || stream.error}</p>}
    {runId && <p role="status" className="builder-panel-hint">{t(`builderTest.stream.${stream.streamStatus}`)}</p>}
    {running && <Button variant="secondary" disabled={busy || !canRun} onClick={async () => {
      if (inFlight.current) return;
      inFlight.current = true; setBusy(true);
      try { await stream.cancelRun(); } finally { setBusy(false); inFlight.current = false; }
    }}>{t('builderTest.stop')}</Button>}
    {dirty && <p role="status" className="builder-panel-hint">{t('builderTest.savePrompt')}</p>}
    {draftPending && <p role="status">{t('builderTest.resolveDraft')}</p>}
    <form className="builder-test-composer" onSubmit={event => { event.preventDefault(); void send(); }}>
      {messageKey && <label><input type="checkbox" checked={jsonMode} onChange={event => setJsonMode(event.target.checked)} /> {t('builderTest.jsonMode')}</label>}
      <label htmlFor="builder-test-input">{useMessage ? t('builderTest.message') : t('ui.initialStateJson')}</label>
      {useMessage && <span className="builder-panel-hint">{t('builderTest.mappedTo')} <code>{messageKey}</code></span>}
      <textarea ref={composer} id="builder-test-input" rows={3} value={useMessage ? message : json}
        disabled={busy || running || !canRun}
        onChange={event => useMessage ? setMessage(event.target.value) : setJson(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
        }} />
      {!useMessage && parsed.parseError && <p role="alert" className="inspection-error">{parsed.parseError}</p>}
      <Button type="submit" variant={open && active ? 'primary' : 'secondary'} loading={busy} disabled={running || !canRun || invalid || draftPending}>
        {dirty ? t('builderTest.saveAndTest') : t('builderTest.send')}
      </Button>
    </form>
  </section>;

  return wide ? <aside hidden={!open} className="workflow-test-panel">{content}</aside> :
    <Modal open={open && active} onClose={onClose} title={t('builderTest.title')} variant="drawer" maxWidth={400}>{content}</Modal>;
}
