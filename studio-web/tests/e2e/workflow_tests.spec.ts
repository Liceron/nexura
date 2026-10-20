import { test, expect, type Page } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

// P22 workflow tests: save a completed run as a test case, run the workflow's tests against
// its current revision and see the regression gate. Offline: synthetic handlers only.

let cluster: TestCluster;

test.beforeAll(async () => {
  cluster = await startTestCluster();
});

test.afterAll(async () => {
  if (cluster) await cluster.stop();
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript((backendUrl) => {
    localStorage.setItem('nexura_api_url', backendUrl);
  }, cluster.backendUrl);
});

const TWO_STEP = {
  name: 'support',
  entry_node: 'lookup',
  nodes: {
    lookup: { id: 'lookup', handler: 'synthetic_order_lookup', default_transition: 'route' },
    route: { id: 'route', handler: 'synthetic_router' },
  },
  terminal_nodes: ['route'],
};
const ONE_STEP = {
  name: 'support',
  entry_node: 'lookup',
  nodes: { lookup: { id: 'lookup', handler: 'synthetic_order_lookup' } },
  terminal_nodes: ['lookup'],
};
const SECRET = 'sk-abcdef1234567890abcdef';

async function runFromStudio(page: Page, state: Record<string, unknown>) {
  await page.getByRole('button', { name: 'Run', exact: true }).first().click();
  const modal = page.getByRole('dialog');
  await modal.getByLabel('Initial state JSON input').fill(JSON.stringify(state));
  await page.getByRole('button', { name: 'Validate Graph' }).click();
  await expect(modal.getByText(/Graph validation passed/i)).toBeVisible();
  await page.getByRole('button', { name: /Launch Run/i }).click();
  await expect(page.getByLabel('Run status badge')).toContainText('Completed', { timeout: 15000 });
}

test.describe('Workflow tests (P22)', () => {
  test('save runs as tests, run them, and see the regression gate', async ({ page }) => {
    const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
      headers: cluster.apiHeaders,
      data: { workflow_id: 'wf-p22', spec: TWO_STEP },
    });
    expect(created.ok()).toBeTruthy();

    await page.goto(`${cluster.frontendUrl}/#/workflows/wf-p22`);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    // No tests yet.
    await page.getByTestId('tab-tests').click();
    await expect(page.getByTestId('workflow-tests-empty')).toBeVisible();

    // First run: create the test dataset from the run console; the secret is redacted.
    await runFromStudio(page, { order_id: 'ORD-DELHI-501', note: `key ${SECRET}` });
    await page.getByTestId('save-as-test').click();
    const dialog = page.getByTestId('save-as-test-dialog');
    await expect(dialog.getByTestId('save-as-test-dataset')).toHaveValue('__new__');
    await expect(dialog.getByTestId('save-as-test-new-id')).toHaveValue('wf-p22-tests');
    await dialog.getByTestId('save-as-test-case-name').fill('delhi');
    await dialog.getByTestId('save-as-test-output-key').fill('route');
    await dialog.getByTestId('save-as-test-submit').click();
    await expect(dialog.getByTestId('save-as-test-result')).toContainText('delhi');
    await expect(dialog.getByTestId('save-as-test-redactions')).toContainText('1');
    await page.getByRole('button', { name: 'Close', exact: true }).click();

    // Second run: append to the existing dataset; its output key is prefilled.
    await runFromStudio(page, { order_id: 'ORD-MUMBAI-202' });
    await page.getByTestId('save-as-test').click();
    await expect(dialog.getByTestId('save-as-test-dataset')).toHaveValue('wf-p22-tests');
    await expect(dialog.getByTestId('save-as-test-output-key')).toHaveValue('route');
    await dialog.getByTestId('save-as-test-case-name').fill('mumbai');
    await dialog.getByTestId('save-as-test-submit').click();
    await expect(dialog.getByTestId('save-as-test-result')).toContainText('mumbai');
    await page.getByRole('button', { name: 'Close', exact: true }).click();

    // Saving the same case name again is refused by the server and shown as text.
    await page.getByTestId('save-as-test').click();
    await dialog.getByTestId('save-as-test-case-name').fill('mumbai');
    await dialog.getByTestId('save-as-test-submit').click();
    await expect(dialog.getByTestId('save-as-test-error')).toContainText('CASE_NAME_TAKEN');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();

    const stored = await page.request.get(`${cluster.backendUrl}/api/v1/evaluations/studio/datasets/wf-p22-tests`);
    expect(await stored.text()).not.toContain(SECRET);

    // Tests tab: first run is the baseline.
    await page.getByTestId('tab-tests').click();
    await expect(page.getByTestId('workflow-tests-count-wf-p22-tests')).toHaveText('2');
    await page.getByTestId('workflow-tests-run-wf-p22-tests').click();
    await expect(page.getByTestId('workflow-tests-results')).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId('workflow-tests-case-delhi')).toHaveAttribute('data-status', 'passed');
    await expect(page.getByTestId('workflow-tests-case-mumbai')).toHaveAttribute('data-status', 'passed');
    await expect(page.getByTestId('workflow-tests-no-baseline')).toBeVisible();

    // Break the workflow (drop the router), run again: both cases regress, the gate fails.
    const current = await (await page.request.get(`${cluster.backendUrl}/api/v1/workflows/wf-p22`)).json();
    const updated = await page.request.put(`${cluster.backendUrl}/api/v1/workflows/wf-p22`, {
      headers: cluster.apiHeaders,
      data: { spec: ONE_STEP, expected_revision: current.revision },
    });
    expect(updated.ok()).toBeTruthy();
    await page.reload();
    await page.getByTestId('tab-tests').click();
    await page.getByTestId('workflow-tests-run-wf-p22-tests').click();
    await expect(page.getByTestId('workflow-tests-gate')).toHaveAttribute('data-passed', 'false', { timeout: 15000 });
    await expect(page.getByTestId('workflow-tests-case-delhi')).toHaveAttribute('data-status', 'failed');
    await expect(page.getByTestId('workflow-tests-changes')).toContainText('2');
  });

  test('another workflow does not list these tests', async ({ page }) => {
    for (const id of ['wf-p22-a', 'wf-p22-b']) {
      const res = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
        headers: cluster.apiHeaders,
        data: { workflow_id: id, spec: TWO_STEP },
      });
      expect(res.ok()).toBeTruthy();
    }
    const run = await page.request.post(`${cluster.backendUrl}/api/v1/runs`, {
      headers: cluster.apiHeaders,
      data: { workflow_id: 'wf-p22-a', initial_state: { order_id: 'ORD-DELHI-501' } },
    });
    const runId = (await run.json()).run_id;
    const saved = await page.request.post(
      `${cluster.backendUrl}/api/v1/evaluations/studio/datasets/a-tests/cases/from-run`,
      { headers: cluster.apiHeaders, data: { run_id: runId, dataset_name: 'A tests' } },
    );
    expect(saved.ok()).toBeTruthy();

    await page.goto(`${cluster.frontendUrl}/#/workflows/wf-p22-b`);
    await page.getByTestId('tab-tests').click();
    await expect(page.getByTestId('workflow-tests-empty')).toBeVisible();
    await page.goto(`${cluster.frontendUrl}/#/workflows/wf-p22-a`);
    await page.getByTestId('tab-tests').click();
    await expect(page.getByTestId('workflow-tests-count-a-tests')).toHaveText('1');
  });
});
