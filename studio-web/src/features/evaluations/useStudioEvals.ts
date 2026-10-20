// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useState } from 'react';
import { useApi } from '../../api/context';
import type {
  CompareResponse,
  EvalCase,
  OpenAiImportBody,
  OpenAiImportResponse,
  StudioDataset,
  StudioDatasetSummary,
  RedteamResult,
  RedteamTarget,
  StudioRun,
  TargetOption,
} from './studioEvalsTypes';
import { formatApiError } from '../../api/errors';
import { useLatestT, type Translate } from '../../i18n';
import { importFieldErrors } from './evals_dataset_json';

const BASE = '/evaluations/studio';

function message(err: unknown, t: Translate): string {
  return formatApiError(err, t);
}

export function useStudioEvals() {
  const t = useLatestT();
  const { manager, connection } = useApi();
  const client = manager.client;

  const [datasets, setDatasets] = useState<StudioDatasetSummary[]>([]);
  const [targets, setTargets] = useState<TargetOption[]>([]);
  const [runs, setRuns] = useState<StudioRun[]>([]);
  const [dataset, setDataset] = useState<StudioDataset | null>(null);
  const [run, setRun] = useState<StudioRun | null>(null);
  const [comparison, setComparison] = useState<CompareResponse | null>(null);
  const [baselineRun, setBaselineRun] = useState<StudioRun | null>(null);
  const [candidateRun, setCandidateRun] = useState<StudioRun | null>(null);
  const [redteam, setRedteam] = useState<RedteamResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [d, t, r] = await Promise.all([
        client.request<{ datasets: StudioDatasetSummary[] }>('GET', `${BASE}/datasets`),
        client.request<{ targets: TargetOption[] }>('GET', `${BASE}/targets`),
        client.request<{ runs: StudioRun[] }>('GET', `${BASE}/runs`),
      ]);
      setDatasets(d.datasets);
      setTargets(t.targets);
      setRuns(r.runs);
    } catch (err) {
      setError(message(err, t));
    }
  }, [client, t]);

  useEffect(() => {
    setDataset(null);
    setRun(null);
    setComparison(null);
    void refresh();
  }, [refresh, connection.workspace]);

  const guard = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(message(err, t));
      return null;
    } finally {
      setBusy(false);
    }
  }, [t]);

  const openDataset = useCallback(
    (id: string) =>
      guard(async () => {
        const ds = await client.request<StudioDataset>('GET', `${BASE}/datasets/${encodeURIComponent(id)}`);
        setDataset(ds);
        return ds;
      }),
    [client, guard],
  );

  const saveDataset = useCallback(
    (id: string, body: { name: string; description: string; cases: EvalCase[]; expected_revision?: number }) =>
      guard(async () => {
        const saved = await client.request<StudioDataset & { redactions?: number }>(
          'PUT',
          `${BASE}/datasets/${encodeURIComponent(id)}`,
          { body },
        );
        setDataset(saved);
        await refresh();
        return saved;
      }),
    [client, guard, refresh],
  );

  /** Convert an OpenAI Evals file on the server (nothing is saved); line errors are returned. */
  const importOpenAi = useCallback(
    async (body: OpenAiImportBody): Promise<{ result: OpenAiImportResponse | null; errors: string[] }> => {
      setBusy(true);
      setError(null);
      try {
        const result = await client.request<OpenAiImportResponse>('POST', `${BASE}/datasets/import-openai`, { body });
        return { result, errors: [] };
      } catch (err) {
        setError(message(err, t));
        return { result: null, errors: importFieldErrors(err) };
      } finally {
        setBusy(false);
      }
    },
    [client, t],
  );

  const deleteDataset = useCallback(
    (id: string) =>
      guard(async () => {
        await client.request('DELETE', `${BASE}/datasets/${encodeURIComponent(id)}`);
        setDataset((d) => (d?.dataset_id === id ? null : d));
        await refresh();
        return true;
      }),
    [client, guard, refresh],
  );

  const runDataset = useCallback(
    (body: {
      dataset_id: string;
      target: Record<string, unknown>;
      scorers?: Array<Record<string, unknown>>;
      repeats?: number;
      label?: string;
    }) =>
      guard(async () => {
        const created = await client.request<StudioRun>('POST', `${BASE}/runs`, { body });
        setRun(created);
        await refresh();
        return created;
      }),
    [client, guard, refresh],
  );

  const runRedteam = useCallback(
    (target: string | RedteamTarget, writeTools: string[]) =>
      guard(async () => {
        setRedteam(null);
        // A plain string is a workflow id (the original signature).
        const where: RedteamTarget = typeof target === 'string' ? { workflow_id: target } : target;
        const result = await client.request<RedteamResult>('POST', `${BASE}/redteam`, {
          body: { ...where, write_tools: writeTools },
        });
        setRedteam(result);
        return result;
      }),
    [client, guard],
  );

  const openRun = useCallback(
    (id: string) =>
      guard(async () => {
        const r = await client.request<StudioRun>('GET', `${BASE}/runs/${encodeURIComponent(id)}`);
        setRun(r);
        return r;
      }),
    [client, guard],
  );

  const compareRuns = useCallback(
    (baseline: string, candidate: string, tolerance: number, minPassRate: number | null) =>
      guard(async () => {
        const body: Record<string, unknown> = {
          baseline_run_id: baseline,
          candidate_run_id: candidate,
          tolerance,
        };
        if (minPassRate !== null) body.min_pass_rate = minPassRate;
        const [cmp, b, c] = await Promise.all([
          client.request<CompareResponse>('POST', `${BASE}/compare`, { body }),
          client.request<StudioRun>('GET', `${BASE}/runs/${encodeURIComponent(baseline)}`),
          client.request<StudioRun>('GET', `${BASE}/runs/${encodeURIComponent(candidate)}`),
        ]);
        setComparison(cmp);
        setBaselineRun(b);
        setCandidateRun(c);
        return cmp;
      }),
    [client, guard],
  );

  return {
    datasets,
    targets,
    runs,
    dataset,
    run,
    comparison,
    baselineRun,
    candidateRun,
    redteam,
    busy,
    error,
    setError,
    refresh,
    openDataset,
    saveDataset,
    importOpenAi,
    deleteDataset,
    runDataset,
    openRun,
    compareRuns,
    runRedteam,
  };
}
