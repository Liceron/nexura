// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// Workflow tests (P22): save a completed workflow run as an eval case, list the test datasets
// bound to a workflow, run one against the workflow's current revision and compare with the
// previous run of that dataset. Everything goes through the Studio evals API; scoring and the
// regression gate are the server's (SDK evals engine).

import { useEffect, useState } from 'react';
import { useApi } from '../../api/context';
import type {
  CaseStatus,
  CompareResponse,
  StudioDataset,
  StudioDatasetSummary,
  StudioRun,
  SaveFromRunBody,
  SaveFromRunResponse,
} from './studioEvalsTypes';
import { getMyMembership } from '../../api/membership';

const BASE = '/evaluations/studio';
export const WORKFLOW_TEST_LABEL = 'workflow-test';
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/;

export interface RequestClient {
  request<T = unknown>(
    method: string,
    path: string,
    options?: { params?: Record<string, unknown>; body?: unknown },
  ): Promise<T>;
}

export interface WorkflowTestOutcome {
  dataset: StudioDataset;
  run: StudioRun;
  /** The newest earlier run of the same dataset against the same workflow, if any. */
  previous: StudioRun | null;
  comparison: CompareResponse | null;
  outputKey: string | null;
}

export interface CaseRow {
  name: string;
  repeat: number;
  status: CaseStatus;
  reasons: string[];
  /** The Studio run a case was saved from ("Save as test"); absent for hand-written cases. */
  sourceRunId?: string;
}

/** The workflow run a case was saved from, when the case metadata records one. */
export function sourceRunIdOf(metadata: Record<string, unknown> | undefined): string | undefined {
  const id = metadata?.source_run_id;
  return typeof id === 'string' && id ? id : undefined;
}

export function isSafeDatasetId(id: string): boolean {
  return SAFE_ID.test(id);
}

/** A valid dataset id derived from a workflow id, e.g. "wf 1" -> "wf-1-tests". */
export function suggestDatasetId(workflowId: string): string {
  const base = workflowId
    .replace(/[^A-Za-z0-9_.-]+/g, '-')
    .replace(/^[^A-Za-z0-9]+/, '')
    .slice(0, 74)
    .replace(/[-.]+$/, '');
  return `${base || 'workflow'}-tests`;
}

/** The state key the dataset's run-saved cases compare (null = whole final state). */
export function datasetOutputKey(dataset: Pick<StudioDataset, 'cases'>): string | null {
  for (const c of dataset.cases) {
    const meta = c.metadata ?? {};
    if (typeof meta.source_run_id === 'string') {
      return typeof meta.output_field === 'string' && meta.output_field ? meta.output_field : null;
    }
  }
  return null;
}

/** Per-case rows of an eval run: failing scorer reasons, or the error message. */
export function caseRows(run: StudioRun): CaseRow[] {
  return (run.report?.results ?? []).map((r) => {
    const reasons = r.scores.filter((s) => !s.passed).map((s) => (s.reason ? `${s.name}: ${s.reason}` : s.name));
    if (r.error?.message) reasons.push(r.error.message);
    const row: CaseRow = { name: r.name, repeat: r.repeat, status: r.status, reasons };
    const sourceRunId = sourceRunIdOf(r.metadata);
    if (sourceRunId) row.sourceRunId = sourceRunId;
    return row;
  });
}

export async function listWorkflowTestDatasets(
  client: RequestClient,
  workflowId: string,
): Promise<StudioDatasetSummary[]> {
  const res = await client.request<{ datasets: StudioDatasetSummary[] }>('GET', `${BASE}/datasets`, {
    params: { workflow_id: workflowId },
  });
  return res.datasets;
}

export async function getDataset(client: RequestClient, datasetId: string): Promise<StudioDataset> {
  return client.request<StudioDataset>('GET', `${BASE}/datasets/${encodeURIComponent(datasetId)}`);
}

export async function saveRunAsTest(
  client: RequestClient,
  datasetId: string,
  body: SaveFromRunBody,
): Promise<SaveFromRunResponse> {
  const clean: Record<string, unknown> = { run_id: body.run_id };
  if (body.case_name?.trim()) clean.case_name = body.case_name.trim();
  if (body.output_key?.trim()) clean.output_key = body.output_key.trim();
  if (body.dataset_name?.trim()) clean.dataset_name = body.dataset_name.trim();
  if (body.expected_revision !== undefined) clean.expected_revision = body.expected_revision;
  if (body.record_path) clean.record_path = true;
  return client.request<SaveFromRunResponse>(
    'POST',
    `${BASE}/datasets/${encodeURIComponent(datasetId)}/cases/from-run`,
    { body: clean },
  );
}

/** Run a test dataset against the workflow's current revision and compare with the last run. */
export async function runWorkflowTests(
  client: RequestClient,
  workflowId: string,
  datasetId: string,
): Promise<WorkflowTestOutcome> {
  const dataset = await getDataset(client, datasetId);
  const outputKey = datasetOutputKey(dataset);
  const prior = await client.request<{ runs: StudioRun[] }>('GET', `${BASE}/runs`, {
    params: { dataset_id: datasetId, limit: 50 },
  });
  // Only a run that scored the same output key is a meaningful baseline.
  const previous =
    prior.runs.find(
      (r) =>
        r.target.kind === 'workflow' &&
        r.target.id === workflowId &&
        (r.target.output_key || null) === (outputKey || null),
    ) ?? null;
  const target: Record<string, unknown> = { kind: 'workflow', id: workflowId };
  if (outputKey) target.output_key = outputKey;
  const run = await client.request<StudioRun>('POST', `${BASE}/runs`, {
    body: { dataset_id: datasetId, target, label: WORKFLOW_TEST_LABEL },
  });
  const comparison = previous
    ? await client.request<CompareResponse>('POST', `${BASE}/compare`, {
        body: { baseline_run_id: previous.run_id, candidate_run_id: run.run_id },
      })
    : null;
  return { dataset, run, previous, comparison, outputKey };
}

/**
 * Whether the caller may edit (and run) in the active workspace: admin or editor. False until
 * the membership is known; without a membership endpoint (local owner) it falls back to admin,
 * like the Skills page. The server checks the role on every request regardless.
 */
export function useCanEdit(): boolean {
  const { manager, connection } = useApi();
  const [role, setRole] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setRole(null);
    Promise.resolve()
      .then(() => getMyMembership(manager.client, connection.workspace))
      .then((m) => live && setRole(String((m as unknown as { role?: string }).role ?? 'admin')))
      .catch(() => live && setRole('admin'));
    return () => {
      live = false;
    };
  }, [manager, connection.workspace]);
  return role === 'admin' || role === 'editor';
}
