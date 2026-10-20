// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useCallback, useEffect, useState } from 'react';
import { NexuraApiError } from '@liceron/nexura-sdk';
import type { RunRecord } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { formatApiError } from '../../api/errors';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { Spinner } from '../../components/Spinner';
import { useI18n, type Translate } from '../../i18n';
import { runHref } from '../../app/routes';
import { RunVisualizer } from './RunVisualizer';

type Load =
  | { kind: 'loading' }
  | { kind: 'ok'; run: RunRecord }
  | { kind: 'notFound' }
  | { kind: 'denied' }
  | { kind: 'error'; message: string };

function classify(err: unknown, t: Translate): Load {
  const status = err instanceof NexuraApiError ? err.statusCode : null;
  const raw = err instanceof Error ? err.message : String(err);
  if (status === 404 || raw.includes('RUN_NOT_FOUND')) return { kind: 'notFound' };
  if (status === 403 || raw.includes('CROSS_WORKSPACE_ACCESS_DENIED') || raw.includes('RESOURCE_SCOPE_DENIED')) {
    return { kind: 'denied' };
  }
  return { kind: 'error', message: formatApiError(err, t) };
}

/**
 * Standalone run page (`#/runs/{id}`): the full Run Console for one run, reachable after a
 * reload and for runs started by a trigger, the API or another user.
 */
export function RunPage({ runId }: { runId: string }) {
  const { manager, connection } = useApi();
  const { t } = useI18n();
  const [load, setLoad] = useState<Load>({ kind: 'loading' });

  const fetchRun = useCallback(async () => {
    setLoad({ kind: 'loading' });
    try {
      setLoad({ kind: 'ok', run: await manager.client.runs.get(runId) });
    } catch (err: unknown) {
      setLoad(classify(err, t));
    }
  }, [manager, runId, t]);

  useEffect(() => {
    void fetchRun();
  }, [fetchRun, connection.workspace]);

  const workflowId = load.kind === 'ok' ? load.run.workflow_id ?? null : null;
  const leave = () => {
    window.location.hash = workflowId ? `#/workflows/${workflowId}` : '#/traces';
  };

  if (load.kind === 'loading') {
    return (
      <div role="status" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 60, gap: 10 }}>
        <Spinner size={24} />
        <span style={{ color: 'var(--text-secondary)' }}>{t('common.loading')}</span>
      </div>
    );
  }

  if (load.kind === 'notFound' || load.kind === 'denied' || load.kind === 'error') {
    const title =
      load.kind === 'notFound' ? t('runs.notFoundTitle') : load.kind === 'denied' ? t('runs.deniedTitle') : t('common.error');
    const message =
      load.kind === 'notFound' ? t('runs.notFoundMessage') : load.kind === 'denied' ? t('runs.deniedMessage') : load.message;
    return (
      <Card
        elevated
        role="alert"
        data-testid={`run-page-${load.kind}`}
        style={{ maxWidth: 600, margin: '40px auto', textAlign: 'center', padding: 32 }}
      >
        <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>{title}</h2>
        <p style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 8 }}>{message}</p>
        <p style={{ fontSize: 12, marginBottom: 20 }}>
          <code>{runId}</code>
        </p>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
          <Button variant="secondary" onClick={() => (window.location.hash = '#/workflows')}>
            {t('workflows.backToWorkflows')}
          </Button>
          {load.kind === 'error' && (
            <Button variant="primary" onClick={() => void fetchRun()}>
              {t('runs.retryLoad')}
            </Button>
          )}
        </div>
      </Card>
    );
  }

  return (
    <div data-testid="run-page" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', fontSize: 13 }}>
        {workflowId ? (
          <a href={`#/workflows/${workflowId}`} data-testid="run-page-workflow-link">
            <Icon name="left" /> {t('runs.backToWorkflow')} <code>{workflowId}</code>
          </a>
        ) : (
          <a href="#/traces" data-testid="run-page-runs-link">
            <Icon name="left" /> {t('traces.back')}
          </a>
        )}
        <a href={`#/traces/${encodeURIComponent(runId)}`}>{t('traces.openTrace')}</a>
      </div>
      <RunVisualizer
        runId={runId}
        onClose={leave}
        onSelectRun={(id) => {
          window.location.hash = runHref(id);
        }}
      />
    </div>
  );
}
