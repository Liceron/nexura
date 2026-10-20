// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useMemo } from 'react';
import { Card } from '../../components/Card';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { Spinner } from '../../components/Spinner';
import { usePendingApprovals } from './usePendingApprovals';
import { ApprovalDecisionModal } from './ApprovalDecisionModal';
import type { PendingApprovalItem } from './types';
import { WaitingForInputList } from './WaitingForInputList';
import { useI18n } from '../../i18n';
import { runHref } from '../../app/routes';

interface ApprovalInboxProps {
  onOpenWorkflow?: (workflowId: string) => void;
}

export function ApprovalInbox({ onOpenWorkflow }: ApprovalInboxProps) {
  const {
    approvals,
    count,
    loading,
    error,
    refresh,
    decideApproval,
  } = usePendingApprovals();

  const { t, locale } = useI18n();
  const [waitingReload, setWaitingReload] = useState(0);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [selectedItem, setSelectedItem] = useState<PendingApprovalItem | null>(null);

  const filteredApprovals = useMemo(() => {
    if (!searchQuery.trim()) return approvals;
    const q = searchQuery.toLowerCase();
    return approvals.filter(
      (item) =>
        item.runId.toLowerCase().includes(q) ||
        item.workflowId.toLowerCase().includes(q) ||
        item.nodeId.toLowerCase().includes(q) ||
        item.approvalId.toLowerCase().includes(q) ||
        JSON.stringify(item.currentState).toLowerCase().includes(q)
    );
  }, [approvals, searchQuery]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* Top Header Card */}
      <Card elevated>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: 16,
          }}
        >
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
              <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>{t('approvalReview.inbox', "Approval Inbox")}</h1>
              <Badge variant={count > 0 ? 'warning' : 'neutral'} aria-label={t('approvalReview.counter', "Pending approvals counter")}>
                {count} {t('approvalReview.pending', 'Pending Approval')}
              </Badge>
            </div>
            <p style={{ color: 'var(--text-secondary)', fontSize: 13, margin: 0 }}>
              {t('approvalReview.subtitle', "Human-in-the-loop decisions paused at approval gates across active executions.")}
            </p>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                void refresh();
                setWaitingReload((n) => n + 1);
              }}
              loading={loading}
            >
              {t('approvalReview.refresh', "Refresh Inbox")}
            </Button>
          </div>
        </div>
      </Card>

      {/* Search Filter */}
      {approvals.length > 0 && (
        <div style={{ display: 'flex', gap: 12 }}>
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t('approvalReview.searchPlaceholder', "Search pending approvals by run, node, or workflow ID...")}
            aria-label={t('approvalReview.search', "Search pending approvals")}
            style={{
              flex: 1,
              padding: '8px 12px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--border-subtle)',
              backgroundColor: 'var(--bg-surface)',
              color: 'var(--text-primary)',
              fontSize: 13,
            }}
          />
        </div>
      )}

      {/* Loading state */}
      {loading && approvals.length === 0 && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 60, gap: 10 }}>
          <Spinner size={24} />
          <span style={{ color: 'var(--text-secondary)' }}>{t('approvalReview.loading', "Loading pending approvals...")}</span>
        </div>
      )}

      {/* Error state */}
      {error && (
        <Card elevated style={{ borderColor: 'var(--status-danger)' }}>
          <div style={{ color: 'var(--status-danger)', fontWeight: 600, fontSize: 14 }}>
            {t('approvalReview.loadingError', "Error loading approvals")}
          </div>
          <div style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 4 }}>{error}</div>
          <div style={{ marginTop: 12 }}>
            <Button variant="secondary" size="sm" onClick={refresh}>
              {t('approvalReview.retry', "Retry")}
            </Button>
          </div>
        </Card>
      )}

      {/* Empty State */}
      {!loading && !error && filteredApprovals.length === 0 && (
        <Card elevated style={{ textAlign: 'center', padding: 48 }}>
          <div style={{ fontSize: 36, marginBottom: 12 }}><Icon name="approvals" /></div>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 6 }}>{t('approvalReview.empty', "No Pending Approvals")}</h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: 13, maxWidth: 440, margin: '0 auto 16px auto' }}>
            {approvals.length === 0
              ? t('approvalReview.emptyHelp', "All workflow runs in this workspace are running smoothly or completed. When an execution requires human authorization, it will appear here.")
              : t('approvalReview.noMatch', "No approvals match your current filter query.")}
          </p>
          <Button variant="secondary" size="sm" onClick={refresh}>
            {t('approvalReview.check', "Check Again")}
          </Button>
        </Card>
      )}

      {/* Approval List */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {filteredApprovals.map((item) => (
          <Card key={item.approvalId} elevated>
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <Badge variant="warning"><Icon name="pause" /> {t('approvalReview.gate', 'Approval Gate')}: {item.nodeId}</Badge>
                  <Badge variant="neutral">{t('exactAction.run', 'Run')}: {item.runId}</Badge>
                  <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                    {t('runs.workflow')}: <strong>{item.workflowId}</strong>
                  </span>
                </div>

                {/* State summary tags */}
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
                  {item.currentState.amount !== undefined && (
                    <Badge variant="info">{t('approvalReview.amount', 'Amount')}: {String(item.currentState.amount)}{typeof item.currentState.currency === 'string' && item.currentState.currency.trim() ? ` ${item.currentState.currency.trim()}` : ''}</Badge>
                  )}
                  {Boolean(item.currentState.category) && (
                    <Badge variant="neutral">{t('approvalReview.category', 'Category')}: {String(item.currentState.category)}</Badge>
                  )}
                  {Boolean(item.currentState.classification) && (
                    <Badge variant="warning">{String(item.currentState.classification)}</Badge>
                  )}
                  {Boolean(item.currentState.order_id) && (
                    <Badge variant="neutral">{t('approvalReview.order', 'Order')}: {String(item.currentState.order_id)}</Badge>
                  )}
                </div>

                <div style={{ fontSize: 12 }}>{t('approvalReview.reviewHint', 'Open Review & Decide to inspect the exact action and its expiry.')}</div>

                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
                  {t('approvalReview.id', 'Approval ID')}: <code>{item.approvalId}</code> • {t('approvalReview.initiated', 'Initiated')}:{' '}
                  {item.createdAt ? new Date(item.createdAt * 1000).toLocaleTimeString(locale) : t('exactAction.notRecorded', 'not recorded')}
                </div>
              </div>

              <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                <a href={runHref(item.runId)} data-testid="approval-open-run" style={{ fontSize: 13 }}>
                  {t('runs.open')}
                  <span className="sr-only" style={{ position: 'absolute', left: -9999 }}>
                    {' '}
                    {item.runId}
                  </span>
                </a>
                {onOpenWorkflow && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => onOpenWorkflow(item.workflowId)}
                  >
                    {t('approvalReview.workflow', "View Workflow")}
                  </Button>
                )}
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => setSelectedItem(item)}
                >
                  {t('approvalReview.review', "Review & Decide")}
                </Button>
              </div>
            </div>
          </Card>
        ))}
      </div>

      {/* Runs paused by interrupt() for an answer (P25); answered on the run page. */}
      <WaitingForInputList reloadKey={waitingReload} />

      {/* Decision Modal */}
      <ApprovalDecisionModal
        open={selectedItem !== null}
        item={selectedItem}
        onClose={() => setSelectedItem(null)}
        onDecide={decideApproval}
      />
    </div>
  );
}
