// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState } from 'react';
import { Card } from '../../components/Card';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { Spinner } from '../../components/Spinner';
import { useRunStream } from './useRunStream';
import { RunTimeline } from './RunTimeline';
import { StateReplayer } from './StateReplayer';
import { RunDiagnostics } from './RunDiagnostics';
import { ApprovalDecisionModal } from '../approvals/ApprovalDecisionModal';
import { PendingInputBanner } from './PendingInputBanner';
import { RunCancelReasonNotice } from './RunCancelReasonNotice';
import { RunHistory } from './RunHistory';
import { SubRunsPanel } from './SubRunsPanel';
import { SaveAsTestButton } from './SaveAsTestButton';
import { describeForkedFrom, showSubRunsTab } from './durable';
import { RunTraceView } from '../traces/RunTraceView';
import { useI18n } from '../../i18n';
import type { NodeExecutionStatus } from './types';

interface RunVisualizerProps {
  runId: string;
  onClose: () => void;
  onSelectRun?: (runId: string) => void;
  onSelectNode?: (nodeId: string) => void;
  onUpdateExecutionState?: (data: {
    nodeStatuses: Map<string, NodeExecutionStatus>;
    activeNodeId: string | null;
  }) => void;
}

export function RunVisualizer({
  runId,
  onClose,
  onSelectRun,
  onSelectNode,
  onUpdateExecutionState,
}: RunVisualizerProps) {
  const {
    run,
    events,
    streamStatus,
    loading,
    error,
    nodeStatuses,
    replaySteps,
    cancelRun,
    decideApproval,
    provideInput,
    refetchSnapshot,
  } = useRunStream(runId);

  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState<'timeline' | 'replay' | 'snapshot' | 'diagnostics' | 'history' | 'subruns' | 'traces'>('timeline');
  const [currentStepIndex, setCurrentStepIndex] = useState<number>(0);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isDeciding, setIsDeciding] = useState(false);
  const [approvalModalOpen, setApprovalModalOpen] = useState(false);

  const forkedFrom = describeForkedFrom(run?.forked_from);
  const showSubRuns = showSubRunsTab(run);

  // Sync node statuses up to canvas whenever replay or stream updates
  const handleSelectStep = (nextIndex: number) => {
    setCurrentStepIndex(nextIndex);
    if (onUpdateExecutionState && replaySteps[nextIndex]) {
      const step = replaySteps[nextIndex];
      onUpdateExecutionState({
        nodeStatuses,
        activeNodeId: step.nodeId || null,
      });
    }
  };

  const handleCancel = async () => {
    if (!window.confirm(t('runConsole.confirmCancel'))) {
      return;
    }
    setIsCancelling(true);
    try {
      await cancelRun('Cancelled from Studio Run Console');
    } finally {
      setIsCancelling(false);
    }
  };

  const handleApprove = async () => {
    setIsDeciding(true);
    try {
      await decideApproval('approve');
    } finally {
      setIsDeciding(false);
    }
  };

  const handleReject = async () => {
    setIsDeciding(true);
    try {
      await decideApproval('reject');
    } finally {
      setIsDeciding(false);
    }
  };

  if (loading && !run) {
    return (
      <Card elevated style={{ padding: 24, textAlign: 'center' }}>
        <Spinner size={24} />
        <div style={{ marginTop: 8, color: 'var(--text-secondary)', fontSize: 13 }}>
          {t('ui.connectingToRunExecutionStream')}
        </div>
      </Card>
    );
  }

  if (error && !run) {
    return (
      <Card elevated style={{ borderColor: 'var(--status-danger)', padding: 16 }}>
        <div style={{ color: 'var(--status-danger)', fontWeight: 600, fontSize: 14 }}>
          {t('ui.errorLoadingRun')}
        </div>
        <div style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 4 }}>{error}</div>
        <div style={{ marginTop: 12 }}>
          <Button variant="secondary" size="sm" onClick={onClose}>
            {t('ui.closeConsole')}
          </Button>
        </div>
      </Card>
    );
  }

  if (!run) return null;

  const isActive = run.status === 'running' ||
    run.status === 'pending_approval' ||
    run.status === 'waiting_for_input';

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'running':
        return <Badge variant="info"><Icon name="workflows" /> {t('ui.running')}</Badge>;
      case 'completed':
        return <Badge variant="success"><Icon name="check" /> {t('ui.completed')}</Badge>;
      case 'failed':
        return <Badge variant="danger"><Icon name="close" /> {t('ui.failed')}</Badge>;
      case 'cancelled':
        return <Badge variant="neutral">{t('ui.cancelled')}</Badge>;
      case 'pending_approval':
        return <Badge variant="warning"><Icon name="pause" /> {t('ui.approvalPending')}</Badge>;
      case 'waiting_for_input':
        return <Badge variant="warning"><Icon name="pause" /> {t('ui.inputRequired')}</Badge>;
      case 'interrupted':
        return <Badge variant="warning"><Icon name="warning" /> {t('ui.interrupted')}</Badge>;
      default:
        return <Badge variant="neutral">{status}</Badge>;
    }
  };

  const getStreamBadge = () => {
    switch (streamStatus) {
      case 'connected':
        return <span style={{ color: 'var(--status-success)', fontSize: 12 }}><Icon name="circle" /> {t('ui.liveSseActive')}</span>;
      case 'connecting':
        return <span style={{ color: 'var(--brand-primary)', fontSize: 12 }}><Icon name="circle" /> {t('ui.connecting')}</span>;
      case 'reconnecting':
        return <span style={{ color: 'var(--status-warning)', fontSize: 12 }}><Icon name="circle" /> {t('ui.reconnectingStream')}</span>;
      case 'closed':
        return <span style={{ color: 'var(--text-muted)', fontSize: 12 }}><Icon name="circle" /> {t('ui.streamClosed')}</span>;
      case 'error':
        return <span style={{ color: 'var(--status-danger)', fontSize: 12 }}><Icon name="circle" /> {t('ui.streamInterrupted')}</span>;
    }
  };

  return (
    <Card elevated style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 14 }}>
      {/* Header Bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: 12,
          borderBottom: '1px solid var(--border-subtle)',
          paddingBottom: 12,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontWeight: 700, fontSize: 15 }}>{t('ui.runConsole')}</span>
          <Badge variant="neutral" aria-label={t('ui.runIdentifierBadge')}>
            {run.run_id}
          </Badge>
          <span aria-label={t('ui.runStatusBadge')}>{getStatusBadge(run.status)}</span>
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
            {t('ui.steps') + " "}<strong>{run.step_count}</strong>
          </span>
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
            {t('ui.events') + " "}<strong>{events.length}</strong>
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {getStreamBadge()}
          {/* A fork starts from a checkpoint, so its input does not replay it; the server refuses. */}
          {!forkedFrom && (
            <SaveAsTestButton runId={run.run_id} workflowId={run.workflow_id} status={run.status} />
          )}
          {isActive && (
            <Button
              variant="danger"
              size="sm"
              loading={isCancelling}
              onClick={handleCancel}
              title={t('ui.haltActiveWorkflowRunOnServer')}
            >
              {t('ui.cancelRun')}
            </Button>
          )}
          <Button variant="secondary" size="sm" onClick={refetchSnapshot} title={t('ui.fetchLatestDatabaseSnapshot')}>
            {t('ui.refresh')}
          </Button>
          <Button variant="secondary" size="sm" onClick={onClose} aria-label={t('ui.closeRunConsole')}>
            <Icon name="close" />
          </Button>
        </div>
      </div>

      {/* Pending Approval Gate Banner */}
      {run.status === 'pending_approval' && run.pending_approval && (
        <div
          role="region"
          aria-label={t('ui.pendingApprovalBanner')}
          style={{
            padding: 12,
            backgroundColor: 'var(--status-warning-bg)',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--status-warning)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: 10,
          }}
        >
          <div>
            <div style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
              <Icon name="pause" /> {t('ui.workflowGateApprovalRequired')}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>
              {t('ui.pausedAtNode') + " "}<strong>{run.pending_approval.node_id}</strong> (ID:{' '}
              <code>{run.pending_approval.approval_id}</code>)
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setApprovalModalOpen(true)}
            >
              {t('ui.reviewDetails')}
            </Button>
            <Button
              variant="danger"
              size="sm"
              loading={isDeciding}
              onClick={handleReject}
            >
              {t('ui.rejectExecution')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              loading={isDeciding}
              onClick={handleApprove}
            >
              {t('ui.approveResume')}
            </Button>
          </div>
        </div>
      )}

      {/* Pending Input Banner */}
      <PendingInputBanner
        run={run}
        onSubmit={async (interruptId, value) => {
          await provideInput(interruptId, value);
        }}
      />

      {/* Full Approval Decision Modal */}
      {run.status === 'pending_approval' && run.pending_approval && (
        <ApprovalDecisionModal
          open={approvalModalOpen}
          item={{
            approvalId: run.pending_approval.approval_id,
            runId: run.run_id,
            workflowId: run.workflow_id || '',
            nodeId: run.pending_approval.node_id,
            createdAt: run.created_at,
            currentState: run.current_state,
            spec: run.spec,
            runRecord: run,
          }}
          onClose={() => setApprovalModalOpen(false)}
          onDecide={async (_rId, _aId, decision, stateUpdate) => {
            const updated = await decideApproval(decision, stateUpdate);
            if (updated) {
              return { success: true, run: updated };
            }
            return { success: false, error: 'Failed to submit decision.' };
          }}
        />
      )}

      {/* Fork lineage */}
      {forkedFrom && (
        <div
          data-testid="forked-from-banner"
          style={{ padding: 8, fontSize: 12, border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)' }}
        >
          {t('durable.fork.parent', 'Forked from run')}{' '}
          <a
            href={`#/runs/${encodeURIComponent(forkedFrom.parentRunId)}`}
            data-testid="forked-from-link"
            onClick={(e) => {
              e.preventDefault();
              onSelectRun?.(forkedFrom.parentRunId);
            }}
          >
            {forkedFrom.parentRunId}
          </a>
          {forkedFrom.checkpointIndex !== null && <> {t('ui.atCheckpoint') + " "}{forkedFrom.checkpointIndex}</>}
          {forkedFrom.stateChanged ? " " + t('ui.stateEdited') : ''}
        </div>
      )}

      {/* Terminal Failure Banner */}
      {run.status === 'failed' && run.error && (
        <div
          role="alert"
          style={{
            padding: 10,
            backgroundColor: 'var(--status-danger-bg)',
            color: 'var(--status-danger)',
            borderRadius: 'var(--radius-sm)',
            fontSize: 12,
          }}
        >
          <strong>{t('ui.executionFailed')}</strong> {run.error}
        </div>
      )}

      {/* Cancelled because access ended (member removed, key or publication revoked) */}
      <RunCancelReasonNotice run={run} />

      {/* Tab Navigation */}
      <div role="tablist" style={{ display: 'flex', gap: 8, borderBottom: '1px solid var(--border-subtle)', paddingBottom: 6 }}>
        <Button
          variant={activeTab === 'diagnostics' ? 'primary' : 'secondary'}
          size="sm"
          role="tab"
          aria-selected={activeTab === 'diagnostics'}
          onClick={() => setActiveTab('diagnostics')}
        >
          {t('ui.diagnostics')}
        </Button>
        <Button
          variant={activeTab === 'traces' ? 'primary' : 'secondary'}
          size="sm"
          role="tab"
          aria-selected={activeTab === 'traces'}
          onClick={() => setActiveTab('traces')}
        >
          {t('traces.tab')}
        </Button>
        <Button
          variant={activeTab === 'timeline' ? 'primary' : 'secondary'}
          size="sm"
          role="tab"
          aria-selected={activeTab === 'timeline'}
          onClick={() => setActiveTab('timeline')}
        >
          {t('ui.eventTimeline')}{events.length})
        </Button>
        <Button
          variant={activeTab === 'replay' ? 'primary' : 'secondary'}
          size="sm"
          role="tab"
          aria-selected={activeTab === 'replay'}
          onClick={() => setActiveTab('replay')}
        >
          {t('ui.stateReplay')}{replaySteps.length} {t('ui.steps2')}
        </Button>
        <Button
          variant={activeTab === 'snapshot' ? 'primary' : 'secondary'}
          size="sm"
          role="tab"
          aria-selected={activeTab === 'snapshot'}
          onClick={() => setActiveTab('snapshot')}
        >
          {t('ui.stateSnapshot')}
        </Button>
        <Button
          variant={activeTab === 'history' ? 'primary' : 'secondary'}
          size="sm"
          role="tab"
          aria-selected={activeTab === 'history'}
          onClick={() => setActiveTab('history')}
        >
          {t('ui.history')}
        </Button>
        {showSubRuns && (
          <Button
            variant={activeTab === 'subruns' ? 'primary' : 'secondary'}
            size="sm"
            role="tab"
            aria-selected={activeTab === 'subruns'}
            onClick={() => setActiveTab('subruns')}
          >
            {t('durable.subruns.tab', 'Sub-runs')}
          </Button>
        )}
      </div>

      {/* Tab Content */}
      {activeTab === 'traces' && <RunTraceView runId={runId} revision={events.length} />}
      {activeTab === 'diagnostics' && <RunDiagnostics runId={runId} revision={events.length} />}
      {activeTab === 'timeline' && (
        <RunTimeline events={events} onSelectNode={onSelectNode} />
      )}

      {activeTab === 'history' && (
        <RunHistory
          runId={runId}
          revision={events.length}
          onForked={(forked) => onSelectRun?.(forked.run_id)}
        />
      )}

      {activeTab === 'subruns' && showSubRuns && <SubRunsPanel runId={runId} revision={events.length} />}

      {activeTab === 'replay' && (
        <StateReplayer
          steps={replaySteps}
          currentStepIndex={currentStepIndex}
          onSelectStepIndex={handleSelectStep}
        />
      )}

      {activeTab === 'snapshot' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
            {t('ui.currentWorkflowState')}
          </div>
          <pre
            style={{
              margin: 0,
              padding: 12,
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'var(--bg-canvas)',
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              overflowX: 'auto',
              maxHeight: 340,
            }}
          >
            {JSON.stringify(run.current_state, null, 2)}
          </pre>
        </div>
      )}
    </Card>
  );
}
