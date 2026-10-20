// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useCallback, useEffect, useState, type CSSProperties } from 'react';
import { useApi } from '../../api/context';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { useI18n } from '../../i18n';
import type { CaseStatus, StudioDatasetSummary } from './studioEvalsTypes';
import { caseRows, listWorkflowTestDatasets, runWorkflowTests, useCanEdit, type WorkflowTestOutcome } from './workflowTests';
import { formatApiError } from '../../api/errors';
import { runHref } from '../../app/routes';

const cell: CSSProperties = { padding: '6px 8px', textAlign: 'left', verticalAlign: 'top', fontSize: 12 };

function statusVariant(s: CaseStatus): 'success' | 'danger' | 'warning' {
  return s === 'passed' ? 'success' : s === 'paused' ? 'warning' : 'danger';
}

interface WorkflowTestsPanelProps {
  workflowId: string;
  /** Bumped by the parent when the workflow changes, to reload the list. */
  revision?: number;
}

/** Workflow page "Tests" tab: the workflow's test datasets, run them, regression verdict. */
export function WorkflowTestsPanel({ workflowId, revision }: WorkflowTestsPanelProps) {
  const { manager, connection } = useApi();
  const { t } = useI18n();
  const canRun = useCanEdit();
  const [datasets, setDatasets] = useState<StudioDatasetSummary[] | null>(null);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<WorkflowTestOutcome | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setDatasets(await listWorkflowTestDatasets(manager.client, workflowId));
    } catch (err) {
      setDatasets([]);
      setError(formatApiError(err, t));
    }
  }, [manager, workflowId]);

  useEffect(() => {
    setOutcome(null);
    void load();
  }, [load, revision, connection.workspace]);

  const run = async (datasetId: string) => {
    setRunningId(datasetId);
    setError(null);
    setOutcome(null);
    try {
      setOutcome(await runWorkflowTests(manager.client, workflowId, datasetId));
      await load();
    } catch (err) {
      setError(formatApiError(err, t));
    } finally {
      setRunningId(null);
    }
  };

  const statusLabel = (s: CaseStatus) => t(`evalsStudio.${s}`, s);
  const rows = outcome ? caseRows(outcome.run) : [];
  const summary = outcome?.run.summary;
  const gate = outcome?.comparison?.gate;

  return (
    <div data-testid="workflow-tests-panel" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <Card>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: '0 0 4px 0' }}>{t('workflowTests.title')}</h2>
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: 0 }}>{t('workflowTests.subtitle')}</p>
        {!canRun && (
          <p data-testid="workflow-tests-viewer-note" style={{ fontSize: 12, color: 'var(--text-muted)', margin: '8px 0 0 0' }}>
            {t('workflowTests.viewerNote')}
          </p>
        )}
      </Card>

      {error && (
        <div data-testid="workflow-tests-error" role="alert" style={{ fontSize: 13, color: 'var(--status-danger)' }}>
          {error}
        </div>
      )}

      <Card>
        {datasets === null ? (
          <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{t('common.loading')}</div>
        ) : datasets.length === 0 ? (
          <div data-testid="workflow-tests-empty" style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('workflowTests.empty')}
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={cell}>{t('workflowTests.dataset')}</th>
                <th style={cell}>{t('workflowTests.colCases')}</th>
                <th style={cell}>{t('workflowTests.colRevision')}</th>
                <th style={cell} />
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => (
                <tr key={d.dataset_id} data-testid={`workflow-tests-dataset-${d.dataset_id}`}>
                  <td style={cell}>
                    <strong>{d.name || d.dataset_id}</strong>{' '}
                    <span style={{ color: 'var(--text-muted)' }}>({d.dataset_id})</span>
                  </td>
                  <td style={cell} data-testid={`workflow-tests-count-${d.dataset_id}`}>
                    {d.case_count}
                  </td>
                  <td style={cell}>{d.revision}</td>
                  <td style={cell}>
                    <Button
                      data-testid={`workflow-tests-run-${d.dataset_id}`}
                      variant="primary"
                      size="sm"
                      disabled={!canRun || runningId !== null}
                      loading={runningId === d.dataset_id}
                      title={canRun ? undefined : t('workflowTests.viewerNote')}
                      onClick={() => void run(d.dataset_id)}
                    >
                      {runningId === d.dataset_id ? t('workflowTests.running') : t('workflowTests.runTests')}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {outcome && summary && (
        <Card data-testid="workflow-tests-results">
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center', marginBottom: 10, fontSize: 13 }}>
            <strong>{outcome.dataset.name || outcome.dataset.dataset_id}</strong>
            <span>
              {t('workflowTests.ranAgainst')}: {outcome.run.target.version ?? '-'}
            </span>
            <span data-testid="workflow-tests-pass-rate">
              {t('evalsStudio.passRate')}: {(summary.pass_rate * 100).toFixed(1)}% ({summary.passed}/{summary.total})
            </span>
            <span style={{ color: 'var(--text-muted)' }}>
              {t('workflowTests.outputKeyUsed')}: {outcome.outputKey ?? t('workflowTests.wholeState')}
            </span>
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse', marginBottom: 12 }}>
            <thead>
              <tr>
                <th style={cell}>{t('evalsStudio.colCase')}</th>
                <th style={cell}>{t('evalsStudio.colStatus')}</th>
                <th style={cell}>{t('workflowTests.colReasons')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={`${r.name}:${r.repeat}`}
                  data-testid={`workflow-tests-case-${r.name}`}
                  data-status={r.status}
                >
                  <td style={cell}>
                    {r.name}
                    {r.sourceRunId && (
                      <div>
                        <a href={runHref(r.sourceRunId)} data-testid="workflow-tests-source-run">
                          {t('runs.openSourceRun')}
                        </a>
                      </div>
                    )}
                  </td>
                  <td style={cell}>
                    <Badge variant={statusVariant(r.status)}>{statusLabel(r.status)}</Badge>
                  </td>
                  <td style={{ ...cell, whiteSpace: 'pre-wrap' }}>{r.reasons.join('\n') || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {gate ? (
            <div
              data-testid="workflow-tests-gate"
              data-passed={String(gate.passed)}
              style={{ fontSize: 13, display: 'flex', flexDirection: 'column', gap: 4 }}
            >
              <div>
                <Badge variant={gate.passed ? 'success' : 'danger'}>
                  {gate.passed ? t('workflowTests.gatePass') : t('workflowTests.gateFail')}
                </Badge>{' '}
                {t('workflowTests.comparedWith')}: {outcome.previous?.run_id}
                {outcome.previous?.target.version ? ` (${outcome.previous.target.version})` : ''}
              </div>
              <div data-testid="workflow-tests-changes">
                {t('evalsStudio.regressions')}: {outcome.comparison?.regressions.length ?? 0},{' '}
                {t('evalsStudio.improvements')}: {outcome.comparison?.improvements.length ?? 0}
              </div>
              {gate.reasons.length > 0 && <div>{gate.reasons.join('; ')}</div>}
              {(outcome.comparison?.regressions ?? []).map((c, i) => (
                <div key={`${c.case}-${c.scorer}-${i}`} style={{ color: 'var(--status-danger)', fontSize: 12 }}>
                  {c.case}
                  {c.scorer ? ` / ${c.scorer}` : ''}: {String(c.baseline)} <Icon name="right" /> {String(c.candidate)}
                  {c.detail ? ` (${c.detail})` : ''}
                </div>
              ))}
            </div>
          ) : (
            <div data-testid="workflow-tests-no-baseline" style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
              {t('workflowTests.noBaseline')}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
