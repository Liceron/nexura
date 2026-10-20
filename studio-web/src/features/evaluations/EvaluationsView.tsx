// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { InlineConfirm } from '../../components/InlineConfirm';
import { Icon } from '../../components/Icon';
import { useState } from 'react';
import { useEvaluations } from './useEvaluations';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Input } from '../../components/Input';
import { Alert } from '../../components/Alert';
import { Spinner } from '../../components/Spinner';
import { Card } from '../../components/Card';
import { Modal } from '../../components/Modal';
import { useI18n } from '../../i18n';
import { StudioEvalsPanel } from './StudioEvalsPanel';

export function EvaluationsView() {
  const { t } = useI18n();
  const {
    datasets,
    selectedDatasetId,
    setSelectedDatasetId,
    selectedDataset,
    runs,
    selectedRun,
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

  return (
    <div
      data-testid="evaluations-view"
      style={{
        padding: 24,
        maxWidth: 1200,
        margin: '0 auto',
        display: 'flex',
        flexDirection: 'column',
        gap: 20,
      }}
    >
      {/* Top Header */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          borderBottom: '1px solid var(--border-subtle)',
          paddingBottom: 16,
        }}
      >
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: 'var(--text-primary)' }}>
            {t('evaluations.title') || t('ui.evaluationsBenchmarks')}
          </h1>
          <p style={{ margin: '4px 0 0 0', fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('evaluations.subtitle') ||
              t('ui.runDeterministicEvaluationsVerifyDatasetMetricsAndTrackRegressionsOfflineWithZe')}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="secondary" size="sm" onClick={refreshAll} disabled={loading || actionLoading}>
            {loading ? <Spinner size={14} /> : <><Icon name="refresh" /> {t('ui.refresh')}</>}
          </Button>
        </div>
      </div>

      {error && (
        <Alert variant="danger" title={t('ui.evaluationError')}>
          {error}
        </Alert>
      )}

      {/* Tabs */}
      <div role="tablist" style={{ display: 'flex', gap: 12, borderBottom: '1px solid var(--border-subtle)', paddingBottom: 4 }}>
        <button
          data-testid="tab-datasets"
          role="tab"
          aria-selected={activeTab === 'datasets'}
          onClick={() => setActiveTab('datasets')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'datasets' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'datasets' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'datasets' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
          }}
        >
          <Icon name="folder" /> {t('ui.datasetsFixtures')}{datasets.length})
        </button>
        <button
          data-testid="tab-run"
          role="tab"
          aria-selected={activeTab === 'run'}
          onClick={() => setActiveTab('run')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'run' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'run' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'run' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
          }}
        >
          <Icon name="workflows" /> {t('ui.runBenchmark')}
        </button>
        <button
          data-testid="tab-compare"
          role="tab"
          aria-selected={activeTab === 'compare'}
          onClick={() => setActiveTab('compare')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'compare' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'compare' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'compare' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
          }}
        >
          <Icon name="compare" /> {t('ui.compareRegressions')}{runs.length} {t('ui.runs')}
        </button>
        <button
          data-testid="tab-studio-evals"
          role="tab"
          aria-selected={activeTab === 'studio'}
          onClick={() => setActiveTab('studio')}
          style={{
            background: 'none',
            border: 'none',
            borderBottom: activeTab === 'studio' ? '2px solid var(--accent-primary)' : '2px solid transparent',
            color: activeTab === 'studio' ? 'var(--accent-primary)' : 'var(--text-secondary)',
            fontWeight: activeTab === 'studio' ? 600 : 500,
            fontSize: 14,
            padding: '8px 16px',
            cursor: 'pointer',
          }}
        >
          {t('evalsStudio.title')}
        </button>
      </div>

      {activeTab === 'studio' && <StudioEvalsPanel />}

      {/* TAB 1: Datasets */}
      {activeTab === 'datasets' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>
              {t('ui.registeredEvaluationDatasets')}
            </h2>
            <Button
              variant="primary"
              size="sm"
              data-testid="btn-open-create-dataset"
              onClick={() => {
                setFormError(null);
                setCreateModalOpen(true);
              }}
            >
              {t('ui.uploadCustomDataset')}
            </Button>
          </div>

          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 16 }}>
            {/* Dataset List */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {datasets.map((ds) => {
                const isSelected = selectedDatasetId === ds.dataset_id;
                return (
                  <Card
                    key={ds.dataset_id}
                    data-testid={`dataset-item-${ds.dataset_id}`}
                    className="studio-mobile-content"
                    style={{
                      cursor: 'pointer',
                      border: isSelected ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                      backgroundColor: isSelected ? 'var(--bg-surface-elevated)' : 'var(--bg-surface)',
                    }}
                    onClick={() => setSelectedDatasetId(ds.dataset_id)}
                  >
                    <div className="studio-mobile-wrap" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                      <span style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)' }}>
                        {ds.dataset_id}
                      </span>
                      {ds.is_builtin ? (
                        <Badge variant="neutral">{t('ui.builtInFixture')}</Badge>
                      ) : (
                        <Badge variant="info">{t('ui.custom')}</Badge>
                      )}
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 8 }}>
                      {ds.description || t('ui.noDescriptionProvided')}
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 11, color: 'var(--text-muted)' }}>
                      <span>v{ds.version}</span>
                      <span>{ds.example_count} {t('ui.examples')}</span>
                    </div>
                  </Card>
                );
              })}
            </div>

            {/* Dataset Detail & Examples */}
            <div>
              {selectedDataset ? (
                <Card className="studio-mobile-content" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                  <div className="studio-mobile-wrap" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                    <div className="studio-mobile-content">
                      <div className="studio-mobile-wrap" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>{selectedDataset.dataset_id}</h3>
                        <Badge variant={selectedDataset.is_builtin ? 'neutral' : 'info'}>
                          v{selectedDataset.version}
                        </Badge>
                      </div>
                      <p style={{ margin: '4px 0 0 0', fontSize: 13, color: 'var(--text-secondary)' }}>
                        {selectedDataset.description || t('ui.noDescription')}
                      </p>
                    </div>
                    {!selectedDataset.is_builtin && (
                      <InlineConfirm key={selectedDataset.dataset_id} consequence={t('confirmation.deleteDataset')} >
                        <Button
                          variant="danger"
                          size="sm"
                          data-testid={`btn-delete-dataset-${selectedDataset.dataset_id}`}
                          onClick={() => handleDeleteDataset(selectedDataset.dataset_id)}
                          disabled={actionLoading}
                        >
                          {t('ui.delete')}
                        </Button>
                      </InlineConfirm>
                    )}
                  </div>

                  <div>
                    <h4 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: 'var(--text-secondary)' }}>
                      {t('ui.examples2')}{selectedDataset.examples.length})
                    </h4>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxHeight: 420, overflowY: 'auto' }}>
                      {selectedDataset.examples.map((ex, idx) => (
                        <div
                          key={ex.example_id || idx}
                          style={{
                            padding: 12,
                            borderRadius: 'var(--radius-md)',
                            backgroundColor: 'var(--bg-surface-elevated)',
                            border: '1px solid var(--border-subtle)',
                            fontSize: 12,
                          }}
                        >
                          <div className="studio-mobile-wrap" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                            <span style={{ fontWeight: 600, color: 'var(--accent-primary)' }}>{ex.example_id}</span>
                            {ex.tags && ex.tags.length > 0 && (
                              <div className="studio-mobile-wrap" style={{ display: 'flex', gap: 4 }}>
                                {ex.tags.map((t) => (
                                  <Badge key={t} variant="neutral">
                                    {t}
                                  </Badge>
                                ))}
                              </div>
                            )}
                          </div>
                          <div style={{ marginBottom: 4 }}>
                            <strong style={{ color: 'var(--text-secondary)' }}>{t('ui.input2')}</strong>{' '}
                            <code style={{ fontSize: 11 }}>{JSON.stringify(ex.input_data)}</code>
                          </div>
                          <div>
                            <strong style={{ color: 'var(--text-secondary)' }}>{t('ui.expected')}</strong>{' '}
                            <span style={{ color: 'var(--text-primary)' }}>{String(ex.expected_output ?? '—')}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </Card>
              ) : (
                <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-secondary)' }}>
                  {t('ui.selectADatasetOnTheLeftToInspectItsExamples')}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: Run Evaluation */}
      {activeTab === 'run' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          <Card style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>
              {t('ui.configureExecuteBenchmark')}
            </h2>
            <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                  {t('ui.dataset')}
                </label>
                <select
                  data-testid="select-eval-dataset"
                  value={evalDatasetId}
                  onChange={(e) => setEvalDatasetId(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-surface)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                  }}
                >
                  {datasets.map((d) => (
                    <option key={d.dataset_id} value={d.dataset_id}>
                      {d.dataset_id} ({d.example_count} {t('ui.examples3')}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                  {t('ui.model2')}
                </label>
                <Input
                  data-testid="input-eval-model"
                  value={evalModelId}
                  onChange={(e) => setEvalModelId(e.target.value)}
                  placeholder="local-eval-fake"
                />
                <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  {t('ui.deterministicLocalOfflineFakeModelZeroPaidTokens')}
                </span>
              </div>
            </div>

            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 8 }}>
                {t('ui.gradersEvaluators')}
              </label>
              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                {availableGraders.map((g) => {
                  const checked = selectedGraders.includes(g.id);
                  return (
                    <label
                      key={g.id}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        padding: '6px 12px',
                        borderRadius: 'var(--radius-md)',
                        backgroundColor: checked ? 'var(--accent-subtle)' : 'var(--bg-surface-elevated)',
                        border: checked ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                        cursor: 'pointer',
                        fontSize: 12,
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleGrader(g.id)}
                        data-testid={`checkbox-grader-${g.id}`}
                      />
                      <div>
                        <strong>{g.name}</strong>
                        <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{g.desc}</div>
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <Button
                variant="primary"
                data-testid="btn-run-eval"
                onClick={handleRunEvaluation}
                disabled={actionLoading || !evalDatasetId}
              >
                {actionLoading ? <Spinner size={14} /> : <><Icon name="play" /> {t('ui.runEvaluation')}</>}
              </Button>
            </div>
          </Card>

          {/* Results Display */}
          {selectedRun && (
            <div data-testid="eval-results-container" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <h3 style={{ fontSize: 16, fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>
                {t('ui.evaluationResults') + " "}<code style={{ fontSize: 14 }}>{selectedRun.run_id}</code>
              </h3>

              <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
                <Card style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)', textTransform: 'uppercase' }}>{t('ui.passRate')}</div>
                  <div
                    data-testid="stat-pass-rate"
                    style={{
                      fontSize: 24,
                      fontWeight: 700,
                      color: (selectedRun.pass_rate ?? 0) === 1.0 ? 'var(--status-success)' : 'var(--status-warning)',
                    }}
                  >
                    {((selectedRun.pass_rate ?? 0) * 100).toFixed(1)}%
                  </div>
                </Card>
                <Card style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)', textTransform: 'uppercase' }}>{t('ui.meanScore')}</div>
                  <div data-testid="stat-mean-score" style={{ fontSize: 24, fontWeight: 700, color: 'var(--text-primary)' }}>
                    {(selectedRun.mean_score ?? 0).toFixed(2)}
                  </div>
                </Card>
                <Card style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)', textTransform: 'uppercase' }}>{t('ui.totalExamples')}</div>
                  <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--text-primary)' }}>
                    {selectedRun.passed_examples ?? 0} / {selectedRun.total_examples ?? 0}
                  </div>
                </Card>
                <Card style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)', textTransform: 'uppercase' }}>{t('ui.meanLatency')}</div>
                  <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--text-primary)' }}>
                    {(
                      selectedRun.mean_latency_ms ??
                      (selectedRun.total_examples
                        ? (selectedRun as any).total_latency_ms / selectedRun.total_examples
                        : 0)
                    ).toFixed(1)}{' '}
                    ms
                  </div>
                </Card>
              </div>

              {selectedRun.results && selectedRun.results.length > 0 && (
                <Card>
                  <h4 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>{t('ui.exampleBreakdown')}</h4>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                    <thead>
                      <tr style={{ borderBottom: '1px solid var(--border-subtle)', textAlign: 'left', color: 'var(--text-secondary)' }}>
                        <th style={{ padding: '8px 12px' }}>{t('ui.exampleId')}</th>
                        <th style={{ padding: '8px 12px' }}>{t('ui.status')}</th>
                        <th style={{ padding: '8px 12px' }}>{t('ui.score')}</th>
                        <th style={{ padding: '8px 12px' }}>{t('ui.actualOutput')}</th>
                        <th style={{ padding: '8px 12px' }}>{t('ui.expectedOutput')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedRun.results.map((r: any) => (
                        <tr key={r.example_id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                          <td style={{ padding: '8px 12px', fontWeight: 600 }}>{r.example_id}</td>
                          <td style={{ padding: '8px 12px' }}>
                            <Badge variant={r.passed ? 'success' : 'danger'}>
                              {r.passed ? t('ui.passed') : t('ui.failed2')}
                            </Badge>
                          </td>
                          <td style={{ padding: '8px 12px' }}>{((r.overall_score ?? r.score ?? 0)).toFixed(2)}</td>
                          <td style={{ padding: '8px 12px', color: 'var(--text-primary)' }}>
                            {String(r.actual_output ?? r.output ?? '—')}
                          </td>
                          <td style={{ padding: '8px 12px', color: 'var(--text-secondary)' }}>
                            {String(r.expected_output ?? '—')}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              )}
            </div>
          )}
        </div>
      )}

      {/* TAB 3: Compare Runs */}
      {activeTab === 'compare' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <Card style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>
              {t('ui.compareTwoEvaluationRuns')}
            </h2>
            <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                  {t('ui.baseRun')}
                </label>
                <select
                  data-testid="select-base-run"
                  value={baseRunId}
                  onChange={(e) => setBaseRunId(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-surface)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                  }}
                >
                  <option value="">{t('ui.selectBaseRun')}</option>
                  {runs.map((r) => (
                    <option key={r.run_id} value={r.run_id}>
                      {r.run_id} ({r.dataset_id} - {(r.pass_rate * 100).toFixed(0)}{t('ui.pass')}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                  {t('ui.candidateRun')}
                </label>
                <select
                  data-testid="select-candidate-run"
                  value={candidateRunId}
                  onChange={(e) => setCandidateRunId(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-surface)',
                    color: 'var(--text-primary)',
                    fontSize: 13,
                  }}
                >
                  <option value="">{t('ui.selectCandidateRun')}</option>
                  {runs.map((r) => (
                    <option key={r.run_id} value={r.run_id}>
                      {r.run_id} ({r.dataset_id} - {(r.pass_rate * 100).toFixed(0)}{t('ui.pass')}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <Button
                variant="primary"
                data-testid="btn-compare-runs"
                onClick={handleCompareRuns}
                disabled={actionLoading || !baseRunId || !candidateRunId}
              >
                {actionLoading ? <Spinner size={14} /> : <><Icon name="compare" /> {t('ui.compareMetrics')}</>}
              </Button>
            </div>
          </Card>

          {comparison && (
            <div data-testid="comparison-results-container" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
                <Card style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)', textTransform: 'uppercase' }}>
                    {t('ui.passRateDelta')}
                  </div>
                  <div
                    data-testid="delta-pass-rate"
                    style={{
                      fontSize: 24,
                      fontWeight: 700,
                      color:
                        comparison.pass_rate_diff >= 0
                          ? 'var(--status-success)'
                          : 'var(--status-danger)',
                    }}
                  >
                    {comparison.pass_rate_diff >= 0 ? '+' : ''}
                    {(comparison.pass_rate_diff * 100).toFixed(1)}%
                  </div>
                </Card>
                <Card style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)', textTransform: 'uppercase' }}>
                    {t('ui.meanScoreDelta')}
                  </div>
                  <div
                    data-testid="delta-score"
                    style={{
                      fontSize: 24,
                      fontWeight: 700,
                      color:
                        comparison.mean_score_diff >= 0
                          ? 'var(--status-success)'
                          : 'var(--status-danger)',
                    }}
                  >
                    {comparison.mean_score_diff >= 0 ? '+' : ''}
                    {comparison.mean_score_diff.toFixed(2)}
                  </div>
                </Card>
                <Card style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)', textTransform: 'uppercase' }}>
                    {t('ui.latencyDelta')}
                  </div>
                  <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--text-primary)' }}>
                    {comparison.latency_diff_ms >= 0 ? '+' : ''}
                    {comparison.latency_diff_ms.toFixed(1)} ms
                  </div>
                </Card>
              </div>

              <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
                <Card>
                  <h4 style={{ fontSize: 14, fontWeight: 600, color: 'var(--status-danger)', marginBottom: 8 }}>
                    <Icon name="warning" /> {t('ui.regressions')}{comparison.regressions.length})
                  </h4>
                  {comparison.regressions.length === 0 ? (
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{t('ui.noRegressionsDetected')}</div>
                  ) : (
                    <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
                      {comparison.regressions.map((ex) => (
                        <li key={ex} style={{ color: 'var(--status-danger)', marginBottom: 4 }}>
                          {ex}
                        </li>
                      ))}
                    </ul>
                  )}
                </Card>

                <Card>
                  <h4 style={{ fontSize: 14, fontWeight: 600, color: 'var(--status-success)', marginBottom: 8 }}>
                    <Icon name="sparkles" /> {t('ui.improvements')}{comparison.improvements.length})
                  </h4>
                  {comparison.improvements.length === 0 ? (
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{t('ui.noImprovementsDetected')}</div>
                  ) : (
                    <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
                      {comparison.improvements.map((ex) => (
                        <li key={ex} style={{ color: 'var(--status-success)', marginBottom: 4 }}>
                          {ex}
                        </li>
                      ))}
                    </ul>
                  )}
                </Card>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Modal: Create Dataset */}
      <Modal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        title={t('ui.uploadEvaluationDataset')}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {formError && (
            <Alert variant="danger" title={t('ui.validationError')}>
              {formError}
            </Alert>
          )}

          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
              {t('ui.datasetId')}
            </label>
            <Input
              data-testid="input-new-dataset-id"
              value={newDatasetId}
              onChange={(e) => setNewDatasetId(e.target.value)}
              placeholder={t('ui.eGSupportQaBenchmark')}
            />
          </div>

          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                {t('ui.version')}
              </label>
              <Input
                value={newVersion}
                onChange={(e) => setNewVersion(e.target.value)}
                placeholder="1.0.0"
              />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
                {t('ui.description')}
              </label>
              <Input
                value={newDescription}
                onChange={(e) => setNewDescription(e.target.value)}
                placeholder={t('ui.optionalDescription')}
              />
            </div>
          </div>

          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
              {t('ui.examplesJsonArray')}
            </label>
            <textarea
              data-testid="textarea-new-examples"
              value={newExamplesJson}
              onChange={(e) => setNewExamplesJson(e.target.value)}
              rows={8}
              style={{
                width: '100%',
                padding: '8px 12px',
                borderRadius: 'var(--radius-md)',
                border: '1px solid var(--border-subtle)',
                backgroundColor: 'var(--bg-surface-elevated)',
                color: 'var(--text-primary)',
                fontFamily: 'monospace',
                fontSize: 12,
              }}
            />
          </div>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 10 }}>
            <Button variant="secondary" onClick={() => setCreateModalOpen(false)}>
              {t('ui.cancel')}
            </Button>
            <Button
              variant="primary"
              data-testid="btn-submit-create-dataset"
              disabled={actionLoading}
              onClick={async () => {
                const ok = await handleCreateDataset();
                if (ok) setCreateModalOpen(false);
              }}
            >
              {actionLoading ? <Spinner size={14} /> : t('ui.saveDataset')}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
