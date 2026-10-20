// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useEffect, useCallback } from 'react';
import { useApi } from '../../api/context';
import { Card } from '../../components/Card';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { Input } from '../../components/Input';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { REGISTRY_TEMPLATES } from './templates';
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

  const handleDelete = async (workflowId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm(t('workflows.confirmDelete'))) return;
    try {
      await manager.client.workflows.delete(workflowId);
      fetchWorkflows();
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  };

  const handleQuickCopy = async (wf: WorkflowRecord, e: React.MouseEvent) => {
    e.stopPropagation();
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* Top Header Card with Search & Actions */}
      <Card elevated>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 16 }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <h2 style={{ fontSize: 18, fontWeight: 600 }}>{t('workflows.title')}</h2>
              <Badge variant="neutral">
                {t('connection.workspace')}: {connection.workspace}
              </Badge>
              <Badge variant="info">
                {total} {total === 1 ? t('ui.workflow3') : t('ui.workflows')}
              </Badge>
            </div>
            <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 4 }}>
              {t('ui.tenantScopedWorkflowSpecificationsRevisionsAndExecutionBlueprints')}
            </p>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Button variant="primary" onClick={() => handleOpenCreateModal()}>
              + {t('workflows.create')}
            </Button>
          </div>
        </div>

        {/* Search Bar */}
        <form onSubmit={handleSearchSubmit} style={{ display: 'flex', gap: 8, marginTop: 16 }}>
          <div style={{ flex: 1 }}>
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t('workflows.search')}
              aria-label={t('ui.searchWorkflowsInput')}
            />
          </div>
          <Button type="submit" variant="secondary">
            {t('ui.search')}
          </Button>
          {searchQuery && (
            <Button type="button" variant="secondary" onClick={handleClearSearch}>
              {t('workflows.clearSearch')}
            </Button>
          )}
        </form>
      </Card>

      {/* Registry Templates Quick-Pick Carousel / Cards */}
      <div>
        <h3 style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          {t('workflows.templatesTitle')}
        </h3>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12 }}>
          {REGISTRY_TEMPLATES.map((tmpl) => (
            <Card key={tmpl.id} style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
              <div>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                  <span style={{ fontWeight: 600, fontSize: 14 }}>{t(`examples.${tmpl.id}.title`, tmpl.title)}</span>
                  <Badge variant="info">{t('workflows.templatesTitle')}</Badge>
                </div>
                <p style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.4, marginBottom: 12 }}>
                  {t(`examples.${tmpl.id}.description`, tmpl.description)}
                </p>
              </div>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => handleOpenCreateModal(tmpl.id)}
              >
                {t('workflows.useTemplate')} <Icon name="right" />
              </Button>
            </Card>
          ))}
        </div>
      </div>

      {/* Workflows List Table / Empty States */}
      <Card style={{ minWidth: 0, maxWidth: '100%' }}>
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
          <div style={{ textAlign: 'center', padding: '32px 16px' }}>
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
              <Button variant="primary" onClick={() => handleOpenCreateModal()}>
                + {t('workflows.create')}
              </Button>
            )}
          </div>
        ) : (
          <div>
            <div tabIndex={0} style={{ width: '100%', minWidth: 0, maxWidth: '100%', overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: 13 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-secondary)' }}>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('ui.workflowName')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('ui.entryNode')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('ui.nodes2')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('workflows.revision')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600 }}>{t('workflows.lastModified')}</th>
                    <th style={{ padding: '10px 12px', fontWeight: 600, textAlign: 'right' }}>{t('ui.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {workflows.map((wf) => (
                    <tr
                      key={wf.workflow_id}
                      onClick={() => onSelectWorkflow(wf.workflow_id)}
                      style={{
                        borderBottom: '1px solid var(--border-subtle)',
                        cursor: 'pointer',
                        transition: 'background-color 0.15s ease',
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = 'var(--bg-canvas)')}
                      onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = 'transparent')}
                    >
                      <td style={{ padding: '12px' }}>
                        <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                          {wf.name || (wf.spec as Record<string, unknown> | undefined)?.name as string || t('ui.untitledWorkflow')}
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{wf.workflow_id}</div>
                      </td>
                      <td style={{ padding: '12px' }}>
                        <code>{wf.entry_node || 'start'}</code>
                      </td>
                      <td style={{ padding: '12px' }}>
                        <Badge variant="neutral">{wf.node_count ?? 1} {t('ui.nodes3')}</Badge>
                      </td>
                      <td style={{ padding: '12px' }}>
                        {/* Authoritative revision counter */}
                        <Badge variant="success">{t('ui.rev3') + " "}{wf.revision}</Badge>
                      </td>
                      <td style={{ padding: '12px', color: 'var(--text-secondary)', fontSize: 12 }}>
                        {wf.updated_at ? new Date(wf.updated_at * 1000).toLocaleDateString(locale) : t('common.notAvailable')}
                      </td>
                      <td style={{ padding: '12px', textAlign: 'right' }}>
                        <div style={{ display: 'inline-flex', gap: 6 }}>
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={(e) => {
                              e.stopPropagation();
                              onSelectWorkflow(wf.workflow_id);
                            }}
                          >
                            {t('workflows.open')}
                          </Button>
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={(e) => handleQuickCopy(wf, e)}
                          >
                            {t('ui.copy')}
                          </Button>
                          <Button
                            variant="danger"
                            size="sm"
                            onClick={(e) => handleDelete(wf.workflow_id, e)}
                          >
                            {t('ui.delete')}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination Controls */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--border-subtle)' }}>
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
      <CreateWorkflowModal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onCreated={handleCreated}
        initialTemplateId={templateToInstantiate}
      />
    </div>
  );
}
