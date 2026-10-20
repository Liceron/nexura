import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { NexuraApiError, NexuraClient } from '../dist/index.js';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

function record(calls, status = 200, body = {}) {
  globalThis.fetch = async (url, options) => {
    calls.push({ url: new URL(url), options, body: options.body ? JSON.parse(options.body) : undefined });
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  };
}

const sdk = new NexuraClient({ baseUrl: 'https://studio.invalid', workspaceId: 'ws-a', apiKey: 'local-test-key' });

test('agents client lists, reads options and gets with encoded ids', async () => {
  const calls = [];
  record(calls, 200, { items: [], total: 0, limits: { max_agents_per_workspace: 100 } });
  const listed = await sdk.agents.list();
  assert.equal(listed.total, 0);
  await sdk.agents.options();
  await sdk.agents.get('agt/1');
  assert.deepEqual(calls.map((c) => [c.options.method, c.url.pathname]), [
    ['GET', '/api/v1/agents'],
    ['GET', '/api/v1/agents/options'],
    ['GET', '/api/v1/agents/agt%2F1'],
  ]);
  assert.equal(calls[0].options.headers['X-Workspace-ID'], 'ws-a');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer local-test-key');
});

test('agents client creates, updates with expected_revision and deletes', async () => {
  const calls = [];
  record(calls, 200, { agent_id: 'agt_1', revision: 2 });
  const spec = { name: 'helper', instructions: 'Help.', tools: ['nexura.http.request'], limits: { max_turns: 4 } };
  await sdk.agents.create(spec);
  const updated = await sdk.agents.update('agt_1', spec, 1);
  assert.equal(updated.revision, 2);
  await sdk.agents.delete('agt_1', { expectedRevision: 2 });
  await sdk.agents.delete('agt_1');
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(calls[0].body, spec);
  assert.equal(calls[1].options.method, 'PUT');
  assert.deepEqual(calls[1].body, { ...spec, expected_revision: 1 });
  assert.equal(calls[2].options.method, 'DELETE');
  assert.equal(calls[2].url.searchParams.get('expected_revision'), '2');
  assert.equal(calls[3].url.searchParams.has('expected_revision'), false);
});

test('refused specs surface code, message and field_errors', async () => {
  const calls = [];
  record(calls, 422, {
    detail: {
      code: 'MODEL_NOT_AVAILABLE',
      message: 'Invalid agent: Unknown model',
      field_errors: [{ field: 'model', message: "Unknown model 'x'." }],
    },
  });
  await assert.rejects(sdk.agents.create({ name: 'a', model: 'x' }), (err) => {
    assert.ok(err instanceof NexuraApiError);
    assert.equal(err.statusCode, 422);
    assert.equal(err.code, 'MODEL_NOT_AVAILABLE');
    assert.deepEqual(err.details.field_errors, [{ field: 'model', message: "Unknown model 'x'." }]);
    return true;
  });
});

test('revision conflicts raise REVISION_CONFLICT', async () => {
  record([], 409, { detail: { code: 'REVISION_CONFLICT', message: 'stale', current_revision: 3 } });
  await assert.rejects(sdk.agents.update('agt_1', { name: 'a' }, 1), (err) => {
    assert.equal(err.statusCode, 409);
    assert.equal(err.code, 'REVISION_CONFLICT');
    assert.equal(err.details.current_revision, 3);
    return true;
  });
});

test('chat can name a saved agent', async () => {
  const calls = [];
  record(calls, 200, { reply: 'hi', agent_id: 'agt_1', refused_tool_calls: [] });
  const res = await sdk.sessions.chat('s1', { message: 'hi', agent_id: 'agt_1' });
  assert.equal(res.agent_id, 'agt_1');
  assert.deepEqual(calls[0].body, { message: 'hi', agent_id: 'agt_1' });
  assert.equal(calls[0].url.pathname, '/api/v1/sessions/s1/chat');
});
