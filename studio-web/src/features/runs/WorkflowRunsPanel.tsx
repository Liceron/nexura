// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { selectStyle } from '../../components/Input';
import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import type { RunRecord } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { formatApiError } from '../../api/errors';
import { RunStatus } from './RunStatus';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { runHref } from '../../app/routes';
import { RUN_STATUSES, pageRange, runStatusLabel } from './runStatus';

export const WORKFLOW_RUNS_PAGE_SIZE = 20;

const cell: CSSProperties = { padding: '6px 8px', textAlign: 'left', verticalAlign: 'top', fontSize: 12 };

/** Workflow page "Runs" tab: this workflow's runs, newest first, with a status filter. */
export function WorkflowRunsPanel({ workflowId }: { workflowId: string }) {
  const { manager, connection } = useApi();
  const { t, locale } = useI18n();
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  // Only the newest request may update the list: a slower earlier one (another filter or
  // page) must not overwrite it.
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const request = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await manager.client.runs.list({
        workflowId,
        status: status || undefined,
        limit: WORKFLOW_RUNS_PAGE_SIZE,
        offset,
      });
      if (request !== requestRef.current) return;
      setRuns(res.items);
      setTotal(res.total);
      setHasMore(res.has_more);
    } catch (err: unknown) {
      if (request !== requestRef.current) return;
      setRuns([]);
      setTotal(0);
      setHasMore(false);
      setError(formatApiError(err, t));
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }, [manager, workflowId, status, offset]);

  useEffect(() => {
    void load();
  }, [load, reload, connection.workspace]);

  return (
    <Card data-testid="workflow-runs-panel">
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 12 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>{t('runs.title')}</h2>
        <label style={{ fontSize: 13 }}>
          {t('traces.filterStatus')}{' '}
          <select
            style={selectStyle}
            aria-label={t('traces.filterStatus')}
            data-testid="workflow-runs-status"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">{t('traces.statusAll')}</option>
            {RUN_STATUSES.map((s) => (
              <option key={s} value={s}>
                {runStatusLabel(t, s)}
              </option>
            ))}
          </select>
        </label>
        <Button variant="secondary" size="sm" onClick={() => setReload((n) => n + 1)}>
          {t('runs.refresh')}
        </Button>
      </div>

      {loading && (
        <p role="status" style={{ fontSize: 13 }}>
          <Spinner size={14} /> {t('common.loading')}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="workflow-runs-error" style={{ fontSize: 13, color: 'var(--status-danger)' }}>
          {error}
        </p>
      )}
      {!loading && !error && runs.length === 0 && (
        <p data-testid="workflow-runs-empty" style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          {status || offset > 0 ? t('traces.noRuns') : t('runs.noRunsYet')}
        </p>
      )}

      {runs.length > 0 && (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={cell}>{t('traces.status')}</th>
              <th style={cell}>{t('runs.colRun')}</th>
              <th style={cell}>{t('runs.colStarted')}</th>
              <th style={cell}>{t('runs.colSteps')}</th>
              <th style={cell} />
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr
                key={run.run_id}
                data-testid="workflow-run-row"
                data-status={run.status}
                style={{ borderTop: '1px solid var(--border-subtle)' }}
              >
                <td style={cell}>
                  <RunStatus status={run.status} />
                </td>
                <td style={cell}>
                  <code>{run.run_id}</code>
                </td>
                <td style={cell}>{run.created_at ? new Date(run.created_at * 1000).toLocaleString(locale) : '-'}</td>
                <td style={cell}>{run.step_count}</td>
                <td style={cell}>
                  <a href={runHref(run.run_id)} data-testid="workflow-run-open">
                    {t('runs.open')}
                    <span className="studio-sr-only">
                      {' '}
                      {run.run_id}
                    </span>
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {(offset > 0 || hasMore) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12, fontSize: 12 }}>
          <Button
            variant="secondary"
            size="sm"
            disabled={offset === 0 || loading}
            onClick={() => setOffset((o) => Math.max(0, o - WORKFLOW_RUNS_PAGE_SIZE))}
          >
            {t('runs.prev')}
          </Button>
          <span data-testid="workflow-runs-range">{pageRange(t, offset, runs.length, total)}</span>
          <Button
            variant="secondary"
            size="sm"
            disabled={!hasMore || loading}
            onClick={() => setOffset((o) => o + WORKFLOW_RUNS_PAGE_SIZE)}
          >
            {t('runs.next')}
          </Button>
        </div>
      )}
    </Card>
  );
}
