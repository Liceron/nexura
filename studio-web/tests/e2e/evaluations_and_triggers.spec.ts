import { test, expect, type Page } from '@playwright/test';
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


async function expectNoPageOverflow(page: Page) {
  const { scrollWidth, innerWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(innerWidth);
}


/** The element's box lies fully inside the 390px phone viewport (not pushed off to the side). */
async function expectInPhoneView(locator: import('@playwright/test').Locator) {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
}

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
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // 2. Navigate to Evaluations tab
    const evalNavBtn = page.locator('[data-testid="nav-item-evaluations"]');
    await expect(evalNavBtn).toBeVisible();
    await evalNavBtn.click();

    const evalView = page.locator('[data-testid="evaluations-view"]');
    await expect(evalView).toBeVisible();

    // 3. Verify built-in benchmark dataset is present
    await expect(page.locator('[data-testid="dataset-item-benchmark_customer_support"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="dataset-item-benchmark_customer_support"]')).toContainText('Built-in fixture');

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
    await expect(page.locator('[data-testid="eval-results-container"]').getByRole('cell', { name: 'Passed', exact: true }).first()).toBeVisible();
    await expect(page.locator('[data-testid="eval-results-container"]').getByRole('cell', { name: 'Failed', exact: true })).toHaveCount(0);

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
    await expect(page.locator('[data-testid="schedule-timezone-sched_asia_kolkata"]')).toHaveText('Asia/Kolkata');
    await expect(page.locator('[data-testid="schedule-status-sched_asia_kolkata"]')).toContainText('Active');

    // 7. Create second schedule in America/New_York timezone
    await openCreateSchedBtn.click();
    await page.locator('[data-testid="input-new-schedule-id"]').fill('sched_ny_hourly');
    await page.locator('[data-testid="select-new-schedule-wfid"]').selectOption(targetWorkflowId);
    await page.locator('[data-testid="input-schedule-timezone"]').fill('America/New_York');
    await page.locator('[data-testid="btn-submit-create-schedule"]').click();

    await expect(page.locator('[data-testid="schedule-item-sched_ny_hourly"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="schedule-timezone-sched_ny_hourly"]')).toHaveText('America/New_York');

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
    // Masked by default: Copy reads the secret from memory while the page holds only bullets.
    await expect(page.locator('[data-testid="webhook-secret-value"]')).toHaveText(/^•+$/);
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.locator('[data-testid="webhook-secret-copy"]').click();
    const copied = (await page.evaluate(() => navigator.clipboard.readText())).trim();
    expect(copied.length).toBeGreaterThanOrEqual(32);
    expect(await page.content()).not.toContain(copied);
    await page.locator('[data-testid="webhook-secret-reveal"]').click();
    const secret = ((await page.locator('[data-testid="webhook-secret-value"]').textContent()) || '').trim();
    expect(secret).toBe(copied);
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
    await expect(page.locator('[data-testid="webhook-test-result"]')).toContainText('Delivered');
    await expect(page.getByText('Duplicate Flag: false')).toBeVisible();

    // 11. Replay exact duplicate event -> enqueues once, shows DUPLICATE flag
    // Replay, bad-signature and expired-window tests live under the "More tests" disclosure.
    await page.getByTestId('webhook-more-tests').getByText(/More tests/).click();
    const replayBtn = page.locator('[data-testid="btn-test-webhook-replay"]');
    await replayBtn.click();
    await expect(page.locator('[data-testid="webhook-test-result"]')).toContainText('Duplicate (accepted once)', { timeout: 10000 });
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
    await page.getByRole('button', { name: 'More', exact: true }).click();
    await page.locator('[data-testid="workflow-open-triggers"]').click();
    await expect(page.locator('[data-testid="triggers-workflow-filter"]')).toContainText(targetWorkflowId);
    await page.locator('[data-testid="btn-open-create-schedule"]').click();
    await expect(page.locator('[data-testid="select-new-schedule-wfid"]')).toHaveValue(targetWorkflowId);
  });

  test('phone: evaluations and triggers fit the screen in every tab', async ({ page }) => {
    const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
      headers: cluster.apiHeaders,
      data: { spec: { name: 'Phone trigger target', entry_node: 'lookup', nodes: { lookup: { id: 'lookup', handler: 'synthetic_order_lookup' } }, terminal_nodes: ['lookup'] } },
    });
    expect(created.ok()).toBeTruthy();
    const workflowId: string = (await created.json()).workflow_id;
    const sched = await page.request.post(`${cluster.backendUrl}/api/v1/triggers/schedules`, {
      headers: cluster.apiHeaders,
      data: { schedule_id: 'sched_phone_check', workflow_id: workflowId, interval_seconds: 3600, timezone: 'Asia/Kolkata' },
    });
    expect(sched.ok()).toBeTruthy();
    await page.setViewportSize({ width: 390, height: 844 });

    await page.goto(`${cluster.frontendUrl}/#/evaluations`);
    await expect(page.getByRole('heading', { level: 1, name: 'Evaluations' })).toBeVisible();
    const dataset = page.locator('[data-testid="dataset-item-benchmark_customer_support"]');
    await expect(dataset).toBeVisible({ timeout: 10000 });
    await expectNoPageOverflow(page);
    await dataset.click();
    await expect(page.getByRole('heading', { level: 2, name: 'benchmark_customer_support' })).toBeFocused();
    await expectNoPageOverflow(page);
    for (const tab of ['Benchmark', 'Studio tests']) {
      await page.getByRole('tab', { name: tab, exact: true }).click();
      await expectNoPageOverflow(page);
    }
    await page.getByRole('tab', { name: 'Benchmark', exact: true }).click();
    await page.getByRole('button', { name: 'Run benchmark', exact: true }).click();
    await expect(page.locator('[data-testid="eval-results-container"]')).toBeVisible({ timeout: 10000 });
    await expectNoPageOverflow(page);

    await page.goto(`${cluster.frontendUrl}/#/triggers`);
    await expect(page.getByRole('heading', { level: 1, name: 'Triggers' })).toBeVisible();
    await expect(page.locator('[data-testid="schedule-item-sched_phone_check"]')).toBeVisible({ timeout: 10000 });
    // Phones stack each schedule (review M4): status and actions stay on screen.
    const scheduleRow = page.locator('[data-testid="schedule-item-sched_phone_check"]');
    await expectInPhoneView(page.locator('[data-testid="schedule-status-sched_phone_check"]'));
    await expect(page.locator('[data-testid="schedule-status-sched_phone_check"]')).toContainText('Active');
    await expectInPhoneView(scheduleRow.getByTestId('btn-pause-schedule-sched_phone_check'));
    await expectInPhoneView(scheduleRow.getByTestId('btn-delete-schedule-sched_phone_check'));
    await expectNoPageOverflow(page);
    for (const tab of [/^Webhooks/, /^Outbox/]) {
      await page.getByRole('tab', { name: tab }).click();
      await expectNoPageOverflow(page);
    }
    await page.getByRole('button', { name: 'Simulate message', exact: true }).click();
    await expect(page.locator('[data-testid="dispatch-result-box"]')).toBeVisible({ timeout: 10000 });
    await expectNoPageOverflow(page);
  });

  test('token mode delivery tokens are shown once, masked, copyable and never stored (mocked token responses)', async ({ page }) => {
    // The test service runs in local mode, which issues no delivery tokens. The registration
    // response is the real one with a delivery_token added, and the rotation route is answered
    // locally; everything else (the webhook, its listing and deletion) is the real service.
    const ISSUED = 'dlv_e2e_issued_token_sentinel_0123456789abcdef';
    const ROTATED = 'dlv_e2e_rotated_token_sentinel_fedcba9876543210';
    const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
      headers: cluster.apiHeaders,
      data: { spec: { name: 'Token mode target', entry_node: 'lookup', nodes: { lookup: { id: 'lookup', handler: 'synthetic_order_lookup' } }, terminal_nodes: ['lookup'] } },
    });
    expect(created.ok()).toBeTruthy();
    await page.route('**/api/v1/triggers/webhooks', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      const real = await route.fetch();
      const body = await real.json();
      await route.fulfill({ response: real, json: { ...body, delivery_token: ISSUED } });
    });
    let rotations = 0;
    await page.route('**/api/v1/triggers/webhooks/*/delivery-credential', async (route) => {
      rotations += 1;
      await route.fulfill({ json: { endpoint_id: 'token_ingress', delivery_token: ROTATED, delivery_token_header: 'Authorization: Bearer <delivery_token>' } });
    });
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto(`${cluster.frontendUrl}/#/triggers`);
    await page.getByRole('tab', { name: /^Webhooks/ }).click();
    await page.getByRole('button', { name: 'New webhook', exact: true }).click();
    await page.getByTestId('input-new-webhook-id').fill('token_ingress');
    await page.getByTestId('select-new-webhook-wfid').selectOption((await created.json()).workflow_id);
    await page.getByTestId('btn-submit-register-webhook').click();

    const panel = page.getByTestId('webhook-secret-panel');
    await expect(panel).toContainText('This delivery token is shown once');
    const value = panel.getByTestId('webhook-secret-delivery-token-value');
    await expect(value).not.toContainText(ISSUED);
    expect(await page.content()).not.toContain(ISSUED);
    await panel.getByTestId('webhook-secret-delivery-token-copy').click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(ISSUED);
    await panel.getByTestId('webhook-secret-delivery-token-reveal').click();
    await expect(value).toHaveText(ISSUED);
    const storage = () => page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage));
    expect(await storage()).not.toContain(ISSUED);
    expect(page.url()).not.toContain(ISSUED);
    await panel.getByRole('button', { name: 'I have saved the secret and token, dismiss' }).click();
    await expect(panel).toHaveCount(0);
    expect(await page.content()).not.toContain(ISSUED);

    // Rotation needs a confirmation, then shows the new token once.
    const rotate = page.getByTestId('btn-rotate-delivery-token-token_ingress');
    await rotate.click();
    await expect(page.getByText('A new delivery token is issued and the current one stops working immediately.')).toBeVisible();
    expect(rotations).toBe(0);
    await page.getByTestId('btn-rotate-delivery-token-token_ingress-confirm').click();
    const rotated = page.getByTestId('delivery-token-panel');
    await expect(rotated).toBeVisible();
    expect(rotations).toBe(1);
    await expect(rotated.getByTestId('rotated-delivery-token-value')).not.toContainText(ROTATED);
    expect(await page.content()).not.toContain(ROTATED);
    await expectNoPageOverflow(page);
    await rotated.getByRole('button', { name: 'I have saved the token, dismiss' }).click();
    await expect(rotated).toHaveCount(0);
    expect(await page.content()).not.toContain(ROTATED);
    expect(await storage()).not.toContain(ROTATED);
  });
});
