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

test.describe('Run Console, Replay, and Debugging (F07)', () => {
  test('launches async run, visualizes live events timeline and canvas execution badges', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create a new workflow from Customer Order template
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();

    const nameInput = page.getByLabel('Workflow Name');
    await nameInput.fill('F07 Live Execution Workflow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();

    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    // Click the top Run button
    const runBtn = page.getByRole('button', { name: 'Run', exact: true });
    await expect(runBtn).toBeVisible();
    await runBtn.click();

    // Verify RunStartModal opens
    const runModal = page.getByRole('dialog');
    await expect(runModal).toBeVisible();
    await expect(runModal.getByText('Run Workflow: F07 Live Execution Workflow')).toBeVisible();

    // Verify pre-flight validation button
    const validateBtn = page.getByRole('button', { name: 'Validate Graph' });
    await expect(validateBtn).toBeVisible();
    await validateBtn.click();
    await expect(runModal.getByText(/Graph validation passed/i)).toBeVisible();

    // Enable async execution toggle
    const asyncCheckbox = runModal.getByRole('checkbox', { name: /Execute in background/i });
    await asyncCheckbox.check();
    await expect(asyncCheckbox).toBeChecked();

    // Launch the run
    const launchBtn = page.getByRole('button', { name: /Launch Run/i });
    await launchBtn.click();

    // Wait for the modal to close and Run Console tab to activate
    const runConsoleTab = page.getByTestId('tab-run-console');
    await expect(runConsoleTab).toBeVisible({ timeout: 10000 });

    // Verify Run Console view header
    await expect(page.getByLabel('Run identifier badge')).toBeVisible();
    await expect(page.getByLabel('Run status badge')).toBeVisible();
    await expect(page.getByText(/Events:/i)).toBeVisible();

    // Verify events appear in the timeline
    await expect(page.getByText('RUN STARTED').first()).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('Node Started').first()).toBeVisible();
    await expect(page.getByText('Node Completed').first()).toBeVisible();

    // Switch to visual graph to inspect execution
    await page.getByRole('tab', { name: 'Graph' }).click();
    await expect(page.locator('.react-flow__node').first()).toBeVisible();

    // Switch back to Run Console
    await runConsoleTab.click();

    // Switch to State Replay tab
    await page.getByRole('tab', { name: /State Replay/i }).click();
    await expect(page.getByLabel('Execution replay step scrubber')).toBeVisible();
    await expect(page.getByText(/Step 0 \(Initialized\)/i)).toBeVisible();
    await expect(page.getByText('Cumulative State Snapshot')).toBeVisible();
  });

  test('validates initial state JSON and blocks launch on invalid syntax', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create workflow
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('English and Hindi greeting', { exact: true }).click();
    await page.getByLabel('Workflow Name').fill('Validation Error Test');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    const runBtn = page.getByRole('button', { name: 'Run', exact: true });
    await runBtn.click();

    const runModal = page.getByRole('dialog');
    await expect(runModal).toBeVisible();

    const stateTextarea = runModal.getByLabel('Initial state JSON input');
    await stateTextarea.fill('INVALID { JSON');

    // Verify error message displayed and launch button disabled
    await expect(runModal.getByText(/JSON syntax error:/i)).toBeVisible();
    const launchBtn = runModal.getByRole('button', { name: /Launch Run/i });
    await expect(launchBtn).toBeDisabled();
  });

  test('pauses execution at approval gate and resumes to completion on approval decision', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create workflow from Customer Order template
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();

    const nameInput = page.getByLabel('Workflow Name');
    await nameInput.fill('Approval Gate Flow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    // Open Run Modal
    const runBtn = page.getByRole('button', { name: 'Run', exact: true });
    await runBtn.click();

    const runModal = page.getByRole('dialog');
    await expect(runModal).toBeVisible();

    // Fill high amount to trigger refund branch and approval gate
    const stateTextarea = runModal.getByLabel('Initial state JSON input');
    await stateTextarea.fill(JSON.stringify({ order_id: 'ORD-999', amount: 5000, category: 'refund' }, null, 2));

    // Enable async execution
    const asyncCheckbox = runModal.getByRole('checkbox', { name: /Execute in background/i });
    await asyncCheckbox.check();

    // Launch run
    const launchBtn = runModal.getByRole('button', { name: /Launch Run/i });
    await launchBtn.click();

    // Wait for Run Console
    await expect(page.getByTestId('tab-run-console')).toBeVisible({ timeout: 10000 });

    // Verify approval required banner
    const approvalBanner = page.getByRole('region', { name: 'Pending approval banner' });
    await expect(approvalBanner).toBeVisible({ timeout: 10000 });
    await expect(approvalBanner).toContainText('approval_gate');

    // Click Approve & Resume
    await approvalBanner.getByRole('button', { name: 'Review action', exact: true }).click();
    const approveBtn = page.getByRole('dialog', { name: 'Review action', exact: true }).getByRole('button', { name: 'Approve action', exact: true });
    await expect(approveBtn).toBeVisible();
    await approveBtn.click();

    // Verify run resumes and reaches Completed
    await expect(page.getByLabel('Run status badge')).toContainText('Completed', { timeout: 10000 });
  });
});
