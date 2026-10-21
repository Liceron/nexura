import { test, expect, Page } from '@playwright/test';
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

async function seed(page: Page, name: string): Promise<string> {
  const res = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
    headers: cluster.apiHeaders,
    data: {
      name,
      spec: {
        name,
        entry_node: 'start',
        nodes: { start: { id: 'start', handler: 'synthetic_router' } },
      },
    },
  });
  expect(res.ok()).toBe(true);
  return (await res.json()).workflow_id as string;
}

async function open(page: Page, workflowId: string) {
  await page.goto(`${cluster.frontendUrl}/#/workflows/${workflowId}`);
  await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
  await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);
  await expect(page.getByTestId('node-start')).toBeVisible();
}

async function savedSpec(page: Page, workflowId: string) {
  const res = await page.request.get(`${cluster.backendUrl}/api/v1/workflows/${workflowId}`);
  return (await res.json()).spec as { nodes: Record<string, Record<string, unknown>> };
}

async function save(page: Page, expectedRevision: number) {
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByLabel('Workflow revision')).toContainText(`Revision: ${expectedRevision}`);
  await expect(page.getByText('Unsaved changes')).not.toBeVisible();
}

const inspector = (page: Page) => page.getByLabel('Node properties inspector');

test('parallel step: create, edit, save, reload', async ({ page }) => {
  const wf = await seed(page, 'P2 parallel');
  await open(page, wf);

  // Keyboard-operable: focus the toolbar button and press Enter.
  await page.getByRole('button', { name: 'Add step', exact: true }).focus();
  await page.keyboard.press('Enter');
  const add = page.getByRole('button', { name: 'Parallel' });
  await add.focus();
  await page.keyboard.press('Enter');

  const node = page.locator('[data-step-kind="parallel"]');
  await expect(node).toHaveCount(1);
  await expect(inspector(page).getByTestId('composite-kind')).toHaveText('Parallel');
  // Empty branches are reported inline straight away.
  await expect(inspector(page).getByTestId('composite-errors')).toContainText('Choose at least one parallel branch');

  await inspector(page).getByLabel('Branch handler synthetic_router').check();
  await inspector(page).getByLabel('Branch handler synthetic_order_lookup').check();
  await expect(inspector(page).getByTestId('composite-errors')).toHaveCount(0);
  await expect(node).toContainText('2 branches, then join');

  // Reducer editing
  await inspector(page).getByLabel('State key').fill('results');
  await inspector(page).getByRole('button', { name: 'Add reducer' }).click();
  await expect(inspector(page).getByLabel('Remove reducer results')).toBeVisible();

  await page.getByRole('button', { name: 'Validate' }).click();
  await expect(page.getByText(/Workflow is valid/i)).toBeVisible();
  await save(page, 2);

  await page.reload();
  await expect(page.getByLabel('Workflow revision')).toContainText('Revision: 2');
  const reloaded = page.locator('[data-step-kind="parallel"]');
  await expect(reloaded).toHaveCount(1);
  await expect(reloaded).toContainText('synthetic_router | synthetic_order_lookup');
  await reloaded.click();
  await expect(inspector(page).getByLabel('Branch handler synthetic_router')).toBeChecked();
  await expect(inspector(page).getByLabel('Branch handler synthetic_order_lookup')).toBeChecked();

  const spec = await savedSpec(page, wf);
  const stored = Object.values(spec.nodes).find((n) => n.parallel);
  expect(stored?.parallel).toEqual(['synthetic_router', 'synthetic_order_lookup']);
  expect(stored?.reducers).toEqual({ results: 'append' });
});

test('map step: create, edit, save, reload', async ({ page }) => {
  const wf = await seed(page, 'P2 map');
  await open(page, wf);

  await page.getByRole('button', { name: 'Add step', exact: true }).click();
  await page.getByRole('group', { name: 'Add step', exact: true }).getByRole('button', { name: 'Map', exact: true }).click();
  const node = page.locator('[data-step-kind="map"]');
  await expect(node).toHaveCount(1);

  await inspector(page).getByLabel('Handler run for each item').selectOption('synthetic_router');
  await inspector(page).getByLabel('Map over (state list)').fill('orders');
  await inspector(page).getByLabel('Item key').fill('order');
  await inspector(page).getByLabel('Concurrency limit').fill('3');
  await expect(node).toContainText('for each orders as order (max 3)');

  await save(page, 2);
  await page.reload();
  const reloaded = page.locator('[data-step-kind="map"]');
  await expect(reloaded).toContainText('for each orders as order (max 3)');
  await reloaded.click();
  await expect(inspector(page).getByLabel('Map over (state list)')).toHaveValue('orders');
  await expect(inspector(page).getByLabel('Item key')).toHaveValue('order');
  await expect(inspector(page).getByLabel('Concurrency limit')).toHaveValue('3');

  const spec = await savedSpec(page, wf);
  const stored = Object.values(spec.nodes).find((n) => n.map_over);
  expect(stored).toMatchObject({ map_over: 'orders', map_as: 'order', max_concurrency: 3, handler: 'synthetic_router' });
});

test('sub-workflow step: reference a saved workflow, save, reload', async ({ page }) => {
  await seed(page, 'P2 child flow');
  const wf = await seed(page, 'P2 parent');
  await open(page, wf);

  await page.getByRole('button', { name: 'Add step', exact: true }).click();
  await page.getByRole('button', { name: 'Sub-workflow' }).click();
  const node = page.locator('[data-step-kind="subworkflow"]');
  await expect(node).toHaveCount(1);
  await expect(inspector(page).getByTestId('composite-errors')).toContainText('Choose the saved workflow');

  await inspector(page).getByLabel('Saved workflow').selectOption({ label: 'P2 child flow' });
  await expect(inspector(page).getByTestId('subworkflow-summary')).toContainText('1 steps, entry start');
  await expect(node).toContainText('runs P2 child flow');

  await save(page, 2);
  await page.reload();
  const reloaded = page.locator('[data-step-kind="subworkflow"]');
  await expect(reloaded).toContainText('runs P2 child flow');

  const spec = await savedSpec(page, wf);
  const stored = Object.values(spec.nodes).find((n) => n.subgraph) as Record<string, any>;
  expect(stored.subgraph.name).toBe('P2 child flow');
  expect(stored.subgraph.nodes.start.handler).toBe('synthetic_router');
});

test('undo and redo cover composite steps and their edits', async ({ page }) => {
  const wf = await seed(page, 'P2 undo');
  await open(page, wf);

  await page.getByRole('button', { name: 'Add step', exact: true }).click();
  await page.getByRole('group', { name: 'Add step', exact: true }).getByRole('button', { name: 'Map', exact: true }).click();
  await expect(page.locator('[data-step-kind="map"]')).toHaveCount(1);
  await inspector(page).getByLabel('Map over (state list)').fill('rows');
  await expect(page.locator('[data-step-kind="map"]')).toContainText('for each rows');

  await page.getByRole('button', { name: /Undo/ }).click();
  await expect(page.locator('[data-step-kind="map"]')).toContainText('for each ?');
  await page.getByRole('button', { name: /Undo/ }).click();
  await expect(page.locator('[data-step-kind="map"]')).toHaveCount(0);
  await page.getByRole('button', { name: /Redo/ }).click();
  await page.getByRole('button', { name: /Redo/ }).click();
  await expect(page.locator('[data-step-kind="map"]')).toContainText('for each rows');
  await expect(savedSpec(page, wf).then((s) => Object.keys(s.nodes))).resolves.toEqual(['start']);
});

test('invalid map field is rejected with an inline error and nothing is saved', async ({ page }) => {
  const wf = await seed(page, 'P2 negative');
  await open(page, wf);

  await page.getByRole('button', { name: 'Add step', exact: true }).click();
  await page.getByRole('group', { name: 'Add step', exact: true }).getByRole('button', { name: 'Map', exact: true }).click();
  await inspector(page).getByLabel('Handler run for each item').selectOption('synthetic_router');
  await inspector(page).getByLabel('Map over (state list)').fill('items');
  await inspector(page).getByLabel('Item key').fill('bad key!');

  // Early inline feedback in the form...
  await expect(inspector(page).getByTestId('composite-errors')).toContainText('item key must be a plain name');
  // ...and the backend verdict on save, shown inline, with no save performed.
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  const issues = page.getByTestId('validation-issues');
  await expect(issues).toContainText('INVALID_COMPOSITE');
  await expect(issues).toContainText('map_as');
  await expect(page.getByText(/Save blocked/)).toBeVisible();
  await expect(inspector(page).getByTestId('inspector-validation-error')).toContainText('INVALID_COMPOSITE');
  await expect(page.getByLabel('Workflow revision')).toContainText('Revision: 1');
  await expect(savedSpec(page, wf).then((s) => Object.keys(s.nodes))).resolves.toEqual(['start']);

  // Fixing the field clears the form error and the save goes through.
  await inspector(page).getByLabel('Item key').fill('item');
  await expect(inspector(page).getByTestId('composite-errors')).toHaveCount(0);
  await save(page, 2);
});
