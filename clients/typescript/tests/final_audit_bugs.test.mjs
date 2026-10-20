import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { NexuraClient } from '../dist/index.js';

test('B08: subruns can reach later pages and returns pagination metadata', async () => {
  const original = globalThis.fetch;
  let called;
  const page = { items: [], total: 51, limit: 1, offset: 50, has_more: false };
  globalThis.fetch = async (url) => {
    called = String(url);
    return new Response(JSON.stringify(page));
  };
  try {
    const result = await new NexuraClient().runs.subruns('r', { limit: 1, offset: 50 });
    const url = new URL(called);
    assert.equal(url.searchParams.get('limit'), '1');
    assert.equal(url.searchParams.get('offset'), '50');
    assert.deepEqual(result, page);
  } finally {
    globalThis.fetch = original;
  }
});

test('B09: a strict TypeScript consumer can configure a run budget', async () => {
  const project = dirname(dirname(fileURLToPath(import.meta.url)));
  const temporary = await mkdtemp(join(project, 'tests/.final-audit-'));
  try {
    const consumer = join(temporary, 'consumer.mts');
    await writeFile(consumer, `
      import { NexuraClient, type BudgetConfig } from '../../dist/index.js';
      const budget: BudgetConfig = {max_tokens: 100, max_cost_usd: '0.25'};
      await new NexuraClient().runs.start({workflow_id: 'w', budget_config: budget});
      const page = await new NexuraClient().runs.subruns('r', {offset: 50, limit: 1});
      const total: number = page.total;
      const more: boolean = page.has_more;
      // @ts-expect-error Token limits must be numeric.
      const invalid: BudgetConfig = {max_tokens: '100'};
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
