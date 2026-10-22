// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import type { RunRecord } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { RunStatus } from '../runs/RunStatus';
import { Button } from '../../components/Button';
import { selectStyle } from '../../components/Input';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { RunTraceView } from './RunTraceView';
import { runHref } from '../../app/routes';
import { RUN_STATUSES, pageRange } from '../runs/runStatus';

// Every status the service stores ('' = all), so paused and stopped runs can be found too.
const STATUSES = ['', ...RUN_STATUSES];
export const TRACES_PAGE_SIZE = 50;

export function TracesView({ runId }: { runId?: string | null }) {
  const { manager, connection } = useApi();
  const { t, locale } = useI18n();
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
      <PageLayout width="full">
        <PageHeader title={t('traces.title')} metadata={<code data-testid="trace-run-id">{runId}</code>}
          breadcrumbs={<a href="#/traces">{t('traces.back')}</a>} />
        <RunTraceView runId={runId} showOpenRun />
      </PageLayout>
    );
  }

  return (
    <PageLayout width="full">
      <PageHeader title={t('traces.title')} />
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
      <div className="inspection-table-scroll" role="region" aria-label={t('traces.runList')} tabIndex={0}>
        <table className="inspection-table"><thead><tr>
          <th>{t('exactAction.run')}</th><th>{t('runs.workflow')}</th><th>{t('traces.status')}</th><th>{t('reviewDesign.updated')}</th><th>{t('traces.openTrace')}</th>
        </tr></thead><tbody>
          {runs.map(run => <tr key={run.run_id} data-testid="trace-run-row" data-status={run.status}>
            <td><code>{run.run_id}</code></td><td>{run.workflow_id || t('exactAction.notRecorded')}</td>
            <td><RunStatus status={run.status} /></td>
            <td>{new Date(run.updated_at * 1000).toLocaleString(locale)}</td>
            <td><div className="inspection-actions">
              <a href={'#/traces/' + encodeURIComponent(run.run_id)}>{t('traces.openTrace')}<span className="studio-sr-only"> {run.run_id}</span></a>
              <a href={runHref(run.run_id)} data-testid="trace-run-open">{t('runs.open')}<span className="studio-sr-only"> {run.run_id}</span></a>
            </div></td>
          </tr>)}
        </tbody></table>
      </div>
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
    </PageLayout>
  );
}
