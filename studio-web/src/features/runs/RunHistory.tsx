// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useState } from 'react';
import type { RunCheckpoint, RunRecord } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { Spinner } from '../../components/Spinner';
import { canForkCheckpoint, formatCheckpointTime, parseJsonObject } from './durable';
import { useDurableT } from './useDurableT';
import { formatApiError } from '../../api/errors';

interface RunHistoryProps {
  runId: string;
  revision?: number;
  onForked?: (run: RunRecord) => void;
}

export function RunHistory({ runId, revision, onForked }: RunHistoryProps) {
  const { manager } = useApi();
  const t = useDurableT();
  const [items, setItems] = useState<RunCheckpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forking, setForking] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [stateText, setStateText] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await manager.client.runs.history(runId);
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

  // Another run means another list; do not carry a selection over.
  useEffect(() => {
    setSelected(null);
    setStateText('');
  }, [runId]);

  const fork = async (index: number, stateUpdate?: Record<string, unknown>) => {
    setForking(index);
    setError(null);
    try {
      const run = await manager.client.runs.fork(
        runId,
        stateUpdate ? { checkpointIndex: index, stateUpdate } : { checkpointIndex: index }
      );
      onForked?.(run);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setForking(null);
    }
  };

  const parsed = parseJsonObject(stateText, t);
  const selectedCp = selected !== null ? items[selected] : undefined;

  return (
    <div data-testid="tab-run-history" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <a href={`#/traces/${encodeURIComponent(runId)}`} data-testid="run-history-trace-link" style={{ fontSize: 12 }}>
        {t('ui.viewTraceWaterfall')}
      </a>
      {loading && <Spinner size={16} />}
      {error && (
        <div role="alert" style={{ color: 'var(--status-danger)', fontSize: 12 }}>
          {error}
        </div>
      )}
      {!loading && !error && items.length === 0 && (
        <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{t('ui.noCheckpointsRecorded')}</div>
      )}
      {items.map((cp, index) => (
        <div
          key={index}
          data-testid="run-history-row"
          data-selected={selected === index ? 'true' : 'false'}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 10,
            padding: 8,
            border: `1px solid ${selected === index ? 'var(--accent, var(--border-strong, var(--border-subtle)))' : 'var(--border-subtle)'}`,
            borderRadius: 'var(--radius-sm)',
            fontSize: 12,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <span>
              {t('ui.step2') + " "}<strong>{cp.step_count}</strong>
            </span>
            <span>
              {t('ui.node') + " "}<code>{cp.current_node_id ?? 'end'}</code>
            </span>
            <Badge variant="neutral">{cp.status}</Badge>
            <span style={{ color: 'var(--text-secondary)' }} data-testid="run-history-time">
              {formatCheckpointTime(cp.updated_at, t)}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <Button
              variant="secondary"
              size="sm"
              aria-label={`${t('durable.history.viewState', 'View state')} ${index}`}
              onClick={() => setSelected(selected === index ? null : index)}
            >
              {selected === index ? t('durable.history.hideState', 'Hide state') : t('durable.history.viewState', 'View state')}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              loading={forking === index}
              disabled={forking !== null}
              onClick={() => fork(index)}
            >
              {t('ui.forkFromHere')}
            </Button>
          </div>
        </div>
      ))}

      {selectedCp && selected !== null && (
        <div
          data-testid="run-history-detail"
          style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 10, border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)' }}
        >
          <div style={{ fontSize: 12, fontWeight: 600 }}>
            {t('durable.history.stateAt', 'State at checkpoint')} {selected} {t('ui.step3') + " "}{selectedCp.step_count},{' '}
            {selectedCp.current_node_id ?? t('ui.end')})
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {t('durable.history.redacted', 'Shown as the API returns it: secrets are redacted.')}
          </div>
          <pre
            data-testid="run-history-state"
            style={{ margin: 0, padding: 8, borderRadius: 'var(--radius-sm)', backgroundColor: 'var(--bg-canvas)', fontFamily: 'var(--font-mono)', fontSize: 12, overflow: 'auto', maxHeight: 240 }}
          >
            {JSON.stringify(selectedCp.state, null, 2)}
          </pre>
          {canForkCheckpoint(selectedCp) ? (
            <>
              <label htmlFor="fork-state-update" style={{ fontSize: 12, fontWeight: 600 }}>
                {t('durable.fork.label', 'Fork state edits (JSON object, merged over the checkpoint state)')}
              </label>
              <textarea
                id="fork-state-update"
                aria-label={t('ui.forkStateUpdateJson')}
                rows={4}
                value={stateText}
                onChange={(e) => setStateText(e.target.value)}
                placeholder='{ "region": "EU" }'
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 12,
                  padding: 8,
                  borderRadius: 'var(--radius-sm)',
                  border: `1px solid ${parsed.error ? 'var(--status-danger)' : 'var(--border-subtle)'}`,
                  backgroundColor: 'var(--bg-canvas)',
                  color: 'var(--text-primary)',
                }}
              />
              {parsed.error && (
                <div data-testid="fork-state-error" style={{ fontSize: 12, color: 'var(--status-danger)' }}>
                  {parsed.error}
                </div>
              )}
              <div>
                <Button
                  variant="primary"
                  size="sm"
                  loading={forking === selected}
                  disabled={forking !== null || Boolean(parsed.error)}
                  onClick={() => fork(selected, parsed.value)}
                >
                  {t('ui.forkWithEditedState')}
                </Button>
              </div>
            </>
          ) : (
            <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
              {t('durable.fork.atEnd', 'This checkpoint is the end of the run; pick an earlier checkpoint to fork.')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
