import { test, expect, Page } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

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

const skillMd = (name: string, body: string) =>
  `---\nname: ${name}\ndescription: Notes for ${name}.\n---\n${body}`;

async function createViaUi(page: Page, markdown: string) {
  await page.getByTestId('skills-new').click();
  await page.getByTestId('skill-markdown').fill(markdown);
  await page.getByTestId('skill-create-submit').click();
}

test('skills page: create from pasted SKILL.md, read as text, label versions, filter, remove', async ({ page }) => {
  await page.goto(`${cluster.frontendUrl}/#/workflows`);
  await page.getByTestId('nav-item-skills').click();
  await expect(page).toHaveURL(/#\/skills$/);
  await expect(page.getByTestId('skills-empty')).toBeVisible();

  // Skill text that looks like markup must be shown as text, never run or rendered.
  const hostile = '<img src=x onerror="window.__skillXss=1"> <script>window.__skillXss=2</script> **bold**';
  await createViaUi(page, skillMd('refunds', hostile));
  await expect(page.getByTestId('skills-notice')).toContainText('refunds');
  await expect(page.getByTestId('skill-row-refunds')).toBeVisible();
  const text = page.getByTestId('skill-text');
  await expect(text).toContainText('<img src=x onerror="window.__skillXss=1">');
  await expect(text).toContainText('**bold**');
  await expect(text.locator('img, script, strong')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __skillXss?: number }).__skillXss)).toBeUndefined();

  // New version, then pin production at v1 and filter by label.
  await createViaUi(page, skillMd('refunds', 'Refund within 60 days.'));
  await expect(page.getByTestId('skill-version-select')).toHaveValue('2');
  await expect(text).toContainText('60 days');
  await page.getByTestId('skill-version-select').selectOption('1');
  await expect(text).toContainText('**bold**');
  await page.getByTestId('skill-label-input').fill('production');
  await page.getByTestId('skill-set-label').click();
  await expect(page.getByTestId('skill-label-refunds-production')).toContainText('production: v1');
  await expect(page.getByTestId('skill-label-refunds-latest')).toContainText('latest: v2');

  await createViaUi(page, skillMd('house-style', 'Write short sentences.'));
  await expect(page.getByTestId('skill-row-house-style')).toBeVisible();
  await page.getByTestId('skills-label-filter').selectOption('production');
  await expect(page.getByTestId('skill-row-refunds')).toBeVisible();
  await expect(page.getByTestId('skill-row-house-style')).toHaveCount(0);
  await page.getByTestId('skills-label-filter').selectOption('');

  // The server holds what the page shows.
  const stored = await (await page.request.get(`${cluster.backendUrl}/api/v1/skills/refunds?label=production`)).json();
  expect(stored.version).toBe(1);

  // Refusals show the server's reason and store nothing.
  await createViaUi(page, '---\nname: Bad Name\ndescription: x\n---\nbody');
  await expect(page.getByTestId('skill-create-error')).toContainText(/invalid skill name/i);
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('skill-row-Bad Name')).toHaveCount(0);

  // Remove needs a second click.
  await page.getByTestId('skill-row-house-style').click();
  await page.getByTestId('skill-remove').click();
  await expect(page.getByTestId('skill-row-house-style')).toBeVisible();
  await page.getByTestId('skill-remove-confirm').click();
  await expect(page.getByTestId('skill-row-house-style')).toHaveCount(0);
  const gone = await page.request.get(`${cluster.backendUrl}/api/v1/skills/house-style`);
  expect(gone.status()).toBe(404);
});

test('agent node inspector: pick skills and a label, saved on the node config', async ({ page }) => {
  // No agent module is registered in the e2e backend, so the module descriptor of the seeded
  // node is rewritten to an agent module that takes skills (the picker keys on that schema).
  await page.route('**/modules/synthetic_router**', async (route) => {
    const res = await route.fetch();
    const desc = await res.json();
    desc.kind = 'agent';
    desc.config_schema = {
      type: 'object',
      properties: {
        skills: { type: 'array', items: { type: 'string' } },
        skills_label: { type: 'string', default: 'production' },
      },
    };
    await route.fulfill({ response: res, json: desc });
  });
  for (const [name, body] of [['alpha', 'Alpha text.'], ['beta', 'Beta text.']]) {
    const r = await page.request.post(`${cluster.backendUrl}/api/v1/skills`, {
      headers: cluster.apiHeaders,
      data: { markdown: skillMd(name, body) },
    });
    expect(r.status()).toBe(201);
  }
  const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
    headers: cluster.apiHeaders,
    data: {
      name: 'P20b picker',
      spec: { name: 'P20b picker', entry_node: 'start', nodes: { start: { id: 'start', handler: 'synthetic_router' } } },
    },
  });
  const wf = (await created.json()).workflow_id as string;

  await page.goto(`${cluster.frontendUrl}/#/workflows/${wf}`);
  await expect(page.getByTestId('node-start')).toBeVisible();
  await page.getByTestId('node-start').click();
  const inspector = page.getByLabel('Node properties inspector');
  await expect(inspector.getByTestId('skills-picker')).toBeVisible();
  await inspector.getByTestId('skills-pick-alpha').check();
  await inspector.getByTestId('skills-pick-beta').check();
  await inspector.getByTestId('skills-pick-beta').uncheck();
  await inspector.getByTestId('skills-label-input').fill('staging');

  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByLabel('Workflow revision')).toContainText('Revision: 2');
  const spec = (await (await page.request.get(`${cluster.backendUrl}/api/v1/workflows/${wf}`)).json()).spec;
  expect(spec.nodes.start.config.skills).toEqual(['alpha']);
  expect(spec.nodes.start.config.skills_label).toBe('staging');
});
