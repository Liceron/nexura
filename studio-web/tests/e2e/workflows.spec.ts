import { test, expect } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

let cluster: TestCluster;

test.beforeAll(async () => {
  cluster = await startTestCluster();
});

test.afterAll(async () => {
  if (cluster) {
    await cluster.stop();
  }
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript((backendUrl) => {
    localStorage.setItem('nexura_api_url', backendUrl);
  }, cluster.backendUrl);
});

test.describe('Workflow Library, Templates & Lifecycle Management (F03)', () => {
  test('creates workflow via real API, verifies authoritative revision, and preserves on reload via deep link', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);

    // Ensure connection is established
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    // Open Create Workflow Modal
    const newWorkflowBtn = page.getByRole('button', { name: /\+ New Workflow/i });
    await expect(newWorkflowBtn).toBeVisible();
    await newWorkflowBtn.click();

    // Click Bilingual Greeting radio option inside modal
    await page.getByRole('dialog').getByText('Bilingual Customer Greeting').click();

    // Set custom name
    const nameInput = page.getByLabel('Workflow Name');
    await nameInput.fill('Bilingual Support DeepLink Spec');

    // Validate and Create
    const createBtn = page.getByRole('button', { name: /Validate & Create/i });
    await createBtn.click();

    // Browser transitions to workflow detail view with deep link hash
    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    const detailUrl = page.url();

    // Verify Authoritative Revision badge rendered
    const revBadge = page.getByLabel('Authoritative revision badge');
    await expect(revBadge).toBeVisible();
    await expect(revBadge).toContainText('Revision: 1');

    // Verify workflow name and spec hash
    await expect(page.getByRole('heading', { name: 'Bilingual Support DeepLink Spec' })).toBeVisible();
    await expect(page.getByText(/Hash:/i)).toBeVisible();

    // Reload browser and verify deep link persistence and identical authoritative revision
    await page.reload();
    await expect(page).toHaveURL(detailUrl);
    await expect(revBadge).toBeVisible();
    await expect(revBadge).toContainText('Revision: 1');
    await expect(page.getByRole('heading', { name: 'Bilingual Support DeepLink Spec' })).toBeVisible();

    // Test Save as Copy
    await page.getByRole('button', { name: /Save as Copy/i }).click();
    await page.getByRole('button', { name: /Save as Copy/i }).last().click();

    // Copy created and opened
    await expect(page.getByRole('heading', { name: /Copy of Bilingual Support DeepLink Spec/i })).toBeVisible();
    await expect(revBadge).toContainText('Revision: 1');

    // Test Deletion
    await page.getByRole('button', { name: /Delete Workflow/i }).click();
    await page.getByRole('button', { name: /^Delete$/i }).click();

    // Redirected back to library
    await expect(page).toHaveURL(/#\/workflows/);
    await expect(page.getByRole('heading', { name: 'Workflow Library' })).toBeVisible();
  });

  test('runs template as runnable local fixture with synthetic data', async ({ page }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    // Click "Use Template" for Bilingual Customer Greeting
    const useTemplateButtons = page.getByRole('button', { name: /^Use Template/i });
    await useTemplateButtons.nth(1).click();

    // Modal opens, submit creation
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    // Click Run Workflow
    const runBtn = page.getByRole('button', { name: /^Run$/i }).first();
    await runBtn.click();

    // Modal opens, submit launch
    const runModal = page.getByRole('dialog');
    await expect(runModal).toBeVisible();
    await runModal.getByRole('button', { name: /Launch Run/i }).click();

    // Result in Run Console
    await expect(page.getByLabel('Run status badge')).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('tab', { name: /^Run Console/i })).toBeVisible();

    // View State Snapshot tab
    await page.getByRole('tab', { name: /State Snapshot/i }).click();
    const resultJson = page.locator('pre');
    await expect(resultJson).toBeVisible();
    const resultText = await resultJson.innerText();
    expect(resultText).toContain('ORD-501');
  });

  test('negative case: cross-workspace search and listing isolation prevents leak', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Create a workflow in default workspace to test cross-tenant isolation
    const newWorkflowBtn = page.getByRole('button', { name: /\+ New Workflow/i });
    await newWorkflowBtn.click();
    await page.getByRole('dialog').getByText('Bilingual Customer Greeting').click();
    await page.getByLabel('Workflow Name').fill('Workspace Isolation Spec');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    await page.getByRole('button', { name: /Back to Workflows/i }).click();
    await expect(page.getByText('Workspace Isolation Spec')).toBeVisible();

    // Switch workspace to tenant-isolated
    await page.getByLabel(/^Active workspace selector:/).click();
    const wsInput = page.getByLabel('Workspace Identifier');
    await wsInput.fill('tenant-isolated');
    await page.getByRole('button', { name: 'Confirm' }).click();

    // Wait for workspace switch
    await expect(page.getByLabel(/^Active workspace selector:/)).toContainText('tenant-isolated');

    // Verify empty state is displayed and default workflows are NOT leaked
    await expect(page.getByText('No workflows found in this workspace.')).toBeVisible();
    await expect(page.getByText('Workspace Isolation Spec')).not.toBeVisible();

    // Search query in isolated workspace does not leak
    const searchInput = page.getByLabel('Search workflows input');
    await searchInput.fill('Workspace');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(page.getByText(/No workflows matching "Workspace"/i)).toBeVisible();
  });

  test('negative case: missing or deleted deep link recovers gracefully with 404 notice', async ({ page }) => {
    // Navigate directly to non-existent workflow deep link
    await page.goto(`${cluster.frontendUrl}/#/workflows/wf-nonexistent-12345`);

    // Verify clean 404 recovery UI
    await expect(page.getByText('Workflow Not Found')).toBeVisible({ timeout: 10000 });
    await expect(
      page.getByText('The requested workflow does not exist or has been deleted.')
    ).toBeVisible();

    // Clicking back to workflows recovers cleanly
    await page.getByRole('button', { name: /Back to Workflows/i }).click();
    await expect(page).toHaveURL(/#\/workflows/);
    await expect(page.getByRole('heading', { name: 'Workflow Library' })).toBeVisible();
  });

  test('negative case: template referencing incompatible module fails validation and cannot run', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);

    // Open create modal
    await page.getByRole('button', { name: /\+ New Workflow/i }).click();

    // B34: the incompatible test fixture is not offered to users any more (it stays a unit-test
    // fixture, passed to the modal explicitly in tests/unit/workflows.test.tsx).
    await expect(page.getByRole('dialog').getByText('Customer Order Triage & Support')).toBeVisible();
    await expect(
      page.getByRole('dialog').getByText('Incompatible Billing Workflow (Test Fixture)')
    ).toHaveCount(0);
    await page.getByRole('button', { name: 'Cancel' }).click();

    // The server still rejects a spec that references the unregistered module.
    const res = await page.request.post(`${cluster.backendUrl}/api/v1/workflows/validate`, {
      headers: cluster.apiHeaders,
      data: {
        spec: {
          name: 'IncompatibleBillingWorkflow',
          entry_node: 'unsupported_step',
          nodes: {
            unsupported_step: {
              id: 'unsupported_step',
              handler: 'nonexistent.billing.v99',
              transitions: {},
              requires_approval: false,
            },
          },
        },
      },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.valid).toBe(false);
    expect(JSON.stringify(body.errors)).toMatch(/UNKNOWN_HANDLER/i);
    expect(JSON.stringify(body.errors)).toMatch(/nonexistent\.billing\.v99/i);
  });
});
