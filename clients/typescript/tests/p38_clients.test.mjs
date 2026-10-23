import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { NexuraClient, NexuraApiError } from '../dist/index.js';

const example = { input_data: {}, expected_trajectory: [{ name: 'lookup', args: { amount: 0 } }] };
const graders = [{ type: 'tool_argument_accuracy', argument_rules: { amount: {} } }];
const config = { sampling_rate: 0, seed: 0, start_time: 0, end_time: 10 };
const trace = { run_id: 'recorded', timestamp: 0, output: null, events: [], messages: [] };
const online = { example, graders, config };
const simulation = { dataset_id: 'ds', dataset_version: 'v1', agent_id: 'fake', graders };
const migration = { expectedDefinitionDigest: 'sha256-saved', resume: false, onUncertain: 'fail' };
const cases = [
  ['graders', c => c.evaluations.graders(), 'GET', '/evaluations/graders', undefined],
  ['importTrace', c => c.evaluations.importTrace(trace), 'POST', '/evaluations/traces', trace],
  ['evaluateOnline', c => c.evaluations.evaluateOnline(online), 'POST', '/evaluations/online', online],
  ['simulate', c => c.evaluations.simulate(simulation), 'POST', '/evaluations/simulate', simulation],
  ['migrate', c => c.runs.migrate('a/b?c#d', migration), 'POST', '/runs/a%2Fb%3Fc%23d/migrate',
    { expected_definition_digest: 'sha256-saved', resume: false, on_uncertain: 'fail' }],
];

for (const [name, invoke, method, path, body] of cases) {
  test(`P38 ${name}: wire contract and unmodified response`, async () => {
    const original = globalThis.fetch;
    const reply = { run_id: 'reply', metadata: { nested: [0, false, null] } };
    let calls = 0;
    globalThis.fetch = async (url, options) => {
      calls++;
      assert.equal(String(url), `http://mock-service/api/v1${path}`);
      assert.equal(options.method, method);
      const headers = new Headers(options.headers);
      assert.equal(headers.get('X-Workspace-ID'), 'test-workspace');
      assert.equal(headers.get('Authorization'), 'Bearer fake-key');
      assert.equal(headers.get('X-Nexura-Session'), method === 'GET' ? null : 'fake-session');
      assert.deepEqual(options.body === undefined ? undefined : JSON.parse(options.body), body);
      return new Response(JSON.stringify(reply));
    };
    try {
      const client = new NexuraClient({
        baseUrl: 'http://mock-service', workspaceId: 'test-workspace',
        apiKey: 'fake-key', sessionToken: 'fake-session',
      });
      assert.deepEqual(await invoke(client), reply);
      assert.equal(calls, 1);
    } finally {
      globalThis.fetch = original;
    }
  });

  test(`P38 ${name}: structured errors`, async () => {
    const original = globalThis.fetch;
    const status = name === 'migrate' ? 409 : 422;
    const code = name === 'migrate' ? 'WORKFLOW_MIGRATION_REFUSED' : 'INVALID_GRADER';
    globalThis.fetch = async () => new Response(
      JSON.stringify({ detail: { code, message: 'refused' } }), { status }
    );
    try {
      await assert.rejects(invoke(new NexuraClient({ baseUrl: 'http://mock-service' })), error => {
        assert.ok(error instanceof NexuraApiError);
        assert.equal(error.statusCode, status);
        assert.equal(error.code, code);
        assert.equal(error.message, `[${status}] ${code}: refused`);
        return true;
      });
    } finally {
      globalThis.fetch = original;
    }
  });
}

test('P38 omitted defaults and explicit empty graders', async () => {
  const original = globalThis.fetch;
  const bodies = [];
  globalThis.fetch = async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return new Response('{}');
  };
  try {
    const client = new NexuraClient({ baseUrl: 'http://mock-service' });
    await client.evaluations.evaluateOnline({ example });
    await client.evaluations.simulate({ dataset_id: 'ds' });
    await client.evaluations.evaluateOnline({ example, graders: [], config: {} });
    await client.evaluations.simulate({ dataset_id: 'ds', graders: [] });
    await client.runs.migrate('run');
    await client.runs.migrate('run', {});
    assert.deepEqual(bodies, [
      { example }, { dataset_id: 'ds' }, { example, graders: [], config: {} },
      { dataset_id: 'ds', graders: [] },
      { resume: true, on_uncertain: 'pause' },
      { resume: true, on_uncertain: 'pause' },
    ]);
  } finally {
    globalThis.fetch = original;
  }
});

test('P38 strict TypeScript consumer can use the exported contracts', async () => {
  const project = dirname(dirname(fileURLToPath(import.meta.url)));
  const temporary = await mkdtemp(join(project, 'tests/.p38-clients-'));
  try {
    const consumer = join(temporary, 'consumer.mts');
    await writeFile(consumer, `
      import { NexuraClient, type TraceEvaluationOptions, type SimulationEvaluationOptions,
        type StoredTrace, type MigrateRunOptions, type TraceEvaluationSummary,
        type GraderCapabilities, type UserSimulation } from '../../dist/index.js';
      const c = new NexuraClient();
      const trace: StoredTrace = { run_id: 'r', timestamp: 0, output: null };
      const imported: 'imported_trace' = (await c.evaluations.importTrace(trace)).source;
      const capabilities: GraderCapabilities = await c.evaluations.graders();
      const online: TraceEvaluationOptions = {
        example: { input_data: {}, expected_trajectory: [{ name: 'lookup', args: { x: null } }] },
        graders: [{ type: 'tool_argument_accuracy', mode: 'strict',
          argument_rules: { x: { normalisers: ['numeric'], absolute_tolerance: 0.01 } } }],
        config: { sampling_rate: 0, seed: 0, start_time: 0, end_time: null },
      };
      const report: TraceEvaluationSummary = await c.evaluations.evaluateOnline(online);
      const score: number = report.grader_scores.tool_argument_accuracy;
      const metadata: Record<string, unknown> = report.results[0].metadata;
      const simulation: SimulationEvaluationOptions = { dataset_id: 'ds', graders: [] };
      await c.evaluations.simulate(simulation);
      const migration: MigrateRunOptions = {
        expectedDefinitionDigest: 'sha256-saved', resume: false, onUncertain: 'fail',
      };
      await c.runs.migrate('r');
      const version: string | number | null | undefined = (await c.runs.migrate('r', migration)).workflow_version;
      // @ts-expect-error Sampling rates must be numeric.
      online.config = { sampling_rate: 'all' };
      // @ts-expect-error Unknown effect policy must be pause or fail.
      migration.onUncertain = 'retry';
      // @ts-expect-error The target is the run's saved workflow; a definition is not accepted.
      await c.runs.migrate('r', { spec: { name: 'v2', version: 2, entry_node: 'done', nodes: {} } });
      // @ts-expect-error The SDK-only old-definition fallback is not a service option.
      await c.runs.migrate('r', { continueOnOldDefinition: true });
      // @ts-expect-error A simulation must have exactly one user source.
      const badSimulation: UserSimulation = { turns: [], persona: 'user' };
    `);
    const result = spawnSync(process.execPath, [
      join(project, 'node_modules/typescript/bin/tsc'),
      '--noEmit', '--strict', '--skipLibCheck', '--target', 'ES2022',
      '--module', 'NodeNext', '--moduleResolution', 'NodeNext', consumer,
    ], { encoding: 'utf8', timeout: 30000 });
    assert.equal(result.status, 0, result.stdout + result.stderr);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});
