// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useMemo, useEffect } from 'react';
import { Modal } from '../../components/Modal';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { ApprovalDetailsPanel } from './ApprovalDetailsPanel';
import type { PendingApprovalItem, DecisionType, DecisionResult, ExactApprovalRequest } from './types';
import { formatApiError } from '../../api/errors';
import { useDurableT } from '../runs/useDurableT';
import { isApprovalExpired, payloadJson, useApprovalClock } from './ExactAction';

interface ApprovalDecisionModalProps {
  open: boolean;
  item: PendingApprovalItem | null;
  onClose: () => void;
  onDecide: (
    runId: string,
    approvalId: string,
    decision: DecisionType,
    stateUpdate?: Record<string, unknown>
  ) => Promise<DecisionResult>;
}

export function ApprovalDecisionModal({
  open,
  item: originalItem,
  onClose,
  onDecide,
}: ApprovalDecisionModalProps) {
  const t = useDurableT();
  const [replacement, setReplacement] = useState<PendingApprovalItem | null>(null);
  const item = replacement ?? originalItem;
  const { expired } = useApprovalClock(open ? item?.runRecord.pending_approval : undefined);
  const [stateUpdateText, setStateUpdateText] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [conflictOccurred, setConflictOccurred] = useState<boolean>(false);
  useEffect(() => {
    setReplacement(null);
    setStateUpdateText('');
    setDecisionError(null);
    setConflictOccurred(false);
  }, [originalItem?.approvalId, open]);

  // Parse state update if provided
  const { parsedUpdate, parseError } = useMemo(() => {
    if (!stateUpdateText.trim()) {
      return { parsedUpdate: undefined, parseError: null };
    }
    try {
      const parsed = JSON.parse(stateUpdateText);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return { parsedUpdate: undefined, parseError: t('approvalReview.objectError', 'State update must be a JSON object.') };
      }
      return { parsedUpdate: parsed as Record<string, unknown>, parseError: null };
    } catch (err: unknown) {
      return {
        parsedUpdate: undefined,
        parseError: `${t('approvalReview.jsonError', 'JSON syntax error')}: ${formatApiError(err, t)}`,
      };
    }
  }, [stateUpdateText, t]);

  if (!open || !item) return null;

  const node = item.spec?.nodes[item.runRecord.pending_approval?.node_id ?? ''];
  const workflowHandler = typeof node?.handler === 'string' ? node.handler : undefined;

  const handleAction = async (decision: DecisionType) => {
    if (parseError || (decision === 'approve' && isApprovalExpired(item.runRecord.pending_approval))) return;
    setIsSubmitting(true);
    setDecisionError(null);
    setConflictOccurred(false);

    try {
      const result = await onDecide(
        item.runId,
        item.approvalId,
        decision,
        decision === 'approve' ? parsedUpdate : undefined
      );

      if (result.success) {
        const run = result.run;
        if (run?.status === 'pending_approval' && run.pending_approval && run.pending_approval.approval_id !== item.approvalId) {
          const prior = item.runRecord.pending_approval as ExactApprovalRequest | undefined;
          const next = run.pending_approval as ExactApprovalRequest;
          const edited = next.node_id === prior?.node_id && (
            (parsedUpdate && Object.keys(parsedUpdate).length > 0) ||
            (prior?.input_state && next.input_state && payloadJson(prior.input_state) !== payloadJson(next.input_state))
          );
          setReplacement({
            ...item, runRecord: run, approvalId: run.pending_approval.approval_id,
            nodeId: run.pending_approval.node_id, currentState: run.current_state,
            previousApproval: edited ? prior : undefined,
          });
          setStateUpdateText('');
        } else {
          onClose();
        }
      } else {
        setDecisionError(result.error || t('approvalReview.submitError', 'Failed to submit decision.'));
        if (result.conflict) {
          setConflictOccurred(true);
        }
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={t('approvalReview.title', 'Review Approval Decision')}>
      <div
        style={{ display: 'flex', flexDirection: 'column', gap: 16, maxHeight: 'calc(100vh - 160px)', overflowY: 'auto' }}
      >
        {/* Run & Gate Context */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Badge variant="warning"><Icon name="pause" /> {t('approvalReview.pending', 'Pending Approval')}</Badge>
          <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('exactAction.run', 'Run')}: <code>{item.runId}</code>
          </span>
          <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('exactAction.node', 'Node')}: <strong>{item.nodeId}</strong>
          </span>
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
            {t('approvalReview.id', 'Approval ID')}: <code>{item.approvalId}</code>
          </span>
        </div>

        {/* Error / Conflict Alert */}
        {decisionError && (
          <div
            style={{
              padding: '10px 14px',
              backgroundColor: 'var(--status-danger-bg)',
              color: 'var(--status-danger)',
              borderRadius: 'var(--radius-sm)',
              fontSize: 13,
              lineHeight: 1.4,
            }}
          >
            <strong>{conflictOccurred ? t('approvalReview.conflict', 'Decision Conflict:') : t('approvalReview.error', 'Submission Error:')}</strong>{' '}
            {decisionError}
            {conflictOccurred && (
              <div style={{ marginTop: 6, fontSize: 12 }}>
                {t('approvalReview.conflictHelp', 'This approval was already claimed by another user or the workflow has reached a terminal state.')}
              </div>
            )}
          </div>
        )}

        <ApprovalDetailsPanel pendingApproval={item.runRecord.pending_approval} previousApproval={item.previousApproval} workflowHandler={workflowHandler} />

        {/* Immutable Action Arguments & State Review */}
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
            {t('approvalReview.state', 'Paused workflow state')}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>
            {t('approvalReview.stateHelp', 'State captured when the workflow paused. The exact action arguments are listed above.')}
          </div>
          <pre
            data-testid="approval-paused-state"
            style={{
              fontFamily: 'var(--font-mono)',
              whiteSpace: 'pre-wrap',
              overflowWrap: 'anywhere',
              fontSize: 12,
              backgroundColor: 'var(--bg-canvas)',
              padding: 12,
              borderRadius: 'var(--radius-sm)',
              maxHeight: 180,
              overflowY: 'auto',
              border: '1px solid var(--border-subtle)',
              margin: 0,
            }}
          >
            {JSON.stringify(item.currentState, null, 2)}
          </pre>
        </div>

        {/* Optional State Update Field */}
        <div>
          <label
            htmlFor="approval-state-update"
            style={{ display: 'block', fontSize: 13, fontWeight: 600, marginBottom: 4 }}
          >
            {t('approvalReview.adjust', 'Optional State Adjustments (Applied on Approval)')}
          </label>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>
            {t('approvalReview.adjustHelp', 'Enter state overrides as JSON. Changed inputs require a fresh approval before execution.')}
          </div>
          <textarea
            id="approval-state-update"
            rows={3}
            value={stateUpdateText}
            onChange={(e) => setStateUpdateText(e.target.value)}
            placeholder={t('approvalReview.placeholder', '{ "override_limit": 10000 }')}
            style={{
              width: '100%',
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              padding: 8,
              borderRadius: 'var(--radius-sm)',
              border: `1px solid ${parseError ? 'var(--status-danger)' : 'var(--border-subtle)'}`,
              backgroundColor: 'var(--bg-canvas)',
              color: 'var(--text-primary)',
            }}
          />
          {parseError && (
            <div style={{ fontSize: 12, color: 'var(--status-danger)', marginTop: 4 }}>
              {parseError}
            </div>
          )}
        </div>

        {/* Consequence Warning Box */}
        <div
          style={{
            padding: '10px 14px',
            backgroundColor: 'var(--bg-surface)',
            border: '1px solid var(--border-subtle)',
            borderRadius: 'var(--radius-sm)',
            fontSize: 12,
            lineHeight: 1.4,
            color: 'var(--text-secondary)',
          }}
        >
          <div>
            {t('approvalReview.approveHelp', 'Approval resumes the reviewed action. If its inputs changed, review the fresh request before it runs.')}
          </div>
          <div style={{ marginTop: 4 }}>
            {t('approvalReview.rejectHelp', 'Rejection stops this workflow without executing downstream actions.')}
          </div>
        </div>

        {/* Footer Actions */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 4 }}>
          <Button variant="secondary" onClick={onClose} disabled={isSubmitting}>
            {t('approvalReview.cancel', 'Cancel')}
          </Button>
          <Button
            variant="danger"
            loading={isSubmitting}
            disabled={isSubmitting || conflictOccurred}
            onClick={() => handleAction('reject')}
          >
            {t('approvalReview.reject', 'Reject Execution')}
          </Button>
          <Button
            variant="primary"
            loading={isSubmitting}
            disabled={isSubmitting || Boolean(parseError) || conflictOccurred || expired}
            onClick={() => handleAction('approve')}
          >
            {t('approvalReview.approve', 'Approve & Resume')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
