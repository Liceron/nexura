// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useApi } from '../../api/context';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { Modal } from '../../components/Modal';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { TemplateGraph } from './TemplateGraph';
import type { InstantiateResult, TemplateDetail, TemplateList, TemplateSummary } from './types';

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
    );
  }, [catalog, search]);

  const openPreview = useCallback(
    async (tpl: TemplateSummary) => {
      setCreateError(null);
      try {
        setPreview(await manager.client.request<TemplateDetail>('GET', `/templates/${tpl.id}`));
      } catch {
        setError(t('templates.loadError'));
      }
    },
    [manager, t]
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
        setCreated(res);
        setPreview(null);
      } catch (err: unknown) {
        setCreateError(err instanceof Error ? err.message : t('templates.createError'));
      } finally {
        setCreatingId(null);
      }
    },
    [manager, t]
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h1 style={{ fontSize: 20, fontWeight: 600, margin: 0 }}>{t('templates.title')}</h1>
        <p style={{ color: 'var(--text-secondary)', fontSize: 14, margin: '4px 0 0' }}>
          {t('templates.subtitle')}
        </p>
      </div>

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
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
            {(catalog?.categories ?? []).map((c) => (
              <option key={c} value={c}>{c}</option>
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
        <p data-testid="templates-empty" style={{ color: 'var(--text-secondary)' }}>{t('templates.empty')}</p>
      )}

      <div
        data-testid="template-grid"
        style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 16 }}
      >
        {items.map((tpl) => (
          <Card key={tpl.id} data-testid={`template-card-${tpl.id}`} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
              <h2 style={{ fontSize: 15, fontWeight: 600, margin: 0 }}>{tpl.title}</h2>
              <Badge variant="info">{tpl.category}</Badge>
            </div>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: 0, flex: 1 }}>{tpl.description}</p>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <Badge>{t(`templates.difficulty.${tpl.difficulty}`)}</Badge>
              <Badge>{tpl.node_count} {t('templates.steps')}</Badge>
              {tpl.has_approval && <Badge variant="warning">{t('templates.approval')}</Badge>}
              {tpl.has_trigger && <Badge variant="success">{t('templates.trigger')}</Badge>}
              {tpl.tags.map((x) => (
                <Badge key={x}>{x}</Badge>
              ))}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <Button size="sm" data-testid={`template-preview-${tpl.id}`} onClick={() => openPreview(tpl)}>
                {t('templates.preview')}
              </Button>
              <Button
                size="sm"
                variant="primary"
                data-testid={`template-use-${tpl.id}`}
                loading={creatingId === tpl.id}
                onClick={() => useTemplate(tpl)}
              >
                {t('templates.use')}
              </Button>
            </div>
          </Card>
        ))}
      </div>

      {preview && (
        <Modal open onClose={() => setPreview(null)} title={preview.title} maxWidth={760}>
          <div data-testid="template-preview" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
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
              <Button onClick={() => setPreview(null)}>{t('templates.close')}</Button>
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
    </div>
  );
}
