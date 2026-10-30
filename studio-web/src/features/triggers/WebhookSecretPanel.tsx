// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
// B39: one-time signing secret, delivery URL, signing headers/snippets and the workflow picker
// used by the trigger forms. Text only; the secret is passed in from React state and never
// written to storage, logs or the URL.
import { useId, useState } from 'react';
import { Button } from '../../components/Button';
import { useI18n } from '../../i18n';
import type { IssuedDeliveryToken, RegisteredWebhookSecret, TriggerWorkflowOption } from './useTriggers';
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
  fontSize: 'var(--type-label)',
  fontWeight: 500,
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
  fontFamily: 'var(--font-mono)',
  fontSize: 'var(--type-meta)',
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

/**
 * P38 C4 token mode: a webhook's delivery token, shown once. Masked until the operator asks to
 * see it; Copy reads it from memory. It is never written to storage, the URL or the console, and
 * once this disappears it cannot be shown again (rotate for a new one).
 */
export function DeliveryTokenValue({ endpointId, token, header, testIdPrefix }: { endpointId: string; token: string; header: string; testIdPrefix: string }) {
  const { t } = useI18n();
  const [revealed, setRevealed] = useState(false);
  const labelId = useId();
  return (
    <div className="resource-field" data-testid={`${testIdPrefix}-delivery-token`}>
      <span id={labelId} style={labelStyle}>{t('triggers.deliveryTokenLabel').replace('{id}', endpointId)}</span>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <code
          aria-labelledby={labelId}
          data-testid={`${testIdPrefix}-delivery-token-value`}
          style={{ ...codeBlockStyle, flex: '1 1 240px' }}
        >
          {revealed ? token : '•'.repeat(24)}
        </code>
        {!revealed && <span className="studio-sr-only">{t('triggers.deliveryTokenHidden')}</span>}
        <Button
          size="sm"
          aria-pressed={revealed}
          data-testid={`${testIdPrefix}-delivery-token-reveal`}
          onClick={() => setRevealed(!revealed)}
        >
          {revealed ? t('triggers.hideToken') : t('triggers.showToken')}
        </Button>
        <CopyButton text={token} testId={`${testIdPrefix}-delivery-token-copy`} />
      </div>
      <span className="resource-help">
        {t('triggers.deliveryTokenUse')} <code>{header}</code>
      </span>
    </div>
  );
}

/** A delivery token issued by rotation, shown once until dismissed. */
export function IssuedDeliveryTokenPanel({ issued, onDismiss }: { issued: IssuedDeliveryToken; onDismiss: () => void }) {
  const { t } = useI18n();
  return (
    <section
      role="region"
      aria-label={t('triggers.deliveryTokenRegion')}
      data-testid="delivery-token-panel"
      className="webhook-secret-panel"
    >
      <h2 style={{ margin: 0 }}>{t('triggers.deliveryTokenRotatedTitle').replace('{id}', issued.endpointId)}</h2>
      <p style={{ margin: 0, fontSize: 'var(--type-ui)', color: 'var(--text-primary)', maxWidth: '65ch' }}>{t('triggers.deliveryTokenOnce')}</p>
      <DeliveryTokenValue endpointId={issued.endpointId} token={issued.token} header={issued.header} testIdPrefix="rotated" />
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button variant="primary" data-testid="delivery-token-dismiss" onClick={onDismiss}>
          {t('triggers.dismissToken')}
        </Button>
      </div>
    </section>
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
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>
          {headers.map(([name, help]) => (
            <li key={name}>
              <code>{name}</code>: {help}
            </li>
          ))}
        </ul>
      </div>
      <div>
        <span style={labelStyle}>{t('triggers.signingTitle')}</span>
        <p style={{ margin: '0 0 6px 0', fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>
          {t('triggers.signingIntro').replace('{env}', WEBHOOK_SECRET_ENV)}
        </p>
        <p data-testid={`${testIdPrefix}-auth-note`} style={{ margin: '0 0 6px 0', fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>
          {t('triggers.signingAuth').replace('{env}', WEBHOOK_API_KEY_ENV)}
        </p>
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <div className="segmented" role="group" aria-label={t('triggers.snippetLanguage')}>
            <button type="button" data-testid={`${testIdPrefix}-snippet-curl`} aria-pressed={lang === 'curl'} onClick={() => setLang('curl')}>
              {t('triggers.snippetCurl')}
            </button>
            <button type="button" data-testid={`${testIdPrefix}-snippet-python`} aria-pressed={lang === 'python'} onClick={() => setLang('python')}>
              {t('triggers.snippetPython')}
            </button>
          </div>
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
  const [secretShown, setSecretShown] = useState(false);
  return (
    <section
      role="region"
      aria-label={t('triggers.secretLabel')}
      data-testid="webhook-secret-panel"
      className="webhook-secret-panel"
    >
      <h2 style={{ margin: 0 }}>
        {t('triggers.secretTitle').replace('{id}', registered.endpointId)}
      </h2>
      <p style={{ margin: 0, fontSize: 'var(--type-ui)', color: 'var(--text-primary)', maxWidth: '65ch' }}>{t('triggers.secretOnce')}</p>
      <div>
        <span style={labelStyle}>{t('triggers.secretLabel')}</span>
        {registered.secret ? (
          // Masked until the operator asks to see it (review M2); Copy reads it from memory.
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
            <code data-testid="webhook-secret-value" style={{ ...codeBlockStyle, flex: '1 1 240px' }}>
              {secretShown ? registered.secret : '•'.repeat(24)}
            </code>
            {!secretShown && <span className="studio-sr-only">{t('triggers.secretHidden')}</span>}
            <Button size="sm" aria-pressed={secretShown} data-testid="webhook-secret-reveal" onClick={() => setSecretShown(!secretShown)}>
              {secretShown ? t('triggers.hideToken') : t('triggers.showToken')}
            </Button>
            <CopyButton text={registered.secret} testId="webhook-secret-copy" />
          </div>
        ) : (
          <p style={{ margin: 0, fontSize: 'var(--type-meta)', color: 'var(--status-danger)' }}>{t('triggers.secretMissing')}</p>
        )}
      </div>
      {registered.deliveryToken && (
        <>
          <p style={{ margin: 0, fontSize: 'var(--type-ui)', color: 'var(--text-primary)', maxWidth: '65ch' }}>{t('triggers.deliveryTokenOnce')}</p>
          <DeliveryTokenValue
            endpointId={registered.endpointId}
            token={registered.deliveryToken}
            header="Authorization: Bearer <delivery_token>"
            testIdPrefix="webhook-secret"
          />
        </>
      )}
      <WebhookSigningSnippets deliveryUrl={registered.deliveryUrl} testIdPrefix="webhook-secret" />
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button variant="primary" data-testid="webhook-secret-dismiss" onClick={onDismiss}>
          {registered.deliveryToken ? t('triggers.dismissSecretAndToken') : t('triggers.dismissSecret')}
        </Button>
      </div>
    </section>
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
    <div className="resource-field">
      <label htmlFor={testId}>
        {t('triggers.workflowLabel')}
      </label>
      <select
        id={testId}
        data-testid={testId}
        value={value}
        disabled={loading || options.length === 0}
        onChange={(e) => onChange(e.target.value)}
        style={{ width: '100%' }}
        aria-describedby={error ? `${testId}-error` : !loading && options.length === 0 ? `${testId}-empty` : undefined}
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
        <span role="alert" id={`${testId}-error`} className="resource-error">
          {t('triggers.workflowLoadError').replace('{error}', error)}
        </span>
      )}
      {!loading && !error && options.length === 0 && (
        <span id={`${testId}-empty`} data-testid={`${testId}-empty`} className="resource-help">
          {t('triggers.workflowNoneHelp')}
        </span>
      )}
    </div>
  );
}
