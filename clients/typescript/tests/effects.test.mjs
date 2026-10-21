import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NexuraClient, NexuraApiError } from '../dist/index.js';

test('effect inspection and resolution encode ids and preserve falsy results', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify(options.method === 'GET' ? { effects: [] } : { status: 'resolved' }));
  };
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000', workspaceId: 'ws' });
    assert.deepEqual(await client.runs.effects('run with space'), { effects: [] });
    const result = await client.runs.resolveEffect('run with space', 'effect?x', { resolution: 'confirmed', result: false });
    assert.equal(result.status, 'resolved');
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/runs/run%20with%20space/effects');
    assert.equal(calls[1].url, 'http://127.0.0.1:8000/api/v1/runs/run%20with%20space/effects/effect%3Fx/resolve');
    assert.equal(calls[0].options.method, 'GET');
    assert.equal(calls[1].options.method, 'POST');
    assert.equal(calls[1].options.headers['X-Workspace-ID'], 'ws');
    assert.deepEqual(JSON.parse(calls[1].options.body), { resolution: 'confirmed', result: false });
  } finally { globalThis.fetch = original; }
});

test('second-resolution conflict is surfaced to the caller', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: 'EFFECT_ALREADY_RESOLVED' } }), { status: 409 });
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000' });
    await assert.rejects(client.runs.resolveEffect('run', 'effect', { resolution: 'failed' }),
      error => error instanceof NexuraApiError && error.statusCode === 409 && error.code === 'EFFECT_ALREADY_RESOLVED');
  } finally { globalThis.fetch = original; }
});

test('recover posts to the run recover route with an optional on_uncertain', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ run_id: 'run 1', status: 'completed' }));
  };
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000', workspaceId: 'ws' });
    const run = await client.runs.recover('run 1');
    assert.equal(run.status, 'completed');
    await client.runs.recover('run 1', { onUncertain: 'fail' });
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/runs/run%201/recover');
    assert.equal(calls[0].options.method, 'POST');
    assert.equal(calls[0].options.headers['X-Workspace-ID'], 'ws');
    assert.deepEqual(JSON.parse(calls[0].options.body), {});
    assert.deepEqual(JSON.parse(calls[1].options.body), { on_uncertain: 'fail' });
  } finally { globalThis.fetch = original; }
});

test('recover surfaces a held run lease as a 409', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: 'RUN_LEASE_HELD', message: 'busy' } }), { status: 409 });
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000' });
    await assert.rejects(client.runs.recover('run'),
      error => error instanceof NexuraApiError && error.statusCode === 409 && error.code === 'RUN_LEASE_HELD');
  } finally { globalThis.fetch = original; }
});
