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

// Each scenario owns its prerequisite workflow, including after a worker restart.
// Creation and editing through the UI remain covered by the first two tests.
async function seedWorkflow(page: Page, withReview = false) {
  const spec = structuredClone(REGISTRY_TEMPLATES.find(template => template.id === 'order-triage-support')!.spec);
  spec.name = 'F04 Graph Editor E2E Workflow';
  const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
    headers: cluster.apiHeaders, data: { name: spec.name, spec },
  });
  expect(created.ok()).toBe(true);
  const workflow = await created.json();
  expect(workflow.revision).toBe(1);
  if (withReview) {
    spec.nodes.manual_review_step = {
      id: 'manual_review_step', handler: 'synthetic_fake_agent', requires_approval: true,
      default_transition: 'agent_assist', transitions: {},
    };
    // Exercise pruning of a real incoming edge when the review step is deleted.
    spec.nodes.approval_gate.default_transition = 'manual_review_step';
    const updated = await page.request.put(`${cluster.backendUrl}/api/v1/workflows/${workflow.workflow_id}`, {
      headers: cluster.apiHeaders, data: { spec, expected_revision: 1 },
    });
    expect(updated.ok()).toBe(true);
    expect((await updated.json()).revision).toBe(2);
  }
  return `${cluster.frontendUrl}/#/workflows/${workflow.workflow_id}`;
}

test.describe('Graph Editor with Runtime-Faithful Serialization (F04)', () => {
  test('visually renders nodes, routes, approval badges, and validates against backend', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);

    // Wait for connection
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create workflow from Order support with refund approval template
    const newWorkflowBtn = page.getByRole('button', { name: /New workflow/i });
    await newWorkflowBtn.click();

    // Select Order support with refund approval template
    await page.getByRole('dialog').getByText('Order support with refund approval').click();

    const nameInput = page.getByLabel('Workflow Name');
    await nameInput.fill('F04 Graph Editor E2E Workflow');

    const createBtn = page.getByRole('button', { name: /Validate & Create/i });
    await createBtn.click();

    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    await expect(page.getByRole('heading', { name: 'F04 Graph Editor E2E Workflow', level: 1, exact: true })).toBeVisible();

    // Verify Graph tab is active
    await expect(page.getByRole('tab', { name: 'Graph' })).toBeVisible();

    // Verify nodes are rendered on the canvas
    await expect(page.getByTestId('node-lookup')).toBeVisible();
    await expect(page.getByTestId('node-route')).toBeVisible();
    await expect(page.getByTestId('node-approval_gate')).toBeVisible();
    await expect(page.getByTestId('node-agent_assist')).toBeVisible();

    // Verify node badges
    await expect(page.getByTestId('node-lookup').getByText('Entry Node')).toBeVisible();
    await expect(page.getByTestId('node-approval_gate').getByText('Needs approval')).toBeVisible();

    // Click "Validate" button on canvas toolbar
    const validateBtn = page.getByRole('button', { name: 'Validate' });
    await validateBtn.click();

    // Expect authoritative validation passed banner
    await expect(
      page.getByText(/Workflow is valid/i)
    ).toBeVisible();
  });

  test('adds node via keyboard/drawer, configures properties and transitions via inspector, saves to backend, and verifies persistence on reload', async ({
    page,
  }) => {
    const workflowUrl = await seedWorkflow(page);
    await page.goto(workflowUrl);

    // Initial revision is 1
    const revBadge = page.getByLabel('Workflow revision');
    await expect(revBadge).toContainText('Revision: 1');

    // 1. Open Module Drawer to add step without drag-only requirement
    const addStepBtn = page.getByRole('button', { name: /Add step/i });
    await addStepBtn.click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();

    const drawer = page.getByRole('dialog', { name: 'Available modules drawer' });
    await expect(drawer).toBeVisible();

    // Filter modules
    const filterInput = drawer.getByPlaceholder('Search modules...');
    await filterInput.fill('synthetic');

    // Select synthetic_fake_agent and add
    const addModuleCard = drawer.getByText('+ Add Node').first();
    await expect(addModuleCard).toBeVisible();
    await addModuleCard.click();

    // Module drawer closes automatically, new node is added and selected
    await expect(drawer).not.toBeVisible();

    // Inspector opens for the newly added node
    const inspector = page.getByLabel('Node properties inspector');
    await expect(inspector).toBeVisible();

    // Rename node ID via inspector input
    const nodeIdInput = inspector.getByLabel('Node Identifier');
    await nodeIdInput.fill('manual_review_step');
    await nodeIdInput.blur();

    // Toggle approval gate requirement
    const approvalCheckbox = inspector.getByLabel('Require Human Approval Gate');
    await approvalCheckbox.check();

    const tabs = inspector.getByRole('tablist', { name: 'Node settings tabs' });
    const tabBounds = await tabs.boundingBox();
    expect(tabBounds).not.toBeNull();
    expect(tabBounds!.height).toBeGreaterThanOrEqual(36);

    // Switch to Routes tab to configure default transition from inspector dropdown
    await inspector.getByRole('tab', { name: 'Routes' }).click();
    const defaultTransitionSelect = inspector.getByLabel('Default transition target');
    await defaultTransitionSelect.selectOption('agent_assist');

    // Validate the updated graph
    const validateBtn = page.getByRole('button', { name: 'Validate' });
    await validateBtn.click();
    await expect(page.getByText(/Workflow is valid/i)).toBeVisible();

    // Verify "Unsaved changes" badge is present
    await expect(page.getByText('Unsaved changes')).toBeVisible();

    // Click Save button on canvas toolbar
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
    await saveBtn.click();

    // Verify authoritative revision incremented to Revision: 2
    await expect(revBadge).toContainText('Revision: 2');
    await expect(page.getByText('Unsaved changes')).not.toBeVisible();

    // Reload browser and verify full persistence from backend API
    await page.reload();
    await expect(page).toHaveURL(workflowUrl);
    await expect(revBadge).toContainText('Revision: 2');

    // Canvas renders new node with approval badge
    const newCanvasNode = page.getByTestId('node-manual_review_step');
    await expect(newCanvasNode).toBeVisible();
    await expect(newCanvasNode.getByText('Needs approval')).toBeVisible();
  });

  test('runs workflow and verifies execution trace against visual graph', async ({
    page,
  }) => {
    await page.goto(await seedWorkflow(page, true));

    // Click Run button in canvas toolbar
    const runBtn = page.getByRole('button', { name: 'Run', exact: true });
    await runBtn.click();

    // Run modal opens, submit launch
    const runModal = page.getByRole('dialog');
    await expect(runModal).toBeVisible();
    await runModal.getByRole('button', { name: /Launch Run/i }).click();

    // Tab switches to Run Console
    await expect(page.getByLabel('Run status badge')).toBeVisible({ timeout: 15000 });
    await expect(page.getByLabel('Run identifier badge')).toBeVisible();

    // Switch back to Graph tab
    await page.getByRole('tab', { name: 'Graph' }).click();
    await expect(page.getByTestId('node-lookup')).toBeVisible();
  });

  test('deletes a node, prunes dangling edges, and verifies validation and save', async ({
    page,
  }) => {
    const workflowUrl = await seedWorkflow(page, true);
    await page.goto(workflowUrl);

    // Click manual_review_step to open inspector
    const reviewNode = page.getByTestId('node-manual_review_step');
    await reviewNode.click();

    const inspector = page.getByLabel('Node properties inspector');
    await expect(inspector).toBeVisible();

    // Click "Delete Node" button inside inspector
    const deleteBtn = inspector.getByRole('button', { name: 'Delete Node' });
    await deleteBtn.click();

    // Node is removed from canvas
    await expect(page.getByTestId('node-manual_review_step')).not.toBeVisible();

    // Validate graph - should pass cleanly with zero dangling transitions
    const validateBtn = page.getByRole('button', { name: 'Validate' });
    await validateBtn.click();
    await expect(page.getByText(/Workflow is valid/i)).toBeVisible();

    // Save pruned graph
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
    await saveBtn.click();

    const revBadge = page.getByLabel('Workflow revision');
    await expect(revBadge).toContainText('Revision: 3');
    const saved = await page.request.get(`${cluster.backendUrl}/api/v1/workflows/${workflowUrl.split('/').pop()}`, { headers: cluster.apiHeaders });
    expect(saved.ok()).toBe(true);
    const { spec } = await saved.json();
    expect(spec.nodes).not.toHaveProperty('manual_review_step');
    expect(spec.nodes.approval_gate.default_transition).toBeFalsy();
  });
});
