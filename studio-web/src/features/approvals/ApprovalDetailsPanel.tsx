// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { ExactApprovalRequest } from './types';
import { ExactAction } from './ExactAction';
import { describeApprovalDetails } from '../runs/durable';
import { useDurableT } from '../runs/useDurableT';
import { describeAgentToolCalls, type AgentToolCallsView } from './agentToolCalls';

interface ApprovalDetailsPanelProps {
  /** `run.pending_approval` as the API returns it (its `details` field is optional). */
  pendingApproval: ExactApprovalRequest | null | undefined;
  previousApproval?: ExactApprovalRequest;
  /** Optional handler from the run's captured workflow spec (display metadata only). */
  workflowHandler?: string;
}

/** A saved agent's tool calls waiting for approval: agent, revision and each exact call. */
function AgentToolCallsDetails({ view }: { view: AgentToolCallsView }) {
  const t = useDurableT();
  return (
    <div data-testid="approval-agent-calls" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div style={{ fontSize: 13, fontWeight: 600 }}>
        {t('agents.approvalTitle', 'Tool calls this agent wants to run')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-secondary)', display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <span data-testid="approval-agent-name">
          {t('agents.approvalAgent', 'Agent')}: <strong>{view.agentName || view.agentId || '?'}</strong>
        </span>
        {view.agentRevision !== null && (
          <span data-testid="approval-agent-revision">
            {t('agents.approvalRevision', 'Revision')}: <strong>{view.agentRevision}</strong>
          </span>
        )}
        <span data-testid="approval-agent-call-count">
          {t('agents.approvalCalls', 'Calls')}: <strong>{view.calls.length}</strong>
        </span>
      </div>
      <ol style={{ margin: 0, paddingLeft: 22, display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 260, overflowY: 'auto' }}>
        {view.calls.map((call, i) => (
          <li key={i} data-testid="approval-agent-call" style={{ fontSize: 12 }}>
            <div>
              {t('agents.approvalTool', 'Tool')}: <code data-testid="approval-agent-call-tool">{call.tool}</code>
            </div>
            <pre
              data-testid="approval-agent-call-arguments"
              style={{
                margin: '2px 0 0',
                padding: 8,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                backgroundColor: 'var(--bg-canvas)',
                borderRadius: 'var(--radius-sm)',
              }}
            >
              {call.argumentsText}
            </pre>
          </li>
        ))}
      </ol>
      <div data-testid="approval-agent-note" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
        {t(
          'agents.approvalNote',
          'Approve to run exactly these calls; reject to stop the run without running them. If the agent is edited before you decide, the run fails instead of running old calls.'
        )}
      </div>
    </div>
  );
}

/**
 * Shows what an approval covers: a saved agent's exact tool calls, or what a mapped module
 * step will do once approved (count, first items, write cap).
 */
export function ApprovalDetailsPanel({ pendingApproval, previousApproval, workflowHandler }: ApprovalDetailsPanelProps) {
  return <>
    <ExactAction request={pendingApproval} previous={previousApproval} workflowHandler={workflowHandler} />
    <LegacyApprovalDetails pendingApproval={pendingApproval} />
  </>;
}

function LegacyApprovalDetails({ pendingApproval }: ApprovalDetailsPanelProps) {
  const t = useDurableT();
  const agentCalls = describeAgentToolCalls(pendingApproval);
  if (agentCalls) return <AgentToolCallsDetails view={agentCalls} />;
  const view = describeApprovalDetails(pendingApproval);
  if (!view) return null;
  const overCap = view.maxWrites !== null && view.itemCount > view.maxWrites;
  return (
    <div data-testid="approval-details" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div style={{ fontSize: 13, fontWeight: 600 }}>
        {t('durable.approval.detailsTitle', 'What this approval covers')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-secondary)', display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        {view.mapOver && (
          <span>
            {t('approvalReview.mapped', 'Mapped over')}: <code>{view.mapOver}</code>
          </span>
        )}
        <span data-testid="approval-item-count">{t('approvalReview.items', 'Items')}: <strong>{view.itemCount}</strong></span>
        {view.maxWrites !== null && (
          <span data-testid="approval-max-writes">
            {t('approvalReview.maxWrites', 'Max writes per approval')}: <strong>{view.maxWrites}</strong>
          </span>
        )}
      </div>
      {overCap && (
        <div role="alert" style={{ fontSize: 12, color: 'var(--status-danger)' }}>
          {t('durable.approval.overCap', 'This is more items than one approval may cover; the run will fail rather than write them.')}
        </div>
      )}
      <ol
        data-testid="approval-items"
        style={{ margin: 0, paddingLeft: 22, fontFamily: 'var(--font-mono)', fontSize: 12, maxHeight: 200, overflowY: 'auto' }}
      >
        {view.shownItems.map((item, i) => (
          <li key={i} data-testid="approval-item">{JSON.stringify(item)}</li>
        ))}
      </ol>
      {view.moreCount > 0 && (
        <div data-testid="approval-more" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
          {t('approvalReview.more', 'and {count} more').replace('{count}', String(view.moreCount))}
        </div>
      )}
      {view.itemCount > view.listedCount && (
        <div data-testid="approval-server-truncated" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
          {t('approvalReview.truncated', 'showing {listed} of {count} items (the server listed {listed})').replaceAll('{listed}', String(view.listedCount)).replace('{count}', String(view.itemCount))}
        </div>
      )}
      <div data-testid="approval-edit-note" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
        {t(
          'durable.approval.editNote',
          'If you edit the inputs before approving, the run pauses again and asks for a fresh approval of the edited items. An approval only covers the inputs you reviewed.'
        )}
      </div>
    </div>
  );
}
