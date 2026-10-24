// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useEffect, useState } from 'react';
import type { WorkflowRecord, ModuleDescriptor } from '@liceron/nexura-sdk';
import type { BuilderNodeData } from './types';
import { Input } from '../../components/Input';
import { Button } from '../../components/Button';
import { useApi } from '../../api/context';
import {
  DEFAULT_MAP_AS,
  DEFAULT_MAX_CONCURRENCY,
  REDUCER_CHOICES,
  compositeIssues,
  stepKind,
  useBuilderT,
} from './composite';
import type { CompositeField } from './composite';
import { isOfferedInPalette } from '../modules/types';

interface Props {
  nodeId: string;
  data: BuilderNodeData;
  onUpdate: (updates: Partial<BuilderNodeData>) => void;
}

const selectStyle: React.CSSProperties = {
  width: '100%',
  padding: '6px 10px',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--border-subtle)',
  backgroundColor: 'var(--bg-canvas)',
  color: 'var(--text-primary)',
  fontSize: 13,
};

const labelStyle: React.CSSProperties = { display: 'block', fontSize: 13, fontWeight: 500, marginBottom: 6 };
const helpStyle: React.CSSProperties = { fontSize: 'var(--type-meta)', color: 'var(--text-muted)', marginTop: 4 };

function FieldError({ id, text }: { id: string; text?: string }) {
  if (!text) return null;
  return (
    <div id={id} role="alert" style={{ fontSize: 12, color: 'var(--status-danger)', marginTop: 4 }}>
      {text}
    </div>
  );
}

/** Inspector form for parallel, map and sub-workflow steps, bound to the real spec fields. */
export function CompositeInspector({ nodeId, data, onUpdate }: Props) {
  const { manager } = useApi();
  const t = useBuilderT();
  const kind = stepKind(data);
  const issues = compositeIssues(data);
  const err = (field: CompositeField) => {
    const hit = issues.find((i) => i.field === field);
    return hit ? t(`builder.errors.${hit.key}`) : undefined;
  };

  const [modules, setModules] = useState<ModuleDescriptor[]>([]);
  const [modulesLoaded, setModulesLoaded] = useState(false);
  const [workflows, setWorkflows] = useState<WorkflowRecord[]>([]);
  const [loadMsg, setLoadMsg] = useState<string | null>(null);
  const [loadingWf, setLoadingWf] = useState(false);
  const [newKey, setNewKey] = useState('');
  const [newHow, setNewHow] = useState<string>('append');

  useEffect(() => {
    let alive = true;
    manager.client.modules
      .list()
      .then((r) => {
        if (!alive) return;
        setModules(r.items || r.modules || []);
        setModulesLoaded(true);
      })
      .catch(() => {
        if (!alive) return;
        setModules([]);
        setModulesLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [manager]);

  useEffect(() => {
    if (kind !== 'subworkflow') return;
    let alive = true;
    manager.client.workflows
      .list({ limit: 100 })
      .then((r) => alive && setWorkflows(r.items || []))
      .catch(() => alive && setWorkflows([]));
    return () => {
      alive = false;
    };
  }, [manager, kind]);

  const available = modules.filter((m) => m.is_available !== false);
  const known = new Set(available.map((m) => m.module_id));
  const parallel = data.parallel || [];
  const savedMapHandler = data.handler || data.module_id || '';
  // Offer what the palette offers (no service-internal modules), but keep an internal module a
  // saved step already uses, so it is not shown as missing and can still be unchecked.
  const handlers = Array.from(
    new Set([
      ...available.filter(isOfferedInPalette).map((m) => m.module_id),
      ...parallel.filter((n) => known.has(n)),
      ...(savedMapHandler && known.has(savedMapHandler) ? [savedMapHandler] : []),
    ]),
  ).sort();
  // Saved branches/handler missing from the module list stay visible so they can be removed.
  const missingBranches = modulesLoaded ? parallel.filter((n) => !known.has(n)) : [];
  const mapHandlerMissing = Boolean(savedMapHandler) && !known.has(savedMapHandler);
  const ref = (data.metadata?.subworkflow || null) as { workflow_id?: string; name?: string; revision?: number } | null;

  const toggleBranch = (name: string, on: boolean) => {
    const next = on ? [...parallel, name] : parallel.filter((n) => n !== name);
    onUpdate({ parallel: next });
  };

  const loadSubworkflow = async (workflowId: string) => {
    if (!workflowId) return;
    setLoadingWf(true);
    setLoadMsg(null);
    try {
      const rec = await manager.client.workflows.get(workflowId);
      if (!rec.spec) throw new Error('no spec in response');
      onUpdate({
        subgraph: JSON.parse(JSON.stringify(rec.spec)),
        metadata: {
          ...(data.metadata || {}),
          subworkflow: { workflow_id: rec.workflow_id, name: rec.name, revision: rec.revision },
        },
      });
    } catch (e: unknown) {
      setLoadMsg(t('builder.subworkflowLoadFailed', { message: e instanceof Error ? e.message : String(e) }));
    } finally {
      setLoadingWf(false);
    }
  };

  const setReducers = (next: Record<string, string>) =>
    onUpdate({ reducers: Object.keys(next).length ? next : undefined });

  const makePlain = () => {
    const meta = { ...(data.metadata || {}) };
    delete meta.subworkflow;
    onUpdate({
      parallel: undefined,
      map_over: undefined,
      map_as: undefined,
      max_concurrency: undefined,
      reducers: undefined,
      subgraph: undefined,
      pendingKind: undefined,
      metadata: meta,
    });
  };

  const fanOut = kind === 'map' || kind === 'parallel' || data.map_over !== undefined;
  const mapped = data.map_over !== undefined;

  return (
    <div
      role="group"
      aria-label={t('builder.tabComposite')}
      data-testid="composite-inspector"
      style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 14 }}
    >
      <div style={{ fontSize: 12 }}>
        {t('builder.kindLabel')}:{' '}
        <strong data-testid="composite-kind">
          {kind === 'parallel'
            ? t('builder.kindParallel')
            : kind === 'map'
              ? t('builder.kindMap')
              : t('builder.kindSubworkflow')}
        </strong>
      </div>

      {issues.length > 0 && (
        <div
          role="alert"
          data-testid="composite-errors"
          style={{
            padding: '8px 10px',
            backgroundColor: 'var(--status-danger-bg)',
            color: 'var(--status-danger)',
            borderRadius: 'var(--radius-sm)',
            fontSize: 12,
          }}
        >
          <strong>{t('builder.stepErrorsTitle')}</strong>
          <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
            {issues.map((i) => (
              <li key={`${i.field}-${i.key}`}>{t(`builder.errors.${i.key}`)}</li>
            ))}
          </ul>
        </div>
      )}

      {kind === 'parallel' && (
        <fieldset style={{ border: 'none', padding: 0, margin: 0 }} aria-describedby={`${nodeId}-par-help`}>
          <legend style={labelStyle}>{t('builder.parallelBranches')}</legend>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 180, overflowY: 'auto' }}>
            {handlers.map((name) => (
              <label key={name} style={{ display: 'flex', gap: 8, fontSize: 12, alignItems: 'center' }}>
                <input
                  type="checkbox"
                  checked={parallel.includes(name)}
                  aria-label={t('builder.branchOption', { name })}
                  onChange={(e) => toggleBranch(name, e.target.checked)}
                />
                <code>{name}</code>
              </label>
            ))}
            {missingBranches.map((name) => (
              <label key={`missing-${name}`} style={{ display: 'flex', gap: 8, fontSize: 12, alignItems: 'center' }}>
                <input
                  type="checkbox"
                  checked
                  aria-label={t('builder.branchOption', { name }) + ' ' + t('builder.notAvailable')}
                  onChange={(e) => toggleBranch(name, e.target.checked)}
                />
                <code>{name}</code>
                <span style={{ color: 'var(--status-danger)' }}>({t('builder.notAvailable')})</span>
              </label>
            ))}
          </div>
          <div id={`${nodeId}-par-help`} style={helpStyle}>
            {t('builder.parallelBranchesHelp')}
          </div>
          <FieldError id={`${nodeId}-par-err`} text={err('parallel')} />
        </fieldset>
      )}

      {kind === 'map' && (
        <div>
          <label htmlFor={`${nodeId}-map-handler`} style={labelStyle}>
            {t('builder.mapHandler')}
          </label>
          <select
            id={`${nodeId}-map-handler`}
            style={selectStyle}
            value={data.handler || data.module_id || ''}
            aria-invalid={Boolean(err('handler'))}
            onChange={(e) => onUpdate({ handler: e.target.value, module_id: e.target.value || undefined })}
          >
            <option value="">{t('builder.mapHandlerPlaceholder')}</option>
            {mapHandlerMissing && (
              <option value={savedMapHandler}>
                {modulesLoaded ? `${savedMapHandler} (${t('builder.notAvailable')})` : savedMapHandler}
              </option>
            )}
            {handlers.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
          <FieldError id={`${nodeId}-map-handler-err`} text={err('handler')} />
        </div>
      )}

      {kind === 'subworkflow' && (
        <div>
          <label htmlFor={`${nodeId}-sub-source`} style={labelStyle}>
            {t('builder.subworkflowSource')}
          </label>
          <select
            id={`${nodeId}-sub-source`}
            style={selectStyle}
            value={ref?.workflow_id || ''}
            aria-describedby={`${nodeId}-sub-help`}
            onChange={(e) => loadSubworkflow(e.target.value)}
          >
            <option value="">{t('builder.subworkflowChoose')}</option>
            {ref?.workflow_id && !workflows.some((w) => w.workflow_id === ref.workflow_id) && (
              <option value={ref.workflow_id}>{ref.name || ref.workflow_id}</option>
            )}
            {workflows.map((w) => (
              <option key={w.workflow_id} value={w.workflow_id}>
                {w.name || w.workflow_id}
              </option>
            ))}
          </select>
          <div id={`${nodeId}-sub-help`} style={helpStyle}>
            {t('builder.subworkflowHelp')}
          </div>
          {data.subgraph && (
            <div style={{ ...helpStyle, color: 'var(--text-secondary)' }} data-testid="subworkflow-summary">
              {t('builder.subworkflowNodes', {
                count: Object.keys(data.subgraph.nodes || {}).length,
                entry: data.subgraph.entry_node,
              })}
            </div>
          )}
          {ref?.workflow_id && (
            <Button
              variant="secondary"
              size="sm"
              style={{ marginTop: 8 }}
              onClick={() => loadSubworkflow(ref.workflow_id as string)}
              disabled={loadingWf}
            >
              {loadingWf ? t('builder.subworkflowLoading') : t('builder.subworkflowRefresh')}
            </Button>
          )}
          <FieldError id={`${nodeId}-sub-missing`} text={err('subgraph')} />
          {loadMsg && <FieldError id={`${nodeId}-sub-err`} text={loadMsg} />}
          <label style={{ display: 'flex', gap: 8, fontSize: 12, alignItems: 'center', marginTop: 10 }}>
            <input
              type="checkbox"
              checked={mapped}
              aria-label={t('builder.mapSubworkflowToggle')}
              onChange={(e) =>
                onUpdate(
                  e.target.checked
                    ? { map_over: '' }
                    : { map_over: undefined, map_as: undefined, max_concurrency: undefined, reducers: undefined }
                )
              }
            />
            <span>{t('builder.mapSubworkflowToggle')}</span>
          </label>
        </div>
      )}

      {mapped && (
        <>
          <Input
            label={t('builder.mapOver')}
            value={data.map_over ?? ''}
            onChange={(e) => onUpdate({ map_over: e.target.value })}
            helperText={t('builder.mapOverHelp')}
            error={err('map_over')}
            aria-invalid={Boolean(err('map_over'))}
            placeholder={t('ui.items')}
          />
          <Input
            label={t('builder.mapAs')}
            value={data.map_as ?? DEFAULT_MAP_AS}
            onChange={(e) => onUpdate({ map_as: e.target.value })}
            helperText={t('builder.mapAsHelp')}
            error={err('map_as')}
            aria-invalid={Boolean(err('map_as'))}
          />
        </>
      )}

      {fanOut && (
        <>
          <Input
            label={t('builder.maxConcurrency')}
            type="number"
            min={1}
            max={100}
            value={data.max_concurrency ?? DEFAULT_MAX_CONCURRENCY}
            onChange={(e) =>
              onUpdate({ max_concurrency: e.target.value === '' ? 0 : Number(e.target.value) })
            }
            helperText={t('builder.maxConcurrencyHelp')}
            error={err('max_concurrency')}
            aria-invalid={Boolean(err('max_concurrency'))}
          />

          <fieldset style={{ border: 'none', padding: 0, margin: 0 }}>
            <legend style={labelStyle}>{t('builder.reducers')}</legend>
            <div style={helpStyle}>{t('builder.reducersHelp')}</div>
            {Object.entries(data.reducers || {}).map(([key, how]) => (
              <div key={key} style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6, fontSize: 12 }}>
                <code style={{ flex: 1 }}>{key}</code>
                <select
                  style={{ ...selectStyle, width: 110 }}
                  value={how}
                  aria-label={`${t('builder.reducerHow')} ${key}`}
                  onChange={(e) => setReducers({ ...(data.reducers || {}), [key]: e.target.value })}
                >
                  {REDUCER_CHOICES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  aria-label={t('builder.reducerRemove', { key })}
                  onClick={() => {
                    const next = { ...(data.reducers || {}) };
                    delete next[key];
                    setReducers(next);
                  }}
                  style={{ color: 'var(--status-danger)', cursor: 'pointer', border: 'none', background: 'none' }}
                >
                  <Icon name="close" />
                </button>
              </div>
            ))}
            <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
              <input
                type="text"
                value={newKey}
                placeholder={t('builder.reducerKey')}
                aria-label={t('builder.reducerKey')}
                onChange={(e) => setNewKey(e.target.value)}
                style={{ ...selectStyle, flex: 1 }}
              />
              <select
                style={{ ...selectStyle, width: 110 }}
                value={newHow}
                aria-label={t('builder.reducerHow')}
                onChange={(e) => setNewHow(e.target.value)}
              >
                {REDUCER_CHOICES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              <Button
                variant="secondary"
                size="sm"
                type="button"
                disabled={!newKey.trim()}
                onClick={() => {
                  setReducers({ ...(data.reducers || {}), [newKey.trim()]: newHow });
                  setNewKey('');
                }}
              >
                {t('builder.reducerAdd')}
              </Button>
            </div>
            <FieldError id={`${nodeId}-red-err`} text={err('reducers')} />
          </fieldset>
        </>
      )}

      <Button variant="secondary" size="sm" type="button" onClick={makePlain}>
        {t('builder.removeComposite')}
      </Button>
    </div>
  );
}
