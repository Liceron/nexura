import { test, expect } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

let cluster: TestCluster;
test.beforeAll(async () => { cluster = await startTestCluster(); });
test.afterAll(async () => { if (cluster) await cluster.stop(); });

test('Studio shows persisted diagnostics and downloads a content-free SDK trace', async ({ page }) => {
  await page.addInitScript((url) => localStorage.setItem('nexura_api_url', url), cluster.backendUrl);
  await page.goto(cluster.frontendUrl);
  await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);
  await page.getByRole('button', { name: /New Workflow/i }).click();
  await page.getByRole('dialog').getByText('Bilingual Customer Greeting').click();
  await page.getByLabel('Workflow Name').fill('SDK Studio Diagnostics');
  await page.getByRole('button', { name: /Validate & Create/i }).click();
  await expect(page).toHaveURL(/#\/workflows\/wf-/);
  await page.getByRole('button', { name: 'Run', exact: true }).first().click();
  await page.getByRole('button', { name: /Launch Run/i }).click();
  await expect(page.getByLabel('Run status badge')).toContainText('Completed');
  await page.getByRole('tab', { name: 'Diagnostics', exact: true }).click();
  const diagnostics = page.getByRole('region', { name: 'SDK run diagnostics' });
  await expect(diagnostics.getByLabel('Run usage summary')).toBeVisible();
  await expect(diagnostics).toContainText('Cost: not reported');
  await expect(diagnostics).toContainText('unknown');
  const download = diagnostics.getByRole('button', { name: 'Download trace JSON' });
  await expect(download).toBeEnabled();
  const waiting = page.waitForEvent('download');
  await download.click();
  const artifact = await waiting;
  const stream = await artifact.createReadStream();
  let text = '';
  for await (const chunk of stream!) text += chunk.toString();
  const trace = JSON.parse(text);
  expect(trace.spans.length).toBeGreaterThan(1);
  expect(trace.spans.every((span: { inputs: unknown; outputs: unknown }) => span.inputs === null && span.outputs === null)).toBe(true);
  await diagnostics.getByRole('checkbox', { name: 'Include redacted inputs and outputs' }).check();
  await expect(diagnostics.getByLabel('Run usage summary')).toBeVisible();
  await diagnostics.locator('article').first().getByRole('button').click();
  await expect(diagnostics.getByText('Recorded input', { exact: true })).toBeVisible();
});

test('Studio displays usage emitted by a real SDK module with a local fake model', async ({ page }) => {
  const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
    headers: cluster.apiHeaders,
    data: { spec: {
      name: 'SDK model diagnostics', entry_node: 'model_a', terminal_nodes: ['model_a'],
      nodes: { model_a: {
        id: 'model_a', module_id: 'nexura.models.provider', module_version: '1.0.0',
        input_mapping: { operation: 'operation', messages: 'messages' },
      } },
    } },
  });
  expect(created.ok()).toBe(true);
  const workflow = await created.json();
  await page.addInitScript((url) => localStorage.setItem('nexura_api_url', url), cluster.backendUrl);
  await page.goto(`${cluster.frontendUrl}/#/workflows/${workflow.workflow_id}`);
  await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);
  await page.getByRole('button', { name: 'Run', exact: true }).first().click();
  await page.getByRole('dialog').locator('textarea').first().fill(JSON.stringify({
    operation: 'generate', messages: [{ role: 'user', content: 'नमस्ते — ORD-501' }],
  }));
  await page.getByRole('button', { name: /Launch Run/i }).click();
  await expect(page.getByLabel('Run status badge')).toContainText('Completed');
  await page.getByRole('tab', { name: 'Diagnostics', exact: true }).click();
  const diagnostics = page.getByRole('region', { name: 'SDK run diagnostics' });
  await expect(diagnostics.getByLabel('Run usage summary')).toContainText('10 input / 20 output');
  await expect(diagnostics).toContainText('Usage reported for 1 of 1 attempts');
  await expect(diagnostics).toContainText('model_a');
  await expect(diagnostics.getByRole('button', { name: 'Download trace JSON' })).toBeEnabled();
});
