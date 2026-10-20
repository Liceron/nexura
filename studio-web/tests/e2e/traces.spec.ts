import { test, expect } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

let cluster: TestCluster;
const FAKE_SECRET = 'sk-FAKE0123456789abcdef';

test.beforeAll(async () => {
  cluster = await startTestCluster();
});
test.afterAll(async () => {
  if (cluster) await cluster.stop();
});
test.beforeEach(async ({ page }) => {
  await page.addInitScript((url) => localStorage.setItem('nexura_api_url', url), cluster.backendUrl);
});

const spec = {
  name: 'Trace agent workflow',
  entry_node: 'agent',
  terminal_nodes: ['agent'],
  nodes: { agent: { id: 'agent', handler: 'synthetic_trace_agent' } },
};

async function startRun(
  request: import('@playwright/test').APIRequestContext,
  state: Record<string, unknown>,
): Promise<void> {
  // A failing node answers with an error status but the run is still recorded.
  await request.post(`${cluster.backendUrl}/api/v1/runs`, {
    headers: cluster.apiHeaders,
    data: { spec, initial_state: state },
  });
}

test.describe('Run traces (P3)', () => {
  test('shows the waterfall, expands a tool call with redacted secrets, filters failed runs', async ({
    page,
  }) => {
    await startRun(page.request, { topic: 'refunds' });
    await startRun(page.request, { topic: 'refunds', fail: true });

    await page.goto(`${cluster.frontendUrl}/#/traces`);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);
    await page.getByTestId('nav-item-traces').waitFor();

    // Filter by status: failed only, then completed only.
    const rows = page.getByTestId('trace-run-row');
    await expect(rows).toHaveCount(2);
    await page.getByLabel('Run status').selectOption('failed');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toHaveAttribute('data-status', 'failed');
    await page.getByLabel('Run status').selectOption('completed');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toHaveAttribute('data-status', 'completed');

    // Open the completed run's trace.
    await rows.first().getByRole('link', { name: /Open trace/ }).click();
    await expect(page).toHaveURL(/#\/traces\/run-/);
    const waterfall = page.getByTestId('trace-waterfall');
    await expect(waterfall).toBeVisible();
    await expect(waterfall.locator('[data-kind="node"]')).toHaveCount(1);
    await expect(waterfall.locator('[data-kind="agent"]')).toHaveCount(1);
    await expect(waterfall.locator('[data-kind="model"]')).toHaveCount(2);
    await expect(waterfall.locator('[data-kind="tool"]')).toHaveCount(1);
    const summary = page.getByLabel('Run trace summary');
    await expect(summary).toContainText('240 in / 60 out');
    await expect(summary).toContainText('Errors: 0');

    // Expand the tool call. Content stays hidden until it is requested.
    const tool = waterfall.locator('[data-kind="tool"]');
    await tool.getByRole('button').click();
    const details = page.getByRole('region', { name: 'Span details for lookup_policy' });
    await expect(details).toContainText('Include redacted inputs and outputs');
    await expect(details).not.toContainText('refunds');

    await page.getByRole('checkbox', { name: 'Include redacted inputs and outputs' }).check();
    await expect(details).toContainText('[REDACTED]');
    await expect(details).toContainText('"topic": "refunds"');
    await expect(details).toContainText('Internal key [REDACTED]');
    expect(await page.content()).not.toContain(FAKE_SECRET);
    expect(await page.locator('body').innerText()).not.toContain(FAKE_SECRET);
  });

  test('highlights errors in a failed run and filters to error spans', async ({ page }) => {
    await startRun(page.request, { fail: true });
    await page.goto(`${cluster.frontendUrl}/#/traces`);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);
    await page.getByLabel('Run status').selectOption('failed');
    await page.getByTestId('trace-run-row').first().getByRole('link', { name: /Open trace/ }).click();

    const waterfall = page.getByTestId('trace-waterfall');
    await expect(waterfall.locator('[data-kind="node"]')).toHaveAttribute('data-status', 'error');
    await expect(waterfall.locator('[data-kind="tool"]')).toHaveAttribute('data-status', 'ok');

    await page.getByLabel('Show spans').selectOption('error');
    await expect(waterfall.locator('[data-kind="tool"]')).toHaveCount(0);
    await expect(waterfall.locator('[data-status="error"]').first()).toBeVisible();

    await page.getByRole('checkbox', { name: 'Include redacted inputs and outputs' }).check();
    await waterfall.locator('[data-kind="node"]').getByRole('button').click();
    const details = page.getByRole('region', { name: 'Span details for synthetic_trace_agent' });
    await expect(details.getByRole('alert')).toContainText('synthetic failure');
    expect(await page.locator('body').innerText()).not.toContain(FAKE_SECRET);
  });

  test('run history links to the trace and the run console has a Trace tab', async ({ page }) => {
    const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
      headers: cluster.apiHeaders,
      data: { spec },
    });
    expect(created.ok()).toBe(true);
    const workflow = await created.json();
    await page.goto(`${cluster.frontendUrl}/#/workflows/${workflow.workflow_id}`);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);
    await page.getByRole('button', { name: 'Run', exact: true }).first().click();
    await page.getByRole('button', { name: /Launch Run/i }).click();
    await expect(page.getByLabel('Run status badge')).toContainText('Completed');

    await page.getByRole('tab', { name: 'Trace', exact: true }).click();
    await expect(page.getByTestId('trace-waterfall').locator('[data-kind="tool"]')).toHaveCount(1);

    await page.getByRole('tab', { name: 'History', exact: true }).click();
    await page.getByTestId('run-history-trace-link').click();
    await expect(page).toHaveURL(/#\/traces\/run-/);
    await expect(page.getByTestId('trace-waterfall')).toBeVisible();
  });
});
