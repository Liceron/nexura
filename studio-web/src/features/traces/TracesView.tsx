// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import type { RunRecord } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { selectStyle } from '../../components/Input';
import { Card } from '../../components/Card';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { RunTraceView } from './RunTraceView';
import { runHref } from '../../app/routes';
import { RUN_STATUSES, pageRange, runStatusVariant } from '../runs/runStatus';

// Every status the service stores ('' = all), so paused and stopped runs can be found too.
const STATUSES = ['', ...RUN_STATUSES];
export const TRACES_PAGE_SIZE = 50;

export function TracesView({ runId }: { runId?: string | null }) {
  const { manager, connection } = useApi();
  const { t } = useI18n();
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (connection.status !== 'connected' || runId) return;
    let active = true;
    setLoading(true);
    setError(null);
    manager.client.runs
      .list({ status: status || undefined, limit: TRACES_PAGE_SIZE, offset })
      .then((res) => {
        if (!active) return;
        setRuns(res.items);
        setTotal(res.total);
        setHasMore(res.has_more);
      })
      .catch(() => {
        if (active) setError(t('traces.loadError'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [manager, connection.status, status, offset, runId, t]);

  if (runId) {
    return (
      <Card>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 12 }}>
          <Button size="sm" onClick={() => (window.location.hash = '#/traces')}>
            {t('traces.back')}
          </Button>
          <h2 style={{ fontSize: 16, margin: 0 }}>
            {t('traces.title')}: <code data-testid="trace-run-id">{runId}</code>
          </h2>
        </div>
        <RunTraceView runId={runId} showOpenRun />
      </Card>
    );
  }

  return (
    <Card>
      <h2 style={{ fontSize: 16, marginTop: 0 }}>{t('traces.title')}</h2>
      <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{t('traces.subtitle')}</p>
      <label style={{ fontSize: 13 }}>
        {t('traces.filterStatus')}{' '}
        <select
          style={selectStyle}
          aria-label={t('traces.filterStatus')}
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setOffset(0);
          }}
        >
          {STATUSES.map((s) => (
            <option key={s || 'all'} value={s}>
              {s ? t('traces.status_' + s, s) : t('traces.statusAll')}
            </option>
          ))}
        </select>
      </label>
      {loading && (
        <p role="status">
          <Spinner size={14} /> {t('common.loading')}
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      {!loading && !error && runs.length === 0 && <p>{t('traces.noRuns')}</p>}
      <ul aria-label={t('traces.runList')} style={{ listStyle: 'none', padding: 0, display: 'grid', gap: 6 }}>
        {runs.map((run) => (
          <li
            key={run.run_id}
            data-testid="trace-run-row"
            data-status={run.status}
            style={{
              display: 'flex',
              gap: 12,
              alignItems: 'center',
              padding: 8,
              border: '1px solid var(--border-subtle)',
              borderRadius: 'var(--radius-sm)',
              fontSize: 13,
            }}
          >
            <Badge variant={runStatusVariant(run.status)}>{run.status}</Badge>
            <code>{run.run_id}</code>
            <span style={{ color: 'var(--text-secondary)', flex: 1 }}>{run.workflow_id ?? ''}</span>
            <a href={`#/traces/${encodeURIComponent(run.run_id)}`}>
              {t('traces.openTrace')}
              <span className="sr-only" style={{ position: 'absolute', left: -9999 }}>
                {' '}
                {run.run_id}
              </span>
            </a>
            <a href={runHref(run.run_id)} data-testid="trace-run-open">
              {t('runs.open')}
              <span className="sr-only" style={{ position: 'absolute', left: -9999 }}>
                {' '}
                {run.run_id}
              </span>
            </a>
          </li>
        ))}
      </ul>
      {(offset > 0 || hasMore) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12 }}>
          <Button
            size="sm"
            variant="secondary"
            disabled={offset === 0 || loading}
            onClick={() => setOffset((o) => Math.max(0, o - TRACES_PAGE_SIZE))}
          >
            {t('runs.prev')}
          </Button>
          <span data-testid="trace-runs-range">{pageRange(t, offset, runs.length, total)}</span>
          <Button
            size="sm"
            variant="secondary"
            disabled={!hasMore || loading}
            onClick={() => setOffset((o) => o + TRACES_PAGE_SIZE)}
          >
            {t('runs.next')}
          </Button>
        </div>
      )}
    </Card>
  );
}
