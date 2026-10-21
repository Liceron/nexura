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

test.describe('SDK, API and Studio consistency (O07)', () => {
  test('playground labels final-only delivery and shows model-reported usage', async ({ page }) => {
    await page.goto(cluster.frontendUrl);
    await page.getByRole('button', { name: /Playground/i }).click();

    await expect(page.getByTestId('delivery-badge')).toContainText(/Final-only/i);

    await page.getByTestId('chat-input-textarea').fill('calculate 6 * 7');
    await page.getByTestId('send-message-button').click();
    await expect(page.getByTestId('chat-transcript')).toContainText('42', { timeout: 15000 });

    const metrics = page.getByTestId('metrics-bar');
    await expect(metrics).toContainText(/Tokens:/i);
    // Field names match the service contract, so counts render instead of "undefined".
    await expect(metrics).not.toContainText('undefined');
    await expect(metrics).toContainText(/\d+ prompt/);
  });

  test('chat turn streams live activity and saves the reply', async ({ page }) => {
    const streamRequests: string[] = [];
    page.on('request', (req) => {
      if (req.url().includes('/chat/stream')) streamRequests.push(req.method());
    });
    await page.goto(cluster.frontendUrl);
    await page.getByRole('button', { name: /Playground/i }).click();

    await page.getByTestId('chat-input-textarea').fill('calculate 9 * 9');
    await page.getByTestId('send-message-button').click();
    await expect(page.getByTestId('chat-transcript')).toContainText('81', { timeout: 15000 });
    expect(streamRequests).toEqual(['POST']);
    await expect(page.getByTestId('turn-interrupted')).toHaveCount(0);

    // The saved transcript survives a reload.
    await page.reload();
    await page.getByRole('button', { name: /Playground/i }).click();
    await expect(page.getByTestId('chat-transcript')).toContainText('81', { timeout: 15000 });
  });

  test('dropped chat stream reports an interrupted turn and saves nothing', async ({ page }) => {
    await page.route('**/chat/stream', (route) => route.abort('connectionreset'));
    await page.goto(cluster.frontendUrl);
    await page.getByRole('button', { name: /Playground/i }).click();

    await page.getByTestId('chat-input-textarea').fill('calculate 5 + 5');
    await page.getByTestId('send-message-button').click();

    const notice = page.getByTestId('turn-interrupted');
    await expect(notice).toBeVisible({ timeout: 15000 });
    await expect(notice).toContainText(/Nothing from this turn was saved/);
    // The optimistic message is replaced by the authoritative (unchanged) transcript.
    await expect(page.getByTestId('chat-transcript')).not.toContainText('calculate 5 + 5');
    await expect(page.getByTestId('send-message-button')).toBeVisible();
  });

  test('run event stream recovers from a dropped connection without duplicate events', async ({
    page,
  }) => {
    let eventRequests = 0;
    await page.route('**/api/v1/runs/*/events**', async (route) => {
      eventRequests += 1;
      if (eventRequests === 1) {
        // Simulate a network drop on the first live stream connection only.
        await route.abort('connectionreset');
      } else {
        await route.continue();
      }
    });

    await page.goto(cluster.frontendUrl);
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();
    await page.getByLabel('Workflow Name').fill('O07 Stream Recovery Flow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    await page.getByRole('button', { name: 'Run', exact: true }).click();
    const runModal = page.getByRole('dialog');
    await runModal
      .getByLabel('Initial state JSON input')
      .fill(JSON.stringify({ order_id: 'ORD-707', amount: 5000, category: 'refund' }));
    await runModal.getByRole('checkbox', { name: /Execute in background/i }).check();
    await runModal.getByRole('button', { name: /Launch Run/i }).click();

    const approvalBanner = page.getByRole('region', { name: 'Pending approval banner' });
    await expect(approvalBanner).toBeVisible({ timeout: 15000 });
    await expect.poll(() => eventRequests, { timeout: 10000 }).toBeGreaterThanOrEqual(2);

    await approvalBanner.getByRole('button', { name: 'Review action', exact: true }).click();
    await page.getByRole('dialog', { name: 'Review action', exact: true }).getByRole('button', { name: 'Approve action', exact: true }).click();
    await expect(page.getByLabel('Run status badge')).toContainText('Completed', { timeout: 15000 });

    // Cursor-based resume must not replay already-delivered events.
    await expect(page.getByText('RUN STARTED')).toHaveCount(1);
  });

  test('foreign workspace sees access denied for a known workflow link', async ({ page }) => {
    await page.goto(cluster.frontendUrl);
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('English and Hindi greeting', { exact: true }).click();
    await page.getByLabel('Workflow Name').fill('O07 Private Workflow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    const ownedUrl = page.url();

    await page.getByLabel(/^Active workspace selector:/).click();
    await page.getByLabel('Workspace Identifier').fill('tenant-o07-foreign');
    await page.getByRole('button', { name: 'Confirm' }).click();
    await expect(page.getByLabel(/^Active workspace selector:/)).toContainText('tenant-o07-foreign');

    await page.goto(ownedUrl);
    const denied = page.getByRole('alert', { name: 'Workflow access denied' });
    await expect(denied).toBeVisible({ timeout: 10000 });
    await expect(denied).toContainText(/different workspace/i);
    await expect(page.getByText('O07 Private Workflow')).toHaveCount(0);
  });
});
