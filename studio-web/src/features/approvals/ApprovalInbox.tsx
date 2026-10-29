// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useState, useMemo, useRef } from 'react';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { Button } from '../../components/Button';
import { useApi } from '../../api/context';
import { usePendingApprovals } from './usePendingApprovals';
import { ApprovalDecisionModal } from './ApprovalDecisionModal';
import type { PendingApprovalItem, ExactApprovalRequest } from './types';
import { WaitingForInputList } from './WaitingForInputList';
import { useI18n } from '../../i18n';
import { runHref } from '../../app/routes';
import { describeAgentToolCalls } from './agentToolCalls';
import { RunStatus } from '../runs/RunStatus';
import { CancelRunButton } from '../runs/CancelRunButton';

export function ApprovalInbox({ onOpenWorkflow }: { onOpenWorkflow?: (workflowId: string) => void }) {
  const { approvals, count, loading, error, refresh, decideApproval } = usePendingApprovals();
  const { connection } = useApi();
  const { t, locale } = useI18n();
  const [waitingReload, setWaitingReload] = useState(0);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedItem, setSelectedItem] = useState<PendingApprovalItem | null>(null);
  const countRef = useRef<HTMLSpanElement>(null);
  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return !q ? approvals : approvals.filter(item =>
      [item.runId, item.workflowId, item.nodeId, item.approvalId, item.spec?.name ?? '', JSON.stringify(item.currentState)].some(value => value.toLowerCase().includes(q)));
  }, [approvals, searchQuery]);
  const missing = t('exactAction.notRecorded');
  const date = (value?: number | null) => typeof value === 'number' && Number.isFinite(value)
    ? new Date(value * 1000).toLocaleString(locale, { timeZoneName: 'short' }) : missing;
  return <PageLayout className="approval-inbox">
    <PageHeader title={t('approvalReview.inbox')} metadata={<span ref={countRef} tabIndex={-1} role="status" aria-label={t('approvalReview.counter')}>{count} {t('approvalReview.pending')}</span>}
      actions={<Button variant="ghost" loading={loading} onClick={() => { void refresh(); setWaitingReload(n => n + 1); }}>{t('approvalReview.refresh')}</Button>}
      filters={approvals.length > 0 ? <input value={searchQuery} onChange={e => setSearchQuery(e.target.value)}
        placeholder={t('approvalReview.searchPlaceholder')} aria-label={t('approvalReview.search')} /> : undefined} />
    {loading && approvals.length === 0 && <p role="status">{t('approvalReview.loading')}</p>}
    {error && <div role="alert" className="inspection-error"><p>{error}</p><Button onClick={refresh}>{t('approvalReview.retry')}</Button></div>}
    {!loading && !error && filtered.length === 0 && <div className="inspection-empty">
      <h2>{t('approvalReview.empty')}</h2>
      <p>{approvals.length === 0 ? t('approvalReview.emptyHelp') : t('approvalReview.noMatch')}</p>
    </div>}
    {filtered.length > 0 && <div className="inspection-table-scroll" role="region" aria-label={t('approvalReview.inbox')} tabIndex={0}>
      <table className="inspection-table"><thead><tr>
        <th>{t('exactAction.name')}</th><th>{t('reviewDesign.workflowAgent')}</th><th>{t('reviewDesign.recordedSummary')}</th>
        <th>{t('approvalReview.initiated')}</th><th>{t('exactAction.expiry')}</th><th>{t('approvalReview.review')}</th>
      </tr></thead><tbody>{filtered.map(item => {
        const request = item.runRecord.pending_approval as ExactApprovalRequest | undefined;
        const agent = describeAgentToolCalls(request);
        return <tr key={item.approvalId}>
          <td><strong>{request?.action_name || item.nodeId || missing}</strong><br /><RunStatus status="pending_approval" /></td>
          <td>{agent?.agentName || item.spec?.name || item.workflowId || missing}</td>
          <td>{agent ? agent.calls.map(call => call.tool).join(', ') : missing}
            {/* Paused state is contextual data, not a fabricated exact-action summary. */}
            {(item.currentState.amount !== undefined || item.currentState.order_id !== undefined) && <details>
              <summary>{t('approvalReview.state')}</summary>
              {item.currentState.amount !== undefined && <div>{t('approvalReview.amount')}: {String(item.currentState.amount)}{typeof item.currentState.currency === 'string' ? ' ' + item.currentState.currency : ''}</div>}
              {item.currentState.order_id !== undefined && <div>{t('approvalReview.order')}: {String(item.currentState.order_id)}</div>}
            </details>}
          </td>
          <td>{date(item.createdAt)}</td><td>{date(request?.expires_at)}</td>
          <td><div className="inspection-actions">
            <Button variant="secondary" onClick={() => setSelectedItem(item)}>{t('approvalReview.review')}</Button>
            <CancelRunButton runId={item.runId} status={item.runRecord.status} onCancelled={refresh} />
            <a href={runHref(item.runId)} data-testid="approval-open-run">{t('runs.open')}<span className="studio-sr-only"> {item.runId}</span></a>
            {onOpenWorkflow && <Button variant="ghost" onClick={() => onOpenWorkflow(item.workflowId)}>{t('approvalReview.workflow')}</Button>}
          </div><code className="inspection-meta">{item.runId}</code></td>
        </tr>;
      })}</tbody></table>
    </div>}
    {count > approvals.length && <p>{t('reviewDesign.queueLimit').replace('{shown}', String(approvals.length)).replace('{total}', String(count))}</p>}
    <WaitingForInputList reloadKey={waitingReload} />
    <ApprovalDecisionModal open={selectedItem !== null} item={selectedItem} workspace={connection.workspace}
      onClose={() => setSelectedItem(null)} onDecide={decideApproval} returnFocusTo={() => countRef.current} />
  </PageLayout>;
}
