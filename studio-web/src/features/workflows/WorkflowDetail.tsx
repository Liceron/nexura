// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { DeleteWorkflowDialog } from './DeleteWorkflowDialog';
import { Icon } from '../../components/Icon';
import { useState, useEffect, useCallback } from 'react';
import { useApi } from '../../api/context';
import { Card } from '../../components/Card';
import { Button } from '../../components/Button';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { WorkflowTabs } from '../../components/WorkflowTabs';
import { useCompactLayout } from '../../app/useCompactLayout';
import { Spinner } from '../../components/Spinner';
import { Modal } from '../../components/Modal';
import { Input } from '../../components/Input';
import { useI18n } from '../../i18n';
import { WorkflowCanvas } from '../builder/WorkflowCanvas';
import { RunStartModal } from '../runs/RunStartModal';
import { RunVisualizer } from '../runs/RunVisualizer';
import type { NodeExecutionStatus } from '../runs/types';
import { hasWorkflowDraft } from './persistence';
import { WorkflowTestsPanel } from '../evaluations/WorkflowTestsPanel';
import { WorkflowRunsPanel } from '../runs/WorkflowRunsPanel';
import type { WorkflowRecord, WorkflowSpec } from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';
import { triggersHref } from '../../app/routes';
import {
  BUILTIN_WORKFLOW_PANELS,
  mergeAfter,
  useStudioExtensions,
  type BuiltinWorkflowPanel,
  type ExtensionId,
} from '../../app/extensions';

interface WorkflowDetailProps {
  workflowId: string;
  /** Run to show in the Run Console, from the URL (`#/workflows/{id}/runs/{runId}`). */
  runId?: string | null;
  /** Called when the Run Console switches run, so the URL (and a reload) keeps it. */
  onRunChange?: (runId: string) => void;
  onBack: () => void;
  onOpenWorkflow: (id: string) => void;
}

export function WorkflowDetail({ workflowId, runId, onRunChange, onBack, onOpenWorkflow }: WorkflowDetailProps) {
  const { manager, connection } = useApi();
  const { t, locale } = useI18n();
  const compact = useCompactLayout();
  const [actionHost, setActionHost] = useState<HTMLDivElement | null>(null);
  const [feedbackHost, setFeedbackHost] = useState<HTMLDivElement | null>(null);
  const [editorDirty, setEditorDirty] = useState(false);
  const [editorLoad, setEditorLoad] = useState(0);

  const [workflow, setWorkflow] = useState<WorkflowRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [accessDenied, setAccessDenied] = useState(false);

  // Copy modal
  const [copyModalOpen, setCopyModalOpen] = useState(false);
  const [copyName, setCopyName] = useState('');
  const [isCopying, setIsCopying] = useState(false);

  // Delete modal
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);


  // Run modal & execution visualizer states
  const [runStartModalOpen, setRunStartModalOpen] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(runId ?? null);
  const [runNodeStatuses, setRunNodeStatuses] = useState<Map<string, NodeExecutionStatus>>(new Map());
  const [activeReplayNodeId, setActiveReplayNodeId] = useState<string | null>(null);

  // Built-in tabs plus panels registered by Studio extensions (the closed edition adds Publish).
  const [activeTab, setActiveTab] = useState<BuiltinWorkflowPanel | ExtensionId>(
    runId ? 'run' : compact ? 'nodes' : 'canvas',
  );
  const extensionPanels = useStudioExtensions().panels('workflow-detail');
  const activePanel = extensionPanels.find((panel) => panel.id === activeTab);

  // A run id arriving through the URL (reload, back/forward, a link) opens the Run Console.
  useEffect(() => {
    if (runId) {
      setActiveRunId(runId);
      setActiveTab('run');
    }
  }, [runId]);

  const selectRun = (id: string) => {
    setActiveRunId(id);
    setActiveTab('run');
    onRunChange?.(id);
  };

  const fetchWorkflow = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNotFound(false);
    setAccessDenied(false);
    try {
      const data = await manager.client.workflows.get(workflowId);
      setWorkflow(data);
      setEditorLoad(value => value + 1);
      setCopyName(t('workflows.copyName').replace('{name}', data.name));
    } catch (err: unknown) {
      const raw = err instanceof Error ? err.message : String(err); // keeps "[404] CODE"
      if (raw.includes('404') || raw.includes('WORKFLOW_NOT_FOUND')) {
        setNotFound(true);
      } else if (raw.includes('403') || raw.includes('CROSS_WORKSPACE_ACCESS_DENIED')) {
        setAccessDenied(true);
      } else {
        setError(formatApiError(err, t));
      }
    } finally {
      setLoading(false);
    }
  }, [manager, workflowId]);

  useEffect(() => {
    fetchWorkflow();
  }, [fetchWorkflow]);

  const handleSaveAsCopy = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!workflow || !workflow.spec) return;
    setIsCopying(true);
    try {
      const specCopy: WorkflowSpec = {
        ...(workflow.spec as WorkflowSpec),
        name: copyName.trim(),
      };
      const created = await manager.client.workflows.create(specCopy, { name: copyName.trim() });
      setCopyModalOpen(false);
      onOpenWorkflow(created.workflow_id);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setIsCopying(false);
    }
  };

  const handleDelete = async () => {
    setDeleteError(null);
    setIsDeleting(true);
    try {
      await manager.client.workflows.delete(workflowId);
      setDeleteModalOpen(false);
      onBack();
    } catch (err: unknown) {
      setDeleteError(formatApiError(err, t));
    } finally {
      setIsDeleting(false);
    }
  };

  const handleDownloadExport = async () => {
    try {
      const buffer = await manager.client.workflows.downloadExport(workflowId);
      const blob = new Blob([buffer], { type: 'application/zip' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `workflow_${workflowId}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 60, gap: 10 }}>
        <Spinner size={24} />
        <span style={{ color: 'var(--text-secondary)' }}>{t('common.loading')}</span>
      </div>
    );
  }

  if (notFound) {
    return (
      <Card elevated style={{ maxWidth: 600, margin: '40px auto', textAlign: 'center', padding: 32 }}>
        <div style={{ fontSize: 32, marginBottom: 12 }}><Icon name="search" /></div>
        <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>{t('workflows.notFoundTitle')}</h2>
        <p style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 20 }}>
          {t('workflows.notFoundMessage')}
        </p>
        <Button variant="primary" onClick={onBack}>
          {t('workflows.backToWorkflows')}
        </Button>
      </Card>
    );
  }

  if (accessDenied) {
    return (
      <Card
        elevated
        role="alert"
        aria-label={t('ui.workflowAccessDenied')}
        style={{ maxWidth: 600, margin: '40px auto', textAlign: 'center', padding: 32 }}
      >
        <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>{t('workflows.accessDeniedTitle')}</h2>
        <p style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 20 }}>
          {t('workflows.accessDeniedMessage')}
        </p>
        <Button variant="primary" onClick={onBack}>
          {t('workflows.backToWorkflows')}
        </Button>
      </Card>
    );
  }

  if (error || !workflow) {
    return (
      <Card elevated style={{ maxWidth: 600, margin: '40px auto', borderColor: 'var(--status-danger)' }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, color: 'var(--status-danger)', marginBottom: 8 }}>
          {t('common.error')}
        </h2>
        <p style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 16 }}>{error}</p>
        <div style={{ display: 'flex', gap: 10 }}>
          <Button variant="secondary" onClick={onBack}>
            {t('workflows.backToWorkflows')}
          </Button>
          <Button variant="primary" onClick={fetchWorkflow}>
            {t('connection.retry')}
          </Button>
        </div>
      </Card>
    );
  }

  const spec = (workflow.spec || {}) as WorkflowSpec;
  const nodes = spec.nodes || {};
  const nodeEntries = Object.entries(nodes);

  return (
    <PageLayout width="full" className="workflow-detail">
      <PageHeader title={workflow.name}
        breadcrumbs={<><a href="#/workflows" onClick={(event) => { event.preventDefault(); onBack(); }}>{t('workflows.title')}</a> / {workflow.name}</>}
        metadata={<>
          <span aria-label={t('ui.authoritativeRevisionBadge')}>{t('workflows.revision')}: {workflow.revision}</span>
          {' · '}{t('workflows.lastModified')}: {workflow.updated_at ? new Date(workflow.updated_at * 1000).toLocaleString(locale) : t('common.notAvailable')}
          <details className="workflow-details"><summary>{t('workflowDesign.details')}</summary>
            <div><code>{workflow.workflow_id}</code></div>
            {workflow.spec_hash && <div>{t('workflows.specHash')}: <code>{workflow.spec_hash}</code></div>}
            <div>{t('workflows.entryNode')}: {spec.entry_node} · {t('workflowDesign.steps')}: {nodeEntries.length}</div>
          </details>
        </>}
        actions={<div className="workflow-actions" ref={setActionHost} />}
      />
      <div ref={setFeedbackHost} />
      <WorkflowTabs label={t('ui.workflowViews')} active={activeTab} panelId="workflow-active-panel"
        onChange={(id) => setActiveTab(id as BuiltinWorkflowPanel | ExtensionId)}
        tabs={[
          ...mergeAfter(BUILTIN_WORKFLOW_PANELS.filter((id) => id !== 'run'), extensionPanels).map((entry) => {
            const labels: Record<string, string> = { canvas: t('workflowTabs.graph'), nodes: `${t('workflows.nodes')} (${nodeEntries.length})`, json: t('workflowTabs.spec'), tests: t('workflowTests.tab'), runs: t('runs.tab') };
            return typeof entry === 'string' ? { id: entry, label: labels[entry] } : { id: entry.id, label: t(entry.labelKey), testId: entry.testId };
          }),
          ...(activeRunId ? [{ id: 'run', label: `${t('ui.runConsole2')}${activeRunId.slice(0, 8)})`, testId: 'tab-run-console' }] : []),
        ]}
      />
      <div id="workflow-active-panel" role="tabpanel" aria-label={activePanel ? t(activePanel.labelKey) : ({ canvas: t('workflowTabs.graph'), nodes: t('workflows.nodes'), json: t('workflowTabs.spec'), tests: t('workflowTests.tab'), runs: t('runs.tab'), run: t('ui.runConsole2') } as Record<string, string>)[activeTab]} tabIndex={0}>
      {/* Keep the editor mounted across tabs: drafts and undo history have one owner. */}
      <div hidden={activeTab !== 'canvas' && activeTab !== 'nodes'} className="workflow-editor">
          <WorkflowCanvas
            key={`${workflow.workflow_id}-${editorLoad}`}
            initialSpec={spec}
            actionHost={actionHost}
            feedbackHost={feedbackHost}
            active={activeTab === 'canvas' || activeTab === 'nodes'}
            primaryAllowed={activeTab !== 'tests' && activeTab !== 'run' && !activePanel}
            viewMode={activeTab === 'nodes' ? 'nodes' : 'graph'}
            onDirtyChange={setEditorDirty}
            moreActions={<>
              <Button variant="ghost" onClick={() => setCopyModalOpen(true)}>{t('workflows.saveAsCopy')}</Button>
              <Button variant="ghost" onClick={handleDownloadExport}>{t('workflows.downloadExport')}</Button>
              <Button variant="ghost" data-testid="workflow-open-triggers" onClick={() => { window.location.hash = triggersHref(workflow.workflow_id); }}>{t('workflows.openTriggers')}</Button>
              <Button variant="ghost" className="workflow-danger-action" onClick={() => { setDeleteError(null); setDeleteModalOpen(true); }}>{t('workflows.deleteWorkflow')}</Button>
            </>}
            onImportedPython={(record) => onOpenWorkflow(record.workflow_id)}
            workflowId={workflow.workflow_id}
            workflowName={workflow.name}
            revision={workflow.revision}
            runNodeStatuses={runNodeStatuses}
            activeReplayNodeId={activeReplayNodeId}
            onSaveSpec={async (newSpec, expectedRev) => {
              const updated = await manager.client.workflows.update(workflow.workflow_id, newSpec, expectedRev);
              setWorkflow(updated);
            }}
            onReloadWorkflow={fetchWorkflow}
            onSaveAsCopy={async (copyName, specCopy) => {
              const copySpec: WorkflowSpec = {
                ...specCopy,
                name: copyName.trim(),
              };
              const created = await manager.client.workflows.create(copySpec, { name: copyName.trim() });
              onOpenWorkflow(created.workflow_id);
            }}
            onRunWorkflow={() => setRunStartModalOpen(true)}
          />
      </div>

      {/* Tab 2: Raw JSON */}
      {activeTab === 'json' && (
        <Card>
          <pre
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              backgroundColor: 'var(--bg-canvas)',
              padding: 16,
              borderRadius: 'var(--radius-sm)',
              overflowX: 'auto',
              margin: 0,
            }}
          >
            {JSON.stringify(spec, null, 2)}
          </pre>
        </Card>
      )}

      {/* Panels registered by Studio extensions */}
      {activePanel && <activePanel.component workflowId={workflowId} workflowName={workflow.name} />}

      {/* Tab 5: Workflow tests (P22) */}
      {activeTab === 'tests' && (
        <WorkflowTestsPanel workflowId={workflow.workflow_id} revision={workflow.revision} />
      )}

      {/* Tab 6: Runs of this workflow (P25) */}
      {activeTab === 'runs' && <WorkflowRunsPanel workflowId={workflow.workflow_id} />}

      {/* Tab 3: Run Console */}
      {activeTab === 'run' && (
        activeRunId ? (
          <RunVisualizer
            runId={activeRunId}
            onClose={() => setActiveTab('canvas')}
            onSelectRun={selectRun}
            onSelectNode={() => setActiveTab('canvas')}
            onUpdateExecutionState={({ nodeStatuses, activeNodeId }) => {
              setRunNodeStatuses(nodeStatuses);
              setActiveReplayNodeId(activeNodeId);
            }}
          />
        ) : (
          <Card>
            <p style={{ color: 'var(--text-secondary)' }}>{t('ui.noActiveRunSelected')}</p>
          </Card>
        )
      )}

      </div>

      {/* Save as Copy Modal */}
      <Modal open={copyModalOpen} onClose={() => setCopyModalOpen(false)} title={t('workflows.saveAsCopy')}>
        <form onSubmit={handleSaveAsCopy} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <Input
            label={t('workflows.workflowName')}
            value={copyName}
            onChange={(e) => setCopyName(e.target.value)}
            required
          />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
            <Button type="button" variant="secondary" onClick={() => setCopyModalOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" variant="primary" loading={isCopying}>
              {t('workflows.saveAsCopy')}
            </Button>
          </div>
        </form>
      </Modal>

      {/* Delete Confirmation Modal */}
      <DeleteWorkflowDialog open={deleteModalOpen} onClose={() => setDeleteModalOpen(false)} name={workflow?.name || workflowId} busy={isDeleting} error={deleteError} onConfirm={handleDelete} />

      {/* Run Start Modal */}
      <RunStartModal
        open={runStartModalOpen}
        workflowId={workflow.workflow_id}
        workflowName={workflow.name}
        serverRevision={workflow.revision}
        currentSpec={spec}
        hasUnsavedDraft={editorDirty || hasWorkflowDraft(connection.workspace, connection.principal, workflow.workflow_id)}
        onClose={() => setRunStartModalOpen(false)}
        onLaunchRun={async (initialState, asyncExecution) => {
          const run = await manager.client.runs.start({
            workflow_id: workflow.workflow_id,
            initial_state: initialState,
            async_execution: asyncExecution,
          });
          selectRun(run.run_id);
        }}
      />
    </PageLayout>
  );
}
