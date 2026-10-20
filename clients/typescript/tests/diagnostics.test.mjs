import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { NexuraClient } from '../dist/index.js';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test('built SDK diagnostics method scopes requests, encodes IDs and forwards cancellation', async () => {
  const controller = new AbortController();
  let recorded;
  globalThis.fetch = async (url, options) => {
    recorded = { url: new URL(url), options };
    return new Response(JSON.stringify({ run_id: 'run/example', source: 'persisted_run_events' }), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  };
  const sdk = new NexuraClient({ baseUrl: 'https://studio.invalid', workspaceId: 'workspace-a', apiKey: 'local-test-key' });
  const result = await sdk.runs.diagnostics('run/example', { includeContent: true, signal: controller.signal });
  assert.equal(recorded.url.pathname, '/api/v1/runs/run%2Fexample/diagnostics');
  assert.equal(recorded.url.searchParams.get('include_content'), 'true');
  assert.equal(recorded.options.headers['X-Workspace-ID'], 'workspace-a');
  assert.equal(recorded.options.headers.Authorization, 'Bearer local-test-key');
  assert.equal(recorded.options.signal, controller.signal);
  assert.equal(result.source, 'persisted_run_events');
});
