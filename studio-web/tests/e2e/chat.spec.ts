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
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    const playgroundTab = page.getByRole('button', { name: /Playground/i });
    await playgroundTab.click();
    await expect(page).toHaveURL(/#\/playground/);

    const playground = page.locator('[data-testid="chat-playground"]');
    await expect(playground).toBeVisible();

    // 2. Verify visibly labelled local deterministic fake model
    await expect(page.getByRole('heading', { level: 1, name: 'Playground' })).toBeVisible();
    const modelBadge = page.locator('[data-testid="model-badge"]');
    await expect(modelBadge).toHaveText('Offline fake model');
    await expect(page.getByLabel('Chat model')).toHaveValue('fake-deterministic');

    // 3. English turn with typed math calculation tool execution
    const inputArea = page.getByLabel('Message', { exact: true });
    await inputArea.fill('calculate 25 * 4');

    const sendBtn = page.getByRole('button', { name: 'Send', exact: true });
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

    // Click to view trace payload (rendered only once expanded)
    const traceToggle = toolTrace.getByRole('button', { name: /View payload/i });
    await expect(traceToggle).toHaveAttribute('aria-expanded', 'false');
    await toolTrace.click();
    await expect(toolTrace.getByRole('button', { name: /Hide/i })).toHaveAttribute('aria-expanded', 'true');
    await expect(toolTrace).toContainText('100');

    // Verify token usage metrics bar
    const metricsBar = page.locator('[data-testid="metrics-bar"]');
    await expect(metricsBar).toBeVisible();
    await expect(metricsBar).toContainText(/Tokens:/i);
    await expect(metricsBar).toContainText(/Latency:/i);

    // 4. Bilingual Hindi turn
    const hiBtn = page.getByRole('group', { name: 'Reply language' }).getByRole('button', { name: 'हिन्दी' });
    await hiBtn.click();
    await expect(hiBtn).toHaveAttribute('aria-pressed', 'true');

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
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

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

  test('phone layout keeps the transcript readable and the composer reachable (390x844)', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${cluster.frontendUrl}/#/playground`);
    await expect(page.getByRole('heading', { level: 1, name: 'Playground' })).toBeVisible();
    await expect(page.getByTestId('model-badge')).toHaveText('Offline fake model');

    // The conversation gets most of the screen; the strip stays compact.
    const transcript = page.getByTestId('chat-transcript');
    const transcriptBox = await transcript.boundingBox();
    expect(transcriptBox!.height).toBeGreaterThanOrEqual(844 * 0.4);
    const message = page.getByLabel('Message', { exact: true });
    await expect(message).toBeInViewport();

    // Agent, model and reply language are in Settings on phones.
    const settings = page.locator('summary', { hasText: 'Settings' });
    await settings.click();
    await expect(page.getByLabel('Chat model')).toBeVisible();
    await expect(page.getByLabel('Chat model')).toHaveValue('fake-deterministic');
    await expect(page.getByRole('group', { name: 'Reply language' })).toBeVisible();
    await settings.click();

    await message.fill('calculate 6 * 7');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(transcript).toContainText('42', { timeout: 15000 });
    await expect(message).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
});
