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

test.describe('Approval Inbox and Restart-Safe Resume (F08)', () => {
  test('discovers pending approval across reload, reviews immutable state, and resumes execution upon approval', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create workflow from Customer Order template
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();

    const nameInput = page.getByLabel('Workflow Name');
    await nameInput.fill('F08 Approval Discovery Workflow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    // Launch async run with high amount to pause at approval gate
    const runBtn = page.getByRole('button', { name: 'Run', exact: true });
    await runBtn.click();

    const runModal = page.getByRole('dialog');
    await expect(runModal).toBeVisible();

    const stateTextarea = runModal.getByLabel('Initial state JSON input');
    await stateTextarea.fill(
      JSON.stringify(
        { order_id: 'ORD-DISCOVER-801', amount: 5000, category: 'refund' },
        null,
        2
      )
    );

    const asyncCheckbox = runModal.getByRole('checkbox', { name: /Execute in background/i });
    await asyncCheckbox.check();

    const launchBtn = runModal.getByRole('button', { name: /Launch Run/i });
    await launchBtn.click();

    // Verify Run Console opens and pauses at approval gate
    await expect(page.getByRole('tab', { name: /^Run Console/i })).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('region', { name: 'Pending approval banner' })).toBeVisible({ timeout: 10000 });

    // Simulate browser restart / navigation to Approval Inbox
    await page.goto(`${cluster.frontendUrl}#/approvals`);

    // Verify Approval Inbox discovered the pending approval
    await expect(page.getByRole('heading', { name: 'Approval Inbox' })).toBeVisible();
    await expect(page.getByLabel('Pending approvals counter')).toContainText('1 Pending');
    await expect(page.getByText('Approval Gate: approval_gate')).toBeVisible();
    await expect(page.getByText('Amount: 5000')).toBeVisible();

    // Click Review & Decide
    const reviewBtn = page.getByRole('button', { name: 'Review & Decide' });
    await expect(reviewBtn).toBeVisible();
    await reviewBtn.click();

    // Verify Decision Modal renders immutable state review and consequences
    const decisionModal = page.getByRole('dialog');
    await expect(decisionModal).toBeVisible();
    await expect(decisionModal.getByText('Review Approval Decision')).toBeVisible();
    // P32c: the dialog shows the exact action first, then the paused workflow state.
    await expect(decisionModal.getByTestId('exact-action')).toBeVisible();
    await expect(decisionModal.getByText('Paused workflow state')).toBeVisible();

    // Verify the paused state content (the last <pre> is the state; earlier ones are the action)
    const statePre = decisionModal.locator('pre').last();
    await expect(statePre).toBeVisible();
    const preText = await statePre.innerText();
    expect(preText).toContain('ORD-DISCOVER-801');
    expect(preText).toContain('5000');

    // Verify consequence explanation
    await expect(decisionModal.getByText(/Approval resumes the reviewed action/i)).toBeVisible();
    await expect(decisionModal.getByText(/Rejection stops this workflow/i)).toBeVisible();

    // Click Approve & Resume
    const approveBtn = decisionModal.getByRole('button', { name: /Approve & Resume/i });
    await approveBtn.click();

    // Modal closes and Approval Inbox clears
    await expect(decisionModal).not.toBeVisible();
    await expect(page.getByText('No Pending Approvals')).toBeVisible({ timeout: 10000 });
    await expect(page.getByLabel('Pending approvals counter')).toContainText('0 Pending');
  });

  test('rejects execution from approval modal and terminates workflow safely', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create workflow
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();

    await page.getByLabel('Workflow Name').fill('F08 Rejection Test Workflow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    // Launch async run with refund to pause at gate
    const runBtn = page.getByRole('button', { name: 'Run', exact: true });
    await runBtn.click();

    const runModal = page.getByRole('dialog');
    await expect(runModal).toBeVisible();

    const stateTextarea = runModal.getByLabel('Initial state JSON input');
    await stateTextarea.fill(
      JSON.stringify(
        { order_id: 'ORD-REJECT-902', amount: 9000, category: 'refund' },
        null,
        2
      )
    );

    const asyncCheckbox = runModal.getByRole('checkbox', { name: /Execute in background/i });
    await asyncCheckbox.check();

    await runModal.getByRole('button', { name: /Launch Run/i }).click();

    // Wait for pause
    await expect(page.getByRole('tab', { name: /^Run Console/i })).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('region', { name: 'Pending approval banner' })).toBeVisible({ timeout: 10000 });

    // Navigate to Approval Inbox
    await page.goto(`${cluster.frontendUrl}#/approvals`);
    await expect(page.getByLabel('Pending approvals counter')).toContainText('1 Pending');

    // Click Review & Decide -> Reject
    await page.getByRole('button', { name: 'Review & Decide' }).click();
    const decisionModal = page.getByRole('dialog');
    await expect(decisionModal).toBeVisible();

    const rejectBtn = decisionModal.getByRole('button', { name: /Reject Execution/i });
    await rejectBtn.click();

    // Modal closes and inbox clears
    await expect(decisionModal).not.toBeVisible();
    await expect(page.getByText('No Pending Approvals')).toBeVisible({ timeout: 10000 });
  });
});
