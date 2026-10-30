// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import type { SubRunDetail } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { Badge } from '../../components/Badge';
import { Spinner } from '../../components/Spinner';
import { describeSubRun } from './durable';
import { useDurableT } from './useDurableT';
import { formatApiError } from '../../api/errors';

interface SubRunViewProps {
  runId: string;
  childKey: string;
  onBack: () => void;
}

/** One child run: status and a timeline of its checkpoints (GET /runs/{id}/subruns/{key}). */
export function SubRunView({ runId, childKey, onBack }: SubRunViewProps) {
  const { manager } = useApi();
  const t = useDurableT();
  const [detail, setDetail] = useState<SubRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    manager.client.runs
      .subrun(runId, childKey)
      .then((d) => alive && setDetail(d))
      .catch((err: unknown) => alive && setError(formatApiError(err, t)));
    return () => {
      alive = false;
    };
  }, [manager, runId, childKey]);

  const view = detail ? describeSubRun(detail) : null;
  return (
    <div data-testid="subrun-view" style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: 12 }}>
      <button type="button" data-testid="subrun-back" onClick={onBack} style={{ alignSelf: 'flex-start', cursor: 'pointer', fontSize: 12 }}>
        {t('durable.subruns.back', 'Back to parent run')} <code>{runId}</code>
      </button>
      {!view && !error && <Spinner size={16} />}
      {error && (
        <div role="alert" style={{ color: 'var(--status-danger)' }}>
          {error}
        </div>
      )}
      {view && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <code data-testid="subrun-view-id">{view.label}</code>
            <Badge variant="neutral">{view.status}</Badge>
            <span>
              {t('ui.step2') + " "}<strong>{view.stepCount}</strong>
            </span>
            <span>
              {t('ui.node') + " "}<code>{view.currentNode}</code>
            </span>
          </div>
          {view.error && (
            <div role="alert" style={{ color: 'var(--status-danger)' }}>
              {view.error}
            </div>
          )}
          <ol data-testid="subrun-steps" style={{ margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {view.steps.map((s) => (
              <li key={s.key} data-testid="subrun-step">
                <code>{s.node}</code> <Badge variant="neutral">{s.status}</Badge> {t('ui.step4') + " "}{s.step}{' '}
                <span style={{ color: 'var(--text-secondary)' }}>{s.time}</span>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}
