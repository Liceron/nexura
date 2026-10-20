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

test.describe('Conversation Playground and Agent Tracing (F09)', () => {
  test('converses in English and Hindi, executes typed local tools, tracks traces and metrics, and reloads persisted transcript', async ({
    page,
  }) => {
    // 1. Open studio and navigate to Playground
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    const playgroundTab = page.getByRole('button', { name: /Playground/i });
    await playgroundTab.click();
    await expect(page).toHaveURL(/#\/playground/);

    const playground = page.locator('[data-testid="chat-playground"]');
    await expect(playground).toBeVisible();

    // 2. Verify visibly labelled local deterministic fake model
    const modelBadge = page.locator('[data-testid="model-badge"]');
    await expect(modelBadge).toContainText(/fake-deterministic \(Local Offline Fixture\)/i);

    // 3. English turn with typed math calculation tool execution
    const inputArea = page.locator('[data-testid="chat-input-textarea"]');
    await inputArea.fill('calculate 25 * 4');

    const sendBtn = page.locator('[data-testid="send-message-button"]');
    await sendBtn.click();

    // Verify user message appears in transcript
    const transcript = page.locator('[data-testid="chat-transcript"]');
    await expect(transcript).toContainText('calculate 25 * 4');

    // Verify assistant reply contains calculation result (100)
    await expect(transcript).toContainText('100', { timeout: 15000 });

    // Verify tool execution trace is rendered
    const toolTrace = page.locator('[data-testid="tool-trace-calculate"]');
    await expect(toolTrace).toBeVisible();
    await expect(toolTrace).toContainText('calculate');

    // Click to view trace payload
    await toolTrace.click();
    await expect(toolTrace).toContainText('100');

    // Verify token usage metrics bar
    const metricsBar = page.locator('[data-testid="metrics-bar"]');
    await expect(metricsBar).toBeVisible();
    await expect(metricsBar).toContainText(/Tokens:/i);
    await expect(metricsBar).toContainText(/Latency:/i);

    // 4. Bilingual Hindi turn
    const hiBtn = page.locator('[data-testid="lang-hi-btn"]');
    await hiBtn.click();

    await inputArea.fill('नमस्ते');
    await sendBtn.click();

    // Verify Hindi assistant greeting in transcript
    await expect(transcript).toContainText('नमस्ते! मैं नेक्सुरा असिस्टेंट हूँ', { timeout: 15000 });

    // 5. Hindi order tracking tool turn
    await inputArea.fill('मेरे ऑर्डर ORD-DELHI-501 की स्थिति क्या है?');
    await sendBtn.click();

    // Verify order tracking tool trace appears
    const orderTrace = page.locator('[data-testid="tool-trace-get_order_status"]');
    await expect(orderTrace).toBeVisible({ timeout: 15000 });
    await expect(transcript).toContainText('पारगमन में (In Transit)');

    // 6. Test session reload persistence
    await page.reload();
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    await playgroundTab.click();
    await expect(playground).toBeVisible();

    // The session should be listed in the conversation sidebar
    const sessionItems = page.locator('[data-testid^="session-item-"]');
    await expect(sessionItems.first()).toBeVisible({ timeout: 10000 });

    // Click on the session to reload the transcript
    await sessionItems.first().click();

    // Verify persisted messages and history are restored from SQLite store
    await expect(transcript).toContainText('calculate 25 * 4');
    await expect(transcript).toContainText('100');
    await expect(transcript).toContainText('नमस्ते! मैं नेक्सुरा असिस्टेंट हूँ');
  });
});
