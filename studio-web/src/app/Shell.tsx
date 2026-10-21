// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../components/Icon';
import { useState, useEffect, useCallback, Suspense } from 'react';
import { lazyWithRetry } from './lazyWithRetry';
import { Header } from './Header';
import { ErrorBoundary } from './ErrorBoundary';
import { Sidebar, NavTab } from './Sidebar';
import { useApi } from '../api/context';
import { Card } from '../components/Card';
import { Button } from '../components/Button';
import { Spinner } from '../components/Spinner';
import { useI18n } from '../i18n';
import { WorkflowList } from '../features/workflows/WorkflowList';
import { usePendingApprovals, APPROVALS_BADGE_POLL_MS } from '../features/approvals/usePendingApprovals';
import { parseHash, workflowRunHref } from './routes';
import { useStudioExtensions } from './extensions';
import { Modal } from '../components/Modal';
import { PageHeader } from '../components/PageHeader';
import { useCompactLayout } from './useCompactLayout';

const WorkflowDetail = lazyWithRetry(() => import('../features/workflows/WorkflowDetail').then(m => ({ default: m.WorkflowDetail })));
const ApprovalInbox = lazyWithRetry(() => import('../features/approvals/ApprovalInbox').then(m => ({ default: m.ApprovalInbox })));
const ChatPlayground = lazyWithRetry(() => import('../features/chat/ChatPlayground').then(m => ({ default: m.ChatPlayground })));
const KnowledgeView = lazyWithRetry(() => import('../features/knowledge/KnowledgeView').then(m => ({ default: m.KnowledgeView })));
const SkillsView = lazyWithRetry(() => import('../features/skills/SkillsView').then(m => ({ default: m.SkillsView })));
const AgentsView = lazyWithRetry(() => import('../features/agents/AgentsView').then(m => ({ default: m.AgentsView })));
const CredentialsView = lazyWithRetry(() => import('../features/credentials/CredentialsView').then(m => ({ default: m.CredentialsView })));
const IntegrationsView = lazyWithRetry(() => import('../features/integrations/IntegrationsView').then(m => ({ default: m.IntegrationsView })));
const EvaluationsView = lazyWithRetry(() => import('../features/evaluations/EvaluationsView').then(m => ({ default: m.EvaluationsView })));
const TriggersView = lazyWithRetry(() => import('../features/triggers/TriggersView').then(m => ({ default: m.TriggersView })));
const LocalOnboarding = lazyWithRetry(() => import('../features/onboarding/LocalOnboarding').then(m => ({ default: m.LocalOnboarding })));
const TracesView = lazyWithRetry(() => import('../features/traces/TracesView').then(m => ({ default: m.TracesView })));
const TemplatesView = lazyWithRetry(() => import('../features/templates/TemplatesView').then(m => ({ default: m.TemplatesView })));
const RunPage = lazyWithRetry(() => import('../features/runs/RunPage').then(m => ({ default: m.RunPage })));
const SystemView = lazyWithRetry(() => import('../features/system/SystemView').then(m => ({ default: m.SystemView })));

export function Shell() {
  const { connection, refreshConnection } = useApi();
  const { t } = useI18n();
  const extensions = useStudioExtensions();
  const compact = useCompactLayout();
  const [navigationOpen, setNavigationOpen] = useState(false);
  useEffect(() => { if (!compact) setNavigationOpen(false); }, [compact]);

  const [currentTab, setCurrentTab] = useState<NavTab>('workflows');
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
  const [selectedTraceRunId, setSelectedTraceRunId] = useState<string | null>(null);
  // P25: #/runs/{id} (standalone run page) and #/workflows/{id}/runs/{runId} (Run Console tab).
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedWorkflowRunId, setSelectedWorkflowRunId] = useState<string | null>(null);
  // B39: #/triggers/{workflowId} (entry point from the workflow page).
  const [selectedTriggersWorkflowId, setSelectedTriggersWorkflowId] = useState<string | null>(null);
  const { count: pendingApprovalsCount } = usePendingApprovals({
    enabled: connection.status === 'connected',
    pollIntervalMs: APPROVALS_BADGE_POLL_MS,
  });

  const parseRoute = useCallback(() => {
    setNavigationOpen(false);
    const route = parseHash(window.location.hash, extensions);
    setCurrentTab(route.tab);
    setSelectedWorkflowId(route.workflowId);
    setSelectedWorkflowRunId(route.workflowRunId);
    setSelectedRunId(route.runId);
    setSelectedTraceRunId(route.traceRunId);
    setSelectedTriggersWorkflowId(route.triggersWorkflowId);
  }, [extensions]);

  useEffect(() => {
    parseRoute();
    window.addEventListener('hashchange', parseRoute);
    return () => window.removeEventListener('hashchange', parseRoute);
  }, [parseRoute]);

  // A page registered by a Studio extension (for example the closed edition's Organisation page).
  const ExtensionPage = extensions.page(currentTab);

  const handleSelectTab = (tab: NavTab) => {
    setNavigationOpen(false);
    setCurrentTab(tab);
    setSelectedWorkflowId(null);
    setSelectedRunId(null);
    setSelectedTriggersWorkflowId(null);
    window.location.hash = `#/${tab}`;
  };

  const handleOpenWorkflow = (workflowId: string) => {
    setSelectedWorkflowId(workflowId);
    setSelectedRunId(null);
    window.location.hash = `#/workflows/${workflowId}`;
  };

  const handleBackToWorkflows = () => {
    setSelectedWorkflowId(null);
    window.location.hash = '#/workflows';
  };

  // The run shown in a workflow's Run Console goes into the URL, so a reload keeps it.
  const handleWorkflowRunChange = (workflowId: string, runId: string) => {
    const next = workflowRunHref(workflowId, runId);
    if (window.location.hash !== next) window.location.hash = next;
  };

  return (
    <div className="studio-shell">
      <Header onOpenNavigation={() => setNavigationOpen(true)} navigationOpen={navigationOpen} />

      <div className="studio-shell-workspace">
        {!compact && <Sidebar
          currentTab={currentTab}
          onSelectTab={handleSelectTab}
          pendingApprovalsCount={connection.status === 'connected' ? pendingApprovalsCount : 0}
        />}
        <Modal open={compact && navigationOpen} onClose={() => setNavigationOpen(false)}
          title={t('ui.studioNavigation')} variant="drawer">
          <Sidebar currentTab={currentTab} onSelectTab={handleSelectTab}
            pendingApprovalsCount={connection.status === 'connected' ? pendingApprovalsCount : 0} />
        </Modal>

        <main className="studio-main-container">
          <ErrorBoundary
            key={JSON.stringify([currentTab, selectedWorkflowId, selectedWorkflowRunId, selectedRunId, selectedTraceRunId])}
            onGoToWorkflows={() => handleSelectTab('workflows')}
          >
          {connection.status === 'connecting' && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: 40, justifyContent: 'center' }}>
              <Spinner size={20} />
              <span style={{ color: 'var(--text-secondary)' }}>{t('connection.connecting')}</span>
            </div>
          )}

          {connection.status === 'offline' && (
            <Card elevated style={{ maxWidth: 600, margin: '20px auto', borderColor: 'var(--status-danger)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <span style={{ fontSize: 24 }}><Icon name="warning" /></span>
                <PageHeader title={t('connection.errorTitle')} />
              </div>
              <p style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 16, lineHeight: 1.5 }}>
                {connection.error || t('connection.errorDescription')}
              </p>
              <div style={{ display: 'flex', gap: 12 }}>
                <Button variant="primary" onClick={refreshConnection}>
                  {t('connection.retry')}
                </Button>
              </div>
            </Card>
          )}

          {connection.status === 'unauthorized' && (
            <Card elevated style={{ maxWidth: 600, margin: '20px auto', borderColor: 'var(--status-danger)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <span style={{ fontSize: 24 }}><Icon name="lock" /></span>
                <PageHeader title={t('connection.unauthorizedTitle')} />
              </div>
              <p style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 16, lineHeight: 1.5 }}>
                {connection.error || t('connection.unauthorizedDescription')}
              </p>
              <div style={{ display: 'flex', gap: 12 }}>
                <Button variant="primary" onClick={refreshConnection}>
                  {t('connection.retry')}
                </Button>
              </div>
            </Card>
          )}

          {connection.status === 'connected' && (
            <Suspense
              fallback={
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 48, gap: 10 }}>
                  <Spinner size={24} />
                  <span style={{ color: 'var(--text-secondary)', fontSize: 14 }}>{t('common.loading')}</span>
                </div>
              }
            >
              {currentTab === 'workflows' && (
                selectedRunId ? (
                  <RunPage key={selectedRunId} runId={selectedRunId} />
                ) : selectedWorkflowId ? (
                  <WorkflowDetail
                    key={selectedWorkflowId}
                    workflowId={selectedWorkflowId}
                    runId={selectedWorkflowRunId}
                    onRunChange={(runId) => handleWorkflowRunChange(selectedWorkflowId, runId)}
                    onBack={handleBackToWorkflows}
                    onOpenWorkflow={handleOpenWorkflow}
                  />
                ) : (
                  <WorkflowList onSelectWorkflow={handleOpenWorkflow} />
                )
              )}
              {currentTab === 'templates' && <TemplatesView />}
              {currentTab === 'approvals' && (
                <ApprovalInbox onOpenWorkflow={handleOpenWorkflow} />
              )}
              {currentTab === 'playground' && <ChatPlayground />}
              {currentTab === 'knowledge' && <KnowledgeView />}
              {currentTab === 'skills' && <SkillsView />}
              {currentTab === 'agents' && <AgentsView />}
              {currentTab === 'credentials' && <CredentialsView />}
              {currentTab === 'tools' && <IntegrationsView />}
              {currentTab === 'evaluations' && <EvaluationsView />}
              {currentTab === 'triggers' && (
                // Not keyed on the workflow: the in-view "Show all triggers" link only lifts the
                // filter, and a remount would drop a one-time webhook secret still on screen.
                <TriggersView initialWorkflowId={selectedTriggersWorkflowId} />
              )}
              {currentTab === 'traces' && <TracesView runId={selectedTraceRunId} />}
              {currentTab === 'onboarding' && <LocalOnboarding />}
              {currentTab === 'system' && <SystemView />}
              {ExtensionPage && <ExtensionPage />}
            </Suspense>
          )}
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
