import { test, expect } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

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

const DATASET_JSON = JSON.stringify({
  format: 'nexura.eval-dataset',
  format_version: 1,
  name: 'Order routing',
  cases: [
    {
      name: 'delhi',
      input: { order_id: 'ORD-DELHI-501' },
      expected_output: 'refund',
      expected_trajectory: ['synthetic_order_lookup', 'synthetic_router'],
    },
    {
      name: 'mumbai',
      input: { order_id: 'ORD-MUMBAI-202' },
      expected_output: 'standard_support',
      expected_trajectory: ['synthetic_order_lookup', 'synthetic_router'],
    },
  ],
});

test.describe('Studio evals view (P4)', () => {
  test('dataset editor, run, results filters, and regression comparison with gate', async ({ page }) => {
    for (const [id, spec] of [
      ['wf-good', TWO_STEP],
      ['wf-bad', ONE_STEP],
    ] as const) {
      const res = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
        headers: cluster.apiHeaders,
        data: { workflow_id: id, spec },
      });
      expect(res.ok()).toBeTruthy();
    }

    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);
    await page.locator('[data-testid="nav-item-evaluations"]').click();
    await page.locator('[data-testid="tab-studio-evals"]').click();
    await expect(page.locator('[data-testid="studio-evals-panel"]')).toBeVisible();

    // Dataset editor: import JSON, add a case via the form, delete it, save.
    await page.locator('[data-testid="studio-evals-new-dataset"]').click();
    await page.locator('[data-testid="studio-evals-dataset-id"]').fill('routing');
    await page.locator('[data-testid="studio-evals-import"]').click();
    await page.locator('[data-testid="studio-evals-json"]').fill('{not json');
    await page.locator('[data-testid="studio-evals-apply-import"]').click();
    await expect(page.locator('[data-testid="studio-evals-form-errors"]')).toContainText('Not valid JSON');
    await page.locator('[data-testid="studio-evals-json"]').fill(DATASET_JSON);
    await page.locator('[data-testid="studio-evals-apply-import"]').click();
    await expect(page.locator('[data-testid="studio-evals-case-delhi"]')).toBeVisible();
    await expect(page.locator('[data-testid="studio-evals-case-mumbai"]')).toBeVisible();

    await page.locator('[data-testid="studio-evals-add-case"]').click();
    await page.locator('[data-testid="studio-evals-case-name"]').fill('extra');
    await page.locator('[data-testid="studio-evals-case-input"]').fill('{"order_id":"ORD-NOPE"}');
    await page.locator('[data-testid="studio-evals-save-case"]').click();
    await expect(page.locator('[data-testid="studio-evals-case-extra"]')).toBeVisible();
    await page.locator('[data-testid="studio-evals-delete-extra"]').click();
    await expect(page.locator('[data-testid="studio-evals-case-extra"]')).toHaveCount(0);

    await page.locator('[data-testid="studio-evals-save-dataset"]').click();
    await expect(page.locator('[data-testid="studio-evals-notice"]')).toContainText('saved');
    await expect(page.locator('[data-testid="studio-evals-dataset-routing"]')).toBeVisible();

    // Run against the good workflow, then the bad one.
    const runAgainst = async (workflowId: string, label: string) => {
      await page.locator('[data-testid="studio-evals-tab-run"]').click();
      await page.locator('[data-testid="studio-evals-run-target"]').selectOption(`workflow:${workflowId}`);
      await page.locator('[data-testid="studio-evals-run-output-key"]').fill('route');
      await page.locator('[data-testid="studio-evals-run-label"]').fill(label);
      await page.locator('[data-testid="studio-evals-run"]').click();
      await expect(page.locator('[data-testid="studio-evals-results"]')).toBeVisible({ timeout: 15000 });
    };
    await runAgainst('wf-good', 'baseline');
    await expect(page.locator('[data-testid="studio-evals-pass-rate"]')).toHaveText('100.0%');
    await expect(page.locator('[data-testid="studio-evals-result-delhi"]')).toContainText('tool_trajectory');
    await runAgainst('wf-bad', 'candidate');
    await expect(page.locator('[data-testid="studio-evals-pass-rate"]')).toHaveText('0.0%');
    await expect(page.locator('[data-testid="studio-evals-result-delhi"]')).toContainText(/missing|differs|calls/i);

    // Results filters.
    await page.locator('[data-testid="studio-evals-status-filter"]').selectOption('passed');
    await expect(page.locator('[data-testid^="studio-evals-result-"]')).toHaveCount(0);
    await page.locator('[data-testid="studio-evals-status-filter"]').selectOption('failed');
    await expect(page.locator('[data-testid^="studio-evals-result-"]')).toHaveCount(2);
    await page.locator('[data-testid="studio-evals-search"]').fill('mumbai');
    await expect(page.locator('[data-testid^="studio-evals-result-"]')).toHaveCount(1);

    // Compare: candidate regressed, gate fails.
    await page.locator('[data-testid="studio-evals-tab-compare"]').click();
    const options = await page.locator('[data-testid="studio-evals-base"] option').allTextContents();
    const baseValue = await page
      .locator('[data-testid="studio-evals-base"] option', { hasText: 'baseline' })
      .getAttribute('value');
    const candValue = await page
      .locator('[data-testid="studio-evals-candidate"] option', { hasText: 'candidate' })
      .getAttribute('value');
    expect(options.length).toBeGreaterThan(2);
    await page.locator('[data-testid="studio-evals-base"]').selectOption(baseValue!);
    await page.locator('[data-testid="studio-evals-candidate"]').selectOption(candValue!);
    await page.locator('[data-testid="studio-evals-compare"]').click();
    await expect(page.locator('[data-testid="studio-evals-gate"]')).toHaveAttribute('data-passed', 'false');
    await expect(page.locator('[data-testid="studio-evals-count-regression"]')).toContainText('2');
    await expect(page.locator('[data-testid="studio-evals-row-delhi"]')).toHaveAttribute('data-change', 'regression');

    await page.locator('[data-testid="studio-evals-change-filter"]').selectOption('improvement');
    await expect(page.locator('[data-testid^="studio-evals-row-"]')).toHaveCount(0);
    await page.locator('[data-testid="studio-evals-change-filter"]').selectOption('regression');
    await expect(page.locator('[data-testid^="studio-evals-row-"]')).toHaveCount(2);

    // Reversed: improvements only, gate passes.
    await page.locator('[data-testid="studio-evals-base"]').selectOption(candValue!);
    await page.locator('[data-testid="studio-evals-candidate"]').selectOption(baseValue!);
    await page.locator('[data-testid="studio-evals-compare"]').click();
    await expect(page.locator('[data-testid="studio-evals-gate"]')).toHaveAttribute('data-passed', 'true');

    // Invalid threshold is refused client-side.
    await page.locator('[data-testid="studio-evals-tolerance"]').fill('7');
    await page.locator('[data-testid="studio-evals-compare"]').click();
    await expect(page.locator('[data-testid="studio-evals-form-errors"]')).toBeVisible();
  });

  test('server-side validation errors are shown for a bad dataset', async ({ page }) => {
    await page.goto(cluster.frontendUrl);
    await page.locator('[data-testid="nav-item-evaluations"]').click();
    await page.locator('[data-testid="tab-studio-evals"]').click();
    await page.locator('[data-testid="studio-evals-new-dataset"]').click();
    await page.locator('[data-testid="studio-evals-dataset-id"]').fill('bad');
    await page.locator('[data-testid="studio-evals-save-dataset"]').click();
    await expect(page.locator('[data-testid="studio-evals-form-errors"]')).toContainText('at least one case');
  });
});
