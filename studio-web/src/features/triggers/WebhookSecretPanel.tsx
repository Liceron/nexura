// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// B39: one-time signing secret, delivery URL, signing headers/snippets and the workflow picker
// used by the trigger forms. Text only; the secret is passed in from React state and never
// written to storage, logs or the URL.
import { useState } from 'react';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { useI18n } from '../../i18n';
import type { RegisteredWebhookSecret, TriggerWorkflowOption } from './useTriggers';
import {
  WEBHOOK_EVENT_ID_HEADER,
  WEBHOOK_SECRET_ENV,
  WEBHOOK_API_KEY_ENV,
  WEBHOOK_SIGNATURE_HEADER,
  WEBHOOK_TIMESTAMP_HEADER,
  buildCurlSnippet,
  buildPythonSnippet,
} from './webhookSnippet';

const labelStyle = {
  fontSize: 12,
  fontWeight: 600,
  color: 'var(--text-secondary)',
  display: 'block',
  marginBottom: 4,
} as const;

const codeBlockStyle = {
  margin: 0,
  padding: 10,
  borderRadius: 'var(--radius-md)',
  border: '1px solid var(--border-subtle)',
  backgroundColor: 'var(--bg-surface-elevated)',
  color: 'var(--text-primary)',
  fontFamily: 'monospace',
  fontSize: 11,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-all',
  overflowX: 'auto',
} as const;

type CopyState = 'idle' | 'copied' | 'failed';

function CopyButton({ text, testId }: { text: string; testId: string }) {
  const { t } = useI18n();
  const [state, setState] = useState<CopyState>('idle');
  return (
    <Button
      variant="secondary"
      size="sm"
      data-testid={testId}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setState('copied');
        } catch {
          setState('failed');
        }
      }}
    >
      {state === 'copied' ? t('triggers.copied') : state === 'failed' ? t('triggers.copyFailed') : t('triggers.copy')}
    </Button>
  );
}

/** Delivery URL, required headers and curl/Python snippets. Contains no secret. */
export function WebhookSigningSnippets({ deliveryUrl, testIdPrefix }: { deliveryUrl: string; testIdPrefix: string }) {
  const { t } = useI18n();
  const [lang, setLang] = useState<'curl' | 'python'>('curl');
  const snippet = lang === 'curl' ? buildCurlSnippet({ deliveryUrl }) : buildPythonSnippet({ deliveryUrl });
  const headers: [string, string][] = [
    [WEBHOOK_TIMESTAMP_HEADER, t('triggers.headerTimestamp')],
    [WEBHOOK_EVENT_ID_HEADER, t('triggers.headerEventId')],
    [WEBHOOK_SIGNATURE_HEADER, t('triggers.headerSignature')],
  ];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div>
        <span style={labelStyle}>{t('triggers.deliveryUrl')}</span>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <code data-testid={`${testIdPrefix}-delivery-url`} style={{ ...codeBlockStyle, flex: 1 }}>
            {deliveryUrl}
          </code>
          <CopyButton text={deliveryUrl} testId={`${testIdPrefix}-copy-url`} />
        </div>
      </div>
      <div>
        <span style={labelStyle}>{t('triggers.headersTitle')}</span>
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12, color: 'var(--text-secondary)' }}>
          {headers.map(([name, help]) => (
            <li key={name}>
              <code>{name}</code>: {help}
            </li>
          ))}
        </ul>
      </div>
      <div>
        <span style={labelStyle}>{t('triggers.signingTitle')}</span>
        <p style={{ margin: '0 0 6px 0', fontSize: 12, color: 'var(--text-secondary)' }}>
          {t('triggers.signingIntro').replace('{env}', WEBHOOK_SECRET_ENV)}
        </p>
        <p data-testid={`${testIdPrefix}-auth-note`} style={{ margin: '0 0 6px 0', fontSize: 12, color: 'var(--text-secondary)' }}>
          {t('triggers.signingAuth').replace('{env}', WEBHOOK_API_KEY_ENV)}
        </p>
        <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
          <Button
            variant={lang === 'curl' ? 'primary' : 'secondary'}
            size="sm"
            data-testid={`${testIdPrefix}-snippet-curl`}
            aria-pressed={lang === 'curl'}
            onClick={() => setLang('curl')}
          >
            {t('triggers.snippetCurl')}
          </Button>
          <Button
            variant={lang === 'python' ? 'primary' : 'secondary'}
            size="sm"
            data-testid={`${testIdPrefix}-snippet-python`}
            aria-pressed={lang === 'python'}
            onClick={() => setLang('python')}
          >
            {t('triggers.snippetPython')}
          </Button>
          <CopyButton text={snippet} testId={`${testIdPrefix}-copy-snippet`} />
        </div>
        <pre data-testid={`${testIdPrefix}-snippet`} style={codeBlockStyle}>
          {snippet}
        </pre>
      </div>
    </div>
  );
}

/** Shown once after registration; dismissing drops the secret from memory. */
export function WebhookSecretPanel({
  registered,
  onDismiss,
}: {
  registered: RegisteredWebhookSecret;
  onDismiss: () => void;
}) {
  const { t } = useI18n();
  return (
    <Card
      elevated
      role="region"
      aria-label={t('triggers.secretLabel')}
      data-testid="webhook-secret-panel"
      style={{ display: 'flex', flexDirection: 'column', gap: 12, borderColor: 'var(--status-warning)' }}
    >
      <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: 'var(--text-primary)' }}>
        {t('triggers.secretTitle').replace('{id}', registered.endpointId)}
      </h2>
      <p style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)' }}>{t('triggers.secretOnce')}</p>
      <div>
        <span style={labelStyle}>{t('triggers.secretLabel')}</span>
        {registered.secret ? (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <code data-testid="webhook-secret-value" style={{ ...codeBlockStyle, flex: 1 }}>
              {registered.secret}
            </code>
            <CopyButton text={registered.secret} testId="webhook-secret-copy" />
          </div>
        ) : (
          <p style={{ margin: 0, fontSize: 12, color: 'var(--status-danger)' }}>{t('triggers.secretMissing')}</p>
        )}
      </div>
      <WebhookSigningSnippets deliveryUrl={registered.deliveryUrl} testIdPrefix="webhook-secret" />
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button variant="primary" data-testid="webhook-secret-dismiss" onClick={onDismiss}>
          {t('triggers.dismissSecret')}
        </Button>
      </div>
    </Card>
  );
}

/** Picker over the workspace's workflows (replaces the free-text workflow id). */
export function WorkflowPicker({
  value,
  onChange,
  options,
  loading,
  error,
  testId,
}: {
  value: string;
  onChange: (id: string) => void;
  options: TriggerWorkflowOption[];
  loading: boolean;
  error: string | null;
  testId: string;
}) {
  const { t } = useI18n();
  return (
    <div>
      <label htmlFor={testId} style={labelStyle}>
        {t('triggers.workflowLabel')}
      </label>
      <select
        id={testId}
        data-testid={testId}
        value={value}
        disabled={loading || options.length === 0}
        onChange={(e) => onChange(e.target.value)}
        style={{
          width: '100%',
          padding: '8px 12px',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--border-subtle)',
          backgroundColor: 'var(--bg-surface)',
          color: 'var(--text-primary)',
          fontSize: 13,
        }}
      >
        {loading && <option value="">{t('triggers.workflowLoading')}</option>}
        {!loading && options.length === 0 && <option value="">{t('triggers.workflowNone')}</option>}
        {options.map((o) => (
          <option key={o.workflow_id} value={o.workflow_id}>
            {o.name === o.workflow_id ? o.workflow_id : `${o.name} (${o.workflow_id})`}
          </option>
        ))}
      </select>
      {error && (
        <span role="alert" style={{ fontSize: 11, color: 'var(--status-danger)' }}>
          {t('triggers.workflowLoadError').replace('{error}', error)}
        </span>
      )}
      {!loading && !error && options.length === 0 && (
        <span data-testid={`${testId}-empty`} style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {t('triggers.workflowNoneHelp')}
        </span>
      )}
    </div>
  );
}
