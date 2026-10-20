import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NexuraClient, NexuraApiError } from '../dist/index.js';

function createMockClient() {
  const calls = [];
  const client = new NexuraClient({
    baseUrl: 'http://127.0.0.1:8765',
    apiKey: 'test-key',
    workspaceId: 'test-ws',
  });
  return { client, calls };
}

test('WorkflowsClient coverage', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765', workspaceId: 'ws-1' });

    await client.workflows.list({ limit: 10, offset: 5, query: 'search' });
    assert.match(calls.at(-1).url, /\/api\/v1\/workflows\?limit=10&offset=5&query=search/);
    assert.equal(calls.at(-1).options.method, 'GET');

    await client.workflows.create({ name: 'W1', spec: {} });
    assert.equal(calls.at(-1).options.method, 'POST');

    await client.workflows.get('wf-1');
    assert.match(calls.at(-1).url, /\/api\/v1\/workflows\/wf-1$/);

    await client.workflows.update('wf-1', { spec: {}, expected_revision: 2 });
    assert.equal(calls.at(-1).options.method, 'PUT');

    await client.workflows.delete('wf-1');
    assert.equal(calls.at(-1).options.method, 'DELETE');

    await client.workflows.export('wf-1', '/tmp/out');
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/workflows\/wf-1\/export$/);

    await client.workflows.validate({ nodes: {} });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/workflows\/validate$/);

    await client.workflows.exportPython('wf-1');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/workflows\/wf-1\/export\/python$/);

    await client.workflows.importPython('def run(): pass', { name: 'PyWF' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/workflows\/import\/python$/);
  } finally {
    globalThis.fetch = original;
  }
});

test('RunsClient coverage: trace, history, subruns, fork, input', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765' });

    await client.runs.list({ workflowId: 'wf-1', status: 'completed' });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\?workflow_id=wf-1&status=completed/);

    await client.runs.start({ workflow_id: 'wf-1' });
    assert.equal(calls.at(-1).options.method, 'POST');

    await client.runs.get('r-1');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1$/);

    await client.runs.cancel('r-1', 'cancelled');
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/cancel$/);

    await client.runs.decide('r-1', { approval_id: 'app-1', decision: 'approve' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/decide$/);

    await client.runs.diagnostics('r-1', { includeContent: true });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/diagnostics\?include_content=true$/);

    await client.runs.trace('r-1', { includeContent: true });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/trace\?include_content=true$/);

    await client.runs.provideInput('r-1', 'int-1', 'user val');
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/input$/);

    await client.runs.history('r-1');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/history$/);

    await client.runs.subruns('r-1');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/subruns$/);

    await client.runs.subrun('r-1', 'child-0');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/subruns\/child-0$/);

    await client.runs.fork('r-1', { checkpointIndex: 2, stateUpdate: { a: 1 } });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/runs\/r-1\/fork$/);
  } finally {
    globalThis.fetch = original;
  }
});

test('Workspaces client coverage', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765', workspaceId: 'default' });

    // Workspaces
    await client.workspaces.list({ limit: 20 });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/workspaces\?limit=20$/);

    await client.workspaces.create({ workspace_id: 'ws-new', name: 'New WS' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/workspaces$/);

    await client.workspaces.get('ws-new');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/workspaces\/ws-new$/);

    await client.workspaces.updatePolicy(true, 'ws-new');
    assert.equal(calls.at(-1).options.method, 'PATCH');
    assert.match(calls.at(-1).url, /\/api\/v1\/workspaces\/ws-new$/);

  } finally {
    globalThis.fetch = original;
  }
});

test('KnowledgeClient coverage: limits and getChunk', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765' });

    await client.knowledge.limits();
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/knowledge\/limits$/);

    await client.knowledge.getChunk('doc-1', 3);
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/knowledge\/documents\/doc-1\/chunks\/3$/);
  } finally {
    globalThis.fetch = original;
  }
});

test('StudioEvaluationsClient coverage', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765' });

    await client.evaluations.studio.listTargets();
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/targets$/);

    await client.evaluations.studio.listDatasets({ workflow_id: 'wf-1' });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/datasets\?workflow_id=wf-1$/);

    await client.evaluations.studio.putDataset('sds-1', { name: 'SDS 1', cases: [] });
    assert.equal(calls.at(-1).options.method, 'PUT');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/datasets\/sds-1$/);

    await client.evaluations.studio.getDataset('sds-1');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/datasets\/sds-1$/);

    await client.evaluations.studio.exportDataset('sds-1');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/datasets\/sds-1\/export$/);

    await client.evaluations.studio.importOpenAI({ content: '{"in":"a"}', dataset_id: 'sds-oa' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/datasets\/import-openai$/);

    await client.evaluations.studio.addCaseFromRun('sds-1', { run_id: 'r-1', case_name: 'c1' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/datasets\/sds-1\/cases\/from-run$/);

    await client.evaluations.studio.createRun({ dataset_id: 'sds-1', target: { kind: 'workflow', id: 'wf-1' } });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/runs$/);

    await client.evaluations.studio.listRuns({ dataset_id: 'sds-1', limit: 30 });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/runs\?dataset_id=sds-1&limit=30$/);

    await client.evaluations.studio.getRun('srun-1');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/runs\/srun-1$/);

    await client.evaluations.studio.deleteRun('srun-1');
    assert.equal(calls.at(-1).options.method, 'DELETE');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/runs\/srun-1$/);

    await client.evaluations.studio.compare({ baseline_run_id: 'sr-1', candidate_run_id: 'sr-2' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/compare$/);

    await client.evaluations.studio.redteam({ workflow_id: 'wf-1' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/evaluations\/studio\/redteam$/);
  } finally {
    globalThis.fetch = original;
  }
});

test('Skills, Templates, Builder clients coverage', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765' });

    // Skills
    await client.skills.list({ label: 'prod' });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/skills\?label=prod$/);

    await client.skills.create({ markdown: '# Skill', label: 'prod' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/skills$/);

    await client.skills.get('s1', { version: 1 });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/skills\/s1\?version=1$/);

    await client.skills.versions('s1');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/skills\/s1\/versions$/);

    await client.skills.setLabel('s1', 'staging', 2);
    assert.equal(calls.at(-1).options.method, 'PUT');
    assert.match(calls.at(-1).url, /\/api\/v1\/skills\/s1\/labels\/staging$/);

    await client.skills.remove('s1', { version: 1 });
    assert.equal(calls.at(-1).options.method, 'DELETE');
    assert.match(calls.at(-1).url, /\/api\/v1\/skills\/s1\?version=1$/);

    // Templates
    await client.templates.list({ category: 'support' });
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/templates\?category=support$/);

    await client.templates.get('tmpl-1');
    assert.equal(calls.at(-1).options.method, 'GET');
    assert.match(calls.at(-1).url, /\/api\/v1\/templates\/tmpl-1$/);

    await client.templates.instantiate('tmpl-1', { name: 'Instantiated WF' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/templates\/tmpl-1\/instantiate$/);

    // Builder
    await client.builder.draft({ description: 'Build an approval workflow', model: 'gpt-4o' });
    assert.equal(calls.at(-1).options.method, 'POST');
    assert.match(calls.at(-1).url, /\/api\/v1\/builder\/draft$/);

  } finally {
    globalThis.fetch = original;
  }
});

test('Strict path parameter encoding with special characters', async () => {
  const original = globalThis.fetch;
  const capturedUrls = [];
  globalThis.fetch = async (url) => {
    capturedUrls.push(String(url));
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765' });
    const idWithSpecialChars = 'a/b:c?d=1#f';
    const encoded = encodeURIComponent(idWithSpecialChars);

    await client.workflows.get(idWithSpecialChars);
    assert.ok(capturedUrls.at(-1).endsWith(`/api/v1/workflows/${encoded}`));

    await client.runs.get(idWithSpecialChars);
    assert.ok(capturedUrls.at(-1).endsWith(`/api/v1/runs/${encoded}`));

    await client.skills.get(idWithSpecialChars);
    assert.ok(capturedUrls.at(-1).endsWith(`/api/v1/skills/${encoded}`));

    await client.templates.get(idWithSpecialChars);
    assert.ok(capturedUrls.at(-1).endsWith(`/api/v1/templates/${encoded}`));

  } finally {
    globalThis.fetch = original;
  }
});

test('the open-source client has no client for closed-edition routes (PKG-03)', async () => {
  const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765' });
  for (const name of ['members', 'secrets', 'organisations', 'published']) {
    assert.equal(client[name], undefined, name);
  }
  const exported = await import('../dist/index.js');
  for (const name of ['WorkspaceMembersClient', 'SecretsClient', 'OrganisationsClient', 'PublishedClient']) {
    assert.equal(exported[name], undefined, name);
  }
  const { readFile, readdir } = await import('node:fs/promises');
  const dist = new URL('../dist/', import.meta.url);
  for (const file of await readdir(dist)) {
    const text = await readFile(new URL(file, dist), 'utf8');
    for (const segment of ['/organisations', '/published', '/secrets', '/members', '/my-membership']) {
      assert.ok(!text.includes(segment), `${file} contains ${segment}`);
    }
  }
});
