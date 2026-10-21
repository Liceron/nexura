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

const SPEC = {
  name: 'support',
  entry_node: 'lookup',
  nodes: { lookup: { id: 'lookup', handler: 'synthetic_order_lookup' } },
  terminal_nodes: ['lookup'],
};

async function openRedteam(page: import('@playwright/test').Page) {
  const res = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
    headers: cluster.apiHeaders,
    data: { workflow_id: `wf-redteam-${Date.now()}-${Math.floor(Math.random() * 1e6)}`, spec: SPEC },
  });
  expect(res.ok()).toBeTruthy();
  await page.goto(cluster.frontendUrl);
  await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
  await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);
  await page.locator('[data-testid="nav-item-evaluations"]').click();
  await page.locator('[data-testid="tab-studio-evals"]').click();
  await page.locator('[data-testid="studio-evals-tab-redteam"]').click();
  await expect(page.locator('[data-testid="studio-evals-redteam"]')).toBeVisible();
}

test.describe('Studio red-team preset (P17b)', () => {
  test('runs the pack offline against a saved workflow and shows cases and both scorers', async ({ page }) => {
    await openRedteam(page);
    await page.locator('[data-testid="studio-evals-redteam-run"]').click();
    const verdict = page.locator('[data-testid="studio-evals-redteam-verdict"]');
    await expect(verdict).toHaveAttribute('data-ok', 'true');
    await expect(page.locator('[data-testid="studio-evals-redteam-model"]')).toContainText('offline fake model');
    await expect(page.locator('[data-testid="studio-evals-redteam-scorers"]')).toContainText(
      'no_canary_leak, no_write_without_approval',
    );
    await expect(page.locator('[data-testid^="studio-evals-redteam-case-"]')).toHaveCount(13);
    await expect(page.locator('[data-testid="studio-evals-redteam-case-injection-1"]')).toHaveAttribute('data-safe', 'true');
    await expect(page.locator('[data-testid^="studio-evals-redteam-case-"][data-safe="false"]')).toHaveCount(0);
  });

  test('flags a failing case with its reason (the response is rewritten; the run itself is real)', async ({ page }) => {
    await page.route('**/evaluations/studio/redteam', async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.ok = false;
      body.cases[0].safe = false;
      body.cases[0].status = 'failed';
      body.cases[0].reason = "leaked in ['answer']";
      await route.fulfill({ response, json: body });
    });
    await openRedteam(page);
    await page.locator('[data-testid="studio-evals-redteam-run"]').click();
    await expect(page.locator('[data-testid="studio-evals-redteam-verdict"]')).toHaveAttribute('data-ok', 'false');
    const row = page.locator('[data-testid="studio-evals-redteam-case-injection-1"]');
    await expect(row).toHaveAttribute('data-safe', 'false');
    await expect(row).toContainText("leaked in ['answer']");
    await expect(page.locator('[data-testid^="studio-evals-redteam-case-"][data-safe="false"]')).toHaveCount(1);
  });

  test('a server error is shown and no results appear', async ({ page }) => {
    await page.route('**/evaluations/studio/redteam', (route) =>
      route.fulfill({ status: 429, json: { detail: { code: 'RATE_LIMITED', message: 'Too many red-team runs; try again shortly.' } } }),
    );
    await openRedteam(page);
    await page.locator('[data-testid="studio-evals-redteam-run"]').click();
    await expect(page.locator('[data-testid="studio-evals-error"]')).toBeVisible();
    await expect(page.locator('[data-testid="studio-evals-redteam-results"]')).toHaveCount(0);
  });
});
