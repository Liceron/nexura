import { test, expect, type Page } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';
import { REGISTRY_TEMPLATES } from '../../src/features/workflows/templates';

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

// Negative cases must also work alone or after a failed test restarts the worker.
// The first test still exercises all creation/configuration through the real UI.
async function openWorkflow(page: Page, scenario: string) {
  const name = `F05 Schema Configured Multi-Instance Spec — ${scenario}`;
  const spec = structuredClone(REGISTRY_TEMPLATES.find(template => template.id === 'order-triage-support')!.spec);
  spec.name = name;
  const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
    headers: cluster.apiHeaders, data: { name, spec },
  });
  expect(created.ok()).toBe(true);
  const workflow = await created.json();
  expect(workflow.revision).toBe(1);
  for (const [id, maxLength, template] of [['prompt_greeting', 1200, 'welcome_tmpl'], ['prompt_followup', 4800, 'followup_tmpl']] as const) {
    spec.nodes[id] = {
      id, handler: 'nexura.prompt.template', module_id: 'nexura.prompt.template', module_version: '1.0.0',
      config: { max_output_length: maxLength }, input_mapping: { template }, transitions: {},
      ...(id === 'prompt_greeting' ? { default_transition: 'prompt_followup' } : {}),
    };
  }
  const updated = await page.request.put(`${cluster.backendUrl}/api/v1/workflows/${workflow.workflow_id}`, {
    headers: cluster.apiHeaders, data: { spec, expected_revision: 1 },
  });
  expect(updated.ok()).toBe(true);
  expect((await updated.json()).revision).toBe(2);
  await page.goto(cluster.frontendUrl);
  await page.getByLabel('Search workflows input').fill(name);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await page.getByRole('link', { name, exact: true }).click();
  await expect(page).toHaveURL(`${cluster.frontendUrl}/#/workflows/${workflow.workflow_id}`);
  await expect(page.getByRole('heading', { name, level: 1, exact: true })).toBeVisible();
}

test.describe('Schema-Driven Module Configuration & Inspector (F05)', () => {
  test('creates workflow, configures two distinct instances with schema forms, saves, reloads, and verifies configuration in the inspector', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create blank workflow
    const newWorkflowBtn = page.getByRole('button', { name: /New workflow/i });
    await newWorkflowBtn.click();

    const nameInput = page.getByLabel('Workflow Name');
    await nameInput.fill('F05 Schema Configured Multi-Instance Spec');

    const createBtn = page.getByRole('button', { name: /Validate & Create/i });
    await createBtn.click();

    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    const workflowUrl = page.url();
    await expect(page.getByRole('heading', { name: 'F05 Schema Configured Multi-Instance Spec', level: 1, exact: true })).toBeVisible();

    // 1. Add Instance A: Prompt Template
    const addStepBtn = page.getByRole('button', { name: /Add step/i });
    await addStepBtn.click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();

    const palette = page.getByRole('dialog', { name: 'Available modules drawer' });
    await expect(palette).toBeVisible();

    // Filter by prompt
    const searchInput = palette.getByPlaceholder(/Search modules/i);
    await searchInput.fill('prompt');

    const addPromptCard = palette.getByText('+ Add Node').first();
    await addPromptCard.click();
    await expect(palette).not.toBeVisible();

    // Inspector opens for Instance A
    const inspector = page.getByLabel('Node properties inspector');
    await expect(inspector).toBeVisible();

    // Rename Node A
    const nodeIdInput = inspector.getByLabel('Node Identifier');
    await nodeIdInput.fill('prompt_greeting');
    await nodeIdInput.blur();

    // Switch to Config Tab and set max_output_length
    await inspector.getByRole('tab', { name: 'Config' }).click();
    const maxOutputInput = inspector.getByLabel('max_output_length');
    await maxOutputInput.fill('1200');

    // Switch to Inputs Tab and bind template
    await inspector.getByRole('tab', { name: /Inputs/i }).click();
    const templateInput = inspector.getByLabel('Input mapping for template');
    await templateInput.fill('welcome_tmpl');

    // 2. Add Instance B: Second Prompt Template with distinct settings
    await addStepBtn.click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();
    await expect(palette).toBeVisible();
    await searchInput.fill('prompt');
    await palette.getByText('+ Add Node').first().click();
    await expect(palette).not.toBeVisible();

    // Configure Instance B
    await nodeIdInput.fill('prompt_followup');
    await nodeIdInput.blur();

    await inspector.getByRole('tab', { name: 'Config' }).click();
    await maxOutputInput.fill('4800');

    await inspector.getByRole('tab', { name: /Inputs/i }).click();
    await templateInput.fill('followup_tmpl');

    // Connect prompt_greeting -> prompt_followup
    const nodeA = page.getByTestId('node-prompt_greeting');
    await nodeA.click();
    await inspector.getByRole('tab', { name: 'Routes' }).click();
    const defaultTransSelect = inspector.getByLabel('Default transition target');
    await defaultTransSelect.selectOption('prompt_followup');

    // Validate graph against backend
    const validateBtn = page.getByRole('button', { name: 'Validate' });
    await validateBtn.click();
    await expect(page.getByText(/Workflow is valid/i)).toBeVisible();

    // Each selected canvas node exposes its own exact configuration in the inspector.
    await nodeA.click();
    await inspector.getByRole('tab', { name: 'Config', exact: true }).click();
    await expect(inspector.getByLabel('max_output_length')).toHaveValue('1200');
    const nodeB = page.getByTestId('node-prompt_followup');
    await nodeB.click();
    await inspector.getByRole('tab', { name: 'Config', exact: true }).click();
    await expect(inspector.getByLabel('max_output_length')).toHaveValue('4800');

    // Save workflow to backend
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
    await saveBtn.click();

    const revBadge = page.getByLabel('Workflow revision');
    await expect(revBadge).toContainText('Revision: 2');

    // Reload browser and verify full persistence of distinct configs and bindings
    await page.reload();
    await expect(page).toHaveURL(workflowUrl);
    await expect(revBadge).toContainText('Revision: 2');

    // Node A configuration verified after reload
    await page.getByTestId('node-prompt_greeting').click();
    await inspector.getByRole('tab', { name: 'Config', exact: true }).click();
    await expect(inspector.getByLabel('max_output_length')).toHaveValue('1200');
    // Node B configuration verified after reload
    await page.getByTestId('node-prompt_followup').click();
    await inspector.getByRole('tab', { name: 'Config', exact: true }).click();
    await expect(inspector.getByLabel('max_output_length')).toHaveValue('4800');
  });

  test('negative case: invalid port mapping triggers authoritative backend error identifying field and node', async ({
    page,
  }) => {
    await openWorkflow(page, 'Invalid mapping');

    // Click Node A
    const nodeA = page.getByTestId('node-prompt_greeting');
    await nodeA.click();

    const inspector = page.getByLabel('Node properties inspector');
    await expect(inspector).toBeVisible();

    // Open Inputs tab and add invalid non-existent port
    await inspector.getByRole('tab', { name: /Inputs/i }).click();
    const portNameInput = inspector.getByLabel('Custom port name');
    const stateKeyInput = inspector.getByLabel('Custom state key');
    await portNameInput.fill('completely_invalid_port_xyz');
    await stateKeyInput.fill('state.test');

    const mapBtn = inspector.getByRole('button', { name: /\+ Map/i });
    await mapBtn.click();

    // Run authoritative validation
    const validateBtn = page.getByRole('button', { name: 'Validate' });
    await validateBtn.click();

    // Expect server validation error identifying field and node
    await expect(page.getByText(/INVALID_INPUT_MAPPING/i).first()).toBeVisible();
    await expect(page.getByText(/references unknown port 'completely_invalid_port_xyz'/i).first()).toBeVisible();

    // Verify node on canvas displays Invalid tag
    await expect(nodeA.getByText('Invalid', { exact: true })).toBeVisible();

    // Remove the invalid mapping to restore validity
    const removeBtn = inspector.getByRole('button', { name: 'Remove mapping completely_invalid_port_xyz' });
    await removeBtn.click();

    await validateBtn.click();
    await expect(page.getByText(/Workflow is valid/i)).toBeVisible();
    await expect(nodeA.getByText('Invalid', { exact: true })).not.toBeVisible();
  });

  test('negative case: displays security warning when raw secret/API key is pasted into plain graph fields', async ({
    page,
  }) => {
    await openWorkflow(page, 'Secret warning');

    const nodeA = page.getByTestId('node-prompt_greeting');
    await nodeA.click();

    const inspector = page.getByLabel('Node properties inspector');
    await expect(inspector).toBeVisible();

    // Open Config Tab and switch to Advanced JSON
    await inspector.getByRole('tab', { name: 'Config' }).click();
    const advToggleBtn = inspector.getByRole('button', { name: /Toggle Advanced JSON editor/i });
    await advToggleBtn.click();

    const advTextarea = inspector.getByLabel('Advanced JSON configuration');
    // Paste raw API key into config
    await advTextarea.fill(JSON.stringify({ max_output_length: 1200, api_token: 'sk-proj-123456789012345678901234567890' }, null, 2));
    await advTextarea.blur();

    // Switch back to Visual Form
    await advToggleBtn.click();

    // Verify security warning is displayed
    await expect(
      page.getByText(/This field looks like a raw secret or API key/i)
    ).toBeVisible();

    // Clean up
    await advToggleBtn.click();
    await advTextarea.fill(JSON.stringify({ max_output_length: 1200 }, null, 2));
    await advTextarea.blur();
  });

  test('switching module version displays migration warning and persists the selected version in the inspector', async ({
    page,
  }) => {
    await openWorkflow(page, 'Version change');

    const nodeA = page.getByTestId('node-prompt_greeting');
    await nodeA.click();

    const inspector = page.getByLabel('Node properties inspector');
    await expect(inspector).toBeVisible();

    // Open Details tab
    await inspector.getByRole('tab', { name: 'Details' }).click();

    // Change version
    const versionSelect = inspector.getByLabel('Module version');
    await versionSelect.selectOption('2.0.0');

    // Switch to Config tab to see migration warning
    await inspector.getByRole('tab', { name: 'Config' }).click();
    await expect(page.getByText(/Switched version to 2.0.0/i)).toBeVisible();

    // Version metadata lives in Details; verify its exact value before saving.
    await inspector.getByRole('tab', { name: 'Details', exact: true }).click();
    await expect(versionSelect).toBeVisible();
    await expect(versionSelect).toHaveValue('2.0.0');

    // Save and verify persistent v2.0.0
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
    await saveBtn.click();

    const revBadge = page.getByLabel('Workflow revision');
    await expect(revBadge).toContainText('Revision: 3');

    await page.reload();
    await page.getByTestId('node-prompt_greeting').click();
    await inspector.getByRole('tab', { name: 'Details', exact: true }).click();
    await expect(versionSelect).toBeVisible();
    await expect(versionSelect).toHaveValue('2.0.0');
  });

  test('B34: palette Channels and Connectors categories are populated and service-internal modules are not offered', async ({
    page,
  }) => {
    await openWorkflow(page, 'Palette');
    await page.getByRole('button', { name: /Add step/i }).click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();

    const palette = page.getByRole('dialog', { name: 'Available modules drawer' });
    await expect(palette).toBeVisible();
    await expect(palette.getByText(/Loading registered modules/i)).toHaveCount(0, { timeout: 15000 });

    // Channels are categorised by module id (nexura.channel.*), not by a "channel" kind.
    await palette.getByTestId('palette-category-channels').click();
    await expect(palette.locator('code', { hasText: /^nexura\.channel\.email$/ })).toHaveCount(1);
    await expect(palette.locator('code', { hasText: /^nexura\.channel\.whatsapp$/ })).toHaveCount(1);
    await expect(palette.getByText('No modules found', { exact: false })).toHaveCount(0);

    // Connectors: the 15 integration connectors.
    await palette.getByTestId('palette-category-connectors').click();
    await expect(palette.getByText('15 available')).toBeVisible();
    await expect(palette.locator('code', { hasText: /^nexura\.connectors\.slack$/ })).toHaveCount(1);

    // Service-internal modules are registered (API lists them, flagged internal) but not offered.
    const res = await page.request.get(`${cluster.backendUrl}/api/v1/modules`);
    const items = (await res.json()).items as Array<{ module_id: string; internal: boolean }>;
    const internal = items.filter((m) => m.internal).map((m) => m.module_id);
    // P28 step 7 moved the auth and workspace-membership modules to the closed edition; the
    // remaining service-internal modules (e.g. triggers) are still registered but not offered.
    expect(internal).toEqual(expect.arrayContaining(['nexura.service.triggers']));
    expect(internal).not.toContain('nexura.service.auth');
    expect(internal).not.toContain('nexura.service.workspaces');

    await palette.getByTestId('palette-category-all').click();
    for (const id of internal) {
      await expect(palette.locator('code', { hasText: new RegExp(`^${id.replace(/\./g, '\\.')}$`) }), `${id} hidden`).toHaveCount(0);
    }
    const search = palette.getByPlaceholder(/Search modules/i);
    await search.fill('nexura.service');
    await expect(palette.getByText('0 available')).toBeVisible();
  });
});
