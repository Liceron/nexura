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

const DESCRIPTION =
  'a support agent that looks up orders and asks a manager before refunds over 100';

// What the service would return for a user's own model: the draft uses installed modules only.
const FAKE_DRAFT = {
  spec: {
    spec_version: 1,
    name: 'Order support',
    entry_node: 'lookup',
    nodes: {
      lookup: {
        id: 'lookup',
        handler: 'synthetic_order_lookup',
        transitions: {},
        default_transition: 'ticket',
        requires_approval: false,
      },
      ticket: {
        id: 'ticket',
        handler: 'synthetic_ticket_creator',
        transitions: {},
        default_transition: null,
        requires_approval: true,
      },
    },
    terminal_nodes: ['ticket'],
  },
  explanation: 'Looks up the order, then opens a ticket after a manager approves.',
  warnings: ["Added a human approval to step 'ticket' because it can write."],
  attempts: 1,
  saved: false,
  model: 'fake-provider (test route)',
  validation: { valid: true, errors: [], warnings: [] },
};

async function openDescribe(page: import('@playwright/test').Page) {
  await page.goto(`${cluster.frontendUrl}/#/workflows`);
  await page.getByRole('button', { name: 'New workflow', exact: true }).click();
  await page.getByTestId('create-describe-it').click();
  await expect(page.getByTestId('describe-panel')).toBeVisible();
}

async function workflowNames(page: import('@playwright/test').Page): Promise<string[]> {
  const res = await page.request.get(`${cluster.backendUrl}/api/v1/workflows`);
  return ((await res.json()).items as Array<{ name: string }>).map((w) => w.name);
}

test('describe, review the unsaved draft, then create it through the normal path', async ({ page }) => {
  let sent: { description?: string } | null = null;
  await page.route('**/api/v1/builder/draft', async (route) => {
    sent = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FAKE_DRAFT) });
  });

  await openDescribe(page);
  await expect(page.getByTestId('describe-draft-btn')).toBeDisabled();
  await page.getByTestId('describe-input').fill(DESCRIPTION);
  await page.getByTestId('describe-draft-btn').click();

  await expect(page.getByTestId('describe-unsaved')).toBeVisible();
  await expect(page.getByTestId('describe-explanation')).toContainText('manager approves');
  await expect(page.getByTestId('describe-warnings')).toContainText('human approval');
  await expect(page.getByTestId('template-node-lookup')).toBeVisible();
  await expect(page.getByTestId('template-node-ticket')).toContainText('approval');
  expect(sent).not.toBeNull();
  expect((sent as unknown as { description: string }).description).toBe(DESCRIPTION);

  // Drafting alone saved nothing.
  expect(await workflowNames(page)).not.toContain('Order support');

  await page.getByTestId('describe-name').fill('Order support (reviewed)');
  await page.getByTestId('describe-create').click();
  await expect(page).toHaveURL(/#\/workflows\/.+/);
  await expect(page.getByTestId('node-lookup')).toBeVisible();
  expect(await workflowNames(page)).toContain('Order support (reviewed)');
});

test('discarding a draft saves nothing', async ({ page }) => {
  await page.route('**/api/v1/builder/draft', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FAKE_DRAFT) })
  );
  await openDescribe(page);
  await page.getByTestId('describe-input').fill(DESCRIPTION);
  await page.getByTestId('describe-draft-btn').click();
  await expect(page.getByTestId('describe-result')).toBeVisible();
  await page.getByTestId('describe-discard').click();
  await expect(page.getByTestId('describe-result')).toHaveCount(0);
  expect(await workflowNames(page)).not.toContain('Order support');
});

test('an unknown-module failure is shown clearly and nothing appears on the canvas', async ({ page }) => {
  await page.route('**/api/v1/builder/draft', (route) =>
    route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: {
          code: 'UNKNOWN_MODULES',
          message: 'The draft needs modules that are not installed: acme.crm.',
          errors: [],
          warnings: ["Module 'acme.crm' is not installed and was not used."],
        },
      }),
    })
  );
  await openDescribe(page);
  await page.getByTestId('describe-input').fill('sync our CRM');
  await page.getByTestId('describe-draft-btn').click();
  await expect(page.getByTestId('describe-error')).toContainText('acme.crm');
  await expect(page.getByTestId('describe-result')).toHaveCount(0);
});

test('with no model credential the real service answers 400 and Studio points to Credentials', async ({ page }) => {
  await openDescribe(page);
  await page.getByTestId('describe-input').fill(DESCRIPTION);
  await page.getByTestId('describe-draft-btn').click();
  await expect(page.getByTestId('describe-error')).toContainText(/credential/i);
  await expect(page.getByTestId('describe-open-credentials')).toBeVisible();
  await expect(page.getByTestId('describe-result')).toHaveCount(0);
});

test('the description box enforces the 4000 character limit', async ({ page }) => {
  await openDescribe(page);
  await page.getByTestId('describe-input').fill('x'.repeat(4001));
  await expect(page.getByTestId('describe-draft-btn')).toBeDisabled();
  await page.getByTestId('describe-input').fill('x'.repeat(4000));
  await expect(page.getByTestId('describe-draft-btn')).toBeEnabled();
});

test('the draft endpoint rejects an oversize description server-side', async ({ page }) => {
  const res = await page.request.post(`${cluster.backendUrl}/api/v1/builder/draft`, {
    headers: cluster.apiHeaders,
    data: { description: 'x'.repeat(4001) },
  });
  expect(res.status()).toBe(422);
  expect((await res.json()).detail.code).toBe('DESCRIPTION_TOO_LONG');
});
