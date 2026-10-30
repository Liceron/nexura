// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useEffect, useCallback } from 'react';
import { useApi } from '../../api/context';
import { Card } from '../../components/Card';
import { Button } from '../../components/Button';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { ActionDisclosure } from '../../components/ActionDisclosure';
import { Input } from '../../components/Input';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { DeleteWorkflowDialog } from './DeleteWorkflowDialog';
import { CreateWorkflowModal } from './CreateWorkflowModal';
import type { WorkflowRecord } from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';

interface WorkflowListProps {
  onSelectWorkflow: (workflowId: string) => void;
}

export function WorkflowList({ onSelectWorkflow }: WorkflowListProps) {
  const { manager, connection } = useApi();
  const { t, locale } = useI18n();

  const [workflows, setWorkflows] = useState<WorkflowRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Pagination & Search
  const [searchQuery, setSearchQuery] = useState('');
  const [limit] = useState(10);
  const [offset, setOffset] = useState(0);

  // Create Modal
  const [deleteTarget, setDeleteTarget] = useState<WorkflowRecord | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [templateToInstantiate, setTemplateToInstantiate] = useState<string | null>(null);

  const fetchWorkflows = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await manager.client.workflows.list({
        query: searchQuery.trim() || undefined,
        limit,
        offset,
      });
      setWorkflows(res.items);
      setTotal(res.total);
      setHasMore(res.has_more);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setLoading(false);
    }
  }, [manager, searchQuery, limit, offset]);

  // Re-fetch when workspace changes or search/page changes
  useEffect(() => {
    fetchWorkflows();
  }, [fetchWorkflows, connection.workspace]);

  const handleOpenCreateModal = (templateId?: string) => {
    setTemplateToInstantiate(templateId || null);
    setCreateModalOpen(true);
  };

  const handleCreated = (workflowId: string) => {
    fetchWorkflows();
    onSelectWorkflow(workflowId);
  };

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setOffset(0);
    fetchWorkflows();
  };

  const handleClearSearch = () => {
    setSearchQuery('');
    setOffset(0);
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleteError(null);
    setDeleting(true);
    try {
      const workflowId = deleteTarget.workflow_id;
      await manager.client.workflows.delete(workflowId);
      setDeleteTarget(null);
      fetchWorkflows();
    } catch (err: unknown) {
      setDeleteError(formatApiError(err, t));
    }
    finally { setDeleting(false); }
  };

  const handleQuickCopy = async (wf: WorkflowRecord, e: React.MouseEvent) => {
    e.preventDefault();
    try {
      const full = await manager.client.workflows.get(wf.workflow_id);
      if (full && full.spec) {
        const copyName = t('workflows.copyName').replace('{name}', full.name);
        const created = await manager.client.workflows.create(
          { ...full.spec, name: copyName },
          { name: copyName }
        );
        fetchWorkflows();
        onSelectWorkflow(created.workflow_id);
      }
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  };

  return (
    <PageLayout className="workflow-library">
      <PageHeader title={t('workflows.title')} actions={
        <Button variant="primary" onClick={() => handleOpenCreateModal()}>{t('workflows.create')}</Button>
      } filters={
        <form onSubmit={handleSearchSubmit} className="workflow-search">
          <Input value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} placeholder={t('workflows.search')} aria-label={t('ui.searchWorkflowsInput')} />
          <Button type="submit">{t('ui.search')}</Button>
          {searchQuery && <Button type="button" variant="ghost" onClick={handleClearSearch}>{t('workflows.clearSearch')}</Button>}
        </form>
      } />

      {/* Workflows List Table / Empty States */}
      <Card style={{ minWidth: 0, maxWidth: '100%', padding: 0 }}>
        {error && (
          <div style={{ padding: 12, backgroundColor: 'var(--status-danger-bg)', color: 'var(--status-danger)', borderRadius: 'var(--radius-sm)', marginBottom: 12 }}>
            <strong>{t('ui.error2')}</strong> {error}
          </div>
        )}

        {loading ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 40, gap: 10 }}>
            <Spinner size={20} />
            <span style={{ color: 'var(--text-secondary)' }}>{t('common.loading')}</span>
          </div>
        ) : workflows.length === 0 ? (
          <div className="studio-empty-state" style={{ textAlign: 'center', padding: '32px 16px' }}>
            <div style={{ fontSize: 28, marginBottom: 8 }}><Icon name="folder" /></div>
            <h4 style={{ fontSize: 15, fontWeight: 600, marginBottom: 4 }}>
              {searchQuery ? `${t('workflows.noMatches')} "${searchQuery}"` : t('workflows.empty')}
            </h4>
            <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginBottom: 16 }}>
              {searchQuery ? t('workflows.searchEmptyDescription') : t('workflows.emptyAction')}
            </p>
            {searchQuery ? (
              <Button variant="secondary" onClick={handleClearSearch}>
                {t('workflows.clearSearch')}
              </Button>
            ) : (
              <Button variant="secondary" onClick={() => handleOpenCreateModal()}>
                {t('workflows.create')}
              </Button>
            )}
          </div>
        ) : (
          <div>
            <div role="region" aria-label={t('workflows.title')} tabIndex={0} style={{ width: '100%', minWidth: 0, maxWidth: '100%', overflowX: 'auto' }}>
              <table className="workflow-table" style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: 13 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-secondary)' }}>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('workflowDesign.name')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('workflowDesign.entryStep')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('workflowDesign.steps')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('workflows.revision')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('workflows.lastModified')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600, textAlign: 'right' }}>{t('ui.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {workflows.map((wf) => (
                    <tr
                      key={wf.workflow_id}
                      style={{
                        borderBottom: '1px solid var(--border-subtle)',
                        cursor: 'pointer',
                        transition: 'background-color 0.15s ease',
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = 'var(--bg-canvas)')}
                      onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = 'transparent')}
                    >
                      <td style={{ padding: '12px' }}>
                        <a href={`#/workflows/${encodeURIComponent(wf.workflow_id)}`} onClick={(event) => { if (!event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); onSelectWorkflow(wf.workflow_id); } }} style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                          {wf.name || (wf.spec as Record<string, unknown> | undefined)?.name as string || t('ui.untitledWorkflow')}
                        </a>
                        <div className="workflow-phone-meta">{t('workflows.revision')} {wf.revision} · {wf.updated_at ? new Date(wf.updated_at * 1000).toLocaleDateString(locale) : t('common.notAvailable')}</div>
                      </td>
                      <td style={{ padding: '12px' }}>
                        <code>{wf.entry_node || 'start'}</code>
                      </td>
                      <td style={{ padding: '12px' }}>
                        {wf.node_count ?? 1}
                      </td>
                      <td style={{ padding: '12px' }}>
                        {/* Authoritative revision counter */}
                        {wf.revision}
                      </td>
                      <td style={{ padding: '12px', color: 'var(--text-secondary)', fontSize: 12 }}>
                        {wf.updated_at ? new Date(wf.updated_at * 1000).toLocaleDateString(locale) : t('common.notAvailable')}
                      </td>
                      <td style={{ padding: '12px', textAlign: 'right' }}>
                        <ActionDisclosure label={t('workflowDesign.more')}>
                          <Button variant="ghost" onClick={(e) => handleQuickCopy(wf, e)}>{t('workflowDesign.duplicate')}</Button>
                          <Button variant="ghost" className="workflow-danger-action" onClick={(e) => { e.preventDefault(); setDeleteError(null); setDeleteTarget(wf); }}>{t('ui.delete')}</Button>
                        </ActionDisclosure>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination Controls */}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', justifyContent: 'space-between', padding: 12, borderTop: '1px solid var(--border-subtle)' }}>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                {t('workflows.showing')} {total === 0 ? 0 : offset + 1} - {Math.min(offset + limit, total)} {t('workflows.of')} {total}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                >
                  <Icon name="left" /> {t('workflows.previous')}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!hasMore}
                  onClick={() => setOffset(offset + limit)}
                >
                  {t('workflows.next')} <Icon name="right" />
                </Button>
              </div>
            </div>
          </div>
        )}
      </Card>

      {/* Create Workflow Modal */}
      <DeleteWorkflowDialog open={!!deleteTarget} name={deleteTarget?.name || deleteTarget?.workflow_id || ''} busy={deleting} error={deleteError} onClose={() => setDeleteTarget(null)} onConfirm={handleDelete} />
      <CreateWorkflowModal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onCreated={handleCreated}
        initialTemplateId={templateToInstantiate}
      />
    </PageLayout>
  );
}
