import { test, expect } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

/**
 * P10 connectors pack in Studio. Studio shows modules only in the builder's "Module Registry" drawer
 * (features/modules/ModulePalette.tsx, opened by "+ Add Step"). That drawer renders title, module id,
 * version, Available/Missing badge, description, `kind:` and an "Approval Gate" badge driven by the API
 * field requires_approval. Since B22 the API reports requires_approval=true and write_operations for every
 * connector with a write operation (all but the read-only IMAP connector). Since P14 the drawer also
 * lists each operation as "reads" or "writes - needs approval" with the required credential type, and a
 * connector node offers a credential picker filtered to that type.
 */

let cluster: TestCluster;

test.beforeAll(async () => {
  cluster = await startTestCluster();
});

test.afterAll(async () => {
  if (cluster) await cluster.stop();
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript((backendUrl) => {
    localStorage.setItem('nexura_api_url', backendUrl);
  }, cluster.backendUrl);
});

const CONNECTORS: Record<string, string> = {
  'nexura.connectors.airtable': 'Airtable',
  'nexura.connectors.discord': 'Discord (webhook)',
  'nexura.connectors.gdrive': 'Google Drive',
  'nexura.connectors.github': 'GitHub',
  'nexura.connectors.gsheets': 'Google Sheets',
  'nexura.connectors.imap': 'Email (IMAP read)',
  'nexura.connectors.jira': 'Jira',
  'nexura.connectors.mysql': 'MySQL',
  'nexura.connectors.notion': 'Notion',
  'nexura.connectors.postgres': 'Postgres',
  'nexura.connectors.s3': 'S3-compatible storage',
  'nexura.connectors.slack': 'Slack',
  'nexura.connectors.smtp': 'Email (SMTP send)',
  'nexura.connectors.teams': 'Microsoft Teams (incoming webhook)',
  'nexura.connectors.webhook': 'Webhook (outbound)',
};

test.describe('Connectors pack in the module registry (P10)', () => {
  test('lists all 15 connectors, gates every write connector, and leaks no credential value', async ({
    page,
  }) => {
    const SECRET = 'xoxb-connector-secret-sentinel-5551234';

    // Every request and response the page makes; the secret may appear only in the one request that
    // creates the credential.
    const leaks: string[] = [];
    let createRequests = 0;
    page.on('request', (req) => {
      const body = req.postData() ?? '';
      if (!body.includes(SECRET)) return;
      if (req.method() === 'POST' && /\/api\/v1\/credentials$/.test(new URL(req.url()).pathname)) {
        createRequests += 1;
      } else {
        leaks.push(`request ${req.method()} ${req.url()}`);
      }
    });
    page.on('response', async (resp) => {
      try {
        if ((await resp.text()).includes(SECRET)) leaks.push(`response ${resp.status()} ${resp.url()}`);
      } catch {
        // bodies of redirects and aborted requests are unavailable
      }
    });

    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Register a credential holding a secret sentinel; no page may ever render it.
    await page.getByRole('button', { name: /Credentials/i }).click();
    await page.locator('[data-testid="register-credential-button"]').click();
    await page.locator('[data-testid="cred-id-input"]').fill('slack_bot_token');
    await page.locator('[data-testid="cred-desc-input"]').fill('Slack bot token for connector tests');
    await page.locator('[data-testid="cred-secret-input"]').fill(SECRET);
    await page.locator('[data-testid="cred-env-input"]').fill('SLACK_BOT_TOKEN');
    await page.locator('[data-testid="submit-credential-button"]').click();
    await expect(page.locator('[data-testid="cred-row-slack_bot_token"]')).toBeVisible({ timeout: 10000 });
    expect(createRequests).toBe(1);
    // The credentials list (page and API) shows the reference, never the value.
    expect(await page.locator('[data-testid="cred-row-slack_bot_token"]').innerText()).not.toContain(SECRET);
    expect(await page.content()).not.toContain(SECRET);
    const creds = await page.request.get(`${cluster.backendUrl}/api/v1/credentials`);
    expect(creds.status()).toBe(200);
    const credsText = await creds.text();
    expect(credsText).toContain('slack_bot_token');
    expect(credsText).not.toContain(SECRET);
    const cred = await page.request.get(`${cluster.backendUrl}/api/v1/credentials/slack_bot_token`);
    expect(cred.status()).toBe(200);
    expect(await cred.text()).not.toContain(SECRET);

    // Backend contract the UI is built on: 15 connectors, write operations flagged in display.examples.
    const res = await page.request.get(`${cluster.backendUrl}/api/v1/modules`);
    expect(res.status()).toBe(200);
    const apiText = await res.text();
    expect(apiText).not.toContain(SECRET);
    const items = (JSON.parse(apiText).items as Array<Record<string, any>>).filter((m) =>
      String(m.module_id).startsWith('nexura.connectors.'),
    );
    expect(items.map((m) => m.module_id).sort()).toEqual(Object.keys(CONNECTORS).sort());
    const slack = items.find((m) => m.module_id === 'nexura.connectors.slack')!;
    const slackOps = Object.fromEntries(
      (slack.display.examples as Array<{ operation: string; write: boolean }>).map((e) => [e.operation, e.write]),
    );
    expect(slackOps.post_message).toBe(true);
    expect(slackOps.list_channels).toBe(false);

    // Build a workflow and open the module drawer.
    await page.getByRole('button', { name: /Workflows/i }).first().click();
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByLabel('Workflow Name').fill('P10 Connectors Palette');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    await page.getByRole('button', { name: /Add step/i }).click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();

    const palette = page.getByRole('dialog', { name: 'Available modules drawer' });
    await expect(palette).toBeVisible();
    await expect(palette.getByText(/Loading registered modules/i)).toHaveCount(0, { timeout: 15000 });

    // Narrow to connectors through the search box.
    await palette.getByPlaceholder(/Search modules/i).fill('nexura.connectors');
    await expect(palette.getByText('15 available')).toBeVisible();

    for (const [id, title] of Object.entries(CONNECTORS)) {
      const code = palette.locator('code', { hasText: new RegExp(`^${id.replace(/\./g, '\\.')}$`) });
      await expect(code, `${id} listed`).toHaveCount(1);
      const card = code.locator('xpath=ancestor::div[.//text()[contains(., "+ Add Node")]][1]');
      await expect(card.getByText(title, { exact: true }).first()).toBeVisible();
      await expect(card).toContainText('kind: integration');
      await expect(card.getByText('Available', { exact: true })).toBeVisible();
    }

    // The approval badge follows the API flag: every connector with a write operation is gated (B22);
    // IMAP is read-only. Assert the backend contract first, then that each gated card shows the badge
    // and the read-only one does not.
    const flagged = items.filter((m) => m.requires_approval).map((m) => m.module_id).sort();
    const readOnly = ['nexura.connectors.imap'];
    expect(flagged).toEqual(Object.keys(CONNECTORS).filter((id) => !readOnly.includes(id)).sort());
    expect(slack.write_operations).toContain('post_message');
    expect(slack.write_operations).not.toContain('list_channels');
    await expect(palette.getByText('Approval Gate', { exact: true })).toHaveCount(flagged.length);
    for (const id of Object.keys(CONNECTORS)) {
      const code = palette.locator('code', { hasText: new RegExp(`^${id.replace(/\./g, '\\.')}$`) });
      const card = code.locator('xpath=ancestor::div[.//text()[contains(., "+ Add Node")]][1]');
      await expect(card.getByText('Approval Gate', { exact: true }), `${id} badge`).toHaveCount(
        readOnly.includes(id) ? 0 : 1,
      );
    }

    // Slack is findable by title and id fragment through the registry search.
    await palette.getByPlaceholder(/Search modules/i).fill('slack');
    await expect(palette.locator('code', { hasText: 'nexura.connectors.slack' })).toHaveCount(1);

    // Credential hygiene across the page that lists connectors, storage, and all traffic.
    expect(await page.content()).not.toContain(SECRET);
    expect(await page.evaluate(() => JSON.stringify(window.localStorage))).not.toContain(SECRET);
    expect(await page.evaluate(() => JSON.stringify(window.sessionStorage))).not.toContain(SECRET);
    expect(
      await page.evaluate(
        (secret) =>
          Array.from(document.querySelectorAll('input, textarea')).some((el) =>
            (el as HTMLInputElement).value.includes(secret),
          ),
        SECRET,
      ),
    ).toBe(false);
    expect(leaks).toEqual([]);
  });
  test('connector setup: operations marked read or write, credential picker filtered by type, no secret shown (P14)', async ({
    page,
  }) => {
    const SLACK_SECRET = 'xoxb-p14-slack-secret-sentinel-7771234';
    const OTHER_SECRET = 'ghp_p14GithubSecretSentinel0000000000000000';

    const leaks: string[] = [];
    page.on('response', async (resp) => {
      try {
        const text = await resp.text();
        if (text.includes(SLACK_SECRET) || text.includes(OTHER_SECRET)) leaks.push(`${resp.status()} ${resp.url()}`);
      } catch {
        // bodies of redirects and aborted requests are unavailable
      }
    });

    // Register two credentials through the API: one of the Slack connector's type, one of another type.
    const register = async (id: string, type: string, secret: string, description: string) => {
      const res = await page.request.post(`${cluster.backendUrl}/api/v1/credentials`, {
        headers: cluster.apiHeaders,
        data: { credential_id: id, credential_type: type, provider: 'custom', description, secret },
      });
      expect(res.status(), `register ${id}`).toBeLessThan(300);
    };
    const mods = await page.request.get(`${cluster.backendUrl}/api/v1/modules`);
    const slackDesc = (JSON.parse(await mods.text()).items as Array<Record<string, any>>).find(
      (m) => m.module_id === 'nexura.connectors.slack',
    )!;
    expect(slackDesc.required_credential_types).toHaveLength(1);
    const slackType = String(slackDesc.required_credential_types[0]);
    await register('p14_slack_ok', slackType, SLACK_SECRET, 'P14 Slack bot for the picker');
    await register('p14_github_other', 'github_token', OTHER_SECRET, 'P14 GitHub token, wrong type');

    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);
    await page.getByRole('button', { name: /Workflows/i }).first().click();
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByLabel('Workflow Name').fill('P14 Connector Setup');
    await page.getByRole('button', { name: /Validate & Create/i }).click();
    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    await page.getByRole('button', { name: /Add step/i }).click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();

    const palette = page.getByRole('dialog', { name: 'Available modules drawer' });
    await expect(palette.getByText(/Loading registered modules/i)).toHaveCount(0, { timeout: 15000 });
    await palette.getByPlaceholder(/Search modules/i).fill('nexura.connectors.slack');
    await expect(palette.getByTestId('operation-marker-post_message')).toHaveText('writes - needs approval');
    await expect(palette.getByTestId('operation-marker-list_channels')).toHaveText('reads');
    await expect(palette.getByTestId('credential-type')).toContainText(slackType);

    // Add the Slack node; the inspector opens on the Config tab.
    await palette.locator('div').filter({ has: page.locator('code', { hasText: /^nexura\.connectors\.slack$/ }) }).filter({ has: page.getByRole('button', { name: '+ Add Node', exact: true }) }).last().getByRole('button', { name: '+ Add Node', exact: true }).click();

    // credential_ref is a picker: only the Slack-compatible credential, shown as id and description.
    const picker = page.getByTestId('credential-ref-select');
    await expect(picker).toBeVisible();
    await expect(picker.locator('option')).toHaveCount(2); // placeholder + the one slack credential
    await expect(picker.locator('option', { hasText: 'p14_slack_ok' })).toHaveCount(1);
    await expect(picker.locator('option', { hasText: 'p14_github_other' })).toHaveCount(0);
    await expect(page.getByTestId('credential-ref-empty')).toHaveCount(0);
    await picker.selectOption('p14_slack_ok');
    await expect(picker).toHaveValue('p14_slack_ok');

    // The operation field is a select with the read/write marker.
    await page.getByRole('tab', { name: /^Inputs/ }).click();
    const op = page.getByTestId('operation-select');
    await expect(op.locator('option', { hasText: 'post_message (writes - needs approval)' })).toHaveCount(1);
    await expect(op.locator('option', { hasText: 'list_channels (reads)' })).toHaveCount(1);
    await op.selectOption('post_message');
    await expect(op).toHaveValue('post_message');

    // A connector with no credential of its type shows the empty state with a link to Credentials.
    await page.getByRole('button', { name: /Add step/i }).click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();
    await palette.getByPlaceholder(/Search modules/i).fill('nexura.connectors.airtable');
    await palette.locator('div').filter({ has: page.locator('code', { hasText: /^nexura\.connectors\.airtable$/ }) }).filter({ has: page.getByRole('button', { name: '+ Add Node', exact: true }) }).last().getByRole('button', { name: '+ Add Node', exact: true }).click();
    await page.getByRole('tab', { name: /^Config$/ }).click();
    await expect(page.getByTestId('credential-ref-empty')).toContainText(/none of type airtable_token/);
    await expect(page.getByTestId('credential-ref-add-link')).toHaveAttribute('href', '#/credentials');

    // No secret value anywhere: page, inputs, storage, traffic.
    for (const secret of [SLACK_SECRET, OTHER_SECRET]) {
      expect(await page.content()).not.toContain(secret);
      expect(await page.evaluate(() => JSON.stringify(window.localStorage))).not.toContain(secret);
      expect(await page.evaluate(() => JSON.stringify(window.sessionStorage))).not.toContain(secret);
      expect(
        await page.evaluate(
          (s) =>
            Array.from(document.querySelectorAll('input, textarea, select')).some((el) =>
              (el as HTMLInputElement).value.includes(s),
            ),
          secret,
        ),
      ).toBe(false);
    }
    expect(leaks).toEqual([]);
  });
});
