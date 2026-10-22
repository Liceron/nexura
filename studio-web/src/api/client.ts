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
  /** The server refused this address after too many failed sign-ins (HTTP 429). */
  rateLimited?: boolean;
  /** The server refused the credential itself (HTTP 401), as opposed to a 403 or 429. */
  credentialRefused?: boolean;
  /** HTTP status of an unauthorized answer (401, 403 or 429), for the status chip. */
  httpStatus?: number;
  /**
   * The refusal came from remote single-operator mode (the operator-token provider): a 429 from
   * its failure limiter, or a 401 whose message asks for the operator token / answers a token we
   * sent. Only then does Studio offer the operator sign-in (Round 5 review M3).
   */
  operatorTokenMode?: boolean;
}

/** The origin a base URL ('/api' or an absolute URL) resolves to; '' when it cannot be parsed. */
export function apiOrigin(baseUrl: string): string {
  try {
    return new URL(baseUrl, window.location.href).origin;
  } catch {
    return '';
  }
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

  /** True while an operator token (remote single-operator mode) is set. */
  get hasAuthToken(): boolean {
    return Boolean(this._authToken);
  }

  /**
   * Point the client at another server. Returns true when the operator token was dropped: it
   * is never sent to an origin other than the one it was given for (P38 C4 m4).
   */
  public setBaseUrl(url: string): boolean {
    let dropped = false;
    if (this._baseUrl !== url) {
      this.abortAllRequests('Base URL changed');
      if (this._authToken && apiOrigin(this._baseUrl) !== apiOrigin(url)) {
        this._authToken = null;
        dropped = true;
      }
      this._baseUrl = url;
      this._scopedCache.clear();
      this._client = this._createClient(false);
    }
    return dropped;
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
      // With an operator token the client sends it on this probe too, so a server in remote
      // single-operator mode answers 404 instead of counting a failed sign-in (P38 C4 m3).
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
      const statusCode = typeof (err as { statusCode?: unknown })?.statusCode === 'number' ? (err as { statusCode: number }).statusCode : undefined;
      const is401 = statusCode === 401 || (statusCode === undefined && (raw.includes('401') || raw.toLowerCase().includes('unauthorized')));
      const is403 = statusCode === 403 || (statusCode === undefined && (raw.includes('403') || raw.toLowerCase().includes('forbidden')));
      // Remote single-operator mode refuses an address after repeated failed sign-ins.
      const is429 = statusCode === 429 || raw.includes('TOO_MANY_AUTH_FAILURES') || (statusCode === undefined && raw.includes('429'));
      // The operator-token provider's 401 asks for the operator token; a 401 to a token we sent
      // is also its answer. Shared-service and other 401s keep the service's own message.
      const operatorToken401 = is401 && (Boolean(this._authToken) || /operator token/i.test(raw));

      if (is429) {
        return {
          status: 'unauthorized',
          baseUrl: this._baseUrl,
          workspace: this._workspace,
          principal: this._principal || 'default',
          error: 'Too many failed authentication attempts; try again later.',
          rateLimited: true,
          httpStatus: 429,
          operatorTokenMode: true,
          latencyMs,
        };
      }

      if (is401 || is403) {
        return {
          status: 'unauthorized',
          baseUrl: this._baseUrl,
          workspace: this._workspace,
          principal: this._principal || 'default',
          // A plain lead, then the service's own message (review M3).
          error: [is401 ? 'Unauthorized: Authentication required.' : 'Forbidden: Insufficient workspace permissions.', errMessage]
            .filter(Boolean)
            .join(' '),
          credentialRefused: is401,
          httpStatus: is401 ? 401 : 403,
          operatorTokenMode: operatorToken401,
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
