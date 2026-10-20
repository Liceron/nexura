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

test.describe('Credential References, Ownership and Role UX (F11)', () => {
  test('manages local-owner credential references with zero secret leaks, verifies connection, and revokes keys', async ({
    page,
  }) => {
    const SECRET_SENTINEL = 'sk_live_test_sentinel_xyz987654321';

    // 1. Navigate to Studio and open Credentials tab
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    const credsNavBtn = page.getByRole('button', { name: /Credentials/i });
    await credsNavBtn.click();
    await expect(page).toHaveURL(/#\/credentials/);

    const credentialsView = page.locator('[data-testid="credentials-view"]');
    await expect(credentialsView).toBeVisible();

    // 2. Verify zero-secret storage guarantee banner and default admin role
    await expect(page.locator('[data-testid="current-role-badge"]')).toContainText(/Admin/i);
    await expect(page.getByText('Zero-Secret Vault Architecture')).toBeVisible();

    // 3. Register credential reference
    const registerBtn = page.locator('[data-testid="register-credential-button"]');
    await registerBtn.click();

    const form = page.locator('[data-testid="register-credential-form"]');
    await expect(form).toBeVisible();

    await page.locator('[data-testid="cred-id-input"]').fill('openai_prod_key');
    await page.locator('[data-testid="cred-desc-input"]').fill('Production OpenAI key for LLM agents');
    await page.locator('[data-testid="cred-secret-input"]').fill(SECRET_SENTINEL);
    await page.locator('[data-testid="cred-env-input"]').fill('OPENAI_API_KEY');

    const submitBtn = page.locator('[data-testid="submit-credential-button"]');
    await submitBtn.click();

    // Wait for modal to close and row to appear
    await expect(form).not.toBeVisible({ timeout: 10000 });
    const credRow = page.locator('[data-testid="cred-row-openai_prod_key"]');
    await expect(credRow).toBeVisible();

    // 4. CRITICAL NEGATIVE TEST: Assert secret sentinel is completely absent from browser storage and DOM
    const localStorageDump = await page.evaluate(() => JSON.stringify(window.localStorage));
    expect(localStorageDump).not.toContain(SECRET_SENTINEL);

    const sessionStorageDump = await page.evaluate(() => JSON.stringify(window.sessionStorage));
    expect(sessionStorageDump).not.toContain(SECRET_SENTINEL);

    const pageContent = await page.content();
    expect(pageContent).not.toContain(SECRET_SENTINEL);

    // 5. Test Connection Verification
    const verifyBtn = page.locator('[data-testid="verify-cred-openai_prod_key"]');
    await verifyBtn.click();

    const verifyBadge = page.locator('[data-testid="verification-badge-openai_prod_key"]');
    await expect(verifyBadge).toBeVisible({ timeout: 10000 });
    await expect(verifyBadge).toContainText('VALID');

    // 6. Test Key Revocation (fails closed)
    const revokeBtn = page.locator('[data-testid="revoke-cred-openai_prod_key"]');
    await revokeBtn.click();
    await page.getByRole('button', { name: 'Confirm', exact: true }).click();

    const statusBadge = page.locator('[data-testid="status-openai_prod_key"]');
    await expect(statusBadge).toContainText('REVOKED', { timeout: 10000 });

    // Subsequent verify must reflect revoked status
    await verifyBtn.click();
    await expect(verifyBadge).toContainText('REVOKED', { timeout: 10000 });

    // 7. OSS has a single local owner (admin), without a members UI.
    await expect(page.locator('[data-testid="current-role-badge"]')).toContainText('Admin');
    await expect(credentialsView).toContainText('Local Owner');
    await expect(page.locator('[data-testid="tab-members"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="new-member-id-input"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="add-member-button"]')).toHaveCount(0);

    // 8. Delete Credential
    const credsTab = page.locator('[data-testid="tab-credentials"]');
    await credsTab.click();

    const deleteBtn = page.locator('[data-testid="delete-cred-openai_prod_key"]');
    await deleteBtn.click();
    await page.getByRole('button', { name: 'Confirm', exact: true }).click();

    await expect(credRow).not.toBeVisible({ timeout: 10000 });
  });
});
