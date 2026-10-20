// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { describe, expect, it, vi } from 'vitest';
import { lazyWithRetry } from '../../src/app/lazyWithRetry';

type Loader = () => Promise<{ default: () => null }>;

function payload(component: unknown): Promise<unknown> {
  // React.lazy keeps the loader in _payload._result until first render.
  const lazyObj = component as { _payload: { _result: () => Promise<unknown> } };
  return lazyObj._payload._result();
}

describe('lazyWithRetry', () => {
  it('retries a failed import once and then succeeds', async () => {
    const page = () => null;
    const load = vi.fn<Loader>()
      .mockRejectedValueOnce(new Error('Failed to fetch dynamically imported module'))
      .mockResolvedValueOnce({ default: page });
    const mod = await payload(lazyWithRetry(load, 1));
    expect(load).toHaveBeenCalledTimes(2);
    expect(mod).toEqual({ default: page });
  });

  it('gives up after one retry so a real failure reaches the error boundary', async () => {
    const load = vi.fn<Loader>().mockRejectedValue(new Error('gone'));
    await expect(payload(lazyWithRetry(load, 1))).rejects.toThrow('gone');
    expect(load).toHaveBeenCalledTimes(2);
  });
});
