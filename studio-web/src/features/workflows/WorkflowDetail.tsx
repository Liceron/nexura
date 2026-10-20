// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useEffect, useCallback } from 'react';
import { useApi } from '../../api/context';
import { Card } from '../../components/Card';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { Spinner } from '../../components/Spinner';
import { Modal } from '../../components/Modal';
import { Input } from '../../components/Input';
import { useI18n } from '../../i18n';
import { WorkflowCanvas } from '../builder/WorkflowCanvas';
import { ImportExportModal } from './ImportExportModal';
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

  // Import / Export modal
  const [importExportModalOpen, setImportExportModalOpen] = useState(false);

  // Run modal & execution visualizer states
  const [runStartModalOpen, setRunStartModalOpen] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(runId ?? null);
  const [runNodeStatuses, setRunNodeStatuses] = useState<Map<string, NodeExecutionStatus>>(new Map());
  const [activeReplayNodeId, setActiveReplayNodeId] = useState<string | null>(null);

  // Built-in tabs plus panels registered by Studio extensions (the closed edition adds Publish).
  const [activeTab, setActiveTab] = useState<BuiltinWorkflowPanel | ExtensionId>(
    runId ? 'run' : 'canvas',
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
    setIsDeleting(true);
    try {
      await manager.client.workflows.delete(workflowId);
      setDeleteModalOpen(false);
      onBack();
    } catch (err: unknown) {
      setError(formatApiError(err, t));
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* Top Header Card */}
      <Card elevated>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 16 }}>
          <div className="studio-mobile-content">
            <div className="studio-mobile-wrap" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <Button variant="secondary" size="sm" onClick={onBack}>
                <Icon name="left" /> {t('workflows.backToWorkflows')}
              </Button>
              <Badge variant="neutral">{workflow.workflow_id}</Badge>
              {/* Authoritative Revision Indicator */}
              <Badge variant="success" aria-label={t('ui.authoritativeRevisionBadge')}>
                {t('workflows.revision')}: {workflow.revision}
              </Badge>
              {workflow.spec_hash && (
                <Badge variant="info">
                  {t('workflows.specHash')}: {workflow.spec_hash.substring(0, 8)}
                </Badge>
              )}
            </div>

            <h1 style={{ fontSize: 22, fontWeight: 700, margin: '4px 0 6px 0' }}>{workflow.name}</h1>

            <div className="studio-mobile-wrap" style={{ display: 'flex', gap: 16, fontSize: 13, color: 'var(--text-secondary)' }}>
              <span>
                {t('workflows.entryNode')}: <strong>{spec.entry_node || t('common.notAvailable')}</strong>
              </span>
              <span>
                {t('workflows.nodes')}: <strong>{nodeEntries.length}</strong>
              </span>
              <span>
                {t('workflows.lastModified')}:{' '}
                <strong>
                  {workflow.updated_at
                    ? new Date(workflow.updated_at * 1000).toLocaleString(locale)
                    : t('common.notAvailable')}
                </strong>
              </span>
            </div>
          </div>

          <div className="studio-mobile-wrap" style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <Button variant="secondary" onClick={() => setCopyModalOpen(true)}>
              {t('workflows.saveAsCopy')}
            </Button>
            <Button variant="secondary" onClick={handleDownloadExport}>
              {t('workflows.downloadExport')}
            </Button>
            <Button variant="secondary" onClick={() => setImportExportModalOpen(true)}>
              {t('ui.importExportJson')}
            </Button>
            <Button
              variant="secondary"
              data-testid="workflow-open-triggers"
              onClick={() => {
                window.location.hash = triggersHref(workflow.workflow_id);
              }}
            >
              {t('workflows.openTriggers')}
            </Button>
            <Button variant="danger" onClick={() => setDeleteModalOpen(true)}>
              {t('workflows.deleteWorkflow')}
            </Button>
            <Button variant="primary" onClick={() => setRunStartModalOpen(true)}>
              {t('common.run')}
            </Button>
          </div>
        </div>
      </Card>

      {/* Tabs */}
      <div
        role="tablist"
        aria-label={t('ui.workflowViews')}
        style={{ display: 'flex', gap: 8, borderBottom: '1px solid var(--border-subtle)', paddingBottom: 8 }}
      >
        {mergeAfter(
          BUILTIN_WORKFLOW_PANELS.filter((id) => id !== 'run'),
          extensionPanels,
        ).map((entry) => {
          if (typeof entry !== 'string') {
            return (
              <Button
                key={entry.id}
                data-testid={entry.testId ?? `tab-${entry.id}`}
                variant={activeTab === entry.id ? 'primary' : 'secondary'}
                size="sm"
                role="tab"
                aria-selected={activeTab === entry.id}
                onClick={() => setActiveTab(entry.id)}
              >
                {t(entry.labelKey)}
              </Button>
            );
          }
          const label: Record<string, string> = {
            canvas: t('workflowTabs.graph'),
            nodes: `${t('workflows.nodes')} (${nodeEntries.length})`,
            json: t('workflowTabs.spec'),
            tests: t('workflowTests.tab'),
            runs: t('runs.tab'),
          };
          return (
            <Button
              key={entry}
              data-testid={`tab-${entry}`}
              variant={activeTab === entry ? 'primary' : 'secondary'}
              size="sm"
              role="tab"
              aria-selected={activeTab === entry}
              onClick={() => setActiveTab(entry)}
            >
              {label[entry]}
            </Button>
          );
        })}
        {activeRunId && (
          <Button
            data-testid="tab-run-console"
            variant={activeTab === 'run' ? 'primary' : 'secondary'}
            size="sm"
            role="tab"
            aria-selected={activeTab === 'run'}
            onClick={() => setActiveTab('run')}
          >
            {t('ui.runConsole2')}{activeRunId.slice(0, 8)})
          </Button>
        )}
      </div>

      {/* Tab 0: Visual Canvas */}
      {activeTab === 'canvas' && (
        <Card style={{ height: 680, padding: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          <WorkflowCanvas
            key={`${workflow.workflow_id}-${workflow.revision}`}
            initialSpec={spec}
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
        </Card>
      )}

      {/* Tab 1: Nodes Overview */}
      {activeTab === 'nodes' && (
        <div
          data-testid="accessible-nodes-list"
          role="region"
          aria-label={t('ui.accessibleNodesList')}
          style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
        >
          {nodeEntries.map(([nodeId, node]) => (
            <Card key={nodeId} data-testid={`accessible-node-${nodeId}`}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontWeight: 600, fontSize: 14 }}>{node.id}</span>
                  {node.id === spec.entry_node && <Badge variant="success">{t('ui.entryNode')}</Badge>}
                  {node.requires_approval && <Badge variant="warning">{t('ui.approvalGateRequired')}</Badge>}
                </div>
                <Badge variant="neutral">{t('ui.handler') + " "}{node.handler || node.module_id}</Badge>
              </div>

              <div style={{ fontSize: 12, color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: 4 }}>
                {node.default_transition && (
                  <div>
                    {t('ui.defaultTransition') + " "}<Icon name="right" /> <strong>{node.default_transition}</strong>
                  </div>
                )}
                {node.transitions && Object.keys(node.transitions).length > 0 && (
                  <div>
                    {t('ui.branchTransitions2')}{' '}
                    {Object.entries(node.transitions).map(([b, tgt]) => (
                      <span key={b} style={{ marginRight: 8 }}>
                        <code>{b}</code> <Icon name="right" /> <strong>{String(tgt)}</strong>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}

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
      <Modal open={deleteModalOpen} onClose={() => setDeleteModalOpen(false)} title={t('workflows.deleteWorkflow')}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <p style={{ color: 'var(--text-secondary)', fontSize: 14 }}>
            {t('workflows.confirmDelete')}
          </p>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
            <Button type="button" variant="secondary" onClick={() => setDeleteModalOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button type="button" variant="danger" loading={isDeleting} onClick={handleDelete}>
              {t('common.delete')}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Import / Export JSON Modal */}
      <ImportExportModal
        open={importExportModalOpen}
        initialMode="export"
        workflowId={workflow.workflow_id}
        workflowName={workflow.name}
        currentSpec={spec}
        onClose={() => setImportExportModalOpen(false)}
        onImportedPython={(record) => {
          setImportExportModalOpen(false);
          onOpenWorkflow(record.workflow_id);
        }}
        onApplyImportedSpec={async (newSpec) => {
          try {
            const updated = await manager.client.workflows.update(workflow.workflow_id, newSpec, workflow.revision);
            setWorkflow(updated);
            setImportExportModalOpen(false);
          } catch (err: unknown) {
            setError(formatApiError(err, t));
          }
        }}
        onCreateFromImport={async (name, newSpec) => {
          const created = await manager.client.workflows.create(newSpec, { name });
          setImportExportModalOpen(false);
          onOpenWorkflow(created.workflow_id);
        }}
      />

      {/* Run Start Modal */}
      <RunStartModal
        open={runStartModalOpen}
        workflowId={workflow.workflow_id}
        workflowName={workflow.name}
        serverRevision={workflow.revision}
        currentSpec={spec}
        hasUnsavedDraft={hasWorkflowDraft(connection.workspace, connection.principal, workflow.workflow_id)}
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
    </div>
  );
}
