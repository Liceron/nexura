// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import { useLocale } from '../../i18n';
import { Button } from '../../components/Button';
import { useDurableT } from '../runs/useDurableT';
import type { ExactApprovalRequest } from './types';

/** Display only; the browser never computes or substitutes an approval digest. */
export function payloadJson(value: unknown): string | undefined {
  if (value === undefined) return undefined;
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(payloadJson).join(',')}]`;
  return `{${Object.keys(value).sort((a, b) => {
    const x = Array.from(a, c => c.codePointAt(0)!);
    const y = Array.from(b, c => c.codePointAt(0)!);
    for (let i = 0; i < Math.min(x.length, y.length); i++) {
      if (x[i] !== y[i]) return x[i] - y[i];
    }
    return x.length - y.length;
  }).map(key => `${JSON.stringify(key)}:${payloadJson((value as Record<string, unknown>)[key])}`).join(',')}}`;
}

export function isApprovalExpired(request: ExactApprovalRequest | null | undefined, now = Date.now()) {
  return request?.status === 'expired' ||
    (typeof request?.expires_at === 'number' && Number.isFinite(request.expires_at) && request.expires_at * 1000 <= now);
}

export function useApprovalClock(request: ExactApprovalRequest | null | undefined) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    setNow(Date.now());
    if (typeof request?.expires_at !== 'number' || !Number.isFinite(request.expires_at)) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [request?.expires_at]);
  return { now, expired: isApprovalExpired(request, now) };
}

function JsonText({ label, text }: { label: string; text?: string }) {
  const t = useDurableT();
  const pre = <pre style={{ fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 260, overflowY: 'auto', margin: '6px 0' }}>{text ?? t('exactAction.notRecorded', 'not recorded')}</pre>;
  return text && text.length > 300
    ? <details><summary>{label} — {t('exactAction.expand', 'Expand JSON')}</summary>{pre}</details>
    : <div><strong>{label}</strong>{pre}</div>;
}

function Digest({ label, value }: { label: string; value?: string }) {
  const t = useDurableT();
  const [copyState, setCopyState] = useState('');
  return <div>{label}: {value ? <>
    <code title={value}>{value.length > 16 ? `${value.slice(0, 12)}…${value.slice(-4)}` : value}</code>{' '}
    <Button size="sm" type="button" aria-label={`${t('exactAction.copy', 'Copy')} ${label}`} onClick={async () => {
      try {
        await navigator.clipboard.writeText(value);
        setCopyState(t('exactAction.copied', 'Copied'));
      } catch {
        setCopyState(t('exactAction.copyFailed', 'Copy failed'));
      }
    }}>{t('exactAction.copy', 'Copy')}</Button> <span role="status">{copyState}</span>
  </> : t('exactAction.notRecorded', 'not recorded')}</div>;
}

export function ExactAction({ request, previous: observed, workflowHandler }: {
  request?: ExactApprovalRequest | null;
  previous?: ExactApprovalRequest;
  workflowHandler?: string;
}) {
  const t = useDurableT();
  const locale = useLocale();
  // Server-recorded provenance wins over a snapshot this session happened to observe.
  const previous: ExactApprovalRequest | undefined =
    request?.regate_reason === 'inputs_edited'
      ? { approval_id: request.supersedes_approval_id ?? undefined, input_state: request.previous_input_state ?? undefined }
      : observed;
  const { now, expired } = useApprovalClock(request);
  const missing = t('exactAction.notRecorded', 'not recorded');
  const expiry = typeof request?.expires_at === 'number' && Number.isFinite(request.expires_at) ? request.expires_at * 1000 : null;
  const seconds = expiry === null ? 0 : Math.max(0, Math.ceil((expiry - now) / 1000));
  const text = (value?: string) => value || missing;
  // Workflow requests identify a node even when older servers omit action_kind/name.
  // Explicit action fields always win, and digests remain exclusively server supplied.
  const workflowStep = !request?.action_kind && !!request?.workflow_run_id && !!request?.node_id;
  const handler = request?.handler || (typeof request?.details?.handler === 'string' ? request.details.handler : undefined) || workflowHandler;
  const workflowName = request?.node_id
    ? `${request.node_id}${handler ? ` (${handler})` : ''}`
    : undefined;
  return <section aria-label={t('exactAction.title', 'Exact action')} data-testid="exact-action" style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: 12 }}>
    <strong>{t('exactAction.title', 'Exact action')}</strong>
    <div>{t('exactAction.kind', 'Action kind')}: <code>{text(request?.action_kind || (workflowStep ? t('exactAction.workflowStep', 'Workflow step') : undefined))}</code></div>
    <div>{t('exactAction.name', 'Action name')}: <code>{text(request?.action_name || (workflowStep ? workflowName : undefined))}</code></div>
    <div>{t('exactAction.run', 'Run')}: <code>{text(request?.run_id || request?.workflow_run_id)}</code></div>
    <div>{t('exactAction.node', 'Node')}: <code>{text(request?.node_id)}</code></div>
    <JsonText label={t('exactAction.arguments', 'Arguments (positional and named)')} text={request?.canonical_payload_json ?? payloadJson(request?.action_payload)} />
    {!request?.canonical_payload_json && <div>{t('exactAction.canonicalMissing', 'Server canonical JSON: not recorded. Available arguments are shown as sorted JSON; exact numeric formatting requires the server canonical text.')}</div>}
    <Digest label={t('exactAction.payloadDigest', 'Payload digest')} value={request?.payload_digest} />
    <Digest label={t('exactAction.specDigest', 'Agent spec digest')} value={request?.spec_digest} />
    <div>{t('exactAction.expiry', 'Expires')}: {expiry === null ? missing : <>
      <time dateTime={new Date(expiry).toISOString()}>{new Date(expiry).toLocaleString(locale)}</time>{' · '}
      {expired ? t('exactAction.expired', 'Expired') : `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`}
    </>}</div>
    {expired && <p role="alert">{t('exactAction.expiredReason', 'This request has expired. Approval is disabled; a fresh request is required.')}</p>}
    {previous && <div data-testid="approval-edited-inputs">
      <p>{t('exactAction.edited', 'This is a new request for edited inputs. Review it again before approving.')}</p>
      {previous.approval_id && <div>{t('exactAction.replaces', 'Replaces request')}: <code>{previous.approval_id}</code></div>}
      {previous.input_state !== undefined && request?.input_state !== undefined && <>
        <JsonText label={t('exactAction.previous', 'Previous inputs')} text={payloadJson(previous.input_state)} />
        <JsonText label={t('exactAction.new', 'New inputs')} text={payloadJson(request.input_state)} />
      </>}
    </div>}
    <p>{t('exactAction.editRule', 'Editing paused inputs can create a fresh approval request. Approval covers only the reviewed action.')}</p>
  </section>;
}
