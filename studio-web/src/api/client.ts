// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { NexuraClient } from '@liceron/nexura-sdk';
import type { ServiceCapabilities } from '@liceron/nexura-sdk';
import { formatApiError } from './errors';

export type ConnectionStatus = 'connected' | 'connecting' | 'offline' | 'unauthorized';

export interface ConnectionInfo {
  status: ConnectionStatus;
  baseUrl: string;
  workspace: string;
  principal?: string;
  profile?: string;
  sdkVersion?: string;
  capabilities?: ServiceCapabilities;
  error?: string;
  latencyMs?: number;
}

export class NexuraApiManager {
  private _baseUrl: string;
  private _workspace: string;
  private _principal: string = 'default';
  private _authToken: string | null = null;
  private _sessionToken: string | undefined = undefined;
  private _client: NexuraClient;
  private _abortController: AbortController = new AbortController();
  private _scopedCache: Map<string, unknown> = new Map();

  constructor(baseUrl: string = '/api', workspace: string = 'default', principal: string = 'default') {
    this._baseUrl = baseUrl;
    this._workspace = workspace;
    this._principal = principal;
    this._client = this._createClient();
  }

  get principal(): string {
    return this._principal;
  }

  public setPrincipal(p: string): void {
    this._principal = p;
  }

  get baseUrl(): string {
    return this._baseUrl;
  }

  get workspace(): string {
    return this._workspace;
  }

  get client(): NexuraClient {
    return this._client;
  }

  get abortSignal(): AbortSignal {
    return this._abortController.signal;
  }

  public setBaseUrl(url: string): void {
    if (this._baseUrl !== url) {
      this.abortAllRequests('Base URL changed');
      this._baseUrl = url;
      this._scopedCache.clear();
      this._client = this._createClient(false);
    }
  }

  public setWorkspace(ws: string): void {
    if (this._workspace !== ws) {
      this.abortAllRequests(`Workspace changed to ${ws}`);
      this._workspace = ws;
      this._scopedCache.clear();
      this._client = this._createClient();
    }
  }

  public setAuthToken(token: string | null): void {
    this.abortAllRequests('Authentication credential updated');
    this._authToken = token;
    this._scopedCache.clear();
    this._client = this._createClient();
  }

  public abortAllRequests(reason: string = 'Aborted by user'): void {
    this._abortController.abort(reason);
    this._abortController = new AbortController();
  }

  public clearCache(): void {
    this._scopedCache.clear();
  }

  public getCached<T>(key: string): T | undefined {
    return this._scopedCache.get(key) as T | undefined;
  }

  public setCached<T>(key: string, value: T): void {
    this._scopedCache.set(key, value);
  }

  private _createClient(sameServer: boolean = true): NexuraClient {
    // A token is only ever sent back to the server that issued it.
    this._sessionToken = sameServer ? (this._client?.sessionToken ?? this._sessionToken) : undefined;
    // The local server's per-session token (X-Nexura-Session) is read from this page's own
    // origin (GET /api/v1/local-session) when the service is first reached (pingService) and
    // sent on every state-changing request; a refused request (e.g. after a server restart)
    // reads it again and retries once.
    return new NexuraClient({
      baseUrl: this._baseUrl,
      workspaceId: this._workspace,
      apiKey: this._authToken || undefined,
      sessionToken: this._sessionToken,
      autoSessionToken: true,
    });
  }

  public async pingService(): Promise<ConnectionInfo> {
    const startTime = performance.now();
    try {
      const caps = await this._client.capabilities.get();
      const latencyMs = Math.round(performance.now() - startTime);
      // Read the per-session token now (same origin) so the first state-changing request carries
      // it instead of being refused and retried. Best effort: the refusal path still applies.
      if (!this._client.sessionToken) {
        await this._client.fetchSessionToken().catch(() => undefined);
      }
      return {
        status: 'connected',
        baseUrl: this._baseUrl,
        workspace: this._workspace,
        principal: this._principal || 'default',
        profile: caps.active_profile,
        sdkVersion: caps.sdk_version,
        capabilities: caps,
        latencyMs,
      };
    } catch (err: unknown) {
      const latencyMs = Math.round(performance.now() - startTime);
      const errMessage = formatApiError(err);
      const raw = err instanceof Error ? err.message : String(err); // keeps "[401] CODE"
      const is401 = raw.includes('401') || raw.toLowerCase().includes('unauthorized');
      const is403 = raw.includes('403') || raw.toLowerCase().includes('forbidden');

      if (is401 || is403) {
        return {
          status: 'unauthorized',
          baseUrl: this._baseUrl,
          workspace: this._workspace,
          principal: this._principal || 'default',
          error: is401
            ? 'Unauthorized: Authentication required.'
            : 'Forbidden: Insufficient workspace permissions.',
          latencyMs,
        };
      }

      return {
        status: 'offline',
        baseUrl: this._baseUrl,
        workspace: this._workspace,
        principal: this._principal || 'default',
        error: `Could not reach service at ${this._baseUrl}: ${errMessage}`,
        latencyMs,
      };
    }
  }
}
