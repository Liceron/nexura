// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useEffect, useRef, useState } from 'react';
import type { RunRecord } from '@liceron/nexura-sdk';
import { Button } from '../../components/Button';
import { buildAnswer, describeInterrupt, schemaTemplate } from './durable';
import { useDurableT } from './useDurableT';
import { formatApiError } from '../../api/errors';

interface PendingInputBannerProps {
  run: RunRecord;
  onSubmit: (interruptId: string, value: unknown) => Promise<void>;
}

export function parseAnswer(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function PendingInputBanner({ run, onSubmit }: PendingInputBannerProps) {
  const t = useDurableT();
  const [answer, setAnswer] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sentFor, setSentFor] = useState<string | null>(null);
  const inFlight = useRef(false);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);

  const pending = run.status === 'waiting_for_input' ? run.pending_input : null;
  const interruptId = pending?.interrupt_id;
  const { question, schema } = describeInterrupt(pending?.value);
  const schemaKey = schema ? JSON.stringify(schema) : '';

  // A new question starts with a clean form (and a skeleton when the schema is an object).
  useEffect(() => {
    setAnswer(schemaTemplate(schema));
    setValidationErrors([]);
    setError(null);
  }, [interruptId, schemaKey]);

  // The answer was accepted; unlock only when the run moves on (status or interrupt id changes).
  useEffect(() => {
    setSentFor(null);
    inFlight.current = false;
  }, [interruptId, run.status]);

  if (!pending) return null;

  const alreadySent = sentFor !== null && sentFor === interruptId;

  const handleSend = async () => {
    if (inFlight.current || alreadySent) return;
    const built = buildAnswer(answer, schema, t);
    if (built.errors.length > 0) {
      setValidationErrors(built.errors);
      return;
    }
    setValidationErrors([]);
    inFlight.current = true;
    setSending(true);
    setError(null);
    try {
      await onSubmit(pending.interrupt_id, built.value);
      setAnswer('');
      setSentFor(pending.interrupt_id);
    } catch (err: unknown) {
      inFlight.current = false;
      setError(formatApiError(err, t));
    } finally {
      setSending(false);
    }
  };

  return (
    <div
      role="region"
      aria-label={t('ui.pendingInputBanner')}
      data-testid="pending-input-banner"
      style={{
        padding: 12,
        backgroundColor: 'var(--status-warning-bg)',
        borderRadius: 'var(--radius-sm)',
        border: '1px solid var(--status-warning)',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
        <Icon name="pause" /> {t('ui.workflowPausedInputRequired')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
        {t('ui.pausedAtNode') + " "}<strong>{pending.node_id}</strong> (ID: <code>{pending.interrupt_id}</code>)
      </div>
      {question && (
        <div style={{ fontSize: 13, color: 'var(--text-primary)' }} data-testid="pending-input-question">
          {question}
        </div>
      )}
      <pre
        style={{
          margin: 0,
          padding: 8,
          borderRadius: 'var(--radius-sm)',
          backgroundColor: 'var(--bg-canvas)',
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
          overflowX: 'auto',
          maxHeight: 160,
        }}
      >
        {JSON.stringify(pending.value, null, 2)}
      </pre>
      {schema && (
        <div style={{ fontSize: 12, color: 'var(--text-secondary)' }} data-testid="pending-input-schema">
          {t('durable.input.schemaHint', 'Your answer must match this schema:')}{' '}
          <code>{JSON.stringify(schema)}</code>
        </div>
      )}
      <textarea
        aria-label={t('ui.yourAnswer')}
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        rows={3}
        style={{
          padding: '8px 12px',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--border-subtle)',
          backgroundColor: 'var(--bg-canvas)',
          color: 'var(--text-primary)',
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
        }}
      />
      {validationErrors.length > 0 && (
        <ul role="alert" data-testid="pending-input-validation" style={{ margin: 0, paddingLeft: 18, color: 'var(--status-danger)', fontSize: 12 }}>
          {validationErrors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {error && (
        <div role="alert" style={{ color: 'var(--status-danger)', fontSize: 12 }}>
          {error}
        </div>
      )}
      <div>
        <Button variant="primary" size="sm" loading={sending} aria-disabled={alreadySent || undefined} onClick={handleSend}>
          {t('ui.sendAnswer')}
        </Button>
        {alreadySent && (
          <div role="status" data-testid="pending-input-sent" style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 6 }}>
            {t('durable.input.sent', 'Answer sent, waiting for the run to continue.')}
          </div>
        )}
      </div>
    </div>
  );
}
