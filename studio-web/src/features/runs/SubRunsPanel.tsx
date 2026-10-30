// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useState } from 'react';
import type { RunCheckpoint } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { Badge } from '../../components/Badge';
import { Spinner } from '../../components/Spinner';
import { formatCheckpointTime, subRunLabel } from './durable';
import { SubRunView } from './SubRunView';
import { useDurableT } from './useDurableT';
import { formatApiError } from '../../api/errors';

interface SubRunsPanelProps {
  runId: string;
  revision?: number;
}

/**
 * Child runs started by sub-workflow nodes (GET /runs/{id}/subruns). Each row can expand to its
 * redacted state or open a child view (GET /runs/{id}/subruns/{key}) with a link back to the parent.
 */
export function SubRunsPanel({ runId, revision }: SubRunsPanelProps) {
  const { manager } = useApi();
  const t = useDurableT();
  const [items, setItems] = useState<RunCheckpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [child, setChild] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await manager.client.runs.subruns(runId);
      setItems(res.items);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setLoading(false);
    }
  }, [manager, runId]);

  useEffect(() => {
    void load();
  }, [load, revision]);

  useEffect(() => {
    setChild(null);
  }, [runId]);

  if (child) {
    return (
      <div data-testid="tab-run-subruns">
        <SubRunView runId={runId} childKey={child} onBack={() => setChild(null)} />
      </div>
    );
  }

  return (
    <div data-testid="tab-run-subruns" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {loading && <Spinner size={16} />}
      {error && (
        <div role="alert" style={{ color: 'var(--status-danger)', fontSize: 12 }}>
          {error}
        </div>
      )}
      {!loading && !error && items.length === 0 && (
        <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
          {t('durable.subruns.empty', 'No child runs yet. They appear once a sub-workflow step has started.')}
        </div>
      )}
      {items.map((cp) => {
        const isOpen = open === cp.workflow_run_id;
        return (
          <div
            key={cp.workflow_run_id}
            data-testid="subrun-row"
            style={{ padding: 8, border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)', fontSize: 12 }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              <code data-testid="subrun-id" title={cp.workflow_run_id}>{subRunLabel(cp.workflow_run_id, runId)}</code>
              <Badge variant="neutral">{cp.status}</Badge>
              <span>
                {t('ui.step2') + " "}<strong>{cp.step_count}</strong>
              </span>
              <span>
                {t('ui.node') + " "}<code>{cp.current_node_id ?? 'end'}</code>
              </span>
              <span style={{ color: 'var(--text-secondary)' }}>{formatCheckpointTime(cp.updated_at)}</span>
              {cp.child_key && (
                <button
                  type="button"
                  data-testid="subrun-open"
                  onClick={() => setChild(cp.child_key ?? null)}
                  style={{ marginLeft: 'auto', fontSize: 12, cursor: 'pointer' }}
                >
                  {t('durable.subruns.open', 'Open')}
                </button>
              )}
              <button
                type="button"
                aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : cp.workflow_run_id)}
                style={{ marginLeft: cp.child_key ? 0 : 'auto', fontSize: 12, cursor: 'pointer' }}
              >
                {isOpen ? t('durable.subruns.hide', 'Hide state') : t('durable.subruns.show', 'Show state')}
              </button>
            </div>
            {isOpen && (
              <pre
                data-testid="subrun-state"
                style={{ margin: '8px 0 0', padding: 8, borderRadius: 'var(--radius-sm)', backgroundColor: 'var(--bg-canvas)', fontFamily: 'var(--font-mono)', fontSize: 12, overflow: 'auto', maxHeight: 200 }}
              >
                {JSON.stringify(cp.state, null, 2)}
              </pre>
            )}
          </div>
        );
      })}
    </div>
  );
}
