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


/** The element's box lies fully inside the 390px phone viewport (not pushed off to the side). */
async function expectInPhoneView(locator: import('@playwright/test').Locator) {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
}

test.describe('Credential References, Ownership and Role UX (F11)', () => {
  test('manages local-owner credential references with zero secret leaks, verifies connection, and revokes keys', async ({
    page,
  }) => {
    const SECRET_SENTINEL = 'sk_live_test_sentinel_xyz987654321';

    // 1. Navigate to Studio and open Credentials tab
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    const credsNavBtn = page.getByRole('button', { name: /Credentials/i });
    await credsNavBtn.click();
    await expect(page).toHaveURL(/#\/credentials/);

    const credentialsView = page.locator('[data-testid="credentials-view"]');
    await expect(credentialsView).toBeVisible();

    // 2. Verify zero-secret storage guarantee banner and default admin role
    await expect(page.locator('[data-testid="current-role-badge"]')).toContainText(/Admin/i);
    await expect(page.getByRole('heading', { level: 1, name: 'Credentials' })).toBeVisible();
    // The storage explanation is a disclosure next to Add credential, not a banner.
    const storageHelp = page.getByTestId('credential-storage-help');
    await storageHelp.getByText('How credentials are stored', { exact: true }).click();
    await expect(storageHelp).toContainText('never stores the secret in browser storage');

    // 3. Register credential reference
    const registerBtn = page.locator('[data-testid="register-credential-button"]');
    await registerBtn.click();

    const form = page.locator('[data-testid="register-credential-form"]');
    await expect(form).toBeVisible();
    await expect(page.getByRole('dialog', { name: 'Add credential' })).toBeVisible();
    // The secret field is labelled, masked and write-only.
    await expect(page.getByLabel('Secret value', { exact: true })).toHaveAttribute('type', 'password');

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
    await expect(verifyBadge).toContainText('Valid');
    await expect(verifyBadge).not.toContainText('Invalid');

    // 6. Test Key Revocation (fails closed)
    const revokeBtn = page.locator('[data-testid="revoke-cred-openai_prod_key"]');
    await revokeBtn.click();
    await page.getByRole('button', { name: 'Confirm', exact: true }).click();

    const statusBadge = page.locator('[data-testid="status-openai_prod_key"]');
    await expect(statusBadge).toContainText('Revoked', { timeout: 10000 });

    // Subsequent verify must reflect revoked status
    await verifyBtn.click();
    await expect(verifyBadge).toContainText('Revoked', { timeout: 10000 });

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

  test('phone: credentials fit the screen, each row keeps type, status and actions in view, and the secret is never shown', async ({ page }) => {
    const SECRET = 'sk_phone_sentinel_5551234567';
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${cluster.frontendUrl}/#/credentials`);
    await expect(page.getByRole('heading', { level: 1, name: 'Credentials' })).toBeVisible();
    await expectNoPageOverflow(page);

    await page.getByRole('button', { name: 'Add credential', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Add credential' });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel('Reference', { exact: true }).fill('phone_key');
    await dialog.getByLabel('Secret value', { exact: true }).fill(SECRET);
    await expectNoPageOverflow(page);
    await dialog.getByRole('button', { name: 'Add credential', exact: true }).click();
    await expect(dialog).toHaveCount(0);

    const row = page.getByTestId('cred-row-phone_key');
    await expect(row).toBeVisible({ timeout: 10000 });
    // Phones stack each credential (review M4): type, status and every action stay on screen.
    await expectInPhoneView(row.getByTestId('cred-id-phone_key'));
    await expectInPhoneView(row.getByRole('cell').filter({ hasText: 'api_key' }));
    await expectInPhoneView(row.getByTestId('status-phone_key'));
    await expect(row.getByTestId('status-phone_key')).toContainText('Active');
    for (const action of ['verify-cred-phone_key', 'revoke-cred-phone_key', 'delete-cred-phone_key']) {
      await expectInPhoneView(row.getByTestId(action));
    }
    await expectNoPageOverflow(page);
    expect(await page.content()).not.toContain(SECRET);
    expect(await page.evaluate(() => JSON.stringify(window.localStorage) + JSON.stringify(window.sessionStorage))).not.toContain(SECRET);

    await page.getByRole('tab', { name: 'Permissions', exact: true }).click();
    const matrix = page.getByRole('table', { name: 'Role permissions' });
    await expect(matrix).toBeVisible();
    // Every role's answer for an action is visible together, so roles can be compared.
    const runRow = matrix.getByRole('row', { name: /Run workflows/ });
    for (const [role, answer] of [['Admin', 'Yes'], ['Editor', 'Yes'], ['Viewer', 'Denied']]) {
      const cell = runRow.locator(`td[data-label="${role}"]`);
      await expectInPhoneView(cell);
      await expect(cell).toHaveText(answer);
    }
    await expectNoPageOverflow(page);
  });
});
