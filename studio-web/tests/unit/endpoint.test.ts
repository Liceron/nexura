// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// deriveEndpoint, split out of publish_snippet.test.ts when publishing moved to the closed
// edition (EDITIONS_PLAN step 4); the triggers feature uses it for webhook delivery URLs.
import { describe, it, expect } from 'vitest';
import { deriveEndpoint } from '../../src/api/endpoint';

describe('deriveEndpoint', () => {
  it('turns a relative API base into the page origin', () => {
    expect(deriveEndpoint('/api', 'http://localhost:8080')).toBe('http://localhost:8080');
    expect(deriveEndpoint('', 'http://localhost:8080/')).toBe('http://localhost:8080');
  });
  it('keeps an absolute base and drops /api suffixes', () => {
    expect(deriveEndpoint('http://127.0.0.1:8765', 'http://x')).toBe('http://127.0.0.1:8765');
    expect(deriveEndpoint('https://svc.example.com/api/v1/', 'http://x')).toBe('https://svc.example.com');
  });
});
