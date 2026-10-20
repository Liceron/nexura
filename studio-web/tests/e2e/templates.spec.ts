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

test('gallery lists the templates and filters by category and search', async ({ page }) => {
  await page.goto(`${cluster.frontendUrl}/#/workflows`);
  await page.getByTestId('nav-item-templates').click();
  await expect(page).toHaveURL(/#\/templates$/);
  await expect(page.getByTestId('template-card-support-desk-refund-approval')).toBeVisible();
  await expect(page.getByTestId('template-card-rag-qa-citations')).toBeVisible();
  expect(await page.locator('[data-testid^="template-card-"]').count()).toBeGreaterThanOrEqual(6);

  await page.getByLabel('Category').selectOption('data');
  await expect(page.getByTestId('template-card-structured-extraction')).toBeVisible();
  await expect(page.getByTestId('template-card-rag-qa-citations')).toHaveCount(0);

  await page.getByLabel('Category').selectOption('');
  await page.getByLabel('Search templates...').fill('zzz-no-such-template');
  await expect(page.getByTestId('templates-empty')).toBeVisible();
});

test('preview draws the graph read-only and Use template creates a workflow', async ({ page }) => {
  await page.goto(`${cluster.frontendUrl}/#/templates`);
  await page.getByTestId('template-preview-support-desk-refund-approval').click();
  const graph = page.getByTestId('template-graph');
  await expect(graph).toBeVisible();
  await expect(page.getByTestId('template-node-issue_refund')).toBeVisible();
  await expect(page.getByTestId('template-node-issue_refund')).toContainText('approval');
  // read-only: no editing controls exist in the preview
  await expect(page.getByTestId('template-preview').getByRole('textbox')).toHaveCount(0);

  await page.getByTestId('template-preview-use').click();
  await expect(page.getByTestId('template-created')).toBeVisible();
  await expect(page.getByTestId('template-missing-credentials')).toContainText('model_api_key');
  // B34: the missing-credentials notice links to the Credentials page.
  await expect(page.getByTestId('template-credentials-link')).toHaveAttribute('href', '#/credentials');

  const list = await page.request.get(`${cluster.backendUrl}/api/v1/workflows`);
  const names = ((await list.json()).items as Array<{ name: string }>).map((w) => w.name);
  expect(names).toContain('Support desk with refund approval');

  await page.getByTestId('template-open-workflow').click();
  await expect(page).toHaveURL(/#\/workflows\/.+/);
  await expect(page.getByTestId('node-classify')).toBeVisible();
});

test('Create Workflow links to the gallery', async ({ page }) => {
  await page.goto(`${cluster.frontendUrl}/#/workflows`);
  await page.getByRole('button', { name: /new workflow|create/i }).first().click();
  await page.getByTestId('create-browse-gallery').click();
  await expect(page).toHaveURL(/#\/templates$/);
  await expect(page.getByTestId('template-grid')).toBeVisible();
});

test('instantiating an unknown template through the API is a 404', async ({ page }) => {
  const res = await page.request.post(`${cluster.backendUrl}/api/v1/templates/nope/instantiate`, {
    headers: cluster.apiHeaders,
    data: {},
  });
  expect(res.status()).toBe(404);
});
