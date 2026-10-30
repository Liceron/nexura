// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useApi } from '../../api/context';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { Modal } from '../../components/Modal';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { TemplateGraph } from './TemplateGraph';
import { rememberSampleInput } from './sampleInput';
import type { InstantiateResult, TemplateDetail, TemplateList, TemplateSummary } from './types';

const categoryOrder = ['agents', 'knowledge', 'automation', 'data', 'governance', 'support'];
const categoryRank = (category: string) => {
  const index = categoryOrder.indexOf(category);
  return index === -1 ? categoryOrder.length : index;
};

const selectStyle = {
  padding: '6px 10px',
  borderRadius: 'var(--radius-md)',
  border: '1px solid var(--border-subtle)',
  backgroundColor: 'var(--bg-surface)',
  color: 'var(--text-primary)',
  fontSize: 13,
} as const;

export function TemplatesView() {
  const { manager, connection } = useApi();
  const { t } = useI18n();
  const [catalog, setCatalog] = useState<TemplateList | null>(null);
  const [category, setCategory] = useState('');
  const [tag, setTag] = useState('');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<TemplateDetail | null>(null);
  const [creatingId, setCreatingId] = useState<string | null>(null);
  const [created, setCreated] = useState<InstantiateResult | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [previewId, setPreviewId] = useState(() => new URLSearchParams(window.location.hash.split('?')[1] || '').get('template'));
  const [previewAttempt, setPreviewAttempt] = useState(0);
  useEffect(() => {
    const select = () => setPreviewId(new URLSearchParams(window.location.hash.split('?')[1] || '').get('template'));
    window.addEventListener('hashchange', select);
    return () => window.removeEventListener('hashchange', select);
  }, []);
  useEffect(() => {
    if (!previewId) { setPreview(null); return; }
    let active = true;
    setCreateError(null);
    setError(null);
    setPreview(null);
    void manager.client.request<TemplateDetail>('GET', `/templates/${encodeURIComponent(previewId)}`)
      .then(detail => { if (active) setPreview(detail); })
      .catch(() => { if (active) setError(t('templates.loadError')); });
    return () => { active = false; };
  }, [previewId, previewAttempt, manager, t]);

  useEffect(() => {
    if (connection.status !== 'connected') return;
    let active = true;
    setLoading(true);
    setError(null);
    manager.client
      .request<TemplateList>('GET', '/templates', {
        params: { category: category || undefined, tag: tag || undefined },
      })
      .then((res) => {
        if (active) setCatalog((prev) => ({ ...res, categories: prev?.categories ?? res.categories, tags: prev?.tags ?? res.tags }));
      })
      .catch(() => {
        if (active) setError(t('templates.loadError'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [manager, connection.status, category, tag, t]);

  const items = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (catalog?.items ?? []).filter(
      (tpl) =>
        !q ||
        tpl.title.toLowerCase().includes(q) ||
        tpl.description.toLowerCase().includes(q) ||
        tpl.tags.some((x) => x.toLowerCase().includes(q))
    ).sort((a, b) => categoryRank(a.category) - categoryRank(b.category) || a.title.localeCompare(b.title));
  }, [catalog, search]);

  const openPreview = useCallback(
    (tpl: TemplateSummary) => {
      const hash = `#/templates?template=${encodeURIComponent(tpl.id)}`;
      if (window.location.hash === hash) {
        setPreviewId(tpl.id);
        setPreviewAttempt(attempt => attempt + 1);
      } else window.location.hash = hash;
    },
    []
  );

  const useTemplate = useCallback(
    async (tpl: TemplateSummary) => {
      setCreatingId(tpl.id);
      setCreateError(null);
      setCreated(null);
      try {
        const res = await manager.client.request<InstantiateResult>(
          'POST',
          `/templates/${tpl.id}/instantiate`,
          { body: { name: tpl.title } }
        );
        // The Run dialog of the new workflow starts with the template's sample input.
        try {
          const detail = await manager.client.request<TemplateDetail>('GET', `/templates/${tpl.id}`);
          rememberSampleInput(connection.workspace, res.workflow_id, detail.sample_input);
        } catch {
          // Best effort: without it the Run dialog shows its generic example.
        }
        setCreated(res);
        setPreview(null);
        window.location.replace('#/templates');
      } catch (err: unknown) {
        setCreateError(err instanceof Error ? err.message : t('templates.createError'));
      } finally {
        setCreatingId(null);
      }
    },
    [manager, connection.workspace, t]
  );

  return (
    <PageLayout>
      <PageHeader title={t('templates.title')} />
      <div className="studio-template-toolbar" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center', marginBottom: 16 }}>
        <input
          type="search"
          aria-label={t('templates.search')}
          placeholder={t('templates.search')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ ...selectStyle, minWidth: 220 }}
        />
        <label style={{ fontSize: 13 }}>
          {t('templates.category')}{' '}
          <select aria-label={t('templates.category')} value={category} onChange={(e) => setCategory(e.target.value)} style={selectStyle}>
            <option value="">{t('templates.all')}</option>
            {[...(catalog?.categories ?? [])].sort((a, b) => categoryRank(a) - categoryRank(b)).map((c) => (
              <option key={c} value={c}>{t(`templates.categories.${c}`, c)}</option>
            ))}
          </select>
        </label>
        <label style={{ fontSize: 13 }}>
          {t('templates.tag')}{' '}
          <select aria-label={t('templates.tag')} value={tag} onChange={(e) => setTag(e.target.value)} style={selectStyle}>
            <option value="">{t('templates.all')}</option>
            {(catalog?.tags ?? []).map((x) => (
              <option key={x} value={x}>{x}</option>
            ))}
          </select>
        </label>
      </div>

      {created && (
        <Card data-testid="template-created" role="status" style={{ borderColor: 'var(--status-success)' }}>
          <strong>{t('templates.created')}</strong>
          {created.missing_credentials.length > 0 && (
            <p style={{ fontSize: 13, margin: '8px 0 0' }}>
              {t('templates.missingCredentials')}:{' '}
              <span data-testid="template-missing-credentials">{created.missing_credentials.join(', ')}</span>
              {' '}
              <a
                href="#/credentials"
                data-testid="template-credentials-link"
                style={{ color: 'var(--brand-primary)', fontWeight: 600 }}
              >
                {t('templates.addCredentials')}
              </a>
            </p>
          )}
          <div style={{ marginTop: 10 }}>
            <Button
              variant="primary"
              size="sm"
              data-testid="template-open-workflow"
              onClick={() => {
                window.location.hash = `#/workflows/${created.workflow_id}`;
              }}
            >
              {t('templates.open')}
            </Button>
          </div>
        </Card>
      )}
      {createError && (
        <Card role="alert" style={{ borderColor: 'var(--status-danger)', color: 'var(--status-danger)' }}>
          {createError}
        </Card>
      )}

      {loading && !catalog && <Spinner size={20} />}
      {error && <div role="alert" style={{ color: 'var(--status-danger)' }}>{error}</div>}
      {catalog && items.length === 0 && !loading && (
        <p className="studio-empty-state" data-testid="templates-empty" style={{ color: 'var(--text-secondary)' }}>{t('templates.empty')}</p>
      )}

      <ul data-testid="template-grid" className="workflow-template-list">
        {items.map((tpl) => (
          <li key={tpl.id} data-testid={`template-card-${tpl.id}`}>
            <div>
              <h2><button type="button" data-testid={`template-preview-${tpl.id}`} onClick={() => openPreview(tpl)}>{tpl.title}</button></h2>
              <p>{tpl.description}</p>
              <div className="workflow-template-meta">
                <span>{tpl.node_count} {t('templates.steps')}</span>
                <span>{t(`templates.categories.${tpl.category}`, tpl.category)} · {t(`templates.difficulty.${tpl.difficulty}`)}</span>
                {tpl.has_approval && <span>{t('workflowDesign.needsApproval')}</span>}
                {tpl.has_trigger && <span>{t('templates.trigger')}</span>}
                <span>{t('templates.credentials')}: {tpl.required_credentials.length ? tpl.required_credentials.join(', ') : t('templates.none')}</span>
              </div>
              {tpl.tags.length > 0 && <div className="workflow-template-meta">{tpl.tags.join(' · ')}</div>}
            </div>
            <Button size="sm" variant="ghost" data-testid={`template-use-${tpl.id}`} onClick={() => openPreview(tpl)}>{t('templates.preview')}</Button>
          </li>
        ))}
      </ul>

      {preview && (
        <Modal open onClose={() => { setPreview(null); window.location.hash = '#/templates'; }} title={preview.title} maxWidth={760}>
          <div data-testid="template-preview" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {createError && <div role="alert" style={{ color: 'var(--status-danger)' }}>{createError}</div>}
            <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: 0 }}>{preview.description}</p>
            <strong style={{ fontSize: 13 }}>{t('templates.graph')}</strong>
            <TemplateGraph spec={preview.spec} label={t('templates.graph')} />
            <div style={{ fontSize: 13 }}>
              <strong>{t('templates.modules')}:</strong> {preview.required_modules.join(', ')}
            </div>
            <div style={{ fontSize: 13 }}>
              <strong>{t('templates.credentials')}:</strong>{' '}
              {preview.required_credentials.length ? preview.required_credentials.join(', ') : t('templates.none')}
            </div>
            <details>
              <summary style={{ fontSize: 13, cursor: 'pointer' }}>{t('templates.diagram')}</summary>
              <pre style={{ fontSize: 12, overflow: 'auto' }}>{preview.mermaid}</pre>
            </details>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <Button onClick={() => { setPreview(null); window.location.hash = '#/templates'; }}>{t('templates.close')}</Button>
              <Button
                variant="primary"
                data-testid="template-preview-use"
                loading={creatingId === preview.id}
                onClick={() => useTemplate(preview)}
              >
                {creatingId === preview.id ? t('templates.using') : t('templates.use')}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </PageLayout>
  );
}
