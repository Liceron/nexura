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
    await expect(page.getByRole('heading', { name: 'Nexura Studio' })).toBeVisible();

    // Active workspace indicator
    await expect(page.getByLabel(/^Active workspace selector:/)).toContainText('default');

    // Service connection status badge showing local-owner profile
    const connectionStatus = page.getByLabel(/^Service connection status:/);
    await expect(connectionStatus).toBeVisible();
    await expect(connectionStatus).toContainText(/local-owner/i);
  });

  test('inspects real service capabilities from backend', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Navigate to System Capabilities tab
    await page.getByRole('button', { name: /System Capabilities/i }).click();

    // Verify system capabilities rendered from real API
    await expect(page.getByRole('heading', { name: 'Service Capabilities' })).toBeVisible();
    await expect(page.getByText('0.4.0')).toBeVisible();
    await expect(page.getByText('Module Descriptors & Discovery')).toBeVisible();
    await expect(page.getByText('Authoritative Graph Validation')).toBeVisible();
  });

  test('executes keyless bilingual workflow on local backend without paid API keys', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Open Local Quickstart tab
    await page.getByRole('button', { name: /Local Quickstart/i }).click();

    // Verify keyless onboarding card
    await expect(page.getByRole('heading', { name: /Local Keyless Example/i })).toBeVisible();

    // Click run workflow
    const runBtn = page.getByRole('button', { name: /Run Keyless Workflow/i });
    await expect(runBtn).toBeVisible();
    await runBtn.click();

    // Wait for execution completion from real backend
    await expect(page.getByText(/Execution completed successfully!/i)).toBeVisible({ timeout: 15000 });

    // Inspect real returned payload with Devanagari text
    const resultJson = page.locator('pre');
    await expect(resultJson).toBeVisible();
    const resultText = await resultJson.innerText();
    expect(resultText).toContain('अमित वर्मा (Amit Verma)');
    expect(resultText).toContain('ORD-DELHI-501');
    expect(resultText).toContain('TICKET-');
  });
});
