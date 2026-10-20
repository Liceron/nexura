import { test, expect, Page } from '@playwright/test';
import { startTestCluster, TestCluster } from './harness';

// P26 stage 2: build an agent on the Agents page with the offline model, use it in a workflow
// Agent node that runs, chat with it in the Playground, then delete it. Offline only: the
// harness backend runs local-owner with the fake-deterministic model; no keys, no network.

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

const OFFLINE_REPLY = "Offline fake reply from agent 'greeter'";

async function openWorkflowNode(page: Page, workflowId: string) {
  await page.goto(`${cluster.frontendUrl}/#/workflows/${workflowId}`);
  await expect(page.getByTestId('node-ask')).toBeVisible();
  await page.getByTestId('node-ask').click();
  return page.getByLabel('Node properties inspector');
}

test('agents: create with the offline model, run it in a workflow node, chat with it, delete it', async ({ page }) => {
  await page.goto(`${cluster.frontendUrl}/#/workflows`);
  await page.getByTestId('nav-item-agents').click();
  await expect(page).toHaveURL(/#\/agents$/);
  await expect(page.getByTestId('agents-empty')).toContainText('NEXURA_CHAT_MODELS');

  // The editor checks the form before saving.
  await page.getByTestId('agents-new').click();
  await page.getByTestId('agent-name').fill('Greeter!');
  await page.getByTestId('agent-output-schema').fill('{not json');
  await page.getByTestId('agent-save').click();
  await expect(page.getByTestId('agent-error-name')).toBeVisible();
  await expect(page.getByTestId('agent-error-output_schema')).toContainText('Not valid JSON');
  await expect(page.getByTestId('agent-row-greeter')).toHaveCount(0);

  // Create it on the offline fake model (the default; no key needed).
  await page.getByTestId('agent-name').fill('greeter');
  await page.getByTestId('agent-output-schema').fill('');
  await page.getByTestId('agent-description').fill('Says hello <img src=x onerror="window.__agentXss=1">');
  await page.getByTestId('agent-instructions').fill('Greet the user.');
  await expect(page.getByTestId('agent-model')).toHaveValue('fake-deterministic');
  await page.getByTestId('agent-max-turns').fill('3');
  await page.getByTestId('agent-save').click();
  await expect(page.getByTestId('agents-notice')).toContainText('greeter');
  await expect(page.getByTestId('agent-revision')).toContainText('1');
  const row = page.getByTestId('agent-row-greeter');
  await expect(row).toContainText('fake-deterministic');
  await expect(row).toContainText('<img src=x');
  await expect(row.locator('img')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __agentXss?: number }).__agentXss)).toBeUndefined();
  const agentId = (await page.getByTestId('agent-id').textContent())?.trim() ?? '';
  expect(agentId).toMatch(/^agt_/);

  // The server holds what the page shows; an edit bumps the revision.
  await page.getByTestId('agent-instructions').fill('Greet the user warmly.');
  await page.getByTestId('agent-save').click();
  await expect(page.getByTestId('agent-revision')).toContainText('2');
  const stored = await (await page.request.get(`${cluster.backendUrl}/api/v1/agents/${agentId}`)).json();
  expect(stored.revision).toBe(2);
  expect(stored.spec.limits.max_turns).toBe(3);
  expect(stored.spec.model).toBe('fake-deterministic');

  // A workflow with an Agent node: the inspector shows a picker, not raw JSON.
  const created = await page.request.post(`${cluster.backendUrl}/api/v1/workflows`, {
    headers: cluster.apiHeaders,
    data: {
      name: 'P26 agent node',
      spec: {
        name: 'P26 agent node',
        entry_node: 'ask',
        nodes: {
          ask: {
            id: 'ask',
            handler: 'nexura.agents.agent',
            module_id: 'nexura.agents.agent',
            config: { agent_id: agentId, input_key: 'q' },
          },
        },
        terminal_nodes: ['ask'],
      },
    },
  });
  expect(created.ok()).toBeTruthy();
  const workflowId = (await created.json()).workflow_id as string;
  let inspector = await openWorkflowNode(page, workflowId);
  await expect(inspector.getByTestId('agent-node-picker')).toHaveValue(agentId);
  await expect(inspector.getByTestId('agent-node-input-key')).toHaveValue('q');
  await inspector.getByTestId('agent-node-output-key').fill('answer');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByLabel('Authoritative revision badge')).toContainText('Revision: 2');
  const spec = (await (await page.request.get(`${cluster.backendUrl}/api/v1/workflows/${workflowId}`)).json()).spec;
  expect(spec.nodes.ask.config).toEqual({ agent_id: agentId, input_key: 'q', output_key: 'answer' });

  // The node runs the saved agent on the offline model.
  const started = await page.request.post(`${cluster.backendUrl}/api/v1/runs`, {
    headers: cluster.apiHeaders,
    data: { workflow_id: workflowId, initial_state: { q: 'hello' } },
  });
  expect(started.ok()).toBeTruthy();
  const runId = (await started.json()).run_id as string;
  await expect
    .poll(async () => (await (await page.request.get(`${cluster.backendUrl}/api/v1/runs/${runId}`)).json()).status)
    .toBe('completed');
  const run = await (await page.request.get(`${cluster.backendUrl}/api/v1/runs/${runId}`)).json();
  expect(run.current_state.answer).toContain(`${OFFLINE_REPLY}: hello`);

  // Chat with it: agent_id is sent and the model select is hidden.
  await page.getByTestId('nav-item-playground').click();
  const agentSelect = page.getByTestId('agent-select');
  await expect(agentSelect.locator('option', { hasText: 'greeter' })).toHaveCount(1);
  let chatBody: Record<string, unknown> | null = null;
  page.on('request', (req) => {
    if (req.method() === 'POST' && /\/sessions\/[^/]+\/chat\/stream$/.test(req.url())) {
      chatBody = req.postDataJSON() as Record<string, unknown>;
    }
  });
  await agentSelect.selectOption(agentId);
  await expect(page.getByTestId('model-select')).toHaveCount(0);
  await page.getByTestId('chat-input-textarea').fill('hello there');
  await page.getByTestId('send-message-button').click();
  await expect(page.getByTestId('message-bubble-assistant').last()).toContainText(`${OFFLINE_REPLY}: hello there`);
  expect(chatBody).toMatchObject({ message: 'hello there', agent_id: agentId });
  expect(chatBody).not.toHaveProperty('model');
  await expect(page.getByTestId('refused-tool-calls')).toHaveCount(0);

  // Delete needs a second click; the workflow node then shows the agent as missing.
  await page.getByTestId('nav-item-agents').click();
  await page.getByTestId('agent-row-greeter').click();
  await page.getByTestId('agent-delete').click();
  await expect(page.getByTestId('agent-row-greeter')).toBeVisible();
  await page.getByTestId('agent-delete-confirm').click();
  await expect(page.getByTestId('agent-row-greeter')).toHaveCount(0);
  expect((await page.request.get(`${cluster.backendUrl}/api/v1/agents/${agentId}`)).status()).toBe(404);
  inspector = await openWorkflowNode(page, workflowId);
  await expect(inspector.getByTestId('agent-node-missing')).toContainText(agentId);
});
