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
        nodes: {
          start: { id: 'start', handler: 'synthetic_router', default_transition: 'end', retry: { max_attempts: 2 } },
          end: { id: 'end', handler: 'synthetic_order_lookup' },
        },
        terminal_nodes: ['end'],
      },
    },
  });
  expect(res.ok()).toBe(true);
  return (await res.json()).workflow_id as string;
}

async function openModal(page: Page, workflowId: string) {
  await page.goto(`${cluster.frontendUrl}/#/workflows/${workflowId}`);
  await expect(page.getByTestId('node-start')).toBeVisible();
  await page.getByRole('button', { name: 'Import / Export JSON' }).click();
}

test('export as Python shows code that names the handlers', async ({ page }) => {
  const id = await seed(page, 'py-export');
  await openModal(page, id);
  await page.getByRole('tab', { name: 'Export as Python' }).click();
  const code = page.getByLabel('Exported workflow Python');
  await expect(code).toHaveValue(/spec = WorkflowSpec\(/);
  await expect(code).toHaveValue(/handler="synthetic_router"/);
  await expect(code).toHaveValue(/Retries: up to 2/);
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download .py' }).click();
  expect((await download).suggestedFilename()).toBe(`workflow_${id}.py`);
});

test('import from Python creates a workflow, and unsafe code is rejected with line numbers', async ({
  page,
}) => {
  const id = await seed(page, 'py-roundtrip');
  const res = await page.request.get(`${cluster.backendUrl}/api/v1/workflows/${id}/export/python`);
  const source = ((await res.json()) as { source: string }).source;

  await openModal(page, id);
  await page.getByRole('tab', { name: 'Import from Python' }).click();
  const input = page.getByLabel('Workflow Python import input');

  await input.fill(
    'from nexura_runtime.workflow import WorkflowNode, WorkflowSpec\n' +
      'import os\n' +
      'os.system("touch /tmp/should-never-exist")\n'
  );
  await page.getByRole('button', { name: 'Import Python', exact: true }).click();
  const errors = page.getByLabel('Python import errors');
  await expect(errors).toContainText(/line 2/);

  await input.fill(source.replace('name="py-roundtrip"', 'name="py-imported"'));
  await page.getByRole('button', { name: 'Import Python', exact: true }).click();
  await expect(page).not.toHaveURL(new RegExp(`#/workflows/${id}$`));
  await expect.poll(async () => {
    const list = await page.request.get(`${cluster.backendUrl}/api/v1/workflows`);
    const items = ((await list.json()) as { items: { name: string }[] }).items;
    return items.some((w) => w.name === 'py-imported');
  }).toBe(true);
});
