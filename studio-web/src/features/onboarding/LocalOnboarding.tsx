// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useId, useState } from 'react';
import { useApi } from '../../api/context';
import { Button } from '../../components/Button';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';
import { runHref } from '../../app/routes';
import { RunStatus } from '../runs/RunStatus';

const SAMPLE_INPUTS = {
  order_id: 'ORD-DELHI-501',
  ticket_id: 'TICKET-DELHI-501',
};

const localKeylessSpec: WorkflowSpec = {
  name: 'BilingualOrderSupportWorkflow',
  entry_node: 'lookup_order',
  nodes: {
    lookup_order: {
      id: 'lookup_order',
      handler: 'synthetic_order_lookup',
      transitions: {
        default: 'agent_assist',
      },
      default_transition: 'agent_assist',
      requires_approval: false,
    },
    agent_assist: {
      id: 'agent_assist',
      handler: 'synthetic_fake_agent',
      transitions: {},
      requires_approval: false,
    },
  },
};

export function LocalOnboarding() {
  const { manager } = useApi();
  const { t } = useI18n();
  const ids = useId();

  const [isRunning, setIsRunning] = useState(false);
  const [runResult, setRunResult] = useState<Record<string, unknown> | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  const handleRun = async () => {
    setIsRunning(true);
    setRunError(null);
    setRunResult(null);

    try {
      const run = await manager.client.runs.start({
        spec: localKeylessSpec,
        initial_state: { ...SAMPLE_INPUTS },
      });
      setRunResult(run as unknown as Record<string, unknown>);
    } catch (err: unknown) {
      setRunError(formatApiError(err, t));
    } finally {
      setIsRunning(false);
    }
  };

  const runId = runResult && typeof runResult.run_id === 'string' ? runResult.run_id : null;
  const runButton = (
    <Button variant={runId ? 'secondary' : 'primary'} loading={isRunning} onClick={handleRun}>
      {isRunning ? t('onboarding.running') : runResult ? t('onboarding.runAgain') : t('onboarding.runButton')}
    </Button>
  );

  return (
    <PageLayout width="form">
      <PageHeader title={t('onboarding.title')} description={t('onboarding.description')} actions={runButton} />

      <section className="quickstart-section" aria-labelledby={`${ids}-what`}>
        <h2 id={`${ids}-what`}>{t('onboarding.whatRuns')}</h2>
        <ol className="quickstart-steps">
          <li>
            {t('onboarding.stepLookup')} <code>synthetic_order_lookup</code>
          </li>
          <li>
            {t('onboarding.stepReply')} <code>synthetic_fake_agent</code>
          </li>
        </ol>
        <dl className="quickstart-facts" aria-label={t('onboarding.inputs')}>
          <dt><code>order_id</code></dt>
          <dd><code>{SAMPLE_INPUTS.order_id}</code></dd>
          <dt><code>ticket_id</code></dt>
          <dd><code>{SAMPLE_INPUTS.ticket_id}</code></dd>
        </dl>
        <p className="resource-help">{t('onboarding.currencyNote')}</p>
      </section>

      {runError && (
        <div role="alert" className="inline-notice inline-notice--danger" style={{ marginBottom: 24 }}>
          <span>
            <strong>{t('onboarding.runError')}</strong> {runError}
          </span>
        </div>
      )}

      {runResult && (
        <section className="quickstart-section" aria-labelledby={`${ids}-result`}>
          <div className="quickstart-result-head">
            <h2 id={`${ids}-result`}>{t('onboarding.result')}</h2>
            {runId && (
              <a href={runHref(runId)} className="studio-button studio-button--primary studio-link-button">
                {t('runs.open')}
              </a>
            )}
          </div>
          <div className="inspection-meta">
            <span aria-label={t('onboarding.runStatus')} role="status">
              <RunStatus status={String(runResult.status)} />
            </span>
            {runId && (
              <span>
                {t('ui.runId') + ' '}
                <code>{runId}</code>
              </span>
            )}
          </div>
          <h3 id={`${ids}-state`} className="resource-label">{t('onboarding.finalState')}</h3>
          <pre className="inspection-code" aria-labelledby={`${ids}-state`} tabIndex={0} style={{ overflowX: 'auto', whiteSpace: 'pre' }}>
            {JSON.stringify(runResult.current_state, null, 2)}
          </pre>
        </section>
      )}
    </PageLayout>
  );
}
