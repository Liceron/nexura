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

test.describe('Studio Web Smoke & Local Service Integration', () => {
  test('verifies service health, active profile and workspace indicator', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Header branding
    await expect(page.getByRole('link', { name: 'Nexura Studio' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Workflows' })).toBeVisible();

    // Active workspace indicator
    await expect(page.getByLabel(/^Active workspace selector:/)).toContainText('default');

    // Quiet connection status; profile remains in its accessible description
    const connectionStatus = page.getByLabel(/^Service connection status:/);
    await expect(connectionStatus).toBeVisible();
    await expect(connectionStatus).toHaveText('Connected');
    await expect(connectionStatus).toHaveAccessibleDescription(/local-owner/i);
  });

  test('inspects real service capabilities from backend', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Navigate to System Capabilities tab
    await page.getByRole('button', { name: 'System', exact: true }).click();

    // Verify system capabilities rendered from real API
    await expect(page.getByRole('heading', { level: 1, name: 'System' })).toBeVisible();
    await expect(page.getByText('0.4.0')).toBeVisible();
    const capabilities = page.getByRole('table', { name: 'Capabilities' });
    await expect(capabilities.getByRole('row', { name: /Module discovery/ })).toContainText('Supported');
    await expect(capabilities.getByRole('row', { name: /Graph validation/ })).toContainText('Supported');
    // States come from the service's capability flags only.
    await expect(capabilities.getByRole('row', { name: /Live run events/ })).toContainText('Supported');
  });

  test('executes keyless bilingual workflow on local backend without paid API keys', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Open Local Quickstart tab
    await page.getByRole('button', { name: 'Quickstart', exact: true }).click();

    // Verify the local example page and its no-key statement
    await expect(page.getByRole('heading', { level: 1, name: 'Run a local example' })).toBeVisible();
    await expect(page.getByText('No API key needed.')).toBeVisible();

    // Click run workflow
    const runBtn = page.getByRole('button', { name: 'Run example', exact: true });
    await expect(runBtn).toBeVisible();
    await runBtn.click();

    // Wait for execution completion from real backend (the recorded run status, not a success banner)
    await expect(page.getByLabel('Example run status')).toHaveText(/Completed/, { timeout: 15000 });
    await expect(page.getByRole('link', { name: 'Open run', exact: true })).toHaveAttribute('href', /^#\/runs\/.+/);
    await expect(page.getByRole('button', { name: 'Run again', exact: true })).toBeVisible();

    // Inspect real returned payload with Devanagari text
    const resultJson = page.locator('pre');
    await expect(resultJson).toBeVisible();
    const resultText = await resultJson.innerText();
    expect(resultText).toContain('अमित वर्मा (Amit Verma)');
    expect(resultText).toContain('ORD-DELHI-501');
    expect(resultText).toContain('TICKET-');
  });

  test('local example fits a phone screen before and after a run', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${cluster.frontendUrl}/#/onboarding`);
    const fits = async () =>
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(page.getByRole('heading', { level: 1, name: 'Run a local example' })).toBeVisible();
    await fits();
    await page.getByRole('button', { name: 'Run example', exact: true }).click();
    await expect(page.getByLabel('Example run status')).toHaveText(/Completed/, { timeout: 15000 });
    // The run's JSON scrolls inside its own block; the page does not.
    await expect(page.locator('pre')).toContainText('ORD-DELHI-501');
    await fits();
  });

  test('System fits a phone screen; the capabilities table scrolls in its region', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${cluster.frontendUrl}/#/system`);
    await expect(page.getByRole('heading', { level: 1, name: 'System' })).toBeVisible();
    await expect(page.getByRole('table', { name: 'Capabilities' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
});
