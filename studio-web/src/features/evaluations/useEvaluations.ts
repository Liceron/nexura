// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useState, useEffect, useCallback, useRef } from 'react';
import { useLatestT } from '../../i18n';
import { useApi } from '../../api/context';
import type {
  DatasetSummary,
  EvaluationDataset,
  EvaluationSummary,
  EvaluationComparison,
} from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';

export function useEvaluations() {
  const t = useLatestT();
  const { manager, connection } = useApi();
  const client = manager.client;

  const [datasets, setDatasets] = useState<DatasetSummary[]>([]);
  const [selectedDatasetId, setSelectedDatasetId] = useState<string | null>(null);
  const [selectedDataset, setSelectedDataset] = useState<EvaluationDataset | null>(null);
  const [runs, setRuns] = useState<EvaluationSummary[]>([]);
  const [selectedRun, setSelectedRun] = useState<EvaluationSummary | null>(null);
  const [runLoading, setRunLoading] = useState(false);
  const runLoadGeneration = useRef(0);
  const [comparison, setComparison] = useState<EvaluationComparison | null>(null);

  const [loading, setLoading] = useState<boolean>(true);
  const [actionLoading, setActionLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // Run Eval form state
  const [evalDatasetId, setEvalDatasetId] = useState<string>('benchmark_customer_support');
  const [evalModelId, setEvalModelId] = useState<string>('local-eval-fake');
  const [selectedGraders, setSelectedGraders] = useState<string[]>(['exact_match', 'substring']);

  // Compare form state
  const [baseRunId, setBaseRunId] = useState<string>('');
  const [candidateRunId, setCandidateRunId] = useState<string>('');

  // Create Dataset form state
  const [newDatasetId, setNewDatasetId] = useState<string>('');
  const [newVersion, setNewVersion] = useState<string>('1.0.0');
  const [newDescription, setNewDescription] = useState<string>('');
  const [newExamplesJson, setNewExamplesJson] = useState<string>(
    JSON.stringify(
      [
        {
          example_id: 'ex_custom_1',
          input_data: { query: 'Hello, how do I reset my credentials?' },
          expected_output: 'You can reset your credentials in the account settings.',
          tags: ['auth', 'support'],
        },
      ],
      null,
      2
    )
  );

  const fetchDatasets = useCallback(async () => {
    try {
      const res = await client.evaluations.listDatasets();
      setDatasets(res.datasets || []);
      if (!selectedDatasetId && res.datasets && res.datasets.length > 0) {
        setSelectedDatasetId(res.datasets[0].dataset_id);
      }
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  }, [client, selectedDatasetId, t]);

  const fetchRuns = useCallback(async () => {
    try {
      const res = await client.evaluations.listRuns();
      setRuns(res.runs || []);
      if (res.runs && res.runs.length >= 2) {
        if (!baseRunId) setBaseRunId(res.runs[1].run_id);
        if (!candidateRunId) setCandidateRunId(res.runs[0].run_id);
      }
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    }
  }, [client, baseRunId, candidateRunId, t]);

  const refreshAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([fetchDatasets(), fetchRuns()]);
    } finally {
      setLoading(false);
    }
  }, [fetchDatasets, fetchRuns]);

  useEffect(() => {
    refreshAll();
  }, [connection.workspace]);

  useEffect(() => {
    setSelectedRun(null);
    setRunLoading(false);
    return () => { runLoadGeneration.current += 1; };
  }, [client, connection.workspace]);

  // Load selected dataset detail
  useEffect(() => {
    if (!selectedDatasetId) {
      setSelectedDataset(null);
      return;
    }
    let isCurrent = true;
    client.evaluations
      .getDataset(selectedDatasetId)
      .then((data) => {
        if (isCurrent) setSelectedDataset(data);
      })
      .catch((err: unknown) => {
        if (isCurrent) setError(formatApiError(err, t));
      });
    return () => {
      isCurrent = false;
    };
  }, [client, selectedDatasetId, t]);

  const handleCreateDataset = async (): Promise<boolean> => {
    setFormError(null);
    if (!newDatasetId.trim()) {
      setFormError(t('validation.datasetRequired'));
      return false;
    }
    if (newDatasetId.includes('..') || newDatasetId.includes('/') || newDatasetId.includes('\\')) {
      setFormError(t('validationMessages.datasetIdCharacters'));
      return false;
    }

    let parsedExamples: unknown;
    try {
      parsedExamples = JSON.parse(newExamplesJson);
      if (!Array.isArray(parsedExamples) || parsedExamples.length === 0) {
        setFormError(t('validationMessages.examplesArray'));
        return false;
      }
    } catch {
      setFormError(t('validationMessages.examplesJson'));
      return false;
    }

    setActionLoading(true);
    try {
      await client.evaluations.createDataset({
        dataset_id: newDatasetId.trim(),
        version: newVersion.trim() || '1.0.0',
        description: newDescription.trim() || undefined,
        examples: parsedExamples as any[],
      });
      await fetchDatasets();
      setSelectedDatasetId(newDatasetId.trim());
      setNewDatasetId('');
      setNewDescription('');
      return true;
    } catch (err: unknown) {
      setFormError(formatApiError(err, t));
      return false;
    } finally {
      setActionLoading(false);
    }
  };

  const handleDeleteDataset = async (datasetId: string) => {
    setActionLoading(true);
    setError(null);
    try {
      await client.evaluations.deleteDataset(datasetId);
      await fetchDatasets();
      if (selectedDatasetId === datasetId) {
        setSelectedDatasetId(null);
      }
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  const handleOpenRun = async (runId: string) => {
    const generation = ++runLoadGeneration.current;
    setRunLoading(true);
    setSelectedRun(null);
    setError(null);
    try {
      const detail = await client.evaluations.getRun(runId);
      if (generation === runLoadGeneration.current) setSelectedRun(detail);
    } catch (cause) {
      if (generation === runLoadGeneration.current) setError(formatApiError(cause, t));
    } finally {
      if (generation === runLoadGeneration.current) setRunLoading(false);
    }
  };

  const handleRunEvaluation = async () => {
    if (!evalDatasetId) {
      setError(t('validationMessages.selectDataset'));
      return;
    }
    setActionLoading(true);
    setError(null);
    try {
      const result = await client.evaluations.runEvaluation({
        dataset_id: evalDatasetId,
        model_id: evalModelId || 'local-eval-fake',
        graders: selectedGraders.length > 0 ? selectedGraders : ['exact_match'],
      });
      setSelectedRun(result);
      await fetchRuns();
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  const handleCompareRuns = async () => {
    if (!baseRunId || !candidateRunId) {
      setError(t('validationMessages.selectComparisonRuns'));
      return;
    }
    setActionLoading(true);
    setError(null);
    try {
      const cmp = await client.evaluations.compareRuns(baseRunId, candidateRunId);
      setComparison(cmp);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setActionLoading(false);
    }
  };

  const toggleGrader = (grader: string) => {
    setSelectedGraders((prev) =>
      prev.includes(grader) ? prev.filter((g) => g !== grader) : [...prev, grader]
    );
  };

  return {
    datasets,
    selectedDatasetId,
    setSelectedDatasetId,
    selectedDataset,
    runs,
    selectedRun,
    setSelectedRun,
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
    handleDeleteRun: async (runId: string) => {
      setActionLoading(true); setError(null);
      try {
        await client.request('DELETE', `/evaluations/runs/${encodeURIComponent(runId)}`);
        setRuns(current => current.filter(run => run.run_id !== runId));
        setSelectedRun(current => current?.run_id === runId ? null : current);
        if (baseRunId === runId) setBaseRunId('');
        if (candidateRunId === runId) setCandidateRunId('');
        setComparison(null);
      } catch (cause) { setError(formatApiError(cause, t)); throw cause; }
      finally { setActionLoading(false); }
    },
    handleRunEvaluation,
    handleCompareRuns,
    refreshAll,
  };
}
