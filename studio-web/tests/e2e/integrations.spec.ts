import { test, expect, type Page } from '@playwright/test';
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

test.describe('MCP & OpenAPI Tools and Runnable Export Integration (F12)', () => {
  test('discovers local fixtures, executes safe dry-runs, and registers tools', async ({
    page,
  }) => {
    // 1. Open Studio and navigate to Tools & Integrations
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    const toolsNavBtn = page.locator('[data-testid="nav-item-tools"]');
    await toolsNavBtn.click();

    const integrationsView = page.locator('[data-testid="integrations-view"]');
    await expect(integrationsView).toBeVisible();

    // 2. Verify MCP tab is active and status indicates active adapters
    const mcpTab = page.locator('[data-testid="tab-mcp"]');
    await expect(mcpTab).toContainText(/Active/i);

    // 3. Discover MCP tools from default math_fixture
    const discoverMcpBtn = page.getByRole('button', { name: 'Connect', exact: true });
    await expect(discoverMcpBtn).toBeVisible();
    await discoverMcpBtn.click();

    // Wait for discovered tools to render
    await expect(page.getByRole('heading', { name: /^Tools \(2\)/ })).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId('mcp-tool-math_add')).toBeVisible();
    await expect(page.getByTestId('mcp-tool-math_multiply')).toBeVisible();
    // Each operation says whether it reads or writes, from the service's effect policy.
    await expect(page.getByTestId('mcp-tool-math_add').locator('[data-effect="idempotent_write"]')).toHaveText('Writes (safe to retry)');

    // 4. Execute Dry-Run for the selected tool
    const dryRunBtn = page.getByRole('button', { name: 'Run test', exact: true });
    await expect(dryRunBtn).toBeVisible();
    // With a tool selected, Run test is the page's only primary action; Connect is secondary.
    await expect(page.locator('[data-testid="integrations-view"] .studio-button--primary')).toHaveCount(1);
    await expect(dryRunBtn).toHaveClass(/studio-button--primary/);
    await expect(discoverMcpBtn).not.toHaveClass(/studio-button--primary/);
    await dryRunBtn.click();

    // Dry-run results must explicitly show the server's mock transport label and a success status
    await expect(page.getByText('Fixture Dry-Run (Local Mock Transport)', { exact: true })).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId('tool-run-status')).toHaveText(/Success/);

    // 5. Register the tool into the workspace palette
    const addToPaletteMcpBtn = page.getByRole('button', { name: 'Add to palette', exact: true });
    await expect(addToPaletteMcpBtn).toBeVisible();
    await addToPaletteMcpBtn.click();
    await expect(page.getByRole('button', { name: 'In palette', exact: true })).toBeDisabled({ timeout: 10000 });
    await expect(page.getByTestId('mcp-tool-math_add')).toContainText('In palette');

    // 6. Switch to OpenAPI tab
    const openapiTab = page.locator('[data-testid="tab-openapi"]');
    await openapiTab.click();
    await expect(openapiTab).toContainText(/Active/i);

    // 7. Discover endpoints from petstore fixture
    const discoverOpenApiBtn = page.getByRole('button', { name: 'Import', exact: true });
    await expect(discoverOpenApiBtn).toBeVisible();
    await discoverOpenApiBtn.click();

    await expect(page.getByRole('heading', { name: /^Operations \(\d+\)/ })).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId('openapi-op-getPetById')).toBeVisible();
    await expect(page.getByTestId('openapi-op-getPetById')).toContainText('GET');

    // 8. Execute dry-run on OpenAPI endpoint
    const executeOpenApiBtn = page.getByRole('button', { name: 'Run test', exact: true });
    await expect(executeOpenApiBtn).toBeVisible();
    await executeOpenApiBtn.click();

    await expect(page.getByText('Fixture Dry-Run (Local Mock Transport)', { exact: true })).toBeVisible({ timeout: 10000 });

    // 9. Register OpenAPI tool
    const addToPaletteOpenApiBtn = page.getByRole('button', { name: 'Add to palette', exact: true });
    await expect(addToPaletteOpenApiBtn).toBeVisible();
    await addToPaletteOpenApiBtn.click();
    await expect(page.getByRole('button', { name: 'In palette', exact: true })).toBeDisabled({ timeout: 10000 });
  });

  test('phone: tool list, detail and result fit the screen; focus follows the opened tool', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${cluster.frontendUrl}/#/tools`);
    await expect(page.getByRole('heading', { level: 1, name: 'Tools' })).toBeVisible();
    await expectNoPageOverflow(page);
    await page.getByRole('button', { name: 'Connect', exact: true }).click();
    const row = page.getByTestId('mcp-tool-math_add');
    await expect(row).toBeVisible({ timeout: 10000 });
    await expectNoPageOverflow(page);
    await row.click();
    await expect(page.getByRole('heading', { level: 2, name: 'math_add' })).toBeFocused();
    await page.getByRole('button', { name: 'Run test', exact: true }).click();
    await expect(page.getByTestId('tool-run-status')).toHaveText(/Success/, { timeout: 10000 });
    await expectNoPageOverflow(page);
    await page.getByRole('button', { name: 'Back to list', exact: true }).click();
    await expect(row).toBeFocused();
  });
});
