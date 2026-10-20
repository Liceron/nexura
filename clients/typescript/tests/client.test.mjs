import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NexuraClient, NexuraApiError } from '../dist/index.js';

test('built package preserves workspace headers and structured errors', async () => {
  const original = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ detail: { code: 'DENIED', message: 'denied' } }), {
      status: 403, headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000', workspaceId: 'tenant-test' });
    await assert.rejects(client.workflows.get('fixture'), error => error instanceof NexuraApiError && error.statusCode === 403 && error.code === 'DENIED');
    assert.equal(captured.options.headers['X-Workspace-ID'], 'tenant-test');
  } finally {
    globalThis.fetch = original;
  }
});

test('default base URL matches the port `nexura-runtime serve` listens on', () => {
  const client = new NexuraClient();
  assert.equal(client.buildUrl('/health').toString(), 'http://127.0.0.1:8765/api/v1/health');
});

test('sessions.models() lists allowed chat models and chat() sends the chosen name', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    const body = String(url).endsWith('/chat/models')
      ? [{ name: 'fake-deterministic', provider: 'local', label: 'fake', offline: true, streaming: false, default: true }]
      : { reply: 'ok', delivery: 'streamed', model_name: 'gpt' };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    const client = new NexuraClient({ baseUrl: 'http://svc' });
    const models = await client.sessions.models();
    assert.equal(calls[0].url, 'http://svc/api/v1/chat/models');
    assert.equal(calls[0].options.method, 'GET');
    assert.equal(models[0].default, true);
    const res = await client.sessions.chat('s1', { message: 'hi', model: 'gpt' });
    assert.equal(JSON.parse(calls[1].options.body).model, 'gpt');
    assert.equal(res.delivery, 'streamed');
  } finally {
    globalThis.fetch = original;
  }
});

test('a 422 validation list becomes readable field messages, details stay structured', async () => {
  const original = globalThis.fetch;
  const detail = [
    { loc: ['body', 'replay_window_seconds'], type: 'greater_than_equal', msg: 'Input should be greater than or equal to 10' },
    { loc: ['body', 'workflow_id'], type: 'missing', msg: 'Field required' },
  ];
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail }), { status: 422, headers: { 'Content-Type': 'application/json' } });
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000' });
    await assert.rejects(client.workflows.get('fixture'), error => {
      assert.ok(error instanceof NexuraApiError);
      assert.equal(error.statusCode, 422);
      assert.equal(error.code, 'VALIDATION_ERROR');
      assert.ok(!error.message.includes('{"loc"'), error.message);
      assert.ok(error.message.includes('replay_window_seconds: Input should be greater than or equal to 10'), error.message);
      assert.ok(error.message.includes('workflow_id: Field required'), error.message);
      assert.deepEqual(error.details, detail);
      return true;
    });
  } finally {
    globalThis.fetch = original;
  }
});

test('ids are sent as one encoded path segment and cannot reach another route', async () => {
  const original = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    return new Response(JSON.stringify({ run_id: 'x', status: 'completed' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000' });
    await client.runs.get('../workflows/wf-1');
    await client.runs.get('run-1#x');
    await client.runs.get('run-1?x=1');
    await client.workflows.get('a/b');
    await client.runs.cancel('../x').catch(() => {});
    await client.runs.decide('../x', { approval_id: 'a', decision: 'approve' }).catch(() => {});
    assert.deepEqual(urls.slice(0, 4), [
      'http://127.0.0.1:8000/api/v1/runs/..%2Fworkflows%2Fwf-1',
      'http://127.0.0.1:8000/api/v1/runs/run-1%23x',
      'http://127.0.0.1:8000/api/v1/runs/run-1%3Fx%3D1',
      'http://127.0.0.1:8000/api/v1/workflows/a%2Fb',
    ]);
    assert.ok(urls[4].endsWith('/api/v1/runs/..%2Fx/cancel'), urls[4]);
    assert.ok(urls[5].endsWith('/api/v1/runs/..%2Fx/decide'), urls[5]);
  } finally {
    globalThis.fetch = original;
  }
});
