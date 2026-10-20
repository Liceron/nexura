import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NexuraClient } from '../dist/index.js';

async function withFetch(responseBody, run) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify(responseBody), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000', workspaceId: 'tenant-a' });
    await run(client, calls);
  } finally {
    globalThis.fetch = original;
  }
}

test('provideInput posts the interrupt id and value to /input', async () => {
  const run = { run_id: 'run-1', status: 'completed', pending_input: null };
  await withFetch(run, async (client, calls) => {
    const result = await client.runs.provideInput('run-1', 'int-1', { ok: true });
    assert.equal(result.status, 'completed');
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/runs/run-1/input');
    assert.equal(calls[0].options.method, 'POST');
    assert.deepEqual(JSON.parse(calls[0].options.body), { interrupt_id: 'int-1', value: { ok: true } });
    assert.equal(calls[0].options.headers['X-Workspace-ID'], 'tenant-a');
  });
});

test('provideInput can send a falsy JSON answer', async () => {
  await withFetch({ run_id: 'run-1' }, async (client, calls) => {
    await client.runs.provideInput('run-1', 'int-1', false);
    assert.deepEqual(JSON.parse(calls[0].options.body), { interrupt_id: 'int-1', value: false });
  });
});

test('waiting runs expose pending_input', async () => {
  const waiting = {
    run_id: 'run-2', status: 'waiting_for_input',
    pending_input: { interrupt_id: 'int-9', node_id: 'ask', value: { question: 'ok?' } },
  };
  await withFetch(waiting, async (client) => {
    const run = await client.runs.get('run-2');
    assert.equal(run.status, 'waiting_for_input');
    assert.equal(run.pending_input.interrupt_id, 'int-9');
  });
});

test('history and subruns are GET requests that return items', async () => {
  await withFetch({ items: [{ workflow_run_id: 'run-1', step_count: 1 }] }, async (client, calls) => {
    const history = await client.runs.history('run-1');
    const subruns = await client.runs.subruns('run-1');
    assert.equal(history.items.length, 1);
    assert.equal(subruns.items[0].workflow_run_id, 'run-1');
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/runs/run-1/history');
    assert.equal(calls[1].url, 'http://127.0.0.1:8000/api/v1/runs/run-1/subruns');
    for (const call of calls) {
      assert.equal(call.options.method, 'GET');
      assert.equal(call.options.headers['X-Workspace-ID'], 'tenant-a');
    }
  });
});

test('fork maps camelCase options to the snake_case body', async () => {
  await withFetch({ run_id: 'run-3', forked_from: { workflow_run_id: 'run-1' } }, async (client, calls) => {
    const forked = await client.runs.fork('run-1', { checkpointIndex: 2, stateUpdate: { bonus: 10 } });
    assert.equal(forked.forked_from.workflow_run_id, 'run-1');
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/runs/run-1/fork');
    assert.equal(calls[0].options.method, 'POST');
    assert.deepEqual(JSON.parse(calls[0].options.body), { checkpoint_index: 2, state_update: { bonus: 10 } });
    assert.equal(calls[0].options.headers['X-Workspace-ID'], 'tenant-a');

    await client.runs.fork('run-1', { checkpointIndex: 0 });
    assert.deepEqual(JSON.parse(calls[1].options.body), { checkpoint_index: 0 });
  });
});

test('run ids are URL-encoded', async () => {
  await withFetch({ items: [] }, async (client, calls) => {
    await client.runs.history('run/with space');
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/runs/run%2Fwith%20space/history');
  });
});

test('subrun fetches one child by its URL-encoded key', async () => {
  await withFetch({ child_key: 'c3ViIzE', run_id: 'run-1/sub#1', steps: [], history: { items: [] } }, async (client, calls) => {
    const child = await client.runs.subrun('run-1', 'c3ViIzE');
    assert.equal(child.run_id, 'run-1/sub#1');
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/runs/run-1/subruns/c3ViIzE');
    assert.equal(calls[0].options.method, 'GET');
    await client.runs.subrun('run-1', 'a/b#c');
    assert.equal(calls[1].url, 'http://127.0.0.1:8000/api/v1/runs/run-1/subruns/a%2Fb%23c');
  });
});
