// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/**
 * TypeScript HTTP client implementation for the Nexura Service API.
 */

import type {
  ExecutionEffect,
  ResolveEffectOptions,
  AgentListResponse,
  AgentOptions,
  AgentRecord,
  AgentSpec,
  AnswerResponse,
  ChatModelInfo,
  ChatResponse,
  ChatStreamEvent,
  DecisionOptions,
  ExportBundleResponse,
  IngestDocumentOptions,
  KnowledgeCollection,
  KnowledgeDocument,
  ModuleDescriptor,
  PaginatedResponse,
  RetrieveResponse,
  RunEvent,
  RunRecord,
  RunCheckpoint,
  SubRunDetail,
  ForkRunOptions,
  RunDiagnostics,
  SendChatMessageOptions,
  ServiceCapabilities,
  SessionDetail,
  SessionListItem,
  StartRunOptions,
  StreamEventsOptions,
  WorkflowRecord,
  WorkflowSpec,
  WorkflowValidateResponse,
  CredentialMetadataItem,
  CreateCredentialOptions,
  VerifyCredentialResult,
  IntegrationStatus,
  McpDiscoverOptions,
  McpDiscoverResponse,
  McpTestRunOptions,
  McpTestRunResult,
  OpenAPIDiscoverOptions,
  OpenAPIDiscoverResponse,
  OpenAPITestRunOptions,
  OpenAPITestRunResult,
  IntegrationRegisterOptions,
  IntegrationRegisterResponse,
  WorkflowImportOptions,
  DatasetExample,
  EvaluationDataset,
  DatasetSummary,
  CreateDatasetOptions,
  ExampleEvaluationResult,
  EvaluationSummary,
  RunEvaluationOptions,
  EvaluationComparison,
  ScheduleConfig,
  CreateScheduleOptions,
  WebhookConfig,
  RegisterWebhookOptions,
  TestWebhookOptions,
  TestWebhookResult,
  DurableTriggerJob,
  OutboxReceipt,
  TestDispatchOutboxOptions,
  TestDispatchOutboxResult,
  SkillItem,
  SkillDetail,
  SkillVersionsResponse,
  CreateSkillOptions,
  TemplateSummary,
  TemplateDetail,
  TemplateCatalogResponse,
  InstantiateTemplateOptions,
  BuilderDraftOptions,
  BuilderDraftResponse,
  StudioDatasetSummary,
  StudioDatasetDetail,
  PutStudioDatasetOptions,
  AddCaseFromRunOptions,
  StudioRunOptions,
  StudioRedteamOptions,
  RunTrace,
  WorkflowPythonExportResponse,
  ImportPythonWorkflowOptions,
  KnowledgeLimitsResponse,
  KnowledgeChunkDetail,
  WorkspaceRecord,
} from "./types.js";
import { parseSseData, readSse } from "./sse.js";

/** "field: message" text for FastAPI validation items ({loc, msg}); "" when none apply. */
export function describeValidationErrors(items: unknown[]): string {
  return items
    .map((item: any) => {
      if (!item || typeof item.msg !== "string") return "";
      const loc = Array.isArray(item.loc)
        ? item.loc.filter((part: unknown) => part !== "body" && part !== "query" && part !== "path")
        : [];
      return loc.length ? `${loc.join(".")}: ${item.msg}` : item.msg;
    })
    .filter(Boolean)
    .join("; ");
}

export class NexuraApiError extends Error {
  public readonly statusCode: number;
  public readonly code: string;
  public readonly details?: any;

  constructor(statusCode: number, code: string, message: string, details?: any) {
    super(`[${statusCode}] ${code}: ${message}`);
    this.statusCode = statusCode;
    this.code = code;
    this.details = details;
    this.name = "NexuraApiError";
  }
}

export interface ClientConfig {
  baseUrl?: string;
  apiKey?: string;
  workspaceId?: string;
  /**
   * The local server's per-session token, sent as `X-Nexura-Session` on every state-changing
   * request (POST, PUT, PATCH, DELETE). `nexura-runtime serve` requires it.
   */
  sessionToken?: string;
  /**
   * When the server answers 403 `SESSION_TOKEN_REQUIRED` / `SESSION_TOKEN_INVALID` (a restart
   * changes the token), read it from `GET /api/v1/local-session` and retry once. Default true.
   * The server only hands the token to its own origin (Studio) and to local non-browser clients.
   */
  autoSessionToken?: boolean;
}

/** Header that carries the local server's per-session token. */
export const SESSION_HEADER = "X-Nexura-Session";
const SESSION_TOKEN_CODES = new Set(["SESSION_TOKEN_REQUIRED", "SESSION_TOKEN_INVALID"]);
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

async function isSessionTokenRefusal(response: Response): Promise<boolean> {
  if (response.status !== 403) return false;
  try {
    const detail = (await response.clone().json())?.detail;
    return typeof detail === "object" && detail !== null && SESSION_TOKEN_CODES.has(detail.code);
  } catch {
    return false;
  }
}

/**
 * Client for the routes the open-source Nexura service serves. Clients for the closed edition's
 * hosted routes (organisations, publishing, the hosted secrets admin API and workspace members)
 * are not part of this package; the closed edition ships them as an add-on that wraps this client.
 */
export class NexuraClient {
  public readonly baseUrl: string;
  public readonly apiKey?: string;
  public readonly workspaceId: string;
  /** Current per-session token (set from the config or read from the server). */
  public sessionToken?: string;
  public readonly autoSessionToken: boolean;

  public readonly workflows: WorkflowsClient;
  public readonly runs: RunsClient;
  public readonly modules: ModulesClient;
  public readonly capabilities: CapabilitiesClient;
  public readonly workspaces: WorkspacesClient;
  public readonly sessions: SessionsClient;
  public readonly knowledge: KnowledgeClient;
  public readonly credentials: CredentialsClient;
  public readonly integrations: IntegrationsClient;
  public readonly evaluations: EvaluationsClient;
  public readonly triggers: TriggersClient;
  public readonly agents: AgentsClient;
  public readonly skills: SkillsClient;
  public readonly templates: TemplatesClient;
  public readonly builder: BuilderClient;

  constructor(config: ClientConfig = {}) {
    // Matches `nexura-runtime serve`, which listens on 127.0.0.1:8765 by default.
    this.baseUrl = (config.baseUrl || "http://127.0.0.1:8765").replace(/\/+$/, "");
    this.apiKey = config.apiKey;
    this.workspaceId = config.workspaceId || "default";
    this.sessionToken = config.sessionToken || undefined;
    this.autoSessionToken = config.autoSessionToken ?? true;

    this.workflows = new WorkflowsClient(this);
    this.runs = new RunsClient(this);
    this.modules = new ModulesClient(this);
    this.capabilities = new CapabilitiesClient(this);
    this.workspaces = new WorkspacesClient(this);
    this.sessions = new SessionsClient(this);
    this.knowledge = new KnowledgeClient(this);
    this.credentials = new CredentialsClient(this);
    this.integrations = new IntegrationsClient(this);
    this.evaluations = new EvaluationsClient(this);
    this.triggers = new TriggersClient(this);
    this.agents = new AgentsClient(this);
    this.skills = new SkillsClient(this);
    this.templates = new TemplatesClient(this);
    this.builder = new BuilderClient(this);
  }

  public withWorkspace(workspaceId: string): NexuraClient {
    return new NexuraClient({
      baseUrl: this.baseUrl,
      apiKey: this.apiKey,
      workspaceId,
      sessionToken: this.sessionToken,
      autoSessionToken: this.autoSessionToken,
    });
  }

  /** Read the local server's per-session token (`GET /api/v1/local-session`) and keep it. */
  public async fetchSessionToken(): Promise<string> {
    const response = await fetch(this.buildUrl("/local-session").toString(), {
      headers: { Accept: "application/json", "X-Workspace-ID": this.workspaceId },
    });
    let token: unknown;
    try {
      token = response.ok ? (await response.json())?.token : undefined;
    } catch {
      token = undefined;
    }
    if (typeof token !== "string" || !token) {
      throw new NexuraApiError(
        response.status,
        "SESSION_TOKEN_UNAVAILABLE",
        "The server did not return a session token."
      );
    }
    this.sessionToken = token;
    return token;
  }

  /** Adds `X-Nexura-Session` to `headers` for a state-changing `method` when a token is known. */
  public withSessionHeader(method: string, headers: Record<string, string>): Record<string, string> {
    if (this.sessionToken && !SAFE_METHODS.has(method.toUpperCase())) {
      headers[SESSION_HEADER] = this.sessionToken;
    }
    return headers;
  }

  /**
   * After a refused state-changing request, read the token again when the refusal was for the
   * session token. Resolves true when a new token is available (the caller retries once).
   */
  public async renewSessionToken(method: string, response: Response): Promise<boolean> {
    if (!this.autoSessionToken || SAFE_METHODS.has(method.toUpperCase())) return false;
    if (!(await isSessionTokenRefusal(response))) return false;
    const previous = this.sessionToken;
    try {
      await this.fetchSessionToken();
    } catch {
      return false;
    }
    return this.sessionToken !== previous;
  }

  public buildUrl(path: string): URL {
    const cleanPath = path.startsWith("/") ? path : `/${path}`;
    const raw = `${this.baseUrl}/api/v1${cleanPath}`.replace(/\/api\/api\/v1/, "/api/v1");
    if (raw.startsWith("http://") || raw.startsWith("https://")) {
      return new URL(raw);
    }
    const origin =
      typeof window !== "undefined" && window.location && window.location.origin
        ? window.location.origin
        : "http://127.0.0.1:8765";
    return new URL(raw, origin);
  }

  public async request<T = any>(
    method: string,
    path: string,
    options: { params?: Record<string, any>; body?: any; signal?: AbortSignal } = {}
  ): Promise<T> {
    const url = this.buildUrl(path);
    if (options.params) {
      for (const [key, value] of Object.entries(options.params)) {
        if (value !== undefined && value !== null) {
          url.searchParams.set(key, String(value));
        }
      }
    }

    const send = () => {
      const headers: Record<string, string> = {
        Accept: "application/json",
        "X-Workspace-ID": this.workspaceId,
      };
      if (this.apiKey) {
        headers.Authorization = `Bearer ${this.apiKey}`;
      }
      if (options.body) {
        headers["Content-Type"] = "application/json";
      }
      return fetch(url.toString(), {
        method,
        headers: this.withSessionHeader(method, headers),
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: options.signal,
      });
    };

    let response = await send();
    if (!response.ok && (await this.renewSessionToken(method, response))) {
      response = await send();
    }

    if (!response.ok) {
      let code = "HTTP_ERROR";
      let message = response.statusText;
      let details: any = undefined;
      try {
        const errorJson = await response.json();
        const detail = errorJson.detail;
        if (Array.isArray(detail)) {
          // FastAPI request validation: a list of {loc, msg, type}.
          code = "VALIDATION_ERROR";
          details = detail;
          message = describeValidationErrors(detail) || message;
        } else if (typeof detail === "object" && detail !== null) {
          code = detail.code || code;
          message = detail.message || message;
          details = detail.errors || detail.details || detail;
          if (Array.isArray(details) && details.length > 0) {
            const extra = details
              .map((d: any) =>
                d.message
                  ? d.node_id
                    ? `[${d.node_id}] ${d.message}`
                    : d.message
                  : describeValidationErrors([d]) || JSON.stringify(d)
              )
              .join("; ");
            if (extra && !message.includes(extra)) {
              message = `${message} (${extra})`;
            }
          }
        } else if (typeof detail === "string") {
          message = detail;
        }
      } catch {
        // Fallback to text if not json
      }
      throw new NexuraApiError(response.status, code, message, details);
    }

    return (await response.json()) as T;
  }
}

export class WorkflowsClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async list(
    options: { limit?: number; offset?: number; query?: string } = {}
  ): Promise<PaginatedResponse<WorkflowRecord>> {
    return this.client.request<PaginatedResponse<WorkflowRecord>>("GET", "/workflows", {
      params: options,
    });
  }

  public async delete(workflowId: string): Promise<{ deleted: boolean; workflow_id: string }> {
    return this.client.request<{ deleted: boolean; workflow_id: string }>(
      "DELETE",
      `/workflows/${encodeURIComponent(workflowId)}`
    );
  }

  public async create(
    spec: WorkflowSpec,
    options: { workflowId?: string; name?: string } = {}
  ): Promise<WorkflowRecord> {
    const body: Record<string, any> = { spec };
    if (options.workflowId) body.workflow_id = options.workflowId;
    if (options.name) body.name = options.name;
    return this.client.request<WorkflowRecord>("POST", "/workflows", { body });
  }

  public async get(workflowId: string): Promise<WorkflowRecord> {
    return this.client.request<WorkflowRecord>("GET", `/workflows/${encodeURIComponent(workflowId)}`);
  }

  public async update(
    workflowId: string,
    spec: WorkflowSpec,
    expectedRevision: number
  ): Promise<WorkflowRecord> {
    return this.client.request<WorkflowRecord>("PUT", `/workflows/${encodeURIComponent(workflowId)}`, {
      body: { spec, expected_revision: expectedRevision },
    });
  }

  public async validate(spec: WorkflowSpec): Promise<WorkflowValidateResponse> {
    return this.client.request<WorkflowValidateResponse>("POST", "/workflows/validate", {
      body: { spec },
    });
  }

  public async export(
    workflowId: string,
    targetDir?: string
  ): Promise<ExportBundleResponse> {
    return this.client.request<ExportBundleResponse>("POST", `/workflows/${encodeURIComponent(workflowId)}/export`, {
      body: targetDir ? { target_dir: targetDir } : {},
    });
  }

  public async downloadExport(workflowId: string): Promise<ArrayBuffer> {
    const url = this.client.buildUrl(`/workflows/${encodeURIComponent(workflowId)}/export/download`);
    const headers: Record<string, string> = {
      "X-Workspace-ID": this.client.workspaceId,
    };
    if (this.client.apiKey) {
      headers.Authorization = `Bearer ${this.client.apiKey}`;
    }

    const response = await fetch(url.toString(), { headers });
    if (!response.ok) {
      throw new NexuraApiError(response.status, "DOWNLOAD_ERROR", response.statusText);
    }
    return await response.arrayBuffer();
  }

  public async import(options: WorkflowImportOptions): Promise<WorkflowRecord> {
    return this.client.request<WorkflowRecord>("POST", "/workflows/import", {
      body: options,
    });
  }

  public async exportPython(workflowId: string): Promise<WorkflowPythonExportResponse> {
    return this.client.request<WorkflowPythonExportResponse>(
      "GET",
      `/workflows/${encodeURIComponent(workflowId)}/export/python`
    );
  }

  public async importPython(
    options: string | ImportPythonWorkflowOptions
  ): Promise<WorkflowRecord> {
    const body = typeof options === "string" ? { source: options } : options;
    return this.client.request<WorkflowRecord>("POST", "/workflows/import/python", {
      body,
    });
  }
}

export class ModulesClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async list(options: {
    kind?: string;
    query?: string;
    available_only?: boolean;
  } = {}): Promise<{ items: ModuleDescriptor[]; total: number; modules: ModuleDescriptor[] }> {
    return this.client.request<{ items: ModuleDescriptor[]; total: number; modules: ModuleDescriptor[] }>(
      "GET",
      "/modules",
      { params: options }
    );
  }

  public async get(moduleId: string, version?: string): Promise<ModuleDescriptor> {
    const params = version ? { version } : undefined;
    return this.client.request<ModuleDescriptor>("GET", `/modules/${encodeURIComponent(moduleId)}`, { params });
  }
}

export class CapabilitiesClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async get(): Promise<ServiceCapabilities> {
    return this.client.request<ServiceCapabilities>("GET", "/capabilities");
  }
}

export class RunsClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async list(options: { workflowId?: string; status?: string; limit?: number; offset?: number } = {}): Promise<PaginatedResponse<RunRecord>> {
    return this.client.request<PaginatedResponse<RunRecord>>("GET", "/runs", {
      params: {
        workflow_id: options.workflowId,
        status: options.status,
        limit: options.limit,
        offset: options.offset,
      },
    });
  }

  public async start(options: StartRunOptions): Promise<RunRecord> {
    return this.client.request<RunRecord>("POST", "/runs", { body: options });
  }

  public async get(runId: string): Promise<RunRecord> {
    return this.client.request<RunRecord>("GET", `/runs/${encodeURIComponent(runId)}`);
  }

  public async diagnostics(
    runId: string,
    options: { includeContent?: boolean; signal?: AbortSignal } = {}
  ): Promise<RunDiagnostics> {
    return this.client.request<RunDiagnostics>(
      "GET", `/runs/${encodeURIComponent(runId)}/diagnostics`, {
        params: { include_content: options.includeContent ?? false }, signal: options.signal,
      }
    );
  }

  public async trace(
    runId: string,
    options: { includeContent?: boolean; signal?: AbortSignal } = {}
  ): Promise<RunTrace> {
    return this.client.request<RunTrace>(
      "GET", `/runs/${encodeURIComponent(runId)}/trace`, {
        params: { include_content: options.includeContent ?? false }, signal: options.signal,
      }
    );
  }

  public async cancel(runId: string, reason?: string): Promise<RunRecord> {
    return this.client.request<RunRecord>("POST", `/runs/${encodeURIComponent(runId)}/cancel`, {
      body: reason ? { reason } : {},
    });
  }

  public async decide(runId: string, options: DecisionOptions): Promise<RunRecord> {
    return this.client.request<RunRecord>("POST", `/runs/${encodeURIComponent(runId)}/decide`, { body: options });
  }

  public async provideInput(runId: string, interruptId: string, value: unknown): Promise<RunRecord> {
    return this.client.request<RunRecord>("POST", `/runs/${encodeURIComponent(runId)}/input`, {
      body: { interrupt_id: interruptId, value },
    });
  }

  public async history(runId: string): Promise<{ items: RunCheckpoint[] }> {
    return this.client.request<{ items: RunCheckpoint[] }>(
      "GET", `/runs/${encodeURIComponent(runId)}/history`
    );
  }

  public async fork(runId: string, options: ForkRunOptions): Promise<RunRecord> {
    const body: Record<string, unknown> = { checkpoint_index: options.checkpointIndex };
    if (options.stateUpdate !== undefined) {
      body.state_update = options.stateUpdate;
    }
    return this.client.request<RunRecord>("POST", `/runs/${encodeURIComponent(runId)}/fork`, { body });
  }

  public async subruns(runId: string, options: { limit?: number; offset?: number } = {}): Promise<PaginatedResponse<RunCheckpoint>> {
    return this.client.request<PaginatedResponse<RunCheckpoint>>(
      "GET", `/runs/${encodeURIComponent(runId)}/subruns`, { params: options }
    );
  }

  /**
   * The run's journaled external effects (`GET /runs/{id}/effects`). A run whose executor
   * does not journal effects yet returns an empty list.
   */
  public async effects(runId: string): Promise<{ effects: ExecutionEffect[] }> {
    return this.client.request<{ effects: ExecutionEffect[] }>(
      "GET", `/runs/${encodeURIComponent(runId)}/effects`
    );
  }

  /**
   * Resolve an `indeterminate` effect once (`POST /runs/{id}/effects/{effectId}/resolve`).
   * Needs the `approve` permission; the caller is recorded as `resolved_by`. A second
   * resolution fails with 409 `EFFECT_ALREADY_RESOLVED`. It does not run anything: the run
   * continues when its executor next claims it.
   */
  public async resolveEffect(
    runId: string,
    effectId: string,
    options: ResolveEffectOptions
  ): Promise<ExecutionEffect> {
    return this.client.request<ExecutionEffect>(
      "POST",
      `/runs/${encodeURIComponent(runId)}/effects/${encodeURIComponent(effectId)}/resolve`,
      { body: options }
    );
  }

  /** One child run (status, steps, redacted history); `childKey` is `child_key` from `subruns()`. */
  public async subrun(runId: string, childKey: string): Promise<SubRunDetail> {
    return this.client.request<SubRunDetail>(
      "GET", `/runs/${encodeURIComponent(runId)}/subruns/${encodeURIComponent(childKey)}`
    );
  }

  public async *streamEvents(
    runId: string,
    options: StreamEventsOptions | number = 0
  ): AsyncGenerator<RunEvent> {
    const opts: StreamEventsOptions =
      typeof options === "number" ? { cursor: options } : options || {};
    const cursor = opts.cursor || 0;
    const live = Boolean(opts.live);

    const url = this.client.buildUrl(`/runs/${encodeURIComponent(runId)}/events`);
    if (cursor > 0) {
      url.searchParams.set("cursor", String(cursor));
    }
    if (live) {
      url.searchParams.set("live", "true");
    }

    const headers: Record<string, string> = {
      Accept: "text/event-stream",
      "X-Workspace-ID": this.client.workspaceId,
    };
    if (this.client.apiKey) {
      headers.Authorization = `Bearer ${this.client.apiKey}`;
    }

    const response = await fetch(url.toString(), {
      headers,
      signal: opts.signal,
    });
    if (!response.ok) {
      throw new NexuraApiError(response.status, "STREAM_ERROR", response.statusText);
    }
    if (!response.body) {
      return;
    }

    for await (const message of readSse(response.body)) {
      const parsedData = parseSseData(message.data);
      yield {
        seq: parseInt(message.id || "0", 10) || 0,
        type: message.event,
        node_id: parsedData.node_id || undefined,
        data: parsedData,
        timestamp: Date.now() / 1000,
      };
      if (message.event === "RUN_STREAM_CLOSED") {
        return;
      }
    }
  }
}

export class SessionsClient {
  constructor(private client: NexuraClient) {}

  public async list(options: { limit?: number; offset?: number } = {}): Promise<PaginatedResponse<SessionListItem>> {
    return this.client.request<PaginatedResponse<SessionListItem>>("GET", "/sessions", {
      params: options,
    });
  }

  public async create(options: { session_id?: string; title?: string } = {}): Promise<SessionDetail> {
    return this.client.request<SessionDetail>("POST", "/sessions", {
      body: options,
    });
  }

  public async get(sessionId: string): Promise<SessionDetail> {
    return this.client.request<SessionDetail>("GET", `/sessions/${encodeURIComponent(sessionId)}`);
  }

  public async delete(sessionId: string): Promise<{ deleted: boolean; session_id: string }> {
    return this.client.request<{ deleted: boolean; session_id: string }>(
      "DELETE",
      `/sessions/${encodeURIComponent(sessionId)}`
    );
  }

  /** Chat models the server operator allowed; pass a `name` as `model` in `chat()`. */
  public async models(): Promise<ChatModelInfo[]> {
    return this.client.request<ChatModelInfo[]>("GET", "/chat/models");
  }

  public async chat(sessionId: string, options: SendChatMessageOptions): Promise<ChatResponse> {
    return this.client.request<ChatResponse>(
      "POST",
      `/sessions/${encodeURIComponent(sessionId)}/chat`,
      {
        body: options,
      }
    );
  }

  /**
   * Stream a chat turn: ordered run events, then `chat_completed` with the same body as
   * `chat()`. The turn is saved only on completion; if the stream is aborted or fails,
   * nothing is persisted and the session can be reloaded to confirm.
   */
  public async *chatStream(
    sessionId: string,
    options: SendChatMessageOptions,
    init: { signal?: AbortSignal } = {}
  ): AsyncGenerator<ChatStreamEvent> {
    const url = this.client.buildUrl(`/sessions/${encodeURIComponent(sessionId)}/chat/stream`);
    const send = () => {
      const headers: Record<string, string> = {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-Workspace-ID": this.client.workspaceId,
      };
      if (this.client.apiKey) {
        headers.Authorization = `Bearer ${this.client.apiKey}`;
      }
      return fetch(url.toString(), {
        method: "POST",
        headers: this.client.withSessionHeader("POST", headers),
        body: JSON.stringify(options),
        signal: init.signal,
      });
    };
    let response = await send();
    if (!response.ok && (await this.client.renewSessionToken("POST", response))) {
      response = await send();
    }
    if (!response.ok) {
      let code = "STREAM_ERROR";
      let message = response.statusText;
      try {
        const detail = (await response.json()).detail;
        if (Array.isArray(detail)) {
          code = "VALIDATION_ERROR";
          message = describeValidationErrors(detail) || message;
        } else {
          code = detail?.code || code;
          message = detail?.message || (typeof detail === "string" ? detail : message);
        }
      } catch {
        // non-JSON error body
      }
      throw new NexuraApiError(response.status, code, message);
    }
    if (!response.body) {
      return;
    }

    for await (const sse of readSse(response.body)) {
      const seq = parseInt(sse.id || "0", 10) || 0;
      const data = parseSseData(sse.data);
      if (sse.event === "run_event") {
        yield { kind: "run_event", seq, type: data.type, timestamp: data.timestamp, data: data.data ?? {} };
      } else if (sse.event === "chat_completed") {
        yield { kind: "chat_completed", seq, response: data as ChatResponse };
        return;
      } else if (sse.event === "chat_failed") {
        yield { kind: "chat_failed", seq, code: data.code, message: data.message, persisted: false };
        return;
      }
    }
  }
}

export class KnowledgeClient {
  constructor(private client: NexuraClient) {}

  public async listCollections(): Promise<{ workspace_id: string; collections: string[] }> {
    return this.client.request<{ workspace_id: string; collections: string[] }>("GET", "/knowledge/collections");
  }

  public async createCollection(options: { collection_id: string; name: string; description?: string }): Promise<KnowledgeCollection> {
    return this.client.request<KnowledgeCollection>("POST", "/knowledge/collections", {
      body: options,
    });
  }

  public async listDocuments(options: { collection_id?: string; limit?: number; offset?: number } = {}): Promise<PaginatedResponse<KnowledgeDocument>> {
    return this.client.request<PaginatedResponse<KnowledgeDocument>>("GET", "/knowledge/documents", {
      params: options,
    });
  }

  public async uploadDocument(options: IngestDocumentOptions): Promise<{
    document_id: string;
    collection_id: string;
    filename: string;
    content_hash: string;
    text_length: number;
    chunk_count: number;
    indexed_records: number;
    created_at: number;
  }> {
    return this.client.request("POST", "/knowledge/documents/upload", {
      body: options,
    });
  }

  public async getDocument(documentId: string): Promise<KnowledgeDocument & { text: string }> {
    return this.client.request<KnowledgeDocument & { text: string }>("GET", `/knowledge/documents/${encodeURIComponent(documentId)}`);
  }

  public async deleteDocument(documentId: string): Promise<{ deleted: boolean; document_id: string }> {
    return this.client.request<{ deleted: boolean; document_id: string }>("DELETE", `/knowledge/documents/${encodeURIComponent(documentId)}`);
  }

  public async retrieve(options: {
    query: string;
    collection_id?: string;
    top_k?: number;
    dense_weight?: number;
    lexical_weight?: number;
    min_score_threshold?: number;
  }): Promise<RetrieveResponse> {
    return this.client.request<RetrieveResponse>("POST", "/knowledge/retrieve", {
      body: options,
    });
  }

  public async answer(options: {
    query: string;
    collection_id?: string;
    language?: "en" | "hi";
  }): Promise<AnswerResponse> {
    return this.client.request<AnswerResponse>("POST", "/knowledge/answer", {
      body: options,
    });
  }

  public async limits(): Promise<KnowledgeLimitsResponse> {
    return this.client.request<KnowledgeLimitsResponse>("GET", "/knowledge/limits");
  }

  public async getChunk(documentId: string, chunkIndex: number): Promise<KnowledgeChunkDetail> {
    return this.client.request<KnowledgeChunkDetail>(
      "GET",
      `/knowledge/documents/${encodeURIComponent(documentId)}/chunks/${encodeURIComponent(String(chunkIndex))}`
    );
  }
}

export class CredentialsClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async list(options: { limit?: number; offset?: number } = {}): Promise<PaginatedResponse<CredentialMetadataItem>> {
    return this.client.request<PaginatedResponse<CredentialMetadataItem>>("GET", "/credentials", {
      params: options,
    });
  }

  public async create(options: CreateCredentialOptions): Promise<CredentialMetadataItem> {
    return this.client.request<CredentialMetadataItem>("POST", "/credentials", {
      body: options,
    });
  }

  public async get(credentialId: string): Promise<CredentialMetadataItem> {
    return this.client.request<CredentialMetadataItem>("GET", `/credentials/${encodeURIComponent(credentialId)}`);
  }

  public async delete(credentialId: string): Promise<{ deleted: boolean; credential_id: string }> {
    return this.client.request<{ deleted: boolean; credential_id: string }>("DELETE", `/credentials/${encodeURIComponent(credentialId)}`);
  }

  public async verify(credentialId: string): Promise<VerifyCredentialResult> {
    return this.client.request<VerifyCredentialResult>("POST", `/credentials/${encodeURIComponent(credentialId)}/verify`);
  }

  public async revoke(credentialId: string): Promise<{ credential_id: string; status: string; revoked: boolean; message: string }> {
    return this.client.request("POST", `/credentials/${encodeURIComponent(credentialId)}/revoke`);
  }
}

export class IntegrationsClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async getStatus(): Promise<IntegrationStatus> {
    return this.client.request<IntegrationStatus>("GET", "/integrations/status");
  }

  public async discoverMcp(options: McpDiscoverOptions = {}): Promise<McpDiscoverResponse> {
    return this.client.request<McpDiscoverResponse>("POST", "/integrations/mcp/discover", {
      body: options,
    });
  }

  public async testRunMcp(options: McpTestRunOptions): Promise<McpTestRunResult> {
    return this.client.request<McpTestRunResult>("POST", "/integrations/mcp/test-run", {
      body: options,
    });
  }

  public async discoverOpenAPI(options: OpenAPIDiscoverOptions): Promise<OpenAPIDiscoverResponse> {
    return this.client.request<OpenAPIDiscoverResponse>("POST", "/integrations/openapi/discover", {
      body: options,
    });
  }

  public async testRunOpenAPI(options: OpenAPITestRunOptions): Promise<OpenAPITestRunResult> {
    return this.client.request<OpenAPITestRunResult>("POST", "/integrations/openapi/test-run", {
      body: options,
    });
  }

  public async register(options: IntegrationRegisterOptions): Promise<IntegrationRegisterResponse> {
    return this.client.request<IntegrationRegisterResponse>("POST", "/integrations/register", {
      body: options,
    });
  }
}

export class EvaluationsClient {
  private readonly client: NexuraClient;
  public readonly studio: StudioEvaluationsClient;

  constructor(client: NexuraClient) {
    this.client = client;
    this.studio = new StudioEvaluationsClient(client);
  }

  public async listDatasets(): Promise<{ datasets: DatasetSummary[]; total: number }> {
    return this.client.request<{ datasets: DatasetSummary[]; total: number }>("GET", "/evaluations/datasets");
  }

  public async getDataset(datasetId: string): Promise<EvaluationDataset> {
    return this.client.request<EvaluationDataset>("GET", `/evaluations/datasets/${encodeURIComponent(datasetId)}`);
  }

  public async createDataset(options: CreateDatasetOptions): Promise<EvaluationDataset> {
    return this.client.request<EvaluationDataset>("POST", "/evaluations/datasets", {
      body: options,
    });
  }

  public async deleteDataset(datasetId: string): Promise<{ deleted: boolean; dataset_id: string }> {
    return this.client.request<{ deleted: boolean; dataset_id: string }>("DELETE", `/evaluations/datasets/${encodeURIComponent(datasetId)}`);
  }

  public async runEvaluation(options: RunEvaluationOptions): Promise<EvaluationSummary> {
    return this.client.request<EvaluationSummary>("POST", "/evaluations/run", {
      body: options,
    });
  }

  public async listRuns(): Promise<{ runs: EvaluationSummary[]; total: number }> {
    return this.client.request<{ runs: EvaluationSummary[]; total: number }>("GET", "/evaluations/runs");
  }

  public async getRun(evalId: string): Promise<EvaluationSummary> {
    return this.client.request<EvaluationSummary>("GET", `/evaluations/runs/${encodeURIComponent(evalId)}`);
  }

  public async compareRuns(baseRunId: string, candidateRunId: string): Promise<EvaluationComparison> {
    return this.client.request<EvaluationComparison>("POST", "/evaluations/compare", {
      body: { base_run_id: baseRunId, candidate_run_id: candidateRunId },
    });
  }
}

export class TriggersClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async listSchedules(): Promise<{ schedules: ScheduleConfig[]; total: number }> {
    return this.client.request<{ schedules: ScheduleConfig[]; total: number }>("GET", "/triggers/schedules");
  }

  public async createSchedule(options: CreateScheduleOptions): Promise<ScheduleConfig> {
    return this.client.request<ScheduleConfig>("POST", "/triggers/schedules", {
      body: options,
    });
  }

  public async pauseSchedule(scheduleId: string): Promise<{ schedule_id: string; paused: boolean }> {
    return this.client.request<{ schedule_id: string; paused: boolean }>("POST", `/triggers/schedules/${encodeURIComponent(scheduleId)}/pause`);
  }

  public async resumeSchedule(scheduleId: string): Promise<{ schedule_id: string; paused: boolean }> {
    return this.client.request<{ schedule_id: string; paused: boolean }>("POST", `/triggers/schedules/${encodeURIComponent(scheduleId)}/resume`);
  }

  public async deleteSchedule(scheduleId: string): Promise<{ deleted: boolean; schedule_id: string }> {
    return this.client.request<{ deleted: boolean; schedule_id: string }>("DELETE", `/triggers/schedules/${encodeURIComponent(scheduleId)}`);
  }

  public async listWebhooks(): Promise<{ webhooks: WebhookConfig[]; total: number }> {
    return this.client.request<{ webhooks: WebhookConfig[]; total: number }>("GET", "/triggers/webhooks");
  }

  public async registerWebhook(options: RegisterWebhookOptions): Promise<WebhookConfig> {
    return this.client.request<WebhookConfig>("POST", "/triggers/webhooks", {
      body: options,
    });
  }

  public async deleteWebhook(endpointId: string): Promise<{ deleted: boolean; endpoint_id: string }> {
    return this.client.request<{ deleted: boolean; endpoint_id: string }>("DELETE", `/triggers/webhooks/${encodeURIComponent(endpointId)}`);
  }

  public async testWebhook(endpointId: string, options: TestWebhookOptions = {}): Promise<TestWebhookResult> {
    return this.client.request<TestWebhookResult>("POST", `/triggers/webhooks/${encodeURIComponent(endpointId)}/test`, {
      body: options,
    });
  }

  public async listJobs(limit = 50): Promise<{ jobs: DurableTriggerJob[]; total: number }> {
    return this.client.request<{ jobs: DurableTriggerJob[]; total: number }>("GET", "/triggers/jobs", {
      params: { limit },
    });
  }

  public async listOutbox(channel?: "email" | "whatsapp"): Promise<{ outbox: OutboxReceipt[]; total: number; guarantee: string }> {
    const params: Record<string, any> = {};
    if (channel) {
      params.channel = channel;
    }
    return this.client.request<{ outbox: OutboxReceipt[]; total: number; guarantee: string }>("GET", "/triggers/outbox", {
      params,
    });
  }

  public async testDispatchOutbox(options: TestDispatchOutboxOptions): Promise<TestDispatchOutboxResult> {
    return this.client.request<TestDispatchOutboxResult>("POST", "/triggers/outbox/test-dispatch", {
      body: options,
    });
  }
}

/**
 * Saved workspace agents (`/agents`). Viewers read; editors create and update; admins delete.
 * Refused specs raise `NexuraApiError` with `details.field_errors`; a stale
 * `expectedRevision` raises code `REVISION_CONFLICT` (409).
 */
export class AgentsClient {
  constructor(private client: NexuraClient) {}

  public async list(): Promise<AgentListResponse> {
    return this.client.request<AgentListResponse>("GET", "/agents");
  }

  /** Models, tools, knowledge collections, skills and limits a spec may use here. */
  public async options(): Promise<AgentOptions> {
    return this.client.request<AgentOptions>("GET", "/agents/options");
  }

  public async get(agentId: string): Promise<AgentRecord> {
    return this.client.request<AgentRecord>("GET", `/agents/${encodeURIComponent(agentId)}`);
  }

  public async create(spec: AgentSpec): Promise<AgentRecord> {
    return this.client.request<AgentRecord>("POST", "/agents", { body: spec });
  }

  /** Replace the spec; `expectedRevision` is the revision you loaded. */
  public async update(agentId: string, spec: AgentSpec, expectedRevision: number): Promise<AgentRecord> {
    return this.client.request<AgentRecord>("PUT", `/agents/${encodeURIComponent(agentId)}`, {
      body: { ...spec, expected_revision: expectedRevision },
    });
  }

  public async delete(
    agentId: string,
    options: { expectedRevision?: number } = {}
  ): Promise<{ deleted: boolean; agent_id: string }> {
    return this.client.request<{ deleted: boolean; agent_id: string }>(
      "DELETE",
      `/agents/${encodeURIComponent(agentId)}`,
      { params: { expected_revision: options.expectedRevision } }
    );
  }
}

export class WorkspacesClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async list(options: { limit?: number; offset?: number } = {}): Promise<PaginatedResponse<WorkspaceRecord> | WorkspaceRecord[]> {
    return this.client.request("GET", "/workspaces", { params: options });
  }

  public async get(workspaceId?: string): Promise<WorkspaceRecord> {
    const ws = workspaceId || this.client.workspaceId;
    return this.client.request<WorkspaceRecord>("GET", `/workspaces/${encodeURIComponent(ws)}`);
  }

  public async create(options: { workspace_id: string; name: string; description?: string }): Promise<WorkspaceRecord> {
    return this.client.request<WorkspaceRecord>("POST", "/workspaces", { body: options });
  }

  public async updatePolicy(requirePromotionApproval: boolean, workspaceId?: string): Promise<WorkspaceRecord> {
    const ws = workspaceId || this.client.workspaceId;
    return this.client.request<WorkspaceRecord>("PATCH", `/workspaces/${encodeURIComponent(ws)}`, {
      body: { require_promotion_approval: requirePromotionApproval },
    });
  }
}

export class StudioEvaluationsClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async listTargets(): Promise<{ workspace_id: string; targets: any[]; scorers: string[]; limits: Record<string, any> }> {
    return this.client.request("GET", "/evaluations/studio/targets");
  }

  public async listDatasets(options: { workflow_id?: string } = {}): Promise<{ datasets: StudioDatasetSummary[]; total: number }> {
    return this.client.request("GET", "/evaluations/studio/datasets", { params: options });
  }

  public async getDataset(datasetId: string): Promise<StudioDatasetDetail> {
    return this.client.request<StudioDatasetDetail>("GET", `/evaluations/studio/datasets/${encodeURIComponent(datasetId)}`);
  }

  public async putDataset(datasetId: string, options: PutStudioDatasetOptions): Promise<StudioDatasetDetail & { redactions?: any }> {
    return this.client.request("PUT", `/evaluations/studio/datasets/${encodeURIComponent(datasetId)}`, { body: options });
  }

  public async deleteDataset(datasetId: string): Promise<{ deleted: boolean; dataset_id: string }> {
    return this.client.request("DELETE", `/evaluations/studio/datasets/${encodeURIComponent(datasetId)}`);
  }

  public async exportDataset(datasetId: string): Promise<Record<string, any>> {
    return this.client.request("GET", `/evaluations/studio/datasets/${encodeURIComponent(datasetId)}/export`);
  }

  public async importOpenAI(options: {
    content: string;
    dataset_id: string;
    input_field?: string;
    expected_field?: string;
    name_field?: string;
    testing_criteria?: any;
    name?: string;
    skip_invalid?: boolean;
  }): Promise<{ dataset: any; report: any }> {
    return this.client.request("POST", "/evaluations/studio/datasets/import-openai", { body: options });
  }

  public async addCaseFromRun(datasetId: string, options: AddCaseFromRunOptions): Promise<Record<string, any>> {
    return this.client.request("POST", `/evaluations/studio/datasets/${encodeURIComponent(datasetId)}/cases/from-run`, { body: options });
  }

  public async createRun(options: StudioRunOptions): Promise<Record<string, any>> {
    return this.client.request("POST", "/evaluations/studio/runs", { body: options });
  }

  public async listRuns(options: { dataset_id?: string; limit?: number } = {}): Promise<{ runs: any[]; total: number }> {
    return this.client.request("GET", "/evaluations/studio/runs", { params: options });
  }

  public async getRun(runId: string): Promise<Record<string, any>> {
    return this.client.request("GET", `/evaluations/studio/runs/${encodeURIComponent(runId)}`);
  }

  public async deleteRun(runId: string): Promise<{ deleted: boolean; run_id: string }> {
    return this.client.request("DELETE", `/evaluations/studio/runs/${encodeURIComponent(runId)}`);
  }

  public async compare(options: {
    baseline_run_id: string;
    candidate_run_id: string;
    tolerance?: number;
    min_pass_rate?: number;
  }): Promise<Record<string, any>> {
    return this.client.request("POST", "/evaluations/studio/compare", { body: options });
  }

  public async redteam(options: StudioRedteamOptions): Promise<Record<string, any>> {
    return this.client.request("POST", "/evaluations/studio/redteam", { body: options });
  }
}

export class SkillsClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async list(options: { label?: string } = {}): Promise<{ items: SkillItem[]; total: number }> {
    return this.client.request("GET", "/skills", { params: options });
  }

  public async create(options: CreateSkillOptions): Promise<SkillDetail> {
    return this.client.request<SkillDetail>("POST", "/skills", { body: options });
  }

  public async get(name: string, options: { version?: number; label?: string } = {}): Promise<SkillDetail> {
    return this.client.request<SkillDetail>("GET", `/skills/${encodeURIComponent(name)}`, { params: options });
  }

  public async versions(name: string): Promise<SkillVersionsResponse> {
    return this.client.request<SkillVersionsResponse>("GET", `/skills/${encodeURIComponent(name)}/versions`);
  }

  public async setLabel(name: string, label: string, version: number): Promise<SkillDetail> {
    return this.client.request<SkillDetail>("PUT", `/skills/${encodeURIComponent(name)}/labels/${encodeURIComponent(label)}`, {
      body: { version },
    });
  }

  public async remove(name: string, options: { version?: number } = {}): Promise<{ removed: boolean; name: string; version?: number }> {
    return this.client.request("DELETE", `/skills/${encodeURIComponent(name)}`, { params: options });
  }
}

export class TemplatesClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async list(options: { category?: string; tag?: string } = {}): Promise<TemplateCatalogResponse> {
    return this.client.request<TemplateCatalogResponse>("GET", "/templates", { params: options });
  }

  public async get(templateId: string): Promise<TemplateDetail> {
    return this.client.request<TemplateDetail>("GET", `/templates/${encodeURIComponent(templateId)}`);
  }

  public async instantiate(templateId: string, options: InstantiateTemplateOptions = {}): Promise<Record<string, any>> {
    return this.client.request("POST", `/templates/${encodeURIComponent(templateId)}/instantiate`, { body: options });
  }
}

export class BuilderClient {
  private readonly client: NexuraClient;

  constructor(client: NexuraClient) {
    this.client = client;
  }

  public async draft(options: BuilderDraftOptions): Promise<BuilderDraftResponse> {
    return this.client.request<BuilderDraftResponse>("POST", "/builder/draft", { body: options });
  }
}
