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

test.describe('MCP & OpenAPI Tools and Runnable Export Integration (F12)', () => {
  test('discovers local fixtures, executes safe dry-runs, and registers tools', async ({
    page,
  }) => {
    // 1. Open Studio and navigate to Tools & Integrations
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    const toolsNavBtn = page.locator('[data-testid="nav-item-tools"]');
    await toolsNavBtn.click();

    const integrationsView = page.locator('[data-testid="integrations-view"]');
    await expect(integrationsView).toBeVisible();

    // 2. Verify MCP tab is active and status indicates active adapters
    const mcpTab = page.locator('[data-testid="tab-mcp"]');
    await expect(mcpTab).toContainText(/Active/i);

    // 3. Discover MCP tools from default math_fixture
    const discoverMcpBtn = page.getByRole('button', { name: /Discover Tools/i });
    await expect(discoverMcpBtn).toBeVisible();
    await discoverMcpBtn.click();

    // Wait for discovered tools to render
    await expect(page.getByText(/Discovered MCP Tools/i)).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('math_add', { exact: true })).toBeVisible();
    await expect(page.getByText('math_multiply', { exact: true })).toBeVisible();

    // 4. Execute Dry-Run for the selected tool
    const dryRunBtn = page.getByRole('button', { name: /Execute Dry-Run/i });
    await expect(dryRunBtn).toBeVisible();
    await dryRunBtn.click();

    // Dry-run results must explicitly show mock transport label and SUCCESS badge
    await expect(page.getByText('Fixture Dry-Run (Local Mock Transport)', { exact: true })).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('SUCCESS', { exact: true })).toBeVisible();

    // 5. Register the tool into the workspace palette
    const addToPaletteMcpBtn = page.getByRole('button', { name: /Add to Palette/i }).first();
    await expect(addToPaletteMcpBtn).toBeVisible();
    await addToPaletteMcpBtn.click();
    await expect(page.getByText(/In Palette/i)).toBeVisible({ timeout: 10000 });

    // 6. Switch to OpenAPI tab
    const openapiTab = page.locator('[data-testid="tab-openapi"]');
    await openapiTab.click();
    await expect(openapiTab).toContainText(/Active/i);

    // 7. Discover endpoints from petstore fixture
    const discoverOpenApiBtn = page.getByRole('button', { name: /Discover Endpoints/i });
    await expect(discoverOpenApiBtn).toBeVisible();
    await discoverOpenApiBtn.click();

    await expect(page.getByText(/Discovered Endpoints/i)).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('getPetById', { exact: true })).toBeVisible();

    // 8. Execute dry-run on OpenAPI endpoint
    const executeOpenApiBtn = page.getByRole('button', { name: /Execute Dry-Run/i });
    await expect(executeOpenApiBtn).toBeVisible();
    await executeOpenApiBtn.click();

    await expect(page.getByText('Fixture Dry-Run (Local Mock Transport)', { exact: true })).toBeVisible({ timeout: 10000 });

    // 9. Register OpenAPI tool
    const addToPaletteOpenApiBtn = page.getByRole('button', { name: /Add to Palette/i }).first();
    await expect(addToPaletteOpenApiBtn).toBeVisible();
    await addToPaletteOpenApiBtn.click();
    await expect(page.getByText(/In Palette/i).first()).toBeVisible({ timeout: 10000 });
  });
});
