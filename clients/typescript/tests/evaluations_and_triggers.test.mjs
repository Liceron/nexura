import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NexuraClient } from '../dist/index.js';

test('evaluations client methods issue correct requests and headers', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ ok: true, datasets: [], runs: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000', workspaceId: 'ws-eval' });

    await client.evaluations.listDatasets();
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/evaluations/datasets');
    assert.equal(calls[0].options.headers['X-Workspace-ID'], 'ws-eval');

    await client.evaluations.getDataset('ds/1');
    assert.equal(calls[1].url, 'http://127.0.0.1:8000/api/v1/evaluations/datasets/ds%2F1');

    await client.evaluations.runEvaluation({ dataset_id: 'ds_1', graders: ['exact_match'] });
    assert.equal(calls[2].url, 'http://127.0.0.1:8000/api/v1/evaluations/run');
    assert.equal(JSON.parse(calls[2].options.body).dataset_id, 'ds_1');

    await client.evaluations.compareRuns('run_1', 'run_2');
    assert.equal(calls[3].url, 'http://127.0.0.1:8000/api/v1/evaluations/compare');
    assert.deepEqual(JSON.parse(calls[3].options.body), { base_run_id: 'run_1', candidate_run_id: 'run_2' });
  } finally {
    globalThis.fetch = original;
  }
});

test('triggers client methods handle schedules, webhooks and dry-run outbox', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ ok: true, schedules: [], webhooks: [], outbox: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000', workspaceId: 'ws-triggers' });

    await client.triggers.listSchedules();
    assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/v1/triggers/schedules');

    await client.triggers.pauseSchedule('sched-1');
    assert.equal(calls[1].url, 'http://127.0.0.1:8000/api/v1/triggers/schedules/sched-1/pause');

    await client.triggers.testWebhook('wh-1', { event_id: 'evt-1' });
    assert.equal(calls[2].url, 'http://127.0.0.1:8000/api/v1/triggers/webhooks/wh-1/test');

    await client.triggers.listOutbox('email');
    assert.equal(calls[3].url, 'http://127.0.0.1:8000/api/v1/triggers/outbox?channel=email');

    await client.triggers.testDispatchOutbox({
      channel: 'email',
      recipient: 'user@example.com',
      body: 'Dry run message',
    });
    assert.equal(calls[4].url, 'http://127.0.0.1:8000/api/v1/triggers/outbox/test-dispatch');
  } finally {
    globalThis.fetch = original;
  }
});

test('triggers.listJobs exposes the run started by the worker', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        jobs: [{ job_id: 'job_1', status: 'completed', run_id: 'run-abc', error_message: null }],
        total: 1,
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000' });
    const { jobs } = await client.triggers.listJobs();
    assert.equal(jobs[0].run_id, 'run-abc');
  } finally {
    globalThis.fetch = original;
  }
});
