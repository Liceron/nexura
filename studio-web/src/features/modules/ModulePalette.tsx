// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Modal } from '../../components/Modal';
import { Button } from '../../components/Button';
import { Fragment, useState, useEffect, useMemo } from 'react';
import { useApi } from '../../api/context';
import type { ModuleDescriptor } from '@liceron/nexura-sdk';
import { Card } from '../../components/Card';
import { Badge } from '../../components/Badge';
import { Spinner } from '../../components/Spinner';
import { MODULE_CATEGORIES, ModuleCategory, isOfferedInPalette, moduleMatchesCategory } from './types';
import { OperationList } from './ConnectorFields';
import { connectorOperations, requiredCredentialTypes } from './connectors';
import { useBuilderT } from '../builder/composite';
import { formatApiError } from '../../api/errors';

export const MODULE_DRAG_TYPE = 'application/x-nexura-module';

interface ModulePaletteProps {
  open: boolean;
  onClose: () => void;
  onSelectModule: (module: ModuleDescriptor) => void;
  sidebar?: boolean;
  catalog?: ReturnType<typeof useModuleCatalog>;
}

export function useModuleCatalog(open: boolean) {
  const { manager, connection } = useApi();
  const t = useBuilderT();
  const [modules, setModules] = useState<ModuleDescriptor[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let isMounted = true;
    setModules([]);
    setLoading(true);
    setError(null);

    Promise.resolve()
      .then(() => manager.client.modules.list())
      .then((res) => {
        if (!isMounted) return;
        const list = res.items || res.modules || [];
        setModules(list);
      })
      .catch((err: unknown) => {
        if (!isMounted) return;
        setError(formatApiError(err, t));
      })
      .finally(() => {
        if (isMounted) setLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [open, manager, connection?.workspace, connection?.principal]);

  return { modules, loading, error };
}

export function ModulePalette({ open, onClose, onSelectModule, sidebar = false, catalog }: ModulePaletteProps) {
  const ownCatalog = useModuleCatalog(open && !catalog);
  const { modules, loading, error } = catalog ?? ownCatalog;
  const t = useBuilderT();
  const [activeCategory, setActiveCategory] = useState<ModuleCategory>('all');
  const [search, setSearch] = useState('');
  const categoryOf = (module: ModuleDescriptor) => MODULE_CATEGORIES.find(category => category.id !== 'all' && moduleMatchesCategory(module, category.id))?.id ?? 'all';

  const filteredModules = useMemo(() => {
    const filtered = modules.filter((mod) => {
      // Service-internal modules (descriptor flag) stay registered but are not offered.
      if (!isOfferedInPalette(mod)) return false;

      // Category filter
      if (!moduleMatchesCategory(mod, activeCategory)) return false;

      // Search filter
      if (search.trim()) {
        const q = search.toLowerCase();
        const matchesId = mod.module_id.toLowerCase().includes(q);
        const matchesTitle = mod.title.toLowerCase().includes(q);
        const matchesDesc = mod.description.toLowerCase().includes(q);
        const matchesKind = mod.kind.toLowerCase().includes(q);
        if (!matchesId && !matchesTitle && !matchesDesc && !matchesKind) {
          return false;
        }
      }

      return true;
    });
    return sidebar && activeCategory === 'all' ? filtered.sort((a, b) => categoryOf(a).localeCompare(categoryOf(b))) : filtered;
  }, [modules, activeCategory, search, sidebar]);

  if (!open) return null;

  const content = <>
      <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{filteredModules.length} {t('ui.available2')}</div>
      {/* Search Input */}
      <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--border-subtle)' }}>
        <input
          type="text"
          placeholder={t('ui.searchModules')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label={t('ui.searchModuleRegistry')}
          style={{
            width: '100%',
            padding: '6px 10px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-primary)',
            fontSize: 12,
          }}
        />
      </div>

      {/* Category Pills */}
      <div
        style={{
          display: 'flex',
          gap: 6,
          padding: '8px 14px',
          overflowX: 'auto',
          borderBottom: '1px solid var(--border-subtle)',
          scrollbarWidth: 'none',
        }}
      >
        {MODULE_CATEGORIES.map((cat) => (
          <button
            key={cat.id}
            data-testid={`palette-category-${cat.id}`}
            aria-pressed={activeCategory === cat.id}
            onClick={() => setActiveCategory(cat.id)}
            style={{
              padding: '4px 10px',
              borderRadius: 'var(--radius-full)',
              border: 'none',
              backgroundColor: activeCategory === cat.id ? 'var(--bg-muted)' : 'transparent',
              color: activeCategory === cat.id ? 'var(--text-primary)' : 'var(--text-secondary)',
              fontSize: 'var(--type-meta)',
              fontWeight: 500,
              cursor: 'pointer',
              whiteSpace: 'nowrap',
              transition: 'background-color 0.15s ease',
            }}
          >
            {t(`paletteCategories.${cat.id}`)}
          </button>
        ))}
      </div>

      {/* Module List Body */}
      <div style={{ overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 8, flex: 1 }}>
        {loading && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 40, gap: 10 }}>
            <Spinner size={18} />
            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{t('ui.loadingRegisteredModules')}</span>
          </div>
        )}

        {error && (
          <div style={{ padding: 12, backgroundColor: 'var(--status-danger-bg)', color: 'var(--status-danger)', borderRadius: 'var(--radius-sm)', fontSize: 12 }}>
            {error}
          </div>
        )}

        {!loading && !error && filteredModules.length === 0 && (
          <div style={{ padding: 30, textAlign: 'center', color: 'var(--text-secondary)', fontSize: 12 }}>
            {t('ui.noModulesFoundMatchingLdquo')}{search}{t('ui.rdquo')}
          </div>
        )}

        {!loading &&
          !error &&
          filteredModules.map((mod, index) => {
            const isSynthetic = mod.module_id.startsWith('synthetic_');
            return (
              <Fragment key={`${mod.module_id}-${mod.version}`}>
              {sidebar && activeCategory === 'all' && (index === 0 || categoryOf(filteredModules[index - 1]) !== categoryOf(mod)) && <h3 style={{ fontSize: 12, margin: '8px 0 0' }}>{t(`paletteCategories.${categoryOf(mod)}`)}</h3>}
              <Card
                onClick={event => {
                  if (sidebar && !(event.target as HTMLElement).closest('button, a, input, select, summary')) onSelectModule(mod);
                }}
                draggable={sidebar}
                onDragStart={(event) => {
                  event.dataTransfer.setData(MODULE_DRAG_TYPE, JSON.stringify({ module_id: mod.module_id, version: mod.version }));
                  event.dataTransfer.effectAllowed = 'copy';
                }}
                style={{
                  padding: 10,
                  cursor: 'pointer',
                  transition: 'border-color 0.15s ease',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 4 }}>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)' }}>
                      {mod.title || mod.module_id}
                    </div>
                    <div style={{ fontSize: 'var(--type-meta)', color: 'var(--text-muted)' }}>
                      <code>{mod.module_id}</code> v{mod.version}
                    </div>
                  </div>

                  <div style={{ display: 'flex', gap: 4 }}>
                    {isSynthetic ? (
                      <Badge variant="info">{t('ui.localMock')}</Badge>
                    ) : mod.is_available ? (
                      <Badge variant="success">{t('ui.available')}</Badge>
                    ) : (
                      <Badge variant="danger">{t('ui.missing')}</Badge>
                    )}
                  </div>
                </div>

                <p style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)', lineHeight: 1.5, margin: '4px 0 8px 0' }}>
                  {mod.description}
                </p>

                <OperationList operations={connectorOperations(mod)} />
                {requiredCredentialTypes(mod).length > 0 && (
                  <div data-testid="credential-type" style={{ fontSize: 'var(--type-meta)', marginBottom: 6 }}>
                    {t('connectors.credentialType', { type: requiredCredentialTypes(mod).join(', ') })}
                  </div>
                )}
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 'var(--type-meta)' }}>
                  <div style={{ display: 'flex', gap: 6, color: 'var(--text-muted)' }}>
                    <span>{t('ui.kind2') + " "}{mod.kind}</span>
                    {mod.requires_approval && <Badge variant="warning">{t('ui.approvalGate')}</Badge>}
                  </div>
                  <Button variant="ghost" size="sm" aria-label={sidebar ? t('builderTest.addStep', { title: mod.title || mod.module_id }) : undefined} onClick={() => onSelectModule(mod)}>{t('ui.addNode')}</Button>
                </div>
              </Card>
              </Fragment>
            );
          })}
      </div>
    </>;
  return sidebar ? <aside className="workflow-module-sidebar" aria-label={t('builderTest.modules')}>
    <div className="builder-panel-heading"><strong>{t('builderTest.modules')}</strong><Button variant="ghost" size="sm" onClick={onClose}>{t('builderTest.collapse')}</Button></div>
    <p className="builder-panel-hint">{t('builderTest.dragHint')}</p>
    {content}
  </aside> : <Modal open={open} onClose={onClose} title={t('ui.availableModulesDrawer')} maxWidth={480}>{content}</Modal>;
}
