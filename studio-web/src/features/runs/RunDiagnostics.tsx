// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useT } from '../../i18n';
import { useEffect, useState } from 'react';
import type { NexuraClient, RunDiagnostics as Diagnostics } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { Button } from '../../components/Button';

export function RunDiagnostics({ runId, revision }: { runId: string; revision: number }) {
  const t = useT();
  const { manager, connection } = useApi();
  const [loaded, setLoaded] = useState<{ client: NexuraClient; identity: string; data: Diagnostics } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [includeContent, setIncludeContent] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const identity = `${connection.baseUrl}|${connection.workspace}|${connection.principal}|${connection.status}|${runId}`;
  const scopedClient = manager.client;
  const record = loaded?.identity === identity && loaded.client === scopedClient ? loaded.data : null;

  // Reset opt-in content and stale trace state when the identity changes.
  useEffect(() => {
    setIncludeContent(false);
    setExpanded(null);
  }, [identity]);

  useEffect(() => {
    setLoaded(null);
    setError(null);
    if (connection.status !== 'connected') {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    const scopeSignal = manager.abortSignal;
    const abort = () => {
      controller.abort();
      setLoaded(null);
    };
    scopeSignal.addEventListener('abort', abort, { once: true });
    if (scopeSignal.aborted) controller.abort();
    let active = true;
    setLoading(true);
    scopedClient.runs.diagnostics(runId, {
      includeContent, signal: controller.signal,
    }).then((data) => {
      if (active && !controller.signal.aborted) setLoaded({ client: scopedClient, identity, data });
    }).catch(() => {
      if (active && !controller.signal.aborted) setError(t('diagnostics.loadError'));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => {
      active = false;
      scopeSignal.removeEventListener('abort', abort);
      controller.abort();
    };
  }, [manager, scopedClient, identity, connection.status, runId, includeContent, revision, refresh, t]);

  const download = () => {
    if (!record?.trace_export) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(record.trace_export, null, 2)], {
      type: 'application/json',
    }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `trace-${runId.replace(/[^a-zA-Z0-9_-]/g, '_')}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section aria-label={t('ui.sdkRunDiagnostics')} style={{ display: 'grid', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
        <Button size="sm" onClick={() => setRefresh((value) => value + 1)}>{t('ui.refreshDiagnostics')}</Button>
        <Button size="sm" disabled={!record?.trace_export} onClick={download}>{t('ui.downloadTraceJson')}</Button>
        <label>
          <input type="checkbox" checked={includeContent} onChange={(event) => setIncludeContent(event.target.checked)} />
          {' '}{t('ui.includeRedactedInputsAndOutputs')}
        </label>
      </div>
      <p style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)', margin: 0 }}>
        {t('ui.basedOnPersistedSdkEventsContentIsHiddenByDefaultRedactionIsNotAPersonalDat')}
      </p>
      {loading && <p role="status">{t('ui.loadingDiagnostics')}</p>}
      {error && <p role="alert">{error}</p>}
      {record && <>
        <div aria-label={t('ui.runUsageSummary')}>
          <strong>{t('ui.recordedModuleTokenTally')}</strong> {record.usage.known_input_tokens} {t('ui.input3') + " "}{record.usage.known_output_tokens} {t('ui.output3')}{' '}{t('diagnostics.reportedUsage').replace('{reported}', String(record.usage.reported_attempts)).replace('{total}', String(record.attempt_count))}{!record.usage.complete && " " + t('ui.usageIsIncompleteMissingValuesAreUnknownNotFree')}
          <p>
            {record.usage.calculated_cost_usd != null
              ? t('diagnostics.costEstimate').replace('{cost}', String(record.usage.calculated_cost_usd))
              : t('ui.costNotReportedNoProviderBillingOrSavingsEstimateIsAvailable')}
          </p>
          {record.usage.successful_task_cost_usd != null && (
            <p>{t('diagnostics.successCost').replace('{cost}', String(record.usage.successful_task_cost_usd)).replace('{successes}', String(record.usage.task_success_count ?? 0)).replace('{total}', String(record.usage.cohort_attempt_count ?? record.attempt_count))}</p>
          )}
          {record.usage.successful_task_cost_status === 'undefined_zero_successes' && (
            <p>{t('ui.successfulTaskCostUndefined0SuccessesInCohort')}</p>
          )}
          <p style={{ fontSize: 'var(--type-meta)' }}>{t('ui.moduleRecordsMayContainAggregateUsageThisTallyIsNotADeduplicatedProviderBill')}</p>
          {record.context_decisions && record.context_decisions.length > 0 && (
            <p style={{ fontSize: 'var(--type-meta)', fontWeight: 500 }}>
              {t('ui.contextSelectionActiveFor') + " "}{record.context_decisions.length} {t('ui.moduleCalls')}
            </p>
          )}
        </div>
        {!record.trace_export && <p>{record.trace_export_unavailable_reason}</p>}
        {record.truncated_attempts > 0 && <p>{t('ui.showingTheLatest200Attempts') + " "}{record.truncated_attempts} {t('ui.omitted')}</p>}
        {record.attempts.length === 0 && <p>{t('ui.noNodeDiagnosticsHaveBeenRecordedYet')}</p>}
        {record.attempts.map((attempt) => <article key={attempt.id} style={{ borderBottom: '1px solid var(--border-subtle)', padding: '12px 0' }}>
          <button type="button" aria-expanded={expanded === attempt.id} onClick={() => setExpanded(expanded === attempt.id ? null : attempt.id)}>
            {attempt.node_id} — {t(`statusLabels.${attempt.status}`, attempt.status)}
          </button>
          <p style={{ margin: '6px 0', fontSize: 'var(--type-meta)' }}>
            {attempt.module_id || t('ui.legacyHandler')} · {attempt.duration_ms === null ? t('ui.timingUnavailable') : `${attempt.duration_ms} ms`}
            {' '}{t('ui.tokens2') + " "}{attempt.input_tokens ?? t('ui.unknown')} {t('ui.input3') + " "}{attempt.output_tokens ?? t('ui.unknown')} {t('ui.output')}
          </p>
          {attempt.identity_source === 'legacy_handler' && <small>{t('ui.identityIsTheRecordedHandlerLabel')}</small>}
          {attempt.context_decision && (
            <div style={{ marginTop: 6, padding: '4px 8px', background: 'var(--bg-surface-elevated)', borderRadius: 4, fontSize: 'var(--type-meta)' }}>
              <span style={{ fontWeight: 600 }}>{t('ui.contextSelection')}</span>{' '}
              {t(`statusLabels.${attempt.context_decision.status}`, attempt.context_decision.status)} ({attempt.context_decision.initial_tokens} <Icon name="right" /> {attempt.context_decision.projected_tokens} {t('ui.tokens3')}{attempt.context_decision.reduction_ratio > 0 ? t('diagnostics.reduction').replace('{percent}', String(Math.round(attempt.context_decision.reduction_ratio * 100))) : ''})
              {' · '}{t('ui.policyV')}{attempt.context_decision.policy_version}
            </div>
          )}
          {expanded === attempt.id && <>
            {attempt.error && <p role="alert">{attempt.error}</p>}
            {attempt.context_decision && (
              <div style={{ marginTop: 10, borderTop: '1px dashed var(--border-subtle)', paddingTop: 8 }}>
                <h4>{t('ui.contextSelectionDecisions')}</h4>
                <p style={{ fontSize: 'var(--type-meta)', margin: '4px 0' }}>
                  {t('ui.status2') + " "}<strong>{t(`statusLabels.${attempt.context_decision.status}`, attempt.context_decision.status)}</strong> · {attempt.context_decision.items.length} {t('ui.itemsEvaluated')}
                </p>
                {attempt.context_decision.overflow_error && (
                  <p role="alert" style={{ color: 'var(--status-danger)', fontSize: 'var(--type-meta)' }}>{attempt.context_decision.overflow_error}</p>
                )}
                <ul style={{ fontSize: 'var(--type-meta)', paddingLeft: 18, margin: '4px 0' }}>
                  {attempt.context_decision.items.map((it, idx) => (
                    <li key={idx} style={{ margin: '2px 0' }}>
                      <code>{it.item_id}</code>: <strong>{t(`statusLabels.${it.decision}`, it.decision)}</strong> ({t(`statusLabels.${it.reason}`, it.reason)})
                      {it.is_protected ? " " + t('ui.protected') : ''} — {it.original_tokens} <Icon name="right" /> {it.projected_tokens} {t('ui.tokens3')}{includeContent && it.content_preview ? ` — "${it.content_preview}"` : ''}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {includeContent ? <>
              <h4>{t('ui.recordedInput')}</h4><pre className="inspection-code">{JSON.stringify(attempt.input, null, 2)}</pre>
              <h4>{t('ui.recordedOutput')}</h4><pre className="inspection-code">{JSON.stringify(attempt.output, null, 2)}</pre>
            </> : <p>{t('ui.enableContentInspectionToViewRedactedInputsAndOutputs')}</p>}
          </>}
        </article>)}
      </>}
    </section>
  );
}
