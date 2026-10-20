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
