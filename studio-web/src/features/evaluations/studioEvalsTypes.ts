// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// Types for the Studio evals API (/evaluations/studio). Scoring and comparison are done by
// the SDK evals engine on the server; these mirror its JSON.

export interface EvalCase {
  name: string;
  input: string | Record<string, unknown>;
  expected_output?: unknown;
  expected_trajectory?: Array<string | { name: string; args?: Record<string, unknown> | null }> | null;
  expected_agent_path?: string[] | null;
  tags?: string[];
  metadata?: Record<string, unknown>;
}

export interface StudioDatasetSummary {
  dataset_id: string;
  name: string;
  description: string;
  revision: number;
  content_hash: string;
  case_count: number;
  updated_at: number;
  /** Workflow this dataset tests (P22 workflow tests); null or absent when unbound. */
  workflow_id?: string | null;
}

export interface StudioDataset extends StudioDatasetSummary {
  cases: EvalCase[];
}

export interface EvalScore {
  name: string;
  value: number;
  passed: boolean;
  reason: string;
}

export type CaseStatus = 'passed' | 'failed' | 'error' | 'paused';

export interface CaseResult {
  name: string;
  repeat: number;
  status: CaseStatus;
  scores: EvalScore[];
  output_text: string;
  error: { type: string; message: string } | null;
  latency_seconds: number;
  agent_path: string[];
  metadata: { input?: unknown; tags?: string[]; [key: string]: unknown };
}

export interface EvalReportJson {
  target: string;
  repeats: number;
  scorer_names: string[];
  results: CaseResult[];
}

export interface RunSummary {
  total: number;
  passed: number;
  failed: number;
  errors: number;
  paused: number;
  pass_rate: number;
  mean_score: number;
  latency_seconds: number;
}

export interface EvalTarget {
  kind: 'workflow' | 'agent';
  id: string;
  version?: string | null;
  output_key?: string | null;
  synthetic?: boolean;
}

export interface StudioRun {
  run_id: string;
  dataset_id: string;
  dataset_revision: number;
  label: string;
  target: EvalTarget;
  scorers: Array<Record<string, unknown>>;
  summary: RunSummary;
  created_at: number;
  report?: EvalReportJson;
}

export interface ChangeEntry {
  case: string;
  scorer: string | null;
  baseline: number | string;
  candidate: number | string;
  detail: string;
}

export interface Gate {
  passed: boolean;
  tolerance: number;
  min_pass_rate: number | null;
  reasons: string[];
  semantics: string;
  summary: string;
}

export interface CompareResponse {
  baseline_run_id: string;
  candidate_run_id: string;
  same_dataset: boolean;
  regressions: ChangeEntry[];
  improvements: ChangeEntry[];
  gate: Gate;
  baseline_summary: RunSummary;
  candidate_summary: RunSummary;
}

export interface TargetOption {
  kind: 'workflow' | 'agent';
  id: string;
  name?: string | null;
  version?: string;
  /** A saved workspace agent (P26): id is `agent:<agent_id>`, version `rev_<n>`. */
  saved?: boolean;
}

/** What the red-team pack attacks: a saved workflow or a saved agent (exactly one). */
export type RedteamTarget = { workflow_id: string } | { agent_id: string };

export interface RedteamCase {
  name: string;
  attack: string | null;
  status: CaseStatus;
  safe: boolean;
  scores: EvalScore[];
  reason: string;
}

export interface RedteamResult {
  /** Set for a workflow target. */
  workflow_id?: string;
  workflow_version?: string;
  /** Set for a saved agent target (P26). */
  agent_id?: string;
  agent_version?: string;
  model: { name: string; offline: boolean };
  write_tools: string[];
  synthetic_write_tool: boolean;
  scorers: string[];
  ok: boolean;
  summary: RunSummary;
  cases: RedteamCase[];
}

/** Body of POST /datasets/{id}/cases/from-run (save a completed workflow run as a test). */
export interface SaveFromRunBody {
  run_id: string;
  case_name?: string;
  output_key?: string;
  /** Required only when the dataset does not exist yet. */
  dataset_name?: string;
  expected_revision?: number;
  /** Also record the handler order of the run's steps as the case's expected trajectory. */
  record_path?: boolean;
}

export interface SaveFromRunResponse extends StudioDataset {
  case: EvalCase;
  created: boolean;
  redactions: number;
}

/** POST /evaluations/studio/datasets/import-openai: an OpenAI Evals JSONL file to convert. */
export interface OpenAiImportBody {
  content: string;
  dataset_id: string;
  input_field?: string;
  expected_field?: string;
  testing_criteria?: unknown;
  skip_invalid?: boolean;
}

/** What the converter chose; mirrors `OpenAIImportResult.report()` on the server. */
export interface OpenAiImportReport {
  source: 'openai-evals';
  dataset_id: string;
  cases: number;
  input_field: string;
  expected_field: string | null;
  scorers: Array<Record<string, unknown>>;
  mapped_criteria: Array<{ name: string; type: string; scorer: Record<string, unknown>; note: string }>;
  unmapped_criteria: Array<{ name: string; type: string; reason: string }>;
  skipped_lines: Array<{ line: number; error: string }>;
  samples_ignored: number;
  redactions: number;
  warnings: string[];
}

export interface OpenAiImportResponse {
  dataset: { dataset_id: string; name: string; description: string; cases: EvalCase[] };
  report: OpenAiImportReport;
}
