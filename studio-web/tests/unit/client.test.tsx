import { describe, it, expect, vi } from 'vitest';
import { NexuraApiManager } from '../../src/api/client';

describe('NexuraApiManager', () => {
  it('initializes with default workspace and baseUrl', () => {
    const manager = new NexuraApiManager('/api', 'default');
    expect(manager.baseUrl).toBe('/api');
    expect(manager.workspace).toBe('default');
    expect(manager.client.workspaceId).toBe('default');
  });


  it('aborts active requests and purges scoped cache when workspace changes', () => {
    const manager = new NexuraApiManager('/api', 'ws-alpha');
    manager.setCached('cached_key', { data: 123 });
    expect(manager.getCached('cached_key')).toEqual({ data: 123 });

    const abortSpy = vi.spyOn(manager, 'abortAllRequests');
    manager.setWorkspace('ws-beta');

    expect(abortSpy).toHaveBeenCalledWith('Workspace changed to ws-beta');
    expect(manager.workspace).toBe('ws-beta');
    expect(manager.client.workspaceId).toBe('ws-beta');
    // Cache must be cleared
    expect(manager.getCached('cached_key')).toBeUndefined();
  });

  it('aborts active requests and purges cache when baseUrl changes', () => {
    const manager = new NexuraApiManager('http://localhost:8000', 'default');
    manager.setCached('query_cache', 'val');

    const abortSpy = vi.spyOn(manager, 'abortAllRequests');
    manager.setBaseUrl('http://127.0.0.1:8765');

    expect(abortSpy).toHaveBeenCalledWith('Base URL changed');
    expect(manager.baseUrl).toBe('http://127.0.0.1:8765');
    expect(manager.getCached('query_cache')).toBeUndefined();
  });

  it('handles offline service gracefully without throwing', async () => {
    const manager = new NexuraApiManager('http://127.0.0.1:59999', 'default');
    // Mock capabilities.get to throw connection refused error
    vi.spyOn(manager.client.capabilities, 'get').mockRejectedValue(
      new Error('fetch failed: connect ECONNREFUSED 127.0.0.1:59999')
    );

    const info = await manager.pingService();
    expect(info.status).toBe('offline');
    expect(info.error).toContain('Could not reach service');
    expect(info.latencyMs).toBeGreaterThanOrEqual(0);
  });

  it('handles 401 unauthorized gracefully without throwing', async () => {
    const manager = new NexuraApiManager('http://localhost:8000', 'default');
    vi.spyOn(manager.client.capabilities, 'get').mockRejectedValue(
      new Error('[401] UNAUTHORIZED: Token expired')
    );

    const info = await manager.pingService();
    expect(info.status).toBe('unauthorized');
    expect(info.error).toContain('Unauthorized: Authentication required');
  });
});

describe('startup session probe', () => {
  it.each(['local-owner', 'authenticated-shared-service'])('probes only the local profile (%s)', async active_profile => {
    const manager = new NexuraApiManager('/api', 'default');
    vi.spyOn(manager.client.capabilities, 'get').mockResolvedValue({ active_profile } as Awaited<ReturnType<typeof manager.client.capabilities.get>>);
    const probe = vi.spyOn(manager.client, 'fetchSessionToken').mockResolvedValue('local-test-token');
    expect((await manager.pingService()).status).toBe('connected');
    expect(probe).toHaveBeenCalledTimes(active_profile === 'local-owner' ? 1 : 0);
  });

  // QA 2026-10-27: embedded in the enterprise web app's local mode, the Studio page is on another
  // origin than the local-owner service, so the token endpoint refuses it; the host's credential
  // is that service's session token and is used as one.
  it.each([
    ['local-owner', true, 'host-session-token', 0],
    ['local-owner', false, undefined, 1],
    ['authenticated-shared-service', true, undefined, 0],
  ] as const)('profile %s, host credential %s', async (active_profile, hosted, expected, probes) => {
    const manager = new NexuraApiManager('/api', 'default');
    manager.hostSuppliesCredential = hosted;
    manager.setAuthToken('host-session-token');
    vi.spyOn(manager.client.capabilities, 'get').mockResolvedValue({ active_profile } as Awaited<ReturnType<typeof manager.client.capabilities.get>>);
    const probe = vi.spyOn(manager.client, 'fetchSessionToken').mockResolvedValue('probed-token');
    expect((await manager.pingService()).status).toBe('connected');
    expect(probe).toHaveBeenCalledTimes(probes);
    if (expected) expect(manager.client.sessionToken).toBe(expected);
    if (active_profile === 'authenticated-shared-service') expect(manager.client.sessionToken).toBeUndefined();
  });
});
