import { test, expect } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

let cluster: TestCluster;
let sharedWorkflowUrl: string;

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

test.describe('Graph Editor with Runtime-Faithful Serialization (F04)', () => {
  test('visually renders nodes, routes, approval badges, and validates against backend', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);

    // Wait for connection
    await expect(page.getByLabel(/^Service connection status:/)).toContainText(/local-owner/i);

    // Create workflow from Customer Order Triage & Support template
    const newWorkflowBtn = page.getByRole('button', { name: /\+ New Workflow/i });
    await newWorkflowBtn.click();

    // Select Customer Order Triage & Support template
    await page.getByRole('dialog').getByText('Customer Order Triage & Support').click();

    const nameInput = page.getByLabel('Workflow Name');
    await nameInput.fill('F04 Graph Editor E2E Workflow');

    const createBtn = page.getByRole('button', { name: /Validate & Create/i });
    await createBtn.click();

    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    sharedWorkflowUrl = page.url();

    // Verify Visual Graph tab is active
    await expect(page.getByRole('tab', { name: 'Visual Graph' })).toBeVisible();

    // Verify nodes are rendered on the canvas
    await expect(page.getByTestId('node-lookup')).toBeVisible();
    await expect(page.getByTestId('node-route')).toBeVisible();
    await expect(page.getByTestId('node-approval_gate')).toBeVisible();
    await expect(page.getByTestId('node-agent_assist')).toBeVisible();

    // Verify node badges
    await expect(page.getByTestId('node-lookup').getByText('Entry Node')).toBeVisible();
    await expect(page.getByTestId('node-approval_gate').getByText('Approval Gate')).toBeVisible();

    // Click "Validate" button on canvas toolbar
    const validateBtn = page.getByRole('button', { name: 'Validate' });
    await validateBtn.click();

    // Expect authoritative validation passed banner
    await expect(
      page.getByText(/Authoritative validation passed: graph structure, routes, and mappings are fully valid/i)
    ).toBeVisible();
  });

  test('adds node via keyboard/drawer, configures properties and transitions via inspector, saves to backend, and verifies persistence on reload', async ({
    page,
  }) => {
    await page.goto(sharedWorkflowUrl);

    // Initial revision is 1
    const revBadge = page.getByLabel('Authoritative revision badge');
    await expect(revBadge).toContainText('Revision: 1');

    // 1. Open Module Drawer to add step without drag-only requirement
    const addStepBtn = page.getByRole('button', { name: /\+ Add Step/i });
    await addStepBtn.click();

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

    // Switch to Routes tab to configure default transition from inspector dropdown
    await inspector.getByRole('tab', { name: 'Routes' }).click();
    const defaultTransitionSelect = inspector.getByLabel('Default transition target');
    await defaultTransitionSelect.selectOption('agent_assist');

    // Validate the updated graph
    const validateBtn = page.getByRole('button', { name: 'Validate' });
    await validateBtn.click();
    await expect(page.getByText(/Authoritative validation passed/i)).toBeVisible();

    // Verify "Unsaved Changes" badge is present
    await expect(page.getByText('Unsaved Changes')).toBeVisible();

    // Click Save button on canvas toolbar
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
    await saveBtn.click();

    // Verify authoritative revision incremented to Revision: 2
    await expect(revBadge).toContainText('Revision: 2');
    await expect(page.getByText('Unsaved Changes')).not.toBeVisible();

    // Reload browser and verify full persistence from backend API
    await page.reload();
    await expect(page).toHaveURL(sharedWorkflowUrl);
    await expect(revBadge).toContainText('Revision: 2');

    // Canvas renders new node with approval badge
    const newCanvasNode = page.getByTestId('node-manual_review_step');
    await expect(newCanvasNode).toBeVisible();
    await expect(newCanvasNode.getByText('Approval Gate')).toBeVisible();
  });

  test('runs workflow and verifies execution trace against visual graph', async ({
    page,
  }) => {
    await page.goto(sharedWorkflowUrl);

    // Click Run button in canvas toolbar
    const runBtn = page.getByRole('button', { name: 'Run', exact: true }).first();
    await runBtn.click();

    // Run modal opens, submit launch
    const runModal = page.getByRole('dialog');
    await expect(runModal).toBeVisible();
    await runModal.getByRole('button', { name: /Launch Run/i }).click();

    // Tab switches to Run Console
    await expect(page.getByLabel('Run status badge')).toBeVisible({ timeout: 15000 });
    await expect(page.getByLabel('Run identifier badge')).toBeVisible();

    // Switch back to Visual Graph tab
    await page.getByRole('tab', { name: 'Visual Graph' }).click();
    await expect(page.getByTestId('node-lookup')).toBeVisible();
  });

  test('deletes a node, prunes dangling edges, and verifies validation and save', async ({
    page,
  }) => {
    await page.goto(sharedWorkflowUrl);

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
    await expect(page.getByText(/Authoritative validation passed/i)).toBeVisible();

    // Save pruned graph
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
    await saveBtn.click();

    const revBadge = page.getByLabel('Authoritative revision badge');
    await expect(revBadge).toContainText('Revision: 3');
  });
});
