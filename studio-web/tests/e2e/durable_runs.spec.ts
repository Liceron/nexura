/**
 * P13 durable runs in Studio: answering an interrupt, checkpoint history, fork with edited state,
 * sub-runs and the approval details dialog, all against a real local backend over HTTP.
 *
 * No builtin or template workflow calls interrupt(), and the default backend cannot register
 * handlers over its API, so this spec starts its own backend: the real create_api_app with the
 * default registry plus three tiny offline handlers (e2e_prep, e2e_ask, e2e_finish). e2e_ask calls
 * interrupt(). Everything else (history, fork, sub-runs, approvals, the mapped Slack step that is
 * only paused, never executed) is the product's own code. No network beyond localhost.
 */
import { test, expect, Page } from '@playwright/test';
import { spawn, ChildProcess } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { killProcessTree, readSessionToken, SESSION_HEADER, stopProcessTree } from './harness';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

interface Cluster {
  backendUrl: string;
  frontendUrl: string;
  /** Headers carrying this backend's per-session token, for direct API calls. */
  apiHeaders: Record<string, string>;
  stop: () => Promise<void>;
}

let cluster: Cluster;
let offset = Math.floor(Math.random() * 150);

const PY = (dbPath: string, port: number) => `
import uvicorn
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.workflow import interrupt

reg = get_default_registry()

def e2e_prep(state):
    return {"prepared": True}

def e2e_ask(state):
    answer = interrupt({
        "question": "Which region should we ship to?",
        "schema": {"type": "object", "properties": {"region": {"type": "string", "enum": ["EU", "US"]}}, "required": ["region"]},
    })
    return {"answer": answer}

def e2e_finish(state):
    return {"finished": True}

reg.register("e2e_prep", e2e_prep)
reg.register("e2e_ask", e2e_ask)
reg.register("e2e_finish", e2e_finish)
app = create_api_app(registry=reg, db_path=${JSON.stringify(dbPath)}, profile="local-owner")
uvicorn.run(app, host="127.0.0.1", port=${port}, log_level="error")
`;

async function waitFor(url: string, proc: ChildProcess, what: string): Promise<void> {
  for (let i = 0; i < 100; i++) {
    if (proc.exitCode !== null) throw new Error(`${what} exited early with code ${proc.exitCode}`);
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`${what} did not start`);
}

async function startCluster(): Promise<Cluster> {
  offset += 1;
  const backendPort = 16000 + offset * 4;
  const frontendPort = 17000 + offset * 4;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nexura-e2e-durable-'));
  const root = path.resolve(__dirname, '../../../');
  const backend = spawn('uv', ['run', '--frozen', 'python', '-c', PY(path.join(tmpDir, 'e2e.sqlite3'), backendPort)], {
    cwd: root,
    stdio: 'pipe',
    detached: true,
  });
  const backendUrl = `http://127.0.0.1:${backendPort}`;
  await waitFor(`${backendUrl}/readyz`, backend, 'backend');
  const frontend = spawn('npx', ['vite', '--port', String(frontendPort), '--strictPort'], {
    cwd: path.resolve(__dirname, '../../'),
    env: { ...process.env, VITE_BACKEND_URL: backendUrl },
    stdio: 'pipe',
    detached: true,
  });
  const frontendUrl = `http://127.0.0.1:${frontendPort}`;
  await waitFor(frontendUrl, frontend, 'frontend');
  // Read the token through the Studio origin (Vite proxies /api to this backend), as Studio does.
  let sessionToken: string;
  try {
    sessionToken = await readSessionToken(frontendUrl);
  } catch (err) {
    killProcessTree(frontend);
    killProcessTree(backend);
    throw err;
  }
  return {
    backendUrl,
    frontendUrl,
    apiHeaders: { [SESSION_HEADER]: sessionToken },
    stop: async () => {
      await Promise.all([stopProcessTree(frontend), stopProcessTree(backend)]);
      fs.rmSync(tmpDir, { recursive: true, force: true });
    },
  };
}

test.beforeAll(async () => {
  cluster = await startCluster();
});

test.afterAll(async () => {
  if (cluster) await cluster.stop();
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript((backendUrl) => {
    localStorage.setItem('nexura_api_url', backendUrl);
  }, cluster.backendUrl);
});

type Spec = Record<string, unknown>;

async function seed(page: Page, name: string, spec: Spec): Promise<string> {
  const res = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
    headers: cluster.apiHeaders,
    data: { name, spec: { name, ...spec } },
  });
  expect(res.ok(), await res.text()).toBe(true);
  return (await res.json()).workflow_id as string;
}

/** Open the workflow in Studio and launch a run with the given initial state. */
async function launch(page: Page, workflowId: string, state: Record<string, unknown>): Promise<void> {
  await page.goto(`${cluster.frontendUrl}#/workflows/${workflowId}`);
  await page.getByRole('button', { name: 'Run', exact: true }).first().click();
  const modal = page.getByRole('dialog');
  await modal.getByLabel('Initial state JSON input').fill(JSON.stringify(state));
  await modal.getByRole('checkbox', { name: /Execute in background/i }).check();
  await modal.getByRole('button', { name: /Launch Run/i }).click();
  await expect(page.getByLabel('Run identifier badge')).toBeVisible({ timeout: 15000 });
}

const node = (id: string, handler: string, extra: Spec = {}) => ({ id, handler, ...extra });

const ASK_SPEC = {
  entry_node: 'prep',
  nodes: {
    prep: node('prep', 'e2e_prep', { default_transition: 'ask' }),
    ask: node('ask', 'e2e_ask', { default_transition: 'finish' }),
    finish: node('finish', 'e2e_finish'),
  },
};

test.describe('Durable runs (P13)', () => {
  test('answers an interrupt, browses history, forks with edited state and lands on the child', async ({ page }) => {
    const wf = await seed(page, 'P13 ask flow', ASK_SPEC);
    await launch(page, wf, { ticket: 'T-1' });

    // 1. Interrupt: question, schema, validation, answer, run continues.
    const banner = page.getByTestId('pending-input-banner');
    await expect(banner).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId('pending-input-question')).toHaveText('Which region should we ship to?');
    await expect(page.getByTestId('pending-input-schema')).toContainText('"required":["region"]');
    await page.getByRole('button', { name: 'Send answer' }).click(); // skeleton {"region": ""} is not in the enum
    await expect(page.getByTestId('pending-input-validation')).toContainText('must be one of');
    await page.getByLabel('Your answer').fill('{"region": "EU"}');
    await page.getByRole('button', { name: 'Send answer' }).click();
    await expect(banner).toBeHidden({ timeout: 15000 });
    await expect(page.getByLabel('Run status badge')).toContainText(/completed/i, { timeout: 15000 });
    const parentId = (await page.getByLabel('Run identifier badge').innerText()).trim();

    // 2. Checkpoint history: step, node, status, time; select one to see its state.
    await page.getByRole('tab', { name: 'History', exact: true }).click();
    const rows = page.getByTestId('run-history-row');
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThanOrEqual(3);
    await expect(rows.first()).toContainText('Step');
    await expect(rows.first()).toContainText('Node: ask');
    await expect(rows.first().getByTestId('run-history-time')).toContainText('UTC');
    await expect(rows.last()).toContainText('completed');
    await page.getByRole('button', { name: 'View state 0' }).click();
    await expect(page.getByTestId('run-history-state')).toContainText('"ticket": "T-1"');
    await expect(page.getByTestId('run-history-state')).toContainText('"prepared": true');

    // 3. Fork with an invalid edit is blocked, a valid edit forks and lands on the child run.
    await page.getByLabel('Fork state update JSON').fill('{oops');
    await expect(page.getByTestId('fork-state-error')).toContainText('JSON syntax error');
    await expect(page.getByRole('button', { name: 'Fork with edited state' })).toBeDisabled();
    await page.getByLabel('Fork state update JSON').fill('{"ticket": "T-EDITED"}');
    await page.getByRole('button', { name: 'Fork with edited state' }).click();

    await expect(page.getByTestId('forked-from-banner')).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId('forked-from-link')).toHaveText(parentId);
    await expect(page.getByTestId('forked-from-banner')).toContainText('state edited');
    await expect(page.getByLabel('Run identifier badge')).not.toHaveText(parentId);
    // The child re-runs the ask node from the checkpoint, so it asks again, with the edited state.
    await expect(page.getByTestId('pending-input-banner')).toBeVisible({ timeout: 15000 });
    await page.getByRole('tab', { name: 'State Snapshot' }).click();
    await expect(page.getByText('"ticket": "T-EDITED"')).toBeVisible();

    // Parent link goes back to the parent run.
    await page.getByTestId('forked-from-link').click();
    await expect(page.getByLabel('Run identifier badge')).toHaveText(parentId, { timeout: 15000 });
    await expect(page.getByTestId('forked-from-banner')).toBeHidden();
  });

  test('lists sub-runs of a composite workflow', async ({ page }) => {
    const wf = await seed(page, 'P13 composite', {
      entry_node: 'prep',
      nodes: {
        prep: node('prep', 'e2e_prep', { default_transition: 'child' }),
        child: node('child', '', {
          default_transition: 'finish',
          subgraph: { spec_version: 1, name: 'lookup child', entry_node: 'c1', nodes: { c1: node('c1', 'synthetic_order_lookup') } },
        }),
        finish: node('finish', 'e2e_finish'),
      },
    });
    await launch(page, wf, { order_id: 'ORD-DELHI-501' });
    await expect(page.getByLabel('Run status badge')).toContainText(/completed/i, { timeout: 15000 });
    await page.getByRole('tab', { name: 'Sub-runs' }).click();
    const rows = page.getByTestId('subrun-row');
    await expect(rows).toHaveCount(1, { timeout: 15000 });
    await expect(page.getByTestId('subrun-id')).toContainText('child#');
    await expect(rows.first()).toContainText('completed');
    await rows.first().getByRole('button', { name: 'Show state' }).click();
    await expect(page.getByTestId('subrun-state')).toContainText('ORD-DELHI-501');

    // Child view: opens from the row, shows status and its checkpoint timeline, links back.
    await rows.first().getByTestId('subrun-open').click();
    const view = page.getByTestId('subrun-view');
    await expect(view).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId('subrun-view-id')).toContainText('child#');
    await expect(view).toContainText('completed');
    expect(await page.getByTestId('subrun-step').count()).toBeGreaterThan(0);
    await page.getByTestId('subrun-back').click();
    await expect(page.getByTestId('subrun-view')).toBeHidden();
    await expect(page.getByTestId('subrun-row')).toHaveCount(1);
  });

  test('a run without sub-workflow nodes has no Sub-runs tab', async ({ page }) => {
    const wf = await seed(page, 'P13 plain', { entry_node: 'prep', nodes: { prep: node('prep', 'e2e_prep') } });
    await launch(page, wf, {});
    await expect(page.getByLabel('Run status badge')).toContainText(/completed/i, { timeout: 15000 });
    // The run snapshot carries has_subworkflows=false, so the tab is not offered at all.
    await expect(page.getByRole('tab', { name: 'History' })).toBeVisible();
    await expect(page.getByRole('tab', { name: 'Sub-runs' })).toHaveCount(0);
    await expect(page.getByTestId('subrun-row')).toHaveCount(0);
  });

  test('a paused run stays reachable after reload: answer it from #/runs/{id} (P25)', async ({ page }) => {
    const wf = await seed(page, 'P25 ask after reload', ASK_SPEC);
    await launch(page, wf, { ticket: 'T-25' });
    await expect(page.getByTestId('pending-input-banner')).toBeVisible({ timeout: 15000 });
    const runId = (await page.getByLabel('Run identifier badge').innerText()).trim();

    // The workflow page URL carries the run, so a reload reopens its Run Console.
    await expect(page).toHaveURL(new RegExp(`#/workflows/${wf}/runs/${runId}$`));
    await page.reload();
    await expect(page.getByLabel('Run identifier badge')).toHaveText(runId, { timeout: 15000 });
    await expect(page.getByTestId('pending-input-banner')).toBeVisible();

    // The workflow's Runs tab lists it as waiting for input.
    await page.getByTestId('tab-runs').click();
    const row = page.getByTestId('workflow-run-row').filter({ hasText: runId });
    await expect(row).toHaveAttribute('data-status', 'waiting_for_input', { timeout: 15000 });
    await expect(row.getByTestId('workflow-run-open')).toHaveAttribute('href', `#/runs/${runId}`);

    // The Approval Inbox lists it under "Waiting for input" and links to the run page.
    await page.goto(`${cluster.frontendUrl}#/approvals`);
    const waiting = page.getByTestId('waiting-input-row').filter({ hasText: runId });
    await expect(waiting).toContainText('Which region should we ship to?', { timeout: 15000 });
    await waiting.getByTestId('waiting-input-open').click();
    await expect(page).toHaveURL(new RegExp(`#/runs/${runId}$`));

    // Reload the standalone run page and answer there.
    await page.reload();
    await expect(page.getByTestId('run-page')).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId('run-page-workflow-link')).toHaveAttribute('href', `#/workflows/${wf}`);
    await expect(page.getByTestId('pending-input-question')).toHaveText('Which region should we ship to?');
    await page.getByLabel('Your answer').fill('{"region": "US"}');
    await page.getByRole('button', { name: 'Send answer' }).click();
    await expect(page.getByLabel('Run status badge')).toContainText(/completed/i, { timeout: 15000 });

    // An unknown run id is a notice, not a crash.
    await page.goto(`${cluster.frontendUrl}#/runs/no-such-run`);
    await expect(page.getByTestId('run-page-notFound')).toBeVisible({ timeout: 15000 });
  });

  test('a run started over the API is approved from #/runs/{id} after a reload (P25)', async ({ page }) => {
    const wf = await seed(page, 'P25 gate', {
      entry_node: 'prep',
      nodes: {
        prep: node('prep', 'e2e_prep', { default_transition: 'gate' }),
        gate: node('gate', 'e2e_finish', { requires_approval: true }),
      },
    });
    // Not started from Studio, so no Run Console holds it in memory.
    const res = await page.request.post(`${cluster.backendUrl}/api/v1/runs`, {
      headers: cluster.apiHeaders,
      data: { workflow_id: wf, initial_state: {} },
    });
    expect(res.ok(), await res.text()).toBe(true);
    const runId = (await res.json()).run_id as string;

    // Approval Inbox item links to the run page.
    await page.goto(`${cluster.frontendUrl}#/approvals`);
    const openRun = page.locator(`a[data-testid="approval-open-run"][href="#/runs/${runId}"]`);
    await expect(openRun).toBeVisible({ timeout: 15000 });
    await openRun.click();
    await expect(page).toHaveURL(new RegExp(`#/runs/${runId}$`));

    await page.reload();
    const banner = page.getByRole('region', { name: 'Pending approval banner' });
    await expect(banner).toBeVisible({ timeout: 15000 });
    await banner.getByRole('button', { name: /Approve & Resume/i }).click();
    await expect(page.getByLabel('Run status badge')).toContainText(/completed/i, { timeout: 15000 });
  });

  test('approval dialog shows mapped-write details and the re-approval note', async ({ page }) => {
    const items = Array.from({ length: 25 }, (_, i) => ({ channel: 'C1', text: `message ${i}` }));
    const wf = await seed(page, 'P13 mapped writes', {
      entry_node: 'send',
      nodes: {
        send: node('send', 'nexura.connectors.slack', {
          module_id: 'nexura.connectors.slack',
          config: { credential_ref: 'cred' },
          map_over: 'items',
          map_as: 'item',
          input_mapping: { operation: 'op', params: 'item' },
          reducers: { data: 'append', operation: 'append', write: 'append' },
        }),
      },
    });
    await launch(page, wf, { op: 'post_message', items });
    await expect(page.getByRole('region', { name: 'Pending approval banner' })).toBeVisible({ timeout: 15000 });

    // From the Run Console.
    await page.getByRole('button', { name: 'Review Details' }).click();
    const dialog = page.getByRole('dialog');
    const details = dialog.getByTestId('approval-details');
    await expect(details).toBeVisible();
    await expect(details.getByTestId('approval-item-count')).toContainText('25');
    await expect(details.getByTestId('approval-max-writes')).toContainText('50');
    await expect(details.getByTestId('approval-item')).toHaveCount(20);
    await expect(details.getByTestId('approval-item').first()).toContainText('message 0');
    await expect(details.getByTestId('approval-more')).toHaveText('and 5 more');
    await expect(details.getByTestId('approval-edit-note')).toContainText('pauses again');
    await expect(details.getByTestId('approval-edit-note')).toContainText('fresh approval');
    await dialog.getByRole('button', { name: 'Cancel' }).click();

    // And from the Approval Inbox.
    await page.goto(`${cluster.frontendUrl}#/approvals`);
    await page.getByRole('button', { name: 'Review & Decide' }).first().click();
    await expect(page.getByRole('dialog').getByTestId('approval-item-count')).toContainText('25');
    await expect(page.getByRole('dialog').getByTestId('approval-more')).toHaveText('and 5 more');
  });
});
