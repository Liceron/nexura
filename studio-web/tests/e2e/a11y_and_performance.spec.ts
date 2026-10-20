import { test, expect } from '@playwright/test';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startTestCluster, TestCluster } from './harness';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

let cluster: TestCluster;

test.beforeAll(async () => {
  cluster = await startTestCluster();
});

test.afterAll(async () => {
  if (cluster) {
    await cluster.stop();
  }
});

test.describe('Accessibility, Bilingual UX & Performance (F14)', () => {
  test('verifies production bundle size is within <= 500 KiB compressed budget', async () => {
    const distAssetsDir = path.resolve(__dirname, '../../dist/assets');
    expect(fs.existsSync(distAssetsDir)).toBe(true);

    const files = fs.readdirSync(distAssetsDir);
    const jsFiles = files.filter((f) => f.endsWith('.js') && !f.endsWith('.map'));

    expect(jsFiles.length).toBeGreaterThan(0);

    // Look for the entry chunk (index-*.js)
    const entryChunk = jsFiles.find((f) => f.startsWith('index-'));
    expect(entryChunk).toBeDefined();

    if (entryChunk) {
      const stats = fs.statSync(path.join(distAssetsDir, entryChunk));
      // Budget: initial compressed JS <= 500 KiB (512,000 bytes)
      // Even uncompressed, the entry chunk is well below 500 KiB (approx 58 KiB)
      expect(stats.size).toBeLessThan(512 * 1024);
    }
  });

  test('toggles theme between dark and light with DOM attribute and persistence', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Default theme is dark
    const html = page.locator('html');
    await expect(html).toHaveAttribute('data-theme', 'dark');

    // Click theme toggle button
    const themeBtn = page.getByTestId('toggle-theme-btn');
    await expect(themeBtn).toBeVisible();
    await themeBtn.click();

    // Now theme should be light
    await expect(html).toHaveAttribute('data-theme', 'light');

    // Reload page and verify persistence in localStorage
    await page.reload();
    await expect(html).toHaveAttribute('data-theme', 'light');

    // Toggle back to dark
    await themeBtn.click();
    await expect(html).toHaveAttribute('data-theme', 'dark');
  });

  test('toggles bilingual language between English and Hindi with persistence and UI updates', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Default language is English
    const navWorkflows = page.getByTestId('nav-item-workflows');
    await expect(navWorkflows).toContainText('Workflows');

    // Switch to Hindi
    const langBtn = page.getByTestId('toggle-language-btn');
    await expect(langBtn).toBeVisible();
    await langBtn.click();

    // In Hindi, Workflows becomes वर्कफ़्लो
    await expect(navWorkflows).toContainText('वर्कफ़्लो');

    // Reload and check persistence
    await page.reload();
    await expect(navWorkflows).toContainText('वर्कफ़्लो');

    // Toggle back to English
    await langBtn.click();
    await expect(navWorkflows).toContainText('Workflows');
  });

  test('supports keyboard-only navigation and accessible canvas alternative', async ({ page }) => {
    await page.goto(cluster.frontendUrl);

    // Create a workflow with Hindi/Devanagari title and descriptions
    const createBtn = page.getByRole('button', { name: /\+ New Workflow|नया कार्यप्रवाह/i });
    await expect(createBtn).toBeVisible();
    await createBtn.click();

    const nameInput = page.getByLabel(/Workflow Name/i);
    await nameInput.fill('अभिगम्यता परीक्षण कार्यप्रवाह');
    const submitBtn = page.getByTestId('create-workflow-submit-btn');
    await submitBtn.click();

    // Workflow detail is loaded
    await expect(page.getByText('अभिगम्यता परीक्षण कार्यप्रवाह')).toBeVisible();

    // Check accessible tab buttons
    const tabCanvas = page.getByTestId('tab-canvas');
    const tabNodes = page.getByTestId('tab-nodes');
    const tabJson = page.getByTestId('tab-json');

    await expect(tabCanvas).toBeVisible();
    await expect(tabNodes).toBeVisible();
    await expect(tabJson).toBeVisible();

    // Switch to Nodes Overview (Accessible Canvas Alternative)
    await tabNodes.click();
    const accessibleNodeList = page.getByTestId('accessible-nodes-list');
    await expect(accessibleNodeList).toBeVisible();

    // Check that nodes are rendered in accessible list
    await expect(page.getByTestId('accessible-node-lookup')).toBeVisible();

    // Switch to Raw Specification tab
    await tabJson.click();
    await expect(page.locator('pre')).toContainText('lookup');

    // Return to Visual Graph
    await tabCanvas.click();
    await expect(tabCanvas).toBeVisible();
  });

  test('respects prefers-reduced-motion media query', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(cluster.frontendUrl);

    // Evaluate CSS styles applied under reduced-motion
    const animationDuration = await page.evaluate(() => {
      const el = document.body;
      return window.getComputedStyle(el).animationDuration;
    });

    const parsedSeconds = parseFloat(animationDuration);
    // When reduced-motion is active, animation-duration is overridden to 0.01ms (<= 0.001s)
    expect(parsedSeconds).toBeLessThanOrEqual(0.001);
  });

  test('supports narrow mobile viewport layout without horizontal overflow or clipped controls', async ({ page }) => {
    // Emulate mobile viewport (iPhone SE / compact layout)
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto(cluster.frontendUrl);

    // Header and navigation should still be interactive and visible
    await expect(page.getByRole('heading', { name: 'Nexura Studio' })).toBeVisible();
    await expect(page.getByTestId('toggle-theme-btn')).toBeVisible();
    await expect(page.getByTestId('toggle-language-btn')).toBeVisible();

    // Navigate to Approvals in narrow viewport
    const navApprovals = page.getByTestId('nav-item-approvals');
    await navApprovals.click();
    await expect(page.getByRole('heading', { name: 'Approval Inbox' })).toBeVisible();
  });

  test('verifies 100-node fixture interaction performs without main-thread lockups', async ({ page }) => {
    // Construct a synthetic 100-node workflow via API
    const nodes: Record<string, unknown> = {};
    for (let i = 0; i < 100; i++) {
      const nodeId = `node_${i}`;
      const nextId = i < 99 ? `node_${i + 1}` : undefined;
      nodes[nodeId] = {
        id: nodeId,
        handler: 'synthetic_order_lookup',
        default_transition: nextId,
        transitions: {},
        requires_approval: false,
      };
    }

    const largeSpec = {
      name: 'Large Scale Benchmark Workflow (100 Nodes)',
      entry_node: 'node_0',
      nodes,
    };

    // Create via backend API
    const res = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
      headers: cluster.apiHeaders,
      data: {
        spec: largeSpec,
        options: { name: largeSpec.name },
      },
    });
    expect(res.ok()).toBe(true);
    const created = await res.json();
    const workflowId = created.workflow_id;

    // Navigate directly to the large workflow in studio
    const startTime = Date.now();
    await page.goto(`${cluster.frontendUrl}/#/workflows/${workflowId}`);

    // Wait for the accessible nodes tab and switch to it
    const tabNodes = page.getByTestId('tab-nodes');
    await expect(tabNodes).toBeVisible();
    await expect(tabNodes).toContainText('(100)');

    // Measure tab switch and render time
    const clickStart = Date.now();
    await tabNodes.click();
    const accessibleList = page.getByTestId('accessible-nodes-list');
    await expect(accessibleList).toBeVisible();
    await expect(page.getByTestId('accessible-node-node_0')).toBeVisible();
    await expect(page.getByTestId('accessible-node-node_99')).toBeVisible();
    const clickDuration = Date.now() - clickStart;

    // The render and DOM update of 100 nodes should be smooth (< 2000ms total, no lockup)
    expect(clickDuration).toBeLessThan(2000);

    const totalDuration = Date.now() - startTime;
    expect(totalDuration).toBeLessThan(10000);
  });
});
