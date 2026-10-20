// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState } from 'react';
import { useApi } from '../../api/context';
import { Card } from '../../components/Card';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { useI18n } from '../../i18n';
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';

export function LocalOnboarding() {
  const { manager } = useApi();
  const { t } = useI18n();

  const [isRunning, setIsRunning] = useState(false);
  const [runResult, setRunResult] = useState<Record<string, unknown> | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

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

  const handleRun = async () => {
    setIsRunning(true);
    setRunError(null);
    setRunResult(null);

    try {
      const run = await manager.client.runs.start({
        spec: localKeylessSpec,
        initial_state: {
          order_id: 'ORD-DELHI-501',
          ticket_id: 'TICKET-DELHI-501',
        },
      });
      setRunResult(run as unknown as Record<string, unknown>);
    } catch (err: unknown) {
      setRunError(formatApiError(err, t));
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <Card elevated>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <h2 style={{ fontSize: 18, fontWeight: 600 }}>{t('onboarding.title')}</h2>
              <Badge variant="success">{t('ui.keylessLocalMode')}</Badge>
            </div>
            <p style={{ color: 'var(--text-secondary)', fontSize: 14, maxWidth: 650, lineHeight: 1.6 }}>
              {t('onboarding.description')}
            </p>
            <p style={{ color: 'var(--text-muted)', fontSize: 12, marginTop: 6 }}>
              {t('onboarding.currencyNote')}
            </p>
          </div>
          <Button variant="primary" loading={isRunning} onClick={handleRun}>
            {isRunning ? t('onboarding.running') : t('onboarding.runButton')}
          </Button>
        </div>

        {runError && (
          <div
            style={{
              marginTop: 16,
              padding: 12,
              backgroundColor: 'var(--status-danger-bg)',
              border: '1px solid var(--status-danger)',
              borderRadius: 'var(--radius-md)',
              color: 'var(--status-danger)',
              fontSize: 13,
            }}
          >
            <strong>{t('ui.executionError')}</strong> {runError}
          </div>
        )}

        {runResult && (
          <div
            style={{
              marginTop: 20,
              padding: 16,
              backgroundColor: 'var(--bg-canvas)',
              border: '1px solid var(--border-subtle)',
              borderRadius: 'var(--radius-md)',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ color: 'var(--status-success)', fontWeight: 600 }}><Icon name="check" /></span>
                <span style={{ fontWeight: 600, fontSize: 14 }}>{t('onboarding.success')}</span>
                <Badge variant="neutral">{t('ui.runId') + " "}{String(runResult.run_id)}</Badge>
              </div>
              <Badge variant="info">{t('ui.status2') + " "}{String(runResult.status)}</Badge>
            </div>

            <div style={{ fontSize: 13, marginBottom: 8, color: 'var(--text-secondary)' }}>
              {t('onboarding.inspectPrompt')}
            </div>

            <pre
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                backgroundColor: 'var(--bg-surface)',
                padding: 12,
                borderRadius: 'var(--radius-sm)',
                overflowX: 'auto',
                border: '1px solid var(--border-subtle)',
                color: 'var(--text-primary)',
              }}
            >
              {JSON.stringify(runResult.current_state, null, 2)}
            </pre>
          </div>
        )}
      </Card>
    </div>
  );
}
