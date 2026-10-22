// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import type { RunRecord } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { formatApiError } from '../../api/errors';
import { RunStatus } from '../runs/RunStatus';

import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { runHref } from '../../app/routes';
import { describeInterrupt } from '../runs/durable';

export const WAITING_FOR_INPUT_LIMIT = 50;

/**
 * Approval Inbox section: runs paused by interrupt() for an answer. The answer is given on the
 * run page (PendingInputBanner); the sidebar badge still counts approvals only.
 */
export function WaitingForInputList({ reloadKey = 0 }: { reloadKey?: number }) {
  const { manager, connection } = useApi();
  const { t } = useI18n();
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    manager.client.runs
      .list({ status: 'waiting_for_input', limit: WAITING_FOR_INPUT_LIMIT })
      .then((res) => {
        if (!active) return;
        setRuns(res.items);
        setTotal(res.total);
      })
      .catch((err: unknown) => {
        if (active) setError(formatApiError(err, t));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [manager, reloadKey, connection.workspace]);

  return (
    <section aria-label={t('runs.waitingTitle')} data-testid="waiting-input-section" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>{t('runs.waitingTitle')}</h2>
        <span data-testid="waiting-input-count">{total}</span>
      </div>
      <p style={{ color: 'var(--text-secondary)', fontSize: 13, margin: 0 }}>{t('runs.waitingSubtitle')}</p>
      {loading && runs.length === 0 && (
        <p role="status" style={{ fontSize: 13 }}>
          <Spinner size={14} /> {t('common.loading')}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="waiting-input-error" style={{ fontSize: 13, color: 'var(--status-danger)' }}>
          {error}
        </p>
      )}
      {!loading && !error && runs.length === 0 && (
        <p data-testid="waiting-input-empty" style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          {t('runs.waitingEmpty')}
        </p>
      )}
      {runs.map((run) => {
        const { question } = describeInterrupt(run.pending_input?.value);
        return (
          <div key={run.run_id} data-testid="waiting-input-row" style={{ padding: '12px 0', borderBottom: '1px solid var(--border-subtle)' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  <RunStatus status="waiting_for_input" />
                  <code>{run.run_id}</code>
                  {run.workflow_id && (
                    <span style={{ color: 'var(--text-secondary)' }}>
                      {t('runs.workflow')}: <strong>{run.workflow_id}</strong>
                    </span>
                  )}
                  {run.pending_input?.node_id && (
                    <span style={{ color: 'var(--text-secondary)' }}>
                      {t('runs.node')}: <strong>{run.pending_input.node_id}</strong>
                    </span>
                  )}
                </div>
                {question && <div data-testid="waiting-input-question">{question}</div>}
              </div>
              <a href={runHref(run.run_id)} data-testid="waiting-input-open">
                {t('runs.openToAnswer')}
                <span className="studio-sr-only">
                  {' '}
                  {run.run_id}
                </span>
              </a>
            </div>
          </div>
        );
      })}
      {total > runs.length && (
        <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: 0 }}>
          {t('runs.waitingMore').replace('{shown}', String(runs.length)).replace('{total}', String(total))}{' '}
          <a href="#/traces">{t('traces.title')}</a>
        </p>
      )}
    </section>
  );
}
