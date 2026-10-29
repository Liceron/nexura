// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useId, useRef } from 'react';
import { Card } from '../../components/Card';
import { Button } from '../../components/Button';
import { RunStatus } from './RunStatus';
import { CancelRunButton } from './CancelRunButton';
import { WorkflowTabs } from '../../components/WorkflowTabs';
import { useApi } from '../../api/context';
import type { PendingApprovalItem } from '../approvals/types';
import { Spinner } from '../../components/Spinner';
import { useRunStream } from './useRunStream';
import { RunTimeline } from './RunTimeline';
import { StateReplayer } from './StateReplayer';
import { RunDiagnostics } from './RunDiagnostics';
import { JsonText } from '../approvals/ExactAction';
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
  headingLevel?: 1 | 2;
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
  headingLevel = 2,
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
    decideApproval,
    provideInput,
    refetchSnapshot,
  } = useRunStream(runId);

  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState<'timeline' | 'replay' | 'snapshot' | 'diagnostics' | 'history' | 'subruns' | 'traces'>('timeline');
  const [currentStepIndex, setCurrentStepIndex] = useState<number>(0);
  const [reviewItem, setReviewItem] = useState<PendingApprovalItem | null>(null);
  const { connection } = useApi();
  const panelId = useId();
  const titleRef = useRef<HTMLHeadingElement>(null);
  const Heading = headingLevel === 1 ? 'h1' : 'h2';

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
    <section className="run-console">
      <header className="run-console-header">
        <div>
          <Heading ref={titleRef} tabIndex={-1}>{t('ui.runConsole')}</Heading>
          <div className="inspection-meta">
            <code aria-label={t('ui.runIdentifierBadge')}>{run.run_id}</code>
            <span aria-label={t('ui.runStatusBadge')}><RunStatus status={run.status} /></span>
            <span>{t('ui.steps')} {run.step_count}</span>
            <span>{t('ui.events')} {events.length}</span>
          </div>
        </div>
        <div className="inspection-actions">
          {getStreamBadge()}
          {!forkedFrom && <SaveAsTestButton runId={run.run_id} workflowId={run.workflow_id} status={run.status} />}
          <CancelRunButton key={run.run_id} runId={run.run_id} status={run.status} onCancelled={refetchSnapshot} />
          <Button variant="ghost" size="sm" onClick={refetchSnapshot} title={t('ui.fetchLatestDatabaseSnapshot')}>{t('ui.refresh')}</Button>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label={t('ui.closeRunConsole')}><Icon name="close" /></Button>
        </div>
      </header>
      {error && <p role="alert" className="inspection-error">{error}</p>}
      {run.status === 'pending_approval' && run.pending_approval && <div role="region" aria-label={t('ui.pendingApprovalBanner')} className="run-review-banner">
        <div><strong>{t('ui.workflowGateApprovalRequired')}</strong>
          <p className="inspection-meta">{t('ui.pausedAtNode')} {run.pending_approval.node_id}</p>
        </div>
        <Button variant="primary" onClick={() => setReviewItem({
          approvalId: run.pending_approval!.approval_id, runId: run.run_id, workflowId: run.workflow_id || '',
          nodeId: run.pending_approval!.node_id, createdAt: run.created_at, currentState: run.current_state,
          spec: run.spec, runRecord: run,
        })}>{t('approvalReview.review')}</Button>
      </div>}
      {/* Pending Input Banner */}
      <PendingInputBanner
        run={run}
        onSubmit={async (interruptId, value) => {
          await provideInput(interruptId, value);
        }}
      />

      <ApprovalDecisionModal open={reviewItem !== null} item={reviewItem} workspace={connection.workspace}
        returnFocusTo={() => titleRef.current}
        stale={run.status !== 'pending_approval'}
        onClose={() => setReviewItem(null)}
        onDecide={async (_runId, approvalId, decision, stateUpdate) => {
          const updated = await decideApproval(decision, stateUpdate, approvalId);
          return updated ? { success: true, run: updated } : { success: false, error: t('approvalReview.submitError') };
        }} />

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

      <WorkflowTabs active={activeTab} onChange={id => setActiveTab(id as typeof activeTab)} label={t('reviewDesign.runViews')} panelId={panelId}
        tabs={[
          { id: 'diagnostics', label: t('ui.diagnostics') }, { id: 'traces', label: t('traces.tab') },
          { id: 'timeline', label: t('ui.eventTimeline') + events.length + ')' },
          { id: 'replay', label: t('ui.stateReplay') + replaySteps.length + ' ' + t('ui.steps2') },
          { id: 'snapshot', label: t('ui.stateSnapshot') }, { id: 'history', label: t('ui.history') },
          ...(showSubRuns ? [{ id: 'subruns', label: t('durable.subruns.tab') }] : []),
        ]} />
      <div role="tabpanel" id={panelId} aria-label={t('reviewDesign.runEvidence')} tabIndex={0} style={{ minWidth: 0 }}>
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
        <><p className="inspection-meta">{t('reviewDesign.replayMode')}</p><StateReplayer
          steps={replaySteps}
          currentStepIndex={currentStepIndex}
          onSelectStepIndex={handleSelectStep}
        /></>
      )}

      {activeTab === 'snapshot' && <JsonText label={t('ui.currentWorkflowState')} text={JSON.stringify(run.current_state, null, 2)} />}
      </div>
    </section>
  );
}
