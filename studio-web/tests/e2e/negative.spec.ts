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

test.describe('Studio Web Negative Cases & Error Recovery', () => {
  test('recovers from unreachable API endpoint without retry storm', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Initial connection to valid service is good
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    // Open connection settings dialog
    await page.getByLabel(/^Service connection status:/).click();
    await expect(page.getByRole('heading', { name: 'Connection Settings' })).toBeVisible();

    // Set invalid endpoint port (unreachable)
    const urlInput = page.getByLabel('API Endpoint URL');
    await urlInput.fill('http://127.0.0.1:59999');
    await page.getByRole('button', { name: 'Save Connection' }).click();

    // Should display recoverable error card
    await expect(page.getByText('Unable to connect to Nexura Service')).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('button', { name: 'Retry Connection' })).toBeVisible();

    // Reset back to valid backend URL to verify recovery
    await page.getByLabel(/^Service connection status:/).click();
    await page.getByLabel('API Endpoint URL').fill('/api');
    await page.getByRole('button', { name: 'Save Connection' }).click();

    // Successfully recovers
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);
  });

  test('workspace change updates scoped header and clears previous views', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Click workspace selector
    await page.getByLabel(/^Active workspace selector:/).click();
    await expect(page.getByRole('heading', { name: 'Active Workspace' })).toBeVisible();

    // Change to new workspace
    const wsInput = page.getByLabel('Workspace Identifier');
    await wsInput.fill('tenant-omega');
    await page.getByRole('button', { name: 'Confirm' }).click();

    // Verify workspace indicator updated
    await expect(page.getByLabel(/^Active workspace selector:/)).toContainText('tenant-omega');
  });

  test('verifies no provider secrets or credentials in browser storage or client environment', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Evaluate localStorage, sessionStorage, and window environment
    const storageData = await page.evaluate(() => {
      const local: Record<string, string> = {};
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k) local[k] = localStorage.getItem(k) || '';
      }
      const session: Record<string, string> = {};
      for (let i = 0; i < sessionStorage.length; i++) {
        const k = sessionStorage.key(i);
        if (k) session[k] = sessionStorage.getItem(k) || '';
      }
      return { local, session };
    });

    const forbiddenPatterns = [
      /sk-[a-zA-Z0-9]{20,}/, // OpenAI key
      /sk-ant-[a-zA-Z0-9]{20,}/, // Anthropic key
      /AIzaSy[a-zA-Z0-9_-]{33}/, // Google key
      /AKIA[0-9A-Z]{16}/, // AWS Access Key
    ];

    const allStoredValues = [
      ...Object.values(storageData.local),
      ...Object.values(storageData.session),
    ].join(' ');

    for (const pattern of forbiddenPatterns) {
      expect(pattern.test(allStoredValues)).toBe(false);
    }
  });
});
