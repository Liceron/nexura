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

test.describe('Revision-Safe Persistence and Import/Export (F06)', () => {
  test('authoritative CAS save increments revision and updates status badge', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create a new workflow from template
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();

    const nameInput = page.getByLabel('Workflow Name');
    await nameInput.fill('F06 Persistence Test Workflow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();

    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    // Check initial save status: Saved (Rev 1)
    const statusBadge = page.getByLabel('Save status');
    await expect(statusBadge).toHaveText('Saved');
    await expect(page.getByLabel('Workflow revision')).toHaveText('Revision: 1');

    // Make an edit: add a new node
    await page.getByRole('button', { name: 'Add step' }).click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();
    const palette = page.getByRole('dialog', { name: 'Available modules drawer' });
    await expect(palette).toBeVisible();
    await palette.getByText('+ Add Node').first().click();

    // Verify status changes to Unsaved changes
    await expect(statusBadge).toHaveText('Unsaved changes');

    // Save changes via Save button
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
    await saveBtn.click();

    // Verify authoritative revision incremented to Rev 2
    await expect(statusBadge).toHaveText('Saved');
    await expect(page.getByLabel('Workflow revision')).toHaveText('Revision: 2');
  });

  test('CAS conflict (409) preserves local edits and recovers via Save as Copy', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create workflow
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();
    await page.getByLabel('Workflow Name').fill('F06 Conflict Test Workflow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();

    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    const url = page.url();
    const workflowId = url.split('/').pop() || '';

    const statusBadge = page.getByLabel('Save status');
    await expect(statusBadge).toHaveText('Saved');
    await expect(page.getByLabel('Workflow revision')).toHaveText('Revision: 1');

    // Simulate concurrent modification in backend by directly issuing PUT with expected_revision=1
    // This advances the backend workflow revision to 2
    const currentWfRes = await fetch(`${cluster.backendUrl}/api/v1/workflows/${workflowId}`);
    const currentWf = await currentWfRes.json();
    const updatedSpec = { ...currentWf.spec, name: 'Concurrent Edit from Another Session' };

    const putRes = await fetch(`${cluster.backendUrl}/api/v1/workflows/${workflowId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...cluster.apiHeaders },
      body: JSON.stringify({
        spec: updatedSpec,
        expected_revision: 1,
      }),
    });
    expect(putRes.ok).toBe(true);
    const updatedWf = await putRes.json();
    expect(updatedWf.revision).toBe(2);

    // Now in our frontend editor (which still expects revision 1), make a local edit
    await page.getByRole('button', { name: 'Add step' }).click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();
    const palette = page.getByRole('dialog', { name: 'Available modules drawer' });
    await expect(palette).toBeVisible();
    await palette.getByText('+ Add Node').first().click();

    await expect(statusBadge).toHaveText('Unsaved changes');

    // Attempt to save -> triggers 409 REVISION_CONFLICT
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
    await saveBtn.click();

    // Verify conflict status badge and ConflictModal appears
    await expect(statusBadge).toHaveText('Revision Conflict');
    const conflictModal = page.getByRole('dialog', { name: /Revision Conflict/i });
    await expect(conflictModal).toBeVisible();
    await expect(conflictModal.getByText(/Concurrent Modification Conflict/i)).toBeVisible();

    // Click "Save as Copy" to reveal input
    await conflictModal.getByRole('button', { name: 'Save as Copy' }).click();

    // Fill copy name and confirm
    const copyInput = conflictModal.getByLabel(/New Workflow Name for Copy/i);
    await copyInput.fill('F06 Recovered Copy');
    const confirmCopyBtn = conflictModal.getByRole('button', { name: /Confirm Save as Copy/i });
    await confirmCopyBtn.click();

    // Expect navigation to newly created workflow
    await expect(page).toHaveURL(/#\/workflows\/wf-/);
    await expect(page.getByRole('heading', { name: 'F06 Recovered Copy', level: 1, exact: true })).toBeVisible();
    const recoveredUrl = page.url();
    await page.getByRole('link', { name: 'Workflows', exact: true }).click();
    await page.getByRole('link', { name: 'F06 Recovered Copy', exact: true }).click();
    await expect(page).toHaveURL(recoveredUrl);
    await expect(page.getByRole('heading', { name: 'F06 Recovered Copy', level: 1, exact: true })).toBeVisible();
  });

  test('local draft autosave displays recovery banner upon fresh page reload', async ({
    page,
  }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create workflow
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();
    await page.getByLabel('Workflow Name').fill('F06 Draft Recovery Workflow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();

    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    // Add a step
    await page.getByRole('button', { name: 'Add step' }).click();
    await page.getByRole('button', { name: 'Standard', exact: true }).click();
    const palette = page.getByRole('dialog', { name: 'Available modules drawer' });
    await expect(palette).toBeVisible();
    await palette.getByText('+ Add Node').first().click();

    // Wait 1.5s for debounced autosave to write draft to localStorage
    await page.waitForTimeout(1500);

    // Reload the page without saving
    await page.reload();

    // Check draft recovery banner is visible
    const draftBanner = page.getByLabel('Local draft recovery banner');
    await expect(draftBanner).toBeVisible();
    await expect(draftBanner.getByText(/Unsaved Local Draft/i)).toBeVisible();

    // Click Restore Draft
    await draftBanner.getByRole('button', { name: 'Restore Draft' }).click();

    // Banner disappears and status becomes Unsaved changes
    await expect(draftBanner).not.toBeVisible();
    await expect(page.getByLabel('Save status')).toHaveText('Unsaved changes');
  });

  test('versioned JSON export and import round-trip', async ({ page }) => {
    await page.goto(cluster.frontendUrl);
    await expect(page.getByLabel(/^Service connection status:/)).toHaveText('Connected');
    await expect(page.getByLabel(/^Service connection status:/)).toHaveAccessibleDescription(/local-owner/i);

    // Create workflow
    await page.getByRole('button', { name: /New workflow/i }).click();
    await page.getByRole('dialog').getByText('Order support with refund approval').click();
    await page.getByLabel('Workflow Name').fill('F06 Import Export Workflow');
    await page.getByRole('button', { name: /Validate & Create/i }).click();

    await expect(page).toHaveURL(/#\/workflows\/wf-/);

    // Click Export JSON button on canvas toolbar (exact: true)
    await page.getByRole('button', { name: 'More', exact: true }).click();
    await page.getByRole('button', { name: 'Import/export JSON', exact: true }).click();

    const modal = page.getByRole('dialog', { name: 'Workflow Specification Import / Export' });
    await expect(modal).toBeVisible();

    // Verify exported JSON in textarea
    const exportTextarea = modal.getByLabel('Exported workflow JSON');
    const exportContent = await exportTextarea.inputValue();
    expect(exportContent).toContain('"format": "nexura-workflow-export"');
    expect(exportContent).toContain('"schema_version": 1');

    // Switch to Import tab
    await modal.getByRole('tab', { name: 'Import JSON', exact: true }).click();

    // Prepare an imported spec JSON
    const importedJson = JSON.stringify({
      format: 'nexura-workflow-export',
      schema_version: 1,
      workflow: {
        name: 'Imported Roundtrip Workflow',
        spec: {
          spec_version: 1,
          entry_node: 'custom_imported_step',
          nodes: {
            custom_imported_step: {
              id: 'custom_imported_step',
              module_id: 'sample.imported',
              handler: 'execute',
              config: { title: 'Imported Step' },
            },
          },
        },
      },
    });

    const importInput = modal.getByLabel('Workflow JSON import input');
    await importInput.fill(importedJson);

    // Live validation shows valid workflow feedback
    await expect(modal.getByText(/Valid workflow JSON/i)).toBeVisible();

    // Apply to Canvas
    await modal.getByRole('button', { name: 'Apply to Canvas' }).click();

    // Verify modal closes and imported node is rendered on canvas
    await expect(modal).not.toBeVisible();
    await expect(page.getByTestId('node-custom_imported_step')).toBeVisible();
    await expect(page.getByLabel('Save status')).toHaveText('Unsaved changes');
  });
});
