import { test, expect } from '@playwright/test';
import { createHmac } from 'node:crypto';
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

test.describe('Evaluations, Schedules & Signed Webhook Dry Runs (F13)', () => {
  test('executes deterministic benchmarks, manages timezone schedules, signed webhooks, and observable dry-run outbox', async ({
    page,
  }) => {
    // Triggers must target a workflow that exists in this workspace, so create one first.
    const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
      headers: cluster.apiHeaders,
      data: {
        spec: {
          name: 'Order support (trigger target)',
          entry_node: 'lookup',
          nodes: { lookup: { id: 'lookup', handler: 'synthetic_order_lookup' } },
          terminal_nodes: ['lookup'],
        },
      },
    });
    expect(created.ok()).toBeTruthy();
    const targetWorkflowId: string = (await created.json()).workflow_id;

    // 1. Open Studio and verify connection
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    // 2. Navigate to Evaluations tab
    const evalNavBtn = page.locator('[data-testid="nav-item-evaluations"]');
    await expect(evalNavBtn).toBeVisible();
    await evalNavBtn.click();

    const evalView = page.locator('[data-testid="evaluations-view"]');
    await expect(evalView).toBeVisible();

    // 3. Verify built-in benchmark dataset is present
    await expect(page.locator('[data-testid="dataset-item-benchmark_customer_support"]')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('Built-in Fixture', { exact: true })).toBeVisible();

    // 4. Switch to "Run Benchmark" tab and run evaluation
    const runTab = page.locator('[data-testid="tab-run"]');
    await runTab.click();

    const runBtn = page.locator('[data-testid="btn-run-eval"]');
    await expect(runBtn).toBeVisible();
    await runBtn.click();

    // Verify evaluation summary results
    await expect(page.locator('[data-testid="eval-results-container"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="stat-pass-rate"]')).toContainText('100.0%');
    await expect(page.locator('[data-testid="stat-mean-score"]')).toContainText('1.00');
    await expect(page.getByText('PASSED').first()).toBeVisible();

    // 5. Navigate to Triggers tab
    const triggersNavBtn = page.locator('[data-testid="nav-item-triggers"]');
    await expect(triggersNavBtn).toBeVisible();
    await triggersNavBtn.click();

    const triggersView = page.locator('[data-testid="triggers-view"]');
    await expect(triggersView).toBeVisible();

    // 6. Create schedule with Asia/Kolkata timezone
    const openCreateSchedBtn = page.locator('[data-testid="btn-open-create-schedule"]');
    await openCreateSchedBtn.click();

    await page.locator('[data-testid="input-new-schedule-id"]').fill('sched_asia_kolkata');
    // B39: the workflow is picked from the workspace's workflows (no free-text id).
    await page.locator('[data-testid="select-new-schedule-wfid"]').selectOption(targetWorkflowId);
    await page.locator('[data-testid="input-schedule-timezone"]').fill('Asia/Kolkata');
    await page.locator('[data-testid="btn-submit-create-schedule"]').click();

    // Verify created schedule in list
    await expect(page.locator('[data-testid="schedule-item-sched_asia_kolkata"]')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('TZ: Asia/Kolkata')).toBeVisible();
    await expect(page.getByText('ACTIVE').first()).toBeVisible();

    // 7. Create second schedule in America/New_York timezone
    await openCreateSchedBtn.click();
    await page.locator('[data-testid="input-new-schedule-id"]').fill('sched_ny_hourly');
    await page.locator('[data-testid="select-new-schedule-wfid"]').selectOption(targetWorkflowId);
    await page.locator('[data-testid="input-schedule-timezone"]').fill('America/New_York');
    await page.locator('[data-testid="btn-submit-create-schedule"]').click();

    await expect(page.locator('[data-testid="schedule-item-sched_ny_hourly"]')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('TZ: America/New_York')).toBeVisible();

    // 8. Test Pause / Resume cycle
    const pauseBtn = page.locator('[data-testid="btn-pause-schedule-sched_asia_kolkata"]');
    await pauseBtn.click();
    await expect(page.locator('[data-testid="btn-resume-schedule-sched_asia_kolkata"]')).toBeVisible({ timeout: 10000 });

    const resumeBtn = page.locator('[data-testid="btn-resume-schedule-sched_asia_kolkata"]');
    await resumeBtn.click();
    await expect(page.locator('[data-testid="btn-pause-schedule-sched_asia_kolkata"]')).toBeVisible({ timeout: 10000 });

    // 9. Switch to Signed Webhooks tab
    const webhooksTab = page.locator('[data-testid="tab-webhooks"]');
    await webhooksTab.click();

    // Register webhook endpoint
    const openRegisterWhBtn = page.locator('[data-testid="btn-open-register-webhook"]');
    await openRegisterWhBtn.click();
    await page.locator('[data-testid="input-new-webhook-id"]').fill('orders_ingress');
    await page.locator('[data-testid="select-new-webhook-wfid"]').selectOption(targetWorkflowId);
    await page.locator('[data-testid="btn-submit-register-webhook"]').click();

    await expect(page.locator('[data-testid="webhook-item-orders_ingress"]')).toBeVisible({ timeout: 10000 });

    // 9b. B39: the signing secret is shown once with the delivery URL. Sign a delivery with it
    // (Node crypto, v2 scheme) and POST it to the real /deliver route from outside Studio.
    const secretPanel = page.locator('[data-testid="webhook-secret-panel"]');
    await expect(secretPanel).toBeVisible();
    const secret = ((await page.locator('[data-testid="webhook-secret-value"]').textContent()) || '').trim();
    expect(secret.length).toBeGreaterThanOrEqual(32);
    const deliveryUrl = ((await page.locator('[data-testid="webhook-secret-delivery-url"]').textContent()) || '').trim();
    // Studio talks to the service through its own origin (/api is proxied to the backend), so the
    // shown URL is on that origin; the same path on the backend's own address works as well.
    const deliverPath = '/api/v1/triggers/webhooks/orders_ingress/deliver';
    expect(deliveryUrl).toBe(`${cluster.frontendUrl.replace(/\/+$/, '')}${deliverPath}`);
    await expect(secretPanel).toContainText('X-Webhook-Signature');
    await expect(secretPanel).toContainText('X-Webhook-Timestamp');
    await expect(secretPanel).toContainText('X-Webhook-Event-ID');

    const deliveryBody = JSON.stringify({ event: 'ping', order_id: 'ord_e2e' }); // canonical: sorted keys, no spaces
    const signedDelivery = (url: string, eventId: string) => {
      const ts = String(Math.floor(Date.now() / 1000));
      const signature = createHmac('sha256', secret).update(`v2.${ts}.${eventId}.${deliveryBody}`).digest('hex');
      return page.request.post(url, {
        data: deliveryBody,
        headers: {
          'Content-Type': 'application/json',
          'X-Webhook-Timestamp': ts,
          'X-Webhook-Event-ID': eventId,
          'X-Webhook-Signature': signature,
        },
      });
    };
    for (const [url, eventId] of [
      [deliveryUrl, `evt-e2e-studio-${Date.now()}`],
      [`${cluster.backendUrl.replace(/\/+$/, '')}${deliverPath}`, `evt-e2e-direct-${Date.now()}`],
    ]) {
      const delivered = await signedDelivery(url, eventId);
      expect(delivered.status()).toBe(200);
      const deliveredJson = await delivered.json();
      expect(deliveredJson.accepted).toBe(true);
      expect(deliveredJson.is_duplicate).toBe(false);
    }

    // The secret was never persisted, and is gone once dismissed.
    const stored = await page.evaluate(() =>
      [localStorage, sessionStorage]
        .map((s) => Array.from({ length: s.length }, (_, i) => `${s.key(i)}=${s.getItem(s.key(i) as string)}`).join('\n'))
        .join('\n'),
    );
    expect(stored).not.toContain(secret);
    expect(page.url()).not.toContain(secret);
    await page.locator('[data-testid="webhook-secret-dismiss"]').click();
    await expect(secretPanel).toHaveCount(0);
    await expect(page.getByText(secret)).toHaveCount(0);

    // 10. Deliver valid signed event
    const deliverValidBtn = page.locator('[data-testid="btn-test-webhook-valid"]');
    await deliverValidBtn.click();

    await expect(page.locator('[data-testid="webhook-test-result"]')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('DELIVERED', { exact: true })).toBeVisible();
    await expect(page.getByText('Duplicate Flag: false')).toBeVisible();

    // 11. Replay exact duplicate event -> enqueues once, shows DUPLICATE flag
    const replayBtn = page.locator('[data-testid="btn-test-webhook-replay"]');
    await replayBtn.click();
    await expect(page.getByText('DUPLICATE (INGESTED ONCE)')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('Duplicate Flag: true')).toBeVisible();

    // 12. Negative test: invalid signature
    const badSigBtn = page.locator('[data-testid="btn-test-webhook-bad-sig"]');
    await badSigBtn.click();
    await expect(page.getByText(/INVALID_SIGNATURE/i).or(page.getByText(/Signature verification failed/i))).toBeVisible({ timeout: 10000 });

    // 13. Switch to Dry-Run Outbox tab
    const outboxTab = page.locator('[data-testid="tab-outbox"]');
    await outboxTab.click();

    // Verify strict dry-run guarantee
    await expect(page.getByText(/DRY_RUN_ONLY/i)).toBeVisible();

    // Dispatch message to dry-run outbox
    const dispatchBtn = page.locator('[data-testid="btn-dispatch-outbox"]');
    await dispatchBtn.click();

    await expect(page.locator('[data-testid="dispatch-result-box"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="dispatch-result-box"]').getByText('dry_run')).toBeVisible();
    await expect(page.locator('[data-testid="dispatch-result-box"]').getByText('Duplicate Flag: false')).toBeVisible();

    // Dispatch duplicate message with same dedup key
    await dispatchBtn.click();
    await expect(page.locator('[data-testid="dispatch-result-box"]').getByText('deduplicated')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="dispatch-result-box"]').getByText('Duplicate Flag: true')).toBeVisible();

    // 14. B39: the workflow page's Triggers entry opens the triggers view preselected for it.
    await page.goto(`${cluster.frontendUrl}/#/workflows/${targetWorkflowId}`);
    await page.locator('[data-testid="workflow-open-triggers"]').click();
    await expect(page.locator('[data-testid="triggers-workflow-filter"]')).toContainText(targetWorkflowId);
    await page.locator('[data-testid="btn-open-create-schedule"]').click();
    await expect(page.locator('[data-testid="select-new-schedule-wfid"]')).toHaveValue(targetWorkflowId);
  });
});
