// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useState, useMemo, useEffect, useRef } from 'react';
import { Modal } from '../../components/Modal';
import { Button } from '../../components/Button';
import { RunStatus } from '../runs/RunStatus';
import { ApprovalDetailsPanel } from './ApprovalDetailsPanel';
import type { PendingApprovalItem, DecisionType, DecisionResult, ExactApprovalRequest } from './types';
import { formatApiError } from '../../api/errors';
import { useDurableT } from '../runs/useDurableT';
import { isApprovalExpired, payloadJson, useApprovalClock } from './ExactAction';

interface ApprovalDecisionModalProps {
  open: boolean;
  item: PendingApprovalItem | null;
  workspace?: string;
  stale?: boolean;
  returnFocusTo?: () => HTMLElement | null;
  onClose: () => void;
  onDecide: (runId: string, approvalId: string, decision: DecisionType, stateUpdate?: Record<string, unknown>) => Promise<DecisionResult>;
}

export function ApprovalDecisionModal({ open, item: originalItem, workspace, stale: externallyStale = false, returnFocusTo, onClose, onDecide }: ApprovalDecisionModalProps) {
  const t = useDurableT();
  const [replacement, setReplacement] = useState<PendingApprovalItem | null>(null);
  const item = replacement ?? originalItem;
  const { expired } = useApprovalClock(open ? item?.runRecord.pending_approval : undefined);
  const [stateUpdateText, setStateUpdateText] = useState('');
  const [editing, setEditing] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const submitting = useRef(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [conflictOccurred, setConflictOccurred] = useState(false);
  const content = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setReplacement(null);
    setStateUpdateText('');
    setEditing(false);
    setDecisionError(null);
    setConflictOccurred(false);
  }, [originalItem?.approvalId, open]);
  useEffect(() => {
    if (!open) return;
    content.current?.closest('[role="dialog"]')?.querySelector<HTMLElement>('h2')?.focus();
    const body = content.current?.closest('.studio-modal-body');
    if (body) body.scrollTop = 0;
  }, [item?.approvalId, open]);

  const { parsedUpdate, parseError } = useMemo(() => {
    if (!stateUpdateText.trim()) return { parsedUpdate: undefined, parseError: null };
    try {
      const parsed = JSON.parse(stateUpdateText);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return { parsedUpdate: undefined, parseError: t('approvalReview.objectError') };
      }
      return { parsedUpdate: parsed as Record<string, unknown>, parseError: null };
    } catch (err) {
      return { parsedUpdate: undefined, parseError: `${t('approvalReview.jsonError')}: ${formatApiError(err, t)}` };
    }
  }, [stateUpdateText, t]);

  if (!open || !item) return null;
  const request = item.runRecord.pending_approval as ExactApprovalRequest | undefined;
  const decided = !!request?.status && !['pending', 'pending_approval', 'expired'].includes(request.status);
  const stale = externallyStale || conflictOccurred || decided || item.runRecord.status !== 'pending_approval';
  const node = item.spec?.nodes[request?.node_id ?? ''];
  const workflowHandler = typeof node?.handler === 'string' ? node.handler : undefined;
  const close = () => { if (!submitting.current) onClose(); };

  const handleAction = async (decision: DecisionType) => {
    if (submitting.current || stale || (decision === 'approve' && (
      isApprovalExpired(request) || (editing && (parseError || !parsedUpdate || !Object.keys(parsedUpdate).length))
    ))) return;
    submitting.current = true;
    setIsSubmitting(true);
    setDecisionError(null);
    try {
      // Existing wire contract: edits use approve + state_update; the server re-gates.
      // The first submission is deliberately not described as approval in the UI.
      const result = await onDecide(item.runId, item.approvalId, decision, decision === 'approve' && editing ? parsedUpdate : undefined);
      if (result.success) {
        const run = result.run;
        if (run?.status === 'pending_approval' && run.pending_approval && run.pending_approval.approval_id !== item.approvalId) {
          const next = run.pending_approval as ExactApprovalRequest;
          const edited = next.node_id === request?.node_id && (
            (editing && parsedUpdate && Object.keys(parsedUpdate).length > 0) ||
            (request?.input_state && next.input_state && payloadJson(request.input_state) !== payloadJson(next.input_state))
          );
          setReplacement({ ...item, runRecord: run, approvalId: run.pending_approval.approval_id,
            nodeId: run.pending_approval.node_id, currentState: run.current_state, previousApproval: edited ? request : undefined });
          setEditing(false);
          setStateUpdateText('');
        } else {
          onClose();
        }
      } else {
        setDecisionError(result.error || t('approvalReview.submitError'));
        setConflictOccurred(Boolean(result.conflict));
      }
    } catch (error) {
      setDecisionError(formatApiError(error, t));
      setConflictOccurred(/409|410|EXPIRED|ALREADY_CLAIMED|NOT_PENDING|ALREADY_TERMINAL/.test(String(error)));
    } finally {
      submitting.current = false;
      setIsSubmitting(false);
    }
  };

  return <Modal open={open} onClose={close} title={t('approvalReview.title')} maxWidth={800}
    initialFocus="title" className="approval-review" closeDisabled={isSubmitting} returnFocusTo={returnFocusTo}
    footer={<div className="approval-decision-footer">
      <p>{t('reviewDesign.consequence')}</p>
      <div className="inspection-actions">
        <Button variant="secondary" disabled={isSubmitting || stale} onClick={() => void handleAction('reject')}>{t('approvalReview.reject')}</Button>
        <Button variant="primary" loading={isSubmitting} aria-label={editing ? t('reviewDesign.submitEdits') : t('approvalReview.approve')}
          disabled={isSubmitting || stale || expired || (editing && (Boolean(parseError) || !parsedUpdate || !Object.keys(parsedUpdate).length))}
          onClick={() => void handleAction('approve')}>
          {editing ? t('reviewDesign.submitEdits') : t('approvalReview.approve')}
        </Button>
      </div>
      {isSubmitting && <p role="status">{t('reviewDesign.submitting')}</p>}
    </div>}>
    <div ref={content} key={item.approvalId} className="approval-review-content">
      <div className="inspection-meta">
        <span>{t('reviewDesign.workspace')}: {workspace || t('exactAction.notRecorded')}</span>
        <span>{item.spec?.name || item.workflowId || t('exactAction.notRecorded')}</span>
        <RunStatus status={item.runRecord.status} />
      </div>
      {decisionError && <div role="alert" className="inspection-error">{decisionError}</div>}
      {stale && <p role="alert">{t('reviewDesign.stale')}</p>}
      <ApprovalDetailsPanel pendingApproval={request} previousApproval={item.previousApproval} workflowHandler={workflowHandler} />
      <details>
        <summary>{t('approvalReview.state')}</summary>
        <p className="inspection-meta">{t('approvalReview.stateHelp')}</p>
        <pre className="inspection-code" data-testid="approval-paused-state" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(item.currentState, null, 2)}</pre>
      </details>
      <div>
        <Button variant="ghost" aria-expanded={editing} aria-controls={editing ? 'approval-edit-inputs' : undefined} disabled={isSubmitting || stale}
          onClick={() => { setEditing(!editing); setStateUpdateText(''); }}>{t('approvalReview.adjust')}</Button>
        {editing && <div id="approval-edit-inputs" className="approval-input-editor">
          <p id="approval-edit-help">{t('reviewDesign.editRule')}</p>
          <label htmlFor="approval-state-update">{t('reviewDesign.editedInputs')}</label>
          <textarea id="approval-state-update" rows={5} value={stateUpdateText} disabled={isSubmitting}
            onChange={e => setStateUpdateText(e.target.value)} aria-invalid={Boolean(parseError)}
            aria-describedby={parseError ? 'approval-edit-help approval-edit-error' : 'approval-edit-help'} />
          {parseError && <p id="approval-edit-error" role="alert" className="inspection-error">{parseError}</p>}
        </div>}
      </div>
    </div>
  </Modal>;
}
