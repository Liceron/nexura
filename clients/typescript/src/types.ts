// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/**
 * Typed domain schemas and payload structures for the Nexura Service API.
 */

/** A person's decision about an `indeterminate` effect. */
export type EffectResolution = "confirmed" | "not_applied" | "failed";

/** One journaled external effect of a run (Execution Contract v1). */
export interface ExecutionEffect {
  effect_id: string;
  execution_id: { workspace_id: string; run_id: string };
  intent: {
    step_key: string;
    effect_class: "pure" | "idempotent" | "non_idempotent" | "reconcilable";
    payload_digest: string;
    idempotency_key: string;
    approval_id: string | null;
    reservation_ids: string[];
    status: "dispatched";
  };
  effect_index: number;
  status: "pending" | "confirmed" | "indeterminate" | "resolved";
  /** Set only when `status` is `resolved`. */
  resolution: EffectResolution | null;
  /** Credential-like fields are redacted. */
  result: unknown;
  resolved_by: string | null;
}

/** Body of `runs.resolveEffect`. The actor is the authenticated caller, never a field. */
export interface ResolveEffectOptions {
  resolution: EffectResolution;
  /** For `confirmed`: what the remote system did; becomes the step's output. */
  result?: unknown;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface WorkflowNode {
  id: string;
  handler: string;
  transitions?: Record<string, string>;
  default_transition?: string | null;
  requires_approval?: boolean;
  module_id?: string | null;
  module_version?: string | null;
  config?: Record<string, any>;
  input_mapping?: Record<string, any>;
  output_mapping?: Record<string, any>;
  presentation?: Record<string, any>;
  metadata?: Record<string, any>;
}

export interface WorkflowSpec {
  name: string;
  version?: string | number | null;
  entry_node: string;
  nodes: Record<string, WorkflowNode | any>;
  spec_version?: number;
  terminal_nodes?: string[];
  metadata?: Record<string, any>;
  presentation?: Record<string, any>;
}

export interface WorkflowRecord {
  workflow_id: string;
  name: string;
  revision: number;
  entry_node: string;
  node_count: number;
  created_at: number;
  updated_at: number;
  spec?: WorkflowSpec;
  spec_hash?: string;
}

export interface RunEvent {
  seq: number;
  type: string;
  node_id?: string | null;
  data: Record<string, any>;
  timestamp: number;
}

export interface PendingApproval {
  approval_id: string;
  node_id: string;
  description?: string;
  state: Record<string, any>;
  /** What a mapped module step will do once approved (item_count, items, map_over, ...). */
  details?: Record<string, unknown>;
}

export interface PendingInput {
  interrupt_id: string;
  node_id: string;
  value: any;
}

export interface ForkedFrom {
  workflow_run_id: string;
  checkpoint_index: number;
  step_count: number;
  state_changed: boolean;
}

export interface RunCheckpoint {
  workflow_run_id: string;
  spec_version: number;
  spec_hash: string;
  current_node_id?: string | null;
  state: Record<string, any>;
  step_count: number;
  status: string;
  pending_approval?: PendingApproval | null;
  error?: string | null;
  updated_at: number;
  budget_state?: Record<string, any> | null;
  interrupt?: Record<string, any> | null;
  forked_from?: ForkedFrom | null;
  workspace_id?: string | null;
  /** Opaque URL-safe key of a child run; only set on `runs.subruns()` items. */
  child_key?: string;
}

/** One checkpoint of a child run as listed in `SubRunDetail.steps`. */
export interface SubRunStep {
  index: number;
  step_count: number;
  node_id?: string | null;
  status: string;
  updated_at: number;
}

/** Child run view returned by `runs.subrun()`: status, steps and redacted history. */
export interface SubRunDetail {
  child_key: string;
  run_id: string;
  parent_run_id: string;
  status: string;
  step_count: number;
  current_node_id?: string | null;
  error?: string | null;
  steps: SubRunStep[];
  history: { items: RunCheckpoint[] };
}

export interface ForkRunOptions {
  checkpointIndex: number;
  stateUpdate?: Record<string, any>;
}

export interface RunRecord {
  run_id: string;
  workflow_id?: string | null;
  spec_hash: string;
  workflow_version?: string | number | null;
  definition_digest?: string;
  started_workflow_version?: string | number | null;
  origin_definition_digest?: string;
  /**
   * `interrupted`: the process executing the run stopped (its run lease expired); continue it
   * with `runs.recover()`. `paused`: a step's outcome is unknown after a crash
   * (`pause_reason` `effect_unknown`); resolve `pending_effect`, then `runs.recover()`.
   */
  status: "running" | "pending_approval" | "waiting_for_input" | "completed" | "failed" | "cancelled" | "rejected" | "interrupted" | "paused" | "pending";
  current_node_id?: string | null;
  step_count: number;
  input_state: Record<string, any>;
  current_state: Record<string, any>;
  pending_approval?: PendingApproval | null;
  pending_input?: PendingInput | null;
  forked_from?: ForkedFrom | null;
  error?: string | null;
  created_at: number;
  updated_at: number;
  events?: RunEvent[];
  spec?: WorkflowSpec;
  /** True when the run's spec has a sub-workflow node (derived server-side). */
  has_subworkflows?: boolean;
  /** `effect_unknown` while `status` is `paused`; null otherwise. */
  pause_reason?: "effect_unknown" | null;
  /** The effect a paused run waits on (`effect_id`, `node_id`, `step_key`, ...). */
  pending_effect?: Record<string, any> | null;
}

/** Options of `runs.recover()` (`POST /runs/{id}/recover`). */
export interface RecoverRunOptions {
  /**
   * A non-idempotent step dispatched without a recorded outcome: `pause` the run
   * (`pause_reason` `effect_unknown`, the default) or `fail` it.
   */
  onUncertain?: "pause" | "fail";
}

/**
 * Options of `runs.migrate()` (`POST /runs/{id}/migrate`). The target definition is the run's
 * saved workflow on the server; pending approvals and input remain pending.
 */
export interface MigrateRunOptions extends RecoverRunOptions {
  /** Refuse (409) unless the saved workflow's definition digest still equals this value. */
  expectedDefinitionDigest?: string;
  /** Recover the run after migrating (default `true`); `false` only migrates. */
  resume?: boolean;
}

/** Per-run limits accepted by the service's BudgetConfig. Decimal fields accept JSON strings. */
export interface BudgetConfig {
  max_cost_usd?: number | string | null;
  max_tokens?: number | null;
  max_turns?: number | null;
  max_tool_calls?: number | null;
  soft_limit_percent?: number | string;
  conservative_unreported_tokens?: number;
  conservative_unreported_cost_usd?: number | string;
  deny_unknown_pricing?: boolean;
  pricing_version?: string | null;
}

export interface StartRunOptions {
  workflow_id?: string;
  spec?: WorkflowSpec;
  initial_state?: Record<string, any>;
  async_execution?: boolean;
  budget_config?: BudgetConfig | null;
}

export interface DecisionOptions {
  approval_id: string;
  decision: "approve" | "reject";
  state_update?: Record<string, any>;
}

export interface ExportBundleResponse {
  workflow_id: string;
  spec_hash: string;
  bundle_path: string;
  files: string[];
}

export interface PortDefinition {
  name: string;
  type: string;
  description?: string;
  required?: boolean;
}

export interface ModuleDescriptor {
  module_id: string;
  version: string;
  kind: string;
  title: string;
  description: string;
  input_schema: Record<string, any>;
  output_schema: Record<string, any>;
  config_schema: Record<string, any>;
  named_ports: PortDefinition[];
  supported_routes: string[];
  required_credential_types: string[];
  capabilities: Record<string, boolean>;
  effect_policy: "pure" | "read" | "idempotent_write" | "non_idempotent_write" | string;
  min_sdk_version: string;
  verification_status: string;
  is_available: boolean;
  missing_reason?: string | null;
  requires_approval: boolean;
  digest: string;
  /** Service-internal module (auth, workspaces, triggers, ...): still runs in saved workflows,
   * but builders should not offer it as a new node. */
  internal?: boolean;
  display?: {
    label?: string | null;
    icon?: string | null;
    group?: string | null;
    examples?: any[];
  } | null;
}

export interface ValidationIssue {
  node_id?: string | null;
  field: string;
  code: string;
  message: string;
}

export interface WorkflowValidateResponse {
  valid: boolean;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  canonical_hash?: string | null;
  node_count: number;
  estimated_effect_policy: string;
}

export interface ServiceCapabilities {
  sdk_version: string;
  active_profile: string;
  supported_profiles: string[];
  capabilities: Record<string, boolean>;
  supported_credential_types: string[];
  supported_export_formats: string[];
  /** Default capabilities declared by each built-in provider adapter. */
  provider_capabilities?: Record<
    string,
    {
      can_generate: boolean;
      can_stream: boolean;
      can_embed: boolean;
      can_tools: boolean;
      can_structured_output: boolean;
      can_multimodal: boolean;
      reports_usage: boolean;
      final_only: boolean;
    }
  >;
}

export interface StreamEventsOptions {
  cursor?: number;
  live?: boolean;
  signal?: AbortSignal;
}

export interface ToolCallItem {
  id: string;
  name: string;
  arguments: Record<string, any>;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  name?: string | null;
  tool_call_id?: string | null;
  tool_calls?: ToolCallItem[];
}

export interface SessionListItem {
  session_id: string;
  revision: number;
  message_count: number;
  last_message?: string | null;
}

export interface SessionDetail {
  session_id: string;
  revision: number;
  messages: ChatMessage[];
  workspace_id?: string;
}

export interface ToolExecutionTrace {
  tool: string;
  arguments: Record<string, any>;
  output: any;
  timestamp: number;
}

/** Chat turn usage as reported by the model; null means the model did not report it. */
export interface TokenUsage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  is_complete: boolean;
}

export interface ChatResponse {
  session_id: string;
  revision: number;
  messages: ChatMessage[];
  reply: string;
  model: string;
  /** "final_only": the reply arrives whole. "streamed": the model produced text deltas on `chatStream()`. */
  delivery: "final_only" | "streaming" | "streamed";
  /** Configured model name chosen from `chat.models()`. */
  model_name?: string;
  provider?: string;
  tool_executions: ToolExecutionTrace[];
  usage: TokenUsage;
  latency_ms: number;
  /** Set when the turn ran a saved agent (`agent_id` in the request). */
  agent_id?: string;
  /** Tool calls of a saved agent that needed approval and were therefore not run. */
  refused_tool_calls?: { id: string; tool: string }[];
}

/** An operator-allowed chat model from `GET /chat/models` (never includes secrets). */
export interface ChatModelInfo {
  name: string;
  provider: string;
  label: string;
  offline: boolean;
  streaming: boolean;
  default: boolean;
}

/** One item from `SessionsClient.chatStream()`. */
export type ChatStreamEvent =
  | { kind: "run_event"; seq: number; type: string; timestamp: number; data: Record<string, any> }
  | { kind: "chat_completed"; seq: number; response: ChatResponse }
  | { kind: "chat_failed"; seq: number; code: string; message: string; persisted: false };

export interface SendChatMessageOptions {
  message: string;
  language?: "en" | "hi";
  model?: string;
  max_turns?: number;
  timeout?: number;
  /**
   * Chat with an agent saved in this workspace (`agents.create()`). It runs on its own model;
   * omit `model` or send the same name. Tools that need approval are refused, never run.
   */
  agent_id?: string;
}

export interface KnowledgeCollection {
  collection_id: string;
  name: string;
  description?: string;
}

export interface KnowledgeDocument {
  document_id: string;
  collection_id: string;
  filename: string;
  mime_type: string;
  content_hash: string;
  text_length: number;
  pages_count: number;
  created_at: number;
  metadata?: Record<string, any>;
}

export interface IngestDocumentOptions {
  collection_id?: string;
  filename: string;
  content: string;
  mime_type?: string;
  metadata?: Record<string, any>;
}

export interface CitationItem {
  citation_id: string;
  document_id: string;
  origin: string;
  page_start: number;
  page_end: number;
  start_char: number;
  end_char: number;
  snippet: string;
}

export interface RetrievedChunkItem {
  chunk_id: string;
  document_id: string;
  origin: string;
  text: string;
  score: number;
  dense_score?: number | null;
  lexical_score?: number | null;
}

export interface RetrieveResponse {
  query: string;
  collection_id: string;
  workspace_id: string;
  has_sufficient_evidence: boolean;
  total_tokens: number;
  citations: CitationItem[];
  items: RetrievedChunkItem[];
  latency_ms: number;
}

export interface AnswerResponse {
  query: string;
  answer: string;
  has_sufficient_evidence: boolean;
  citations: CitationItem[];
  retrieval_duration_ms: number;
  generation_duration_ms: number;
  model: string;
}

export interface CredentialMetadataItem {
  credential_id: string;
  workspace_id: string;
  credential_type: string;
  provider: string;
  description: string;
  environment_variable?: string | null;
  status: 'active' | 'revoked' | 'expired';
  created_at: number;
  last_verified_at?: number | null;
}

export interface RunAttemptDiagnostics {
  id: string;
  node_id: string;
  identity_source: "sdk_node" | "legacy_handler";
  module_id: string | null;
  status: string;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  input: unknown;
  output: unknown;
  error: string | null;
  attempt_id?: string | null;
  provider_call_id?: string | null;
  reservation_id?: string | null;
  approval_id?: string | null;
  effect_id?: string | null;
  cached_input_tokens?: number | null;
  cache_creation_tokens?: number | null;
  calculated_cost_usd?: string | null;
  cost_state?: string | null;
  is_aggregate?: boolean;
  context_decision?: ContextDecisionRecord | null;
}

export interface ContextItemRecord {
  item_id: string;
  item_type: string;
  role?: string | null;
  tool_call_id?: string | null;
  source_provenance?: string | null;
  decision: "included" | "omitted" | "projected" | "compacted";
  reason: string;
  is_protected: boolean;
  protected_reasons: string[];
  original_tokens: number;
  projected_tokens: number;
  content_preview?: string | null;
}

export interface ContextDecisionRecord {
  record_id: string;
  run_id?: string | null;
  node_id?: string | null;
  policy_version: string;
  status: "applied" | "bypassed_short_context" | "bypassed_disabled" | "overflow_failed";
  initial_tokens: number;
  projected_tokens: number;
  reduction_ratio: number;
  items: ContextItemRecord[];
  overflow_error?: string | null;
  timestamp: number;
  metadata?: Record<string, any>;
}

export interface ContextPolicy {
  policy_id?: string;
  version?: string;
  enabled?: boolean;
  max_context_tokens?: number;
  short_context_threshold_tokens?: number;
  overflow_strategy?: "fail" | "compact_tables" | "truncate_unprotected";
  protect_instructions?: boolean;
  protect_corrections?: boolean;
  protect_negations?: boolean;
  protect_identifiers?: boolean;
  protect_financial_amounts?: boolean;
  protect_approval_arguments?: boolean;
  protect_tool_associations?: boolean;
  protect_required_evidence?: boolean;
  json_projections?: Record<string, string[]>;
  max_table_rows?: number;
  max_log_lines?: number;
}

export interface RunDiagnostics {
  run_id: string;
  status: string;
  source: "persisted_run_events";
  content_included: boolean;
  content_retention_status?: string;
  attempts: RunAttemptDiagnostics[];
  attempt_count: number;
  truncated_attempts: number;
  context_decisions?: ContextDecisionRecord[];
  usage: {
    aggregation: "module_result_tally";
    known_input_tokens: number;
    known_output_tokens: number;
    total_tokens?: number;
    cached_input_tokens?: number;
    cache_creation_tokens?: number;
    reported_attempts: number;
    unreported_attempts: number;
    complete: boolean;
    cost: number | null;
    cost_status: "not_reported" | "reported" | "estimated" | "partial" | "unknown" | string;
    calculated_cost_usd?: string | null;
    reported_cost_usd?: string | null;
    estimated_cost_usd?: string | null;
    uncertain_cost_usd?: string | null;
    currency?: string;
    pricing_version?: string | null;
    is_provider_invoice?: boolean;
    disclaimer?: string;
    coverage_ratio?: number;
    unknown_usage_count?: number;
    incomplete_coverage_explanation?: string;
    cohort_attempt_count?: number;
    task_success_count?: number;
    successful_task_cost_usd?: string | null;
    successful_task_cost_status?: string;
  };
  trace_export: Record<string, unknown> | null;
  trace_export_unavailable_reason: string;
}

export interface CreateCredentialOptions {
  credential_id: string;
  credential_type: string;
  provider?: string;
  description?: string;
  secret: string;
  environment_variable?: string | null;
}

export interface VerifyCredentialResult {
  credential_id: string;
  status: 'valid' | 'invalid' | 'revoked';
  message: string;
  checked_at: number;
}

export interface FixtureMetadata {
  id: string;
  name: string;
  description: string;
}

export interface SubsystemStatus {
  available: boolean;
  missing_reason: string | null;
  fixtures: FixtureMetadata[];
}

export interface IntegrationStatus {
  mcp: SubsystemStatus;
  openapi: SubsystemStatus;
}

export interface McpDiscoveredTool {
  name: string;
  description: string;
  input_schema: Record<string, any>;
  digest: string;
  effect_policy: string;
}

export interface McpDiscoverOptions {
  server_name?: string;
  transport_type?: 'in_memory' | 'stdio';
  fixture_id?: string | null;
  command?: string | null;
  args?: string[] | null;
  env?: Record<string, string> | null;
}

export interface McpDiscoverResponse {
  server_name: string;
  tools: McpDiscoveredTool[];
  is_fixture: boolean;
}

export interface McpTestRunOptions {
  server_name?: string;
  tool_name: string;
  arguments?: Record<string, any>;
  fixture_id?: string | null;
  transport_type?: 'in_memory' | 'stdio';
  command?: string | null;
  args?: string[] | null;
}

export interface McpTestRunResult {
  status: 'success' | 'error';
  output?: any;
  content: Array<{ type?: string; text?: string; [key: string]: any }>;
  error?: string | null;
  is_fixture: boolean;
  execution_label: string;
  duration_ms: number;
}

export interface OpenAPIDiscoveredOperation {
  operation_id: string;
  module_id: string;
  method: string;
  path: string;
  summary: string;
  description: string;
  input_schema: Record<string, any>;
  output_schema: Record<string, any>;
  effect_policy: string;
  is_idempotent: boolean;
}

export interface OpenAPIDiscoverOptions {
  spec_content?: string | null;
  spec_dict?: Record<string, any> | null;
  url?: string | null;
  fixture_id?: string | null;
  prefix?: string;
}

export interface OpenAPIDiscoverResponse {
  prefix: string;
  operations: OpenAPIDiscoveredOperation[];
  is_fixture: boolean;
}

export interface OpenAPITestRunOptions {
  operation_id: string;
  prefix?: string;
  inputs?: Record<string, any>;
  fixture_id?: string | null;
  spec_content?: string | null;
  spec_dict?: Record<string, any> | null;
  base_url?: string | null;
  mock_response?: Record<string, any> | null;
}

export interface OpenAPITestRunResult {
  status: 'success' | 'error';
  status_code: number;
  body: any;
  headers: Record<string, string>;
  error?: string | null;
  is_fixture: boolean;
  execution_label: string;
  duration_ms: number;
}

export interface IntegrationRegisterOptions {
  kind: 'mcp' | 'openapi';
  server_name_or_prefix: string;
  fixture_id?: string | null;
  tools?: string[] | null;
}

export interface IntegrationRegisterResponse {
  registered_modules: string[];
  count: number;
}

export interface WorkflowImportOptions {
  spec?: Record<string, any>;
  spec_json?: string;
  archive_base64?: string;
  name?: string;
  workflow_id?: string;
}

// ---------------------------------------------------------------------------
// Evaluations, Datasets & Benchmarks
// ---------------------------------------------------------------------------

export interface DatasetExample {
  example_id?: string;
  input_data: Record<string, any>;
  expected_output?: any;
  expected_citations?: string[] | null;
  expected_trajectory?: (TrajectoryStep | string)[] | null;
  simulation?: UserSimulation | null;
  tags?: string[];
  metadata?: Record<string, any>;
}

export type TrajectoryMode = "exact" | "strict" | "subset" | "in_order" | "any_order";

export interface TrajectoryStep {
  name: string;
  args?: Record<string, unknown>;
}

export interface ArgumentMatch {
  normalisers?: ("strip" | "casefold" | "whitespace" | "numeric")[];
  absolute_tolerance?: number;
  relative_tolerance?: number;
}

/** Exactly one of scripted turns or a persona backed by a host-injected model. */
export type UserSimulation = {
  max_turns?: number;
  stop_on_agent?: string[];
  stop_on_user?: string[];
} & ({ turns: string[]; persona?: never } | { persona: string; turns?: never });

export interface StoredTrace {
  run_id: string;
  timestamp: number;
  workflow_id?: string | null;
  agent_id?: string | null;
  output?: unknown;
  events?: Record<string, unknown>[];
  messages?: Record<string, unknown>[];
}

/** Graders accepted by the online and simulation routes. */
export interface TraceGraderSpec {
  type?: "tool_trajectory" | "tool_argument_accuracy" | "exact_match" | "contains" | "substring";
  mode?: TrajectoryMode;
  check_arguments?: boolean;
  argument_rules?: Record<string, ArgumentMatch>;
}

/** Stable sampling and inclusive UTC epoch-second filters over stored traces. */
export interface OnlineEvalConfig {
  sampling_rate?: number;
  seed?: number;
  workflow_id?: string | null;
  agent_id?: string | null;
  start_time?: number | null;
  end_time?: number | null;
}

export interface TraceEvaluationOptions {
  example: DatasetExample;
  graders?: TraceGraderSpec[];
  config?: OnlineEvalConfig;
}

export interface SimulationEvaluationOptions {
  dataset_id: string;
  dataset_version?: string | null;
  agent_id?: string;
  graders?: TraceGraderSpec[];
}

export interface GraderCapabilities {
  types: string[];
  trajectory_modes: TrajectoryMode[];
  argument_rule_schema: Record<string, unknown>;
  result_schema: Record<string, unknown>;
  simulation_schema: Record<string, unknown>;
  online_config_schema: Record<string, unknown>;
}

export interface TraceEvaluationResult {
  example_id: string;
  passed: boolean;
  actual_output: unknown;
  graders: {
    grader_name: string;
    grader_kind: "deterministic" | "model_judge";
    passed: boolean;
    score: number;
    reason: string;
    details: Record<string, unknown>;
    is_synthetic_judge: boolean;
    quality_evidence: "verified_human" | "model_judge" | "mock_simulated";
  }[];
  overall_score: number;
  latency_ms: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  cost_usd: string;
  error: string | null;
  metadata: Record<string, unknown>;
}

/** Stored evaluation report returned by online and simulation grading. */
export interface TraceEvaluationSummary {
  run_id: string;
  dataset_id: string;
  dataset_version: string;
  dataset_digest: string;
  dataset_hash: string;
  workflow_version: string | null;
  spec_hash: string | null;
  workflow_spec_hash: string | null;
  seed: number | null;
  timestamp: number;
  total_examples: number;
  passed_examples: number;
  failed_examples: number;
  pass_rate: number;
  mean_score: number;
  grader_scores: Record<string, number>;
  mean_latency_ms: number;
  total_latency_ms: number;
  total_cost_usd: string;
  total_tokens: number;
  results: TraceEvaluationResult[];
  is_synthetic_evidence: boolean;
  budget_halted: boolean;
  metadata: Record<string, unknown>;
}

export interface EvaluationDataset {
  dataset_id: string;
  version: string;
  description?: string;
  examples: DatasetExample[];
  created_at: number;
  updated_at: number;
  is_builtin?: boolean;
}

export interface DatasetSummary {
  dataset_id: string;
  version: string;
  description?: string;
  example_count: number;
  is_builtin?: boolean;
}

export interface CreateDatasetOptions {
  dataset_id: string;
  version?: string;
  description?: string;
  examples: DatasetExample[];
}

export interface ExampleEvaluationResult {
  example_id: string;
  passed: boolean;
  score: number;
  latency_ms: number;
  output: any;
  expected_output?: any;
  grader_results: Record<string, any>;
  error?: string | null;
}

export interface EvaluationSummary {
  run_id: string;
  dataset_id: string;
  model_id: string;
  workflow_id?: string | null;
  total_examples: number;
  passed_examples: number;
  pass_rate: number;
  mean_score: number;
  mean_latency_ms: number;
  created_at: number;
  metadata?: Record<string, any>;
  results?: ExampleEvaluationResult[];
}

export interface RunEvaluationOptions {
  dataset_id: string;
  model_id?: string;
  workflow_id?: string;
  graders?: string[];
  metadata?: Record<string, any>;
}

export interface EvaluationComparison {
  base_run_id: string;
  candidate_run_id: string;
  pass_rate_diff: number;
  mean_score_diff: number;
  latency_diff_ms: number;
  regressions: string[];
  improvements: string[];
  total_compared: number;
}

// ---------------------------------------------------------------------------
// Schedules, Signed Webhooks & Observable Dry-Run Outbox
// ---------------------------------------------------------------------------

export interface ScheduleConfig {
  schedule_id: string;
  workspace_id: string;
  workflow_id: string;
  cron?: string | null;
  interval_seconds?: number | null;
  timezone: string;
  missed_run_policy: "fire_once" | "skip" | "burst";
  paused: boolean;
  created_at: number;
  updated_at: number;
  last_run_at?: number | null;
  next_run_at?: number | null;
}

export interface CreateScheduleOptions {
  schedule_id: string;
  workflow_id: string;
  cron?: string | null;
  interval_seconds?: number | null;
  timezone?: string;
  missed_run_policy?: "fire_once" | "skip" | "burst";
}

export interface WebhookConfig {
  endpoint_id: string;
  workspace_id: string;
  workflow_id: string;
  release_id?: string | null;
  secret: string;
  replay_window_seconds: number;
  created_at: number;
  /** Remote single-operator mode only: this endpoint's delivery token, returned once. */
  delivery_token?: string;
}

/** A new webhook delivery token (remote single-operator mode). */
export interface DeliveryTokenResult {
  endpoint_id: string;
  delivery_token: string;
  delivery_token_header: string;
}

export interface RegisterWebhookOptions {
  endpoint_id: string;
  workflow_id: string;
  release_id?: string | null;
  secret?: string;
  replay_window_seconds?: number;
}

export interface TestWebhookOptions {
  event_id?: string;
  payload?: Record<string, any>;
  invalid_signature?: boolean;
  expired_timestamp?: boolean;
}

export interface TestWebhookResult {
  success: boolean;
  job_id: string;
  is_duplicate: boolean;
  status: string;
  timestamp_used: number;
  signature_used: string;
}

export interface DurableTriggerJob {
  job_id: string;
  workspace_id: string;
  workflow_id: string;
  trigger_type: "schedule" | "webhook";
  trigger_source_id: string;
  status: "pending" | "claimed" | "completed" | "failed";
  payload: Record<string, any>;
  scheduled_for: number;
  created_at: number;
  claimed_at?: number | null;
  completed_at?: number | null;
  release_id?: string;
  claimed_by?: string | null;
  error_message?: string | null;
  /** Run started by the trigger worker for this job; null until the job has run. */
  run_id?: string | null;
}

export interface OutboxReceipt {
  receipt_id: string;
  message_id: string;
  channel: "email" | "whatsapp";
  recipient: string;
  status: "dry_run" | "deduplicated" | "delivered" | "failed" | "queued";
  dry_run: boolean;
  rendered_content: Record<string, any>;
  created_at: number;
  metadata?: Record<string, any>;
}

export interface TestDispatchOutboxOptions {
  channel: "email" | "whatsapp";
  recipient: string;
  subject_or_template?: string;
  body: string;
  dedup_key?: string;
  metadata?: Record<string, any>;
}

export interface TestDispatchOutboxResult {
  success: boolean;
  receipt: OutboxReceipt;
  is_duplicate: boolean;
  dry_run: boolean;
  guarantee: string;
}

/** Per-run caps of a saved agent; each is bounded by a server maximum (`AgentServerLimits`). */
export interface AgentLimits {
  max_turns?: number;
  max_tool_calls?: number;
  timeout_seconds?: number;
}

/**
 * A declarative agent spec: references and text only, no code. `model` is a name from
 * `sessions.models()`, `tools` are module ids, `knowledge` collection ids, `skills` workspace
 * skill names read at `skills_label`.
 */
export interface AgentSpec {
  name: string;
  description?: string;
  instructions?: string;
  model?: string;
  tools?: string[];
  knowledge?: string[];
  skills?: string[];
  skills_label?: string;
  output_schema?: Record<string, any> | null;
  limits?: AgentLimits;
}

/** How one tool of a saved agent is exposed; writes always need approval. */
export interface AgentToolPolicy {
  module_id: string;
  tool_name: string;
  available: boolean;
  requires_approval: boolean;
}

export interface AgentRecord {
  agent_id: string;
  workspace_id: string;
  revision: number;
  spec: Required<Omit<AgentSpec, "limits">> & { limits: Required<AgentLimits> };
  created_at: number;
  updated_at: number;
  created_by: string;
  updated_by: string;
  tools: AgentToolPolicy[];
}

export interface AgentSummary {
  agent_id: string;
  name: string;
  description: string;
  model: string;
  tool_count: number;
  /** Tool module ids, in spec order. */
  tools?: string[];
  /** The tool module ids whose calls need approval. */
  approval_tools?: string[];
  revision: number;
  updated_at: number;
}

export interface AgentServerLimits {
  max_agents_per_workspace: number;
  max_spec_chars: number;
  max_instructions_chars: number;
  max_tools: number;
  max_knowledge_collections: number;
  max_skills: number;
  max_turns: number;
  max_tool_calls: number;
  max_timeout_seconds: number;
}

export interface AgentListResponse {
  items: AgentSummary[];
  total: number;
  limits: AgentServerLimits;
}

/** A module the workspace may attach as an agent tool. */
export interface AgentToolOption {
  module_id: string;
  tool_name: string;
  title: string;
  description: string;
  kind: string;
  requires_approval: boolean;
}

/** Everything a spec may name in this workspace (`GET /agents/options`). */
export interface AgentOptions {
  models: ChatModelInfo[];
  tools: AgentToolOption[];
  knowledge_collections: string[];
  skills: string[];
  node_module_id: string;
  limits: AgentServerLimits;
}

/** One field problem of a refused spec (`NexuraApiError.details.field_errors`). */
export interface AgentFieldError {
  field: string;
  message: string;
}

// ---------------------------------------------------------------------------
// Skills
// ---------------------------------------------------------------------------

export interface SkillItem {
  name: string;
  version: number;
  labels: string[];
  created_at: number;
  updated_at?: number;
  description?: string;
}

export interface SkillDetail {
  name: string;
  version: number;
  markdown: string;
  resources: Record<string, string>;
  labels: string[];
  created_at: number;
  created_by?: string;
}

export interface SkillVersionsResponse {
  name: string;
  versions: Array<{ version: number; labels: string[]; created_at: number }>;
}

export interface CreateSkillOptions {
  markdown: string;
  resources?: Record<string, string>;
  label?: string;
}

// ---------------------------------------------------------------------------
// Templates
// ---------------------------------------------------------------------------

export interface TemplateSummary {
  template_id: string;
  name: string;
  description: string;
  category: string;
  tags: string[];
  step_count: number;
  has_approval_gate: boolean;
}

export interface TemplateDetail extends TemplateSummary {
  spec: WorkflowSpec;
  mermaid: string;
  sample_input?: Record<string, any>;
}

export interface TemplateCatalogResponse {
  items: TemplateSummary[];
  total: number;
  categories: string[];
  tags: string[];
}

export interface InstantiateTemplateOptions {
  name?: string;
  workflowId?: string;
}

// ---------------------------------------------------------------------------
// Builder
// ---------------------------------------------------------------------------

export interface BuilderDraftOptions {
  description: string;
  credential?: string;
  model?: string;
  modelRef?: string;
  maxAttempts?: number;
}

export interface BuilderDraftResponse {
  spec: WorkflowSpec;
  explanation: string;
  warnings: string[];
  attempts: number;
  saved: boolean;
  model: string;
  validation: WorkflowValidateResponse;
  errors?: any[];
  unknown_modules?: string[];
}

// ---------------------------------------------------------------------------
// Studio Evaluations
// ---------------------------------------------------------------------------

export interface StudioDatasetSummary {
  dataset_id: string;
  name: string;
  description?: string;
  case_count: number;
  revision: number;
  updated_at: number;
  workflow_id?: string | null;
}

export interface StudioDatasetCase {
  name: string;
  input: Record<string, any>;
  expected_output?: any;
  tags?: string[];
  expected_trajectory?: string[] | null;
  metadata?: Record<string, any>;
}

export interface StudioDatasetDetail {
  dataset_id: string;
  name: string;
  description?: string;
  cases: StudioDatasetCase[];
  revision: number;
  content_hash: string;
  workflow_id?: string | null;
  created_at: number;
  updated_at: number;
}

export interface PutStudioDatasetOptions {
  name?: string;
  description?: string;
  cases?: any[];
  expected_revision?: number;
  format?: string;
  format_version?: number;
  workflow_id?: string | null;
}

export interface AddCaseFromRunOptions {
  runId: string;
  datasetName?: string;
  caseName?: string;
  outputKey?: string;
  recordPath?: boolean;
  expectedRevision?: number;
}

export interface StudioRunTarget {
  kind: "workflow" | "agent";
  id: string;
  name?: string;
  version?: string | null;
  output_key?: string;
}

export interface StudioRunOptions {
  datasetId: string;
  target: StudioRunTarget;
  label?: string;
  repeats?: number;
  scorers?: any[];
}

export interface StudioRedteamOptions {
  workflowId?: string;
  agentId?: string;
  writeTools?: string[];
}

// ---------------------------------------------------------------------------
// Run Traces
// ---------------------------------------------------------------------------

export interface RunTraceSpan {
  span_id: string;
  parent_id?: string | null;
  name: string;
  kind: string;
  status: string;
  start_time_ms: number;
  duration_ms: number;
  input?: unknown;
  output?: unknown;
  error?: string | null;
  attributes?: Record<string, any>;
}

export interface RunTrace {
  run_id: string;
  status: string;
  include_content: boolean;
  truncated: boolean;
  span_count: number;
  duration_ms: number;
  spans: RunTraceSpan[];
}

// ---------------------------------------------------------------------------
// Python Workflow Code
// ---------------------------------------------------------------------------

export interface WorkflowPythonExportResponse {
  workflow_id: string;
  filename: string;
  spec_hash: string;
  source: string;
}

export interface ImportPythonWorkflowOptions {
  source: string;
  name?: string;
  workflowId?: string;
}

// ---------------------------------------------------------------------------
// Knowledge Limits & Chunks
// ---------------------------------------------------------------------------

export interface KnowledgeLimitsResponse {
  max_file_bytes: number;
  max_workspace_bytes: number;
  supported_mime_types: string[];
  workspace_used_bytes: number;
}

export interface KnowledgeChunkDetail {
  chunk_id: string;
  document_id: string;
  chunk_index: number;
  text: string;
  origin: string;
  title: string;
  source_uri?: string | null;
  page_start?: number;
  page_end?: number;
  start_char?: number;
  end_char?: number;
}

// ---------------------------------------------------------------------------
// Workspaces
// ---------------------------------------------------------------------------

export interface WorkspaceRecord {
  workspace_id: string;
  name: string;
  description?: string | null;
  created_at: number;
  require_promotion_approval?: boolean;
}

