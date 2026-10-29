// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useAfterRender } from '../../app/useAfterRender';
import { InlineConfirm } from '../../components/InlineConfirm';
import { useId, useRef, useState } from 'react';
import { useEvaluations } from './useEvaluations';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { WorkflowTabs } from '../../components/WorkflowTabs';
import { useI18n } from '../../i18n';
import { useCompactLayout } from '../../app/useCompactLayout';
import { StudioEvalsPanel } from './StudioEvalsPanel';

export function EvaluationsView({ agentId = '' }: { agentId?: string } = {}) {
  const { t } = useI18n();
  const {
    datasets,
    selectedDatasetId,
    setSelectedDatasetId,
    selectedDataset,
    runs,
    selectedRun,
    runLoading,
    handleOpenRun,
    comparison,
    loading,
    actionLoading,
    error,
    formError,
    setFormError,
    evalDatasetId,
    setEvalDatasetId,
    evalModelId,
    setEvalModelId,
    selectedGraders,
    toggleGrader,
    baseRunId,
    setBaseRunId,
    candidateRunId,
    setCandidateRunId,
    newDatasetId,
    setNewDatasetId,
    newVersion,
    setNewVersion,
    newDescription,
    setNewDescription,
    newExamplesJson,
    setNewExamplesJson,
    handleCreateDataset,
    handleDeleteDataset,
    handleDeleteRun,
    handleRunEvaluation,
    handleCompareRuns,
    refreshAll,
  } = useEvaluations();

  const [activeTab, setActiveTab] = useState<'datasets' | 'run' | 'compare' | 'studio'>('datasets');
  const [createModalOpen, setCreateModalOpen] = useState(false);

  const availableGraders = [
    { id: 'exact_match', name: t('evaluationGraders.label0'), desc: t('evaluationGraders.label1') },
    { id: 'substring', name: t('evaluationGraders.label2'), desc: t('evaluationGraders.label3') },
    { id: 'citation_coverage', name: t('evaluationGraders.label4'), desc: t('evaluationGraders.label5') },
  ];

  const ids = useId();
  const panelId = `${ids}-panel`;
  const compact = useCompactLayout();
  const [phonePane, setPhonePane] = useState<'list' | 'detail'>('list');
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const afterRender = useAfterRender();
  const listRef = useRef<HTMLUListElement>(null);

  const tabs = [
    { id: 'datasets', label: `${t('ui.registeredEvaluationDatasets')} (${datasets.length})` },
    { id: 'run', label: t('ui.runBenchmark') },
    { id: 'compare', label: `${t('evaluations.compareTab')} (${runs.length})` },
    { id: 'studio', label: t('evalsStudio.title'), testId: 'tab-studio-evals' },
  ];
  const signed = (value: number, digits: number, suffix = '') => `${value >= 0 ? '+' : ''}${value.toFixed(digits)}${suffix}`;
  const meanLatency = selectedRun
    ? selectedRun.mean_latency_ms ??
      (selectedRun.total_examples ? (selectedRun as any).total_latency_ms / selectedRun.total_examples : null)
    : null;

  // Benchmark evaluations have model targets; saved-agent evaluations use Studio tests.
  if (agentId) return <PageLayout data-testid="agent-evaluations-view"><PageHeader title={`Evaluations for ${agentId}`} description="Studio tests for this saved agent. Benchmark datasets are shared across the workspace." /><StudioEvalsPanel key={agentId} agentId={agentId} /></PageLayout>;

  return (
    <PageLayout data-testid="evaluations-view">
      <PageHeader
        title={t('evaluations.title')}
        description={t('evaluations.subtitle')}
        actions={
          <Button variant="ghost" onClick={refreshAll} loading={loading} disabled={actionLoading}>
            {t('ui.refresh')}
          </Button>
        }
      />

      {error && (
        <div role="alert" className="inline-notice inline-notice--danger" style={{ marginBottom: 16 }}>
          <span>{error}</span>
        </div>
      )}

      <WorkflowTabs tabs={tabs} active={activeTab} onChange={(id) => setActiveTab(id as typeof activeTab)} label={t('evaluations.tabsLabel')} panelId={panelId} />

      <div role="tabpanel" id={panelId} aria-label={tabs.find((tab) => tab.id === activeTab)?.label} tabIndex={0} style={{ minWidth: 0 }}>
        {activeTab === 'studio' && <StudioEvalsPanel />}

        {activeTab === 'datasets' && (
          <>
            <div className="knowledge-toolbar">
              <Button
                variant="primary"
                data-testid="btn-open-create-dataset"
                onClick={() => {
                  setFormError(null);
                  setCreateModalOpen(true);
                }}
              >
                {t('ui.uploadCustomDataset')}
              </Button>
            </div>
            <div className={`studio-list-detail resource-layout--${phonePane === 'detail' && selectedDataset ? 'detail' : 'browse'}`}>
              <div className="resource-list-pane">
                <ul ref={listRef} className="resource-list" aria-label={t('ui.registeredEvaluationDatasets')}>
                  {datasets.map((ds) => (
                    <li key={ds.dataset_id}>
                      <button
                        type="button"
                        className="resource-row"
                        data-testid={`dataset-item-${ds.dataset_id}`}
                        aria-pressed={selectedDatasetId === ds.dataset_id}
                        onClick={() => {
                          setSelectedDatasetId(ds.dataset_id);
                          setPhonePane('detail');
                          if (compact) afterRender(() => detailHeadingRef.current?.focus());
                        }}
                      >
                        <code className="resource-row-name">{ds.dataset_id}</code>
                        <span className="resource-row-description">{ds.description || t('ui.noDescriptionProvided')}</span>
                        <span className="resource-row-meta">
                          <span>{ds.is_builtin ? t('ui.builtInFixture') : t('ui.custom')}</span>
                          <span>v{ds.version}</span>
                          <span>{ds.example_count} {t('ui.examples')}</span>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
              <div className="resource-detail">
                {selectedDataset ? (
                  <>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="resource-back"
                      onClick={() => {
                        setPhonePane('list');
                        afterRender(() => listRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus());
                      }}
                    >
                      {t('evaluations.back')}
                    </Button>
                    <div className="resource-detail-header" style={{ marginBottom: 4 }}>
                      <h2 ref={detailHeadingRef} tabIndex={-1}><code>{selectedDataset.dataset_id}</code></h2>
                      {!(selectedDataset.is_builtin || datasets.find((d) => d.dataset_id === selectedDataset.dataset_id)?.is_builtin) && (
                        <InlineConfirm key={selectedDataset.dataset_id} consequence={t('confirmation.deleteDataset')}>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="workflow-danger-action"
                            data-testid={`btn-delete-dataset-${selectedDataset.dataset_id}`}
                            onClick={() => handleDeleteDataset(selectedDataset.dataset_id)}
                            disabled={actionLoading}
                          >
                            {t('ui.delete')}
                          </Button>
                        </InlineConfirm>
                      )}
                    </div>
                    <div className="inspection-meta" style={{ marginBottom: 8 }}>
                      <span>v{selectedDataset.version}</span>
                      {/* The detail payload may omit is_builtin; the list summary carries it. */}
                      <span>
                        {(datasets.find((d) => d.dataset_id === selectedDataset.dataset_id)?.is_builtin ?? selectedDataset.is_builtin)
                          ? t('ui.builtInFixture')
                          : t('ui.custom')}
                      </span>
                    </div>
                    <p style={{ color: 'var(--text-secondary)', marginBottom: 16 }}>{selectedDataset.description || t('ui.noDescription')}</p>
                    <div className="inspection-table-scroll" role="region" aria-label={t('evaluations.casesTable')} tabIndex={0}>
                      <table className="inspection-table settings-table">
                        <caption className="studio-sr-only">{t('evaluations.casesTable')}</caption>
                        <thead>
                          <tr>
                            <th scope="col">{t('ui.exampleId')}</th>
                            <th scope="col">{t('evaluations.input')}</th>
                            <th scope="col">{t('evaluations.expected')}</th>
                            <th scope="col">{t('evaluations.tags')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedDataset.examples.map((ex, idx) => (
                            <tr key={ex.example_id || idx}>
                              <td><code>{ex.example_id}</code></td>
                              <td><code className="settings-cell-code">{JSON.stringify(ex.input_data)}</code></td>
                              <td>{String(ex.expected_output ?? '—')}</td>
                              <td className="resource-help">{ex.tags && ex.tags.length > 0 ? ex.tags.join(', ') : '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                ) : (
                  <p className="resource-empty">{t('ui.selectADatasetOnTheLeftToInspectItsExamples')}</p>
                )}
              </div>
            </div>
          </>
        )}

        {activeTab === 'run' && (
          <>
            {runLoading && <p role="status">{t('common.loading')}</p>}
            <form
              className="resource-form"
              aria-label={t('ui.configureExecuteBenchmark')}
              onSubmit={(e) => {
                e.preventDefault();
                void handleRunEvaluation();
              }}
            >
              <div className="resource-limits" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
                <div className="resource-field">
                  <label htmlFor={`${ids}-dataset`}>{t('ui.dataset')}</label>
                  <select id={`${ids}-dataset`} data-testid="select-eval-dataset" value={evalDatasetId} onChange={(e) => setEvalDatasetId(e.target.value)}>
                    {datasets.map((d) => (
                      <option key={d.dataset_id} value={d.dataset_id}>
                        {d.dataset_id} ({d.example_count} {t('ui.examples3')}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="resource-field">
                  <label htmlFor={`${ids}-model`}>{t('ui.model2')}</label>
                  <input
                    id={`${ids}-model`}
                    data-testid="input-eval-model"
                    value={evalModelId}
                    onChange={(e) => setEvalModelId(e.target.value)}
                    placeholder="local-eval-fake"
                    aria-describedby={`${ids}-model-help`}
                  />
                  <span id={`${ids}-model-help`} className="resource-help">{t('ui.deterministicLocalOfflineFakeModelZeroPaidTokens')}</span>
                </div>
              </div>
              <fieldset className="resource-section">
                <legend className="resource-label" style={{ fontSize: 'var(--type-label)', fontWeight: 500, marginBottom: 8 }}>{t('ui.gradersEvaluators')}</legend>
                <div className="resource-checks">
                  {availableGraders.map((g) => (
                    <label key={g.id} className="resource-check">
                      <input
                        type="checkbox"
                        checked={selectedGraders.includes(g.id)}
                        onChange={() => toggleGrader(g.id)}
                        data-testid={`checkbox-grader-${g.id}`}
                      />
                      <span>
                        <strong>{g.name}</strong>
                        <span className="resource-check-detail">{g.desc}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                <Button type="submit" variant="primary" data-testid="btn-run-eval" loading={actionLoading} disabled={!evalDatasetId || runLoading}>
                  {t('ui.runEvaluation')}
                </Button>
              </div>
            </form>

            {selectedRun && (
              <section data-testid="eval-results-container" aria-labelledby={`${ids}-results`} style={{ marginTop: 24 }}>
                <h2 id={`${ids}-results`} style={{ marginBottom: 8 }}>
                  {t('ui.evaluationResults') + ' '}<code>{selectedRun.run_id}</code>
                </h2>
                <InlineConfirm key={selectedRun.run_id} consequence={t('livefixUi.deleteBenchmarkConfirm').replace('{id}', selectedRun.run_id)}>
                  <Button variant="danger" size="sm" disabled={actionLoading} onClick={() => handleDeleteRun(selectedRun.run_id)}>{t('livefixUi.deleteEval')}</Button>
                </InlineConfirm>
                <dl className="settings-metrics">
                  <div>
                    <dt>{t('ui.passRate')}</dt>
                    <dd data-testid="stat-pass-rate">{((selectedRun.pass_rate ?? 0) * 100).toFixed(1)}%</dd>
                  </div>
                  <div>
                    <dt>{t('ui.meanScore')}</dt>
                    <dd data-testid="stat-mean-score">{(selectedRun.mean_score ?? 0).toFixed(2)}</dd>
                  </div>
                  <div>
                    <dt>{t('ui.totalExamples')}</dt>
                    <dd>{selectedRun.passed_examples ?? 0} / {selectedRun.total_examples ?? 0}</dd>
                  </div>
                  <div>
                    <dt>{t('ui.meanLatency')}</dt>
                    <dd>{meanLatency == null ? t('ui.unknown') : `${meanLatency.toFixed(1)} ms`}</dd>
                  </div>
                </dl>
                {selectedRun.results && selectedRun.results.length > 0 && (
                  <div className="inspection-table-scroll" role="region" aria-label={t('ui.exampleBreakdown')} tabIndex={0}>
                    <table className="inspection-table settings-table">
                      <caption className="studio-sr-only">{t('ui.exampleBreakdown')}</caption>
                      <thead>
                        <tr>
                          <th scope="col">{t('ui.exampleId')}</th>
                          <th scope="col">{t('ui.status')}</th>
                          <th scope="col" style={{ textAlign: 'right' }}>{t('ui.score')}</th>
                          <th scope="col">{t('ui.actualOutput')}</th>
                          <th scope="col">{t('ui.expectedOutput')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedRun.results.map((r: any) => (
                          <tr key={r.example_id}>
                            <td><code>{r.example_id}</code></td>
                            <td>
                              <span className={`run-status run-status--${r.passed ? 'success' : 'danger'}`}>
                                <span aria-hidden="true">●</span> {r.passed ? t('ui.passed') : t('ui.failed2')}
                              </span>
                            </td>
                            <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{(r.overall_score ?? r.score ?? 0).toFixed(2)}</td>
                            <td>{String(r.actual_output ?? r.output ?? '—')}</td>
                            <td className="resource-help">{String(r.expected_output ?? '—')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            )}
          </>
        )}

        {activeTab === 'compare' && (
          <>
            {runs.length > 0 && <section aria-label={t('livefixUi.savedBenchmarks')} className="resource-list">
              {runs.map(run => <div key={run.run_id} className="resource-actions">
                <Button variant="ghost" size="sm" disabled={actionLoading || runLoading} onClick={() => { setActiveTab('run'); void handleOpenRun(run.run_id); }}>{run.run_id}</Button>
                <InlineConfirm consequence={t('livefixUi.deleteBenchmarkConfirm').replace('{id}', run.run_id)}>
                  <Button variant="danger" size="sm" disabled={actionLoading || runLoading} onClick={() => handleDeleteRun(run.run_id)}>{t('livefixUi.deleteEval')}</Button>
                </InlineConfirm>
              </div>)}
            </section>}
            {runs.length === 0 && (
              <div className="inspection-empty" data-testid="compare-empty">
                <p>{t('evaluations.compareEmpty')}</p>
                <Button variant="ghost" size="sm" onClick={() => setActiveTab('run')} style={{ marginTop: 8 }}>
                  {t('evaluations.goToBenchmark')}
                </Button>
              </div>
            )}
            <form
              className="resource-form"
              aria-label={t('ui.compareTwoEvaluationRuns')}
              onSubmit={(e) => {
                e.preventDefault();
                void handleCompareRuns();
              }}
            >
              <div className="resource-limits" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
                <div className="resource-field">
                  <label htmlFor={`${ids}-base`}>{t('ui.baseRun')}</label>
                  <select id={`${ids}-base`} data-testid="select-base-run" value={baseRunId} onChange={(e) => setBaseRunId(e.target.value)}>
                    <option value="">{t('ui.selectBaseRun')}</option>
                    {runs.map((r) => (
                      <option key={r.run_id} value={r.run_id}>
                        {r.run_id} ({r.dataset_id} - {(r.pass_rate * 100).toFixed(0)}{t('ui.pass')}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="resource-field">
                  <label htmlFor={`${ids}-candidate`}>{t('ui.candidateRun')}</label>
                  <select id={`${ids}-candidate`} data-testid="select-candidate-run" value={candidateRunId} onChange={(e) => setCandidateRunId(e.target.value)}>
                    <option value="">{t('ui.selectCandidateRun')}</option>
                    {runs.map((r) => (
                      <option key={r.run_id} value={r.run_id}>
                        {r.run_id} ({r.dataset_id} - {(r.pass_rate * 100).toFixed(0)}{t('ui.pass')}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                <Button type="submit" variant="primary" data-testid="btn-compare-runs" loading={actionLoading} disabled={!baseRunId || !candidateRunId}>
                  {t('ui.compareMetrics')}
                </Button>
              </div>
            </form>

            {comparison && (
              <section data-testid="comparison-results-container" aria-label={t('evaluations.comparison')} style={{ marginTop: 24 }}>
                <div className="inspection-table-scroll" role="region" aria-label={t('evaluations.comparison')} tabIndex={0}>
                  <table className="inspection-table settings-table">
                    <caption className="studio-sr-only">{t('evaluations.comparison')}</caption>
                    <thead>
                      <tr>
                        <th scope="col">{t('evaluations.measure')}</th>
                        <th scope="col" style={{ textAlign: 'right' }}>{t('evaluations.change')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        <th scope="row">{t('ui.passRateDelta')}</th>
                        <td data-testid="delta-pass-rate" style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                          {signed(comparison.pass_rate_diff * 100, 1, '%')}
                        </td>
                      </tr>
                      <tr>
                        <th scope="row">{t('ui.meanScoreDelta')}</th>
                        <td data-testid="delta-score" style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                          {signed(comparison.mean_score_diff, 2)}
                        </td>
                      </tr>
                      <tr>
                        <th scope="row">{t('ui.latencyDelta')}</th>
                        <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{signed(comparison.latency_diff_ms, 1, ' ms')}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div className="settings-two-lists">
                  <section aria-labelledby={`${ids}-regressions`}>
                    <h3 id={`${ids}-regressions`} className="resource-label">
                      {t('ui.regressions')}{comparison.regressions.length})
                    </h3>
                    {comparison.regressions.length === 0 ? (
                      <p className="resource-help">{t('ui.noRegressionsDetected')}</p>
                    ) : (
                      <ul className="settings-plain-list">
                        {comparison.regressions.map((ex) => (
                          <li key={ex}>
                            <span className="run-status run-status--danger"><span aria-hidden="true">●</span> {t('evaluations.regression')}</span>{' '}
                            <code>{ex}</code>
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>
                  <section aria-labelledby={`${ids}-improvements`}>
                    <h3 id={`${ids}-improvements`} className="resource-label">
                      {t('ui.improvements')}{comparison.improvements.length})
                    </h3>
                    {comparison.improvements.length === 0 ? (
                      <p className="resource-help">{t('ui.noImprovementsDetected')}</p>
                    ) : (
                      <ul className="settings-plain-list">
                        {comparison.improvements.map((ex) => (
                          <li key={ex}>
                            <span className="run-status run-status--success"><span aria-hidden="true">●</span> {t('evaluations.improvement')}</span>{' '}
                            <code>{ex}</code>
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>
                </div>
              </section>
            )}
          </>
        )}
      </div>

      <Modal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        title={t('ui.uploadEvaluationDataset')}
        maxWidth={640}
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateModalOpen(false)}>
              {t('ui.cancel')}
            </Button>
            <Button
              variant="primary"
              data-testid="btn-submit-create-dataset"
              loading={actionLoading}
              onClick={async () => {
                const ok = await handleCreateDataset();
                if (ok) setCreateModalOpen(false);
              }}
            >
              {t('ui.saveDataset')}
            </Button>
          </>
        }
      >
        <div className="resource-form" style={{ gap: 16 }}>
          {formError && (
            <div role="alert" className="inline-notice inline-notice--danger">
              <span>{formError}</span>
            </div>
          )}
          <div className="resource-field">
            <label htmlFor={`${ids}-new-id`}>{t('ui.datasetId')}</label>
            <input id={`${ids}-new-id`} data-testid="input-new-dataset-id" value={newDatasetId} onChange={(e) => setNewDatasetId(e.target.value)} placeholder={t('ui.eGSupportQaBenchmark')} />
          </div>
          <div className="resource-limits" style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 2fr)' }}>
            <div className="resource-field">
              <label htmlFor={`${ids}-new-version`}>{t('ui.version')}</label>
              <input id={`${ids}-new-version`} value={newVersion} onChange={(e) => setNewVersion(e.target.value)} placeholder="1.0.0" />
            </div>
            <div className="resource-field">
              <label htmlFor={`${ids}-new-desc`}>{`${t('ui.description')} (${t('credentials.optional')})`}</label>
              <input id={`${ids}-new-desc`} value={newDescription} onChange={(e) => setNewDescription(e.target.value)} placeholder={t('ui.optionalDescription')} />
            </div>
          </div>
          <div className="resource-field">
            <label htmlFor={`${ids}-new-examples`}>{t('ui.examplesJsonArray')}</label>
            <textarea
              id={`${ids}-new-examples`}
              data-testid="textarea-new-examples"
              value={newExamplesJson}
              onChange={(e) => setNewExamplesJson(e.target.value)}
              rows={8}
              spellCheck={false}
            />
          </div>
        </div>
      </Modal>
    </PageLayout>
  );
}
