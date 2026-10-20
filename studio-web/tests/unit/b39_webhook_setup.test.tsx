// B39: Studio webhook setup. The signing secret returned by registration is shown once (copy +
// dismiss, never persisted), the delivery URL and v2 signing snippets are shown, both trigger
// forms pick the workflow from the workspace instead of a hard-coded id, and the workflow page
// opens the triggers view preselected for that workflow. The API is a local fake (no network).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createHmac } from 'node:crypto';
import type { ReactNode } from 'react';

const SECRET = 'b39-secret-3f9a0c1d2e4b5a6978c0d1e2f3a4b5c6';

const triggers = {
  listSchedules: vi.fn(),
  listWebhooks: vi.fn(),
  listJobs: vi.fn(),
  listOutbox: vi.fn(),
  registerWebhook: vi.fn(),
  createSchedule: vi.fn(),
};
const workflows = { list: vi.fn(), get: vi.fn() };
const runs = { list: vi.fn(async () => ({ items: [], total: 0, limit: 20, offset: 0, has_more: false })) };
const api = {
  manager: { baseUrl: 'http://127.0.0.1:8765', client: { triggers, workflows, runs, request: vi.fn() } },
  connection: { status: 'connected', workspace: 'w', principal: 'p' },
};
vi.mock('../../src/api/context', () => ({ useApi: () => api }));
vi.mock('../../src/features/runs/RunVisualizer', () => ({ RunVisualizer: () => <div data-testid="run-visualizer-stub" /> }));

import { I18nProvider } from '../../src/i18n';
import { TriggersView } from '../../src/features/triggers/TriggersView';
import { WorkflowDetail } from '../../src/features/workflows/WorkflowDetail';
import { parseHash, triggersHref } from '../../src/app/routes';
import {
  SAMPLE_WEBHOOK_PAYLOAD,
  buildCurlSnippet,
  buildPythonSnippet,
  canonicalJson,
  webhookDeliveryUrl,
  webhookSigningString,
} from '../../src/features/triggers/webhookSnippet';

const wrap = (ui: ReactNode) => render(<I18nProvider>{ui}</I18nProvider>);

function wf(id: string, name: string) {
  return { workflow_id: id, name, revision: 1, entry_node: 'a', node_count: 1, created_at: 1, updated_at: 1 };
}

let webhookList: Array<Record<string, unknown>>;

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  window.location.hash = '';
  webhookList = [];
  triggers.listSchedules.mockResolvedValue({ schedules: [] });
  triggers.listWebhooks.mockImplementation(async () => ({ webhooks: webhookList, total: webhookList.length }));
  triggers.listJobs.mockResolvedValue({ jobs: [], total: 0 });
  triggers.listOutbox.mockResolvedValue({ outbox: [], guarantee: 'DRY_RUN_ONLY' });
  triggers.createSchedule.mockResolvedValue({ success: true });
  triggers.registerWebhook.mockImplementation(async (opts: { endpoint_id: string; workflow_id: string }) => {
    // The server lists webhooks without their secret.
    webhookList = [{ endpoint_id: opts.endpoint_id, workspace_id: 'w', workflow_id: opts.workflow_id, replay_window_seconds: 300, is_active: true }];
    return { success: true, endpoint_id: opts.endpoint_id, workspace_id: 'w', workflow_id: opts.workflow_id, secret: SECRET, replay_window_seconds: 300, signature_scheme: 'v2' };
  });
  workflows.list.mockResolvedValue({ items: [wf('wf-a', 'Alpha'), wf('wf-b', 'Beta')], total: 2, limit: 100, offset: 0, has_more: false });
});

afterEach(() => {
  window.location.hash = '';
});

async function registerWebhook(user: ReturnType<typeof userEvent.setup>, endpointId = 'orders') {
  await user.click(await screen.findByTestId('tab-webhooks'));
  await user.click(screen.getByTestId('btn-open-register-webhook'));
  await user.type(screen.getByTestId('input-new-webhook-id'), endpointId);
  const picker = screen.getByTestId('select-new-webhook-wfid');
  await waitFor(() => expect(picker).not.toBeDisabled());
  await user.click(screen.getByTestId('btn-submit-register-webhook'));
}

describe('B39: workflow picker in the trigger forms', () => {
  it('webhook form lists the workspace workflows and never defaults to order_support_v1', async () => {
    const user = userEvent.setup();
    wrap(<TriggersView />);
    await user.click(await screen.findByTestId('tab-webhooks'));
    await user.click(screen.getByTestId('btn-open-register-webhook'));
    const picker = screen.getByTestId('select-new-webhook-wfid') as HTMLSelectElement;
    await waitFor(() => expect(picker.value).toBe('wf-a'));
    expect(within(picker).getAllByRole('option').map((o) => (o as HTMLOptionElement).value)).toEqual(['wf-a', 'wf-b']);
    expect(document.body.innerHTML).not.toContain('order_support_v1');

    await user.selectOptions(picker, 'wf-b');
    await user.type(screen.getByTestId('input-new-webhook-id'), 'orders');
    await user.click(screen.getByTestId('btn-submit-register-webhook'));
    await waitFor(() =>
      expect(triggers.registerWebhook).toHaveBeenCalledWith(expect.objectContaining({ endpoint_id: 'orders', workflow_id: 'wf-b' })),
    );
  });

  it('schedule form uses the same picker and creates the schedule for the chosen workflow', async () => {
    const user = userEvent.setup();
    wrap(<TriggersView />);
    await user.click(await screen.findByTestId('btn-open-create-schedule'));
    const picker = screen.getByTestId('select-new-schedule-wfid') as HTMLSelectElement;
    await waitFor(() => expect(picker.value).toBe('wf-a'));
    await user.selectOptions(picker, 'wf-b');
    await user.type(screen.getByTestId('input-new-schedule-id'), 'nightly');
    await user.click(screen.getByTestId('btn-submit-create-schedule'));
    await waitFor(() =>
      expect(triggers.createSchedule).toHaveBeenCalledWith(expect.objectContaining({ schedule_id: 'nightly', workflow_id: 'wf-b' })),
    );
  });

  it('pages through every workflow (the server caps a page at 100)', async () => {
    const first = Array.from({ length: 100 }, (_, i) => wf(`wf-${i}`, `W${i}`));
    workflows.list
      .mockResolvedValueOnce({ items: first, total: 101, limit: 100, offset: 0, has_more: true })
      .mockResolvedValueOnce({ items: [wf('wf-last', 'Last')], total: 101, limit: 100, offset: 100, has_more: false });
    const user = userEvent.setup();
    wrap(<TriggersView />);
    await user.click(await screen.findByTestId('btn-open-create-schedule'));
    const picker = screen.getByTestId('select-new-schedule-wfid');
    await waitFor(() => expect(within(picker).getAllByRole('option')).toHaveLength(101));
    expect(workflows.list).toHaveBeenNthCalledWith(2, { limit: 100, offset: 100 });
  });

  it('a workspace without workflows says so and cannot submit either form', async () => {
    workflows.list.mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0, has_more: false });
    const user = userEvent.setup();
    wrap(<TriggersView />);
    await user.click(await screen.findByTestId('btn-open-create-schedule'));
    expect(await screen.findByTestId('select-new-schedule-wfid-empty')).toHaveTextContent('Create a workflow first');
    expect(screen.getByTestId('btn-submit-create-schedule')).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(screen.getByTestId('tab-webhooks'));
    await user.click(screen.getByTestId('btn-open-register-webhook'));
    expect(screen.getByTestId('btn-submit-register-webhook')).toBeDisabled();
  });

  it('a failed workflow list shows a formatted error instead of crashing', async () => {
    workflows.list.mockRejectedValue(new Error('boom'));
    const user = userEvent.setup();
    wrap(<TriggersView />);
    await user.click(await screen.findByTestId('btn-open-create-schedule'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load workflows: boom');
  });
});

describe('B39: the signing secret is shown once', () => {
  it('shows secret, delivery URL and v2 headers after registration, and never persists the secret', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const consoleSpies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) =>
      vi.spyOn(console, m).mockImplementation(() => {}),
    );
    const user = userEvent.setup();
    const view = wrap(<TriggersView />);
    await registerWebhook(user);

    const panel = await screen.findByTestId('webhook-secret-panel');
    expect(within(panel).getByTestId('webhook-secret-value')).toHaveTextContent(SECRET);
    expect(within(panel).getByTestId('webhook-secret-delivery-url')).toHaveTextContent(
      'http://127.0.0.1:8765/api/v1/triggers/webhooks/orders/deliver',
    );
    for (const header of ['X-Webhook-Signature', 'X-Webhook-Timestamp', 'X-Webhook-Event-ID']) {
      expect(panel).toHaveTextContent(header);
    }
    // Shown once: exactly one element carries the secret; the list never does.
    expect(screen.getAllByText(SECRET)).toHaveLength(1);
    expect(within(screen.getByTestId('webhook-item-orders')).queryByText(new RegExp(SECRET.slice(0, 10)))).toBeNull();

    // Copy puts the secret on the clipboard (the only place it goes).
    await user.click(within(panel).getByTestId('webhook-secret-copy'));
    expect(await navigator.clipboard.readText()).toBe(SECRET);
    expect(within(panel).getByTestId('webhook-secret-copy')).toHaveTextContent('Copied');

    // Not in storage, the URL or the console.
    const stored = (s: Storage) => Array.from({ length: s.length }, (_, i) => `${s.key(i)}=${s.getItem(s.key(i) as string)}`).join('\n');
    expect(stored(localStorage)).not.toContain(SECRET);
    expect(stored(sessionStorage)).not.toContain(SECRET);
    expect(setItem.mock.calls.flat().join('\n')).not.toContain(SECRET);
    expect(window.location.href).not.toContain(SECRET);
    for (const spy of consoleSpies) expect(JSON.stringify(spy.mock.calls)).not.toContain(SECRET);

    // Dismiss drops it for good.
    await user.click(within(panel).getByTestId('webhook-secret-dismiss'));
    expect(screen.queryByTestId('webhook-secret-panel')).toBeNull();
    expect(document.body.textContent).not.toContain(SECRET);
    // The delivery URL stays available on the endpoint card (it is not a secret).
    expect(screen.getByTestId('webhook-delivery-url-orders')).toHaveTextContent('/api/v1/triggers/webhooks/orders/deliver');

    view.unmount();
    wrap(<TriggersView />);
    await screen.findByTestId('triggers-view');
    expect(screen.queryByTestId('webhook-secret-panel')).toBeNull();
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it('leaving the triggers view (navigation) also drops the secret', async () => {
    const user = userEvent.setup();
    const view = wrap(<TriggersView />);
    await registerWebhook(user);
    expect(await screen.findByTestId('webhook-secret-value')).toHaveTextContent(SECRET);
    view.unmount();
    wrap(<TriggersView />);
    await screen.findByTestId('triggers-view');
    expect(screen.queryByTestId('webhook-secret-value')).toBeNull();
  });

  it('a failed registration shows the formatted error and no secret panel', async () => {
    triggers.registerWebhook.mockRejectedValue(new Error('WORKFLOW_NOT_FOUND'));
    const user = userEvent.setup();
    wrap(<TriggersView />);
    await registerWebhook(user);
    expect(await screen.findByText('WORKFLOW_NOT_FOUND')).toBeInTheDocument();
    expect(screen.queryByTestId('webhook-secret-panel')).toBeNull();
  });
});

describe('B39: snippets follow the server v2 signing scheme', () => {
  // Vector computed by nexura_runtime.service.triggers.signatures (also asserted in
  // tests/test_b39_webhook_snippet.py).
  const VECTOR_PAYLOAD = { msg: 'hello from a webhook', b: [1, 2, { z: null, a: true }], hi: 'नमस्ते' };
  const VECTOR_CANONICAL =
    '{"b":[1,2,{"a":true,"z":null}],"hi":"\\u0928\\u092e\\u0938\\u094d\\u0924\\u0947","msg":"hello from a webhook"}';
  const VECTOR_SIGNATURE = 'a79d85f0bac76bcbc78e86a999b36a3607e5e290cb4e0d900a23f24d56233077';

  it('canonical JSON and signed string match the server', () => {
    expect(canonicalJson(VECTOR_PAYLOAD)).toBe(VECTOR_CANONICAL);
    const signed = webhookSigningString(1700000000.9, 'evt-b39-1', VECTOR_CANONICAL);
    expect(signed).toBe(`v2.1700000000.evt-b39-1.${VECTOR_CANONICAL}`);
    expect(createHmac('sha256', 'test-secret-b39').update(signed).digest('hex')).toBe(VECTOR_SIGNATURE);
    expect(() => webhookSigningString(1, 'evt.with.dots', '{}')).toThrow();
  });

  it('curl snippet signs v2.<ts>.<event id>.<canonical body> and sends the same body', () => {
    const url = webhookDeliveryUrl('http://127.0.0.1:8765/', 'orders');
    const snippet = buildCurlSnippet({ deliveryUrl: url });
    expect(snippet).toContain(`BODY='${canonicalJson(SAMPLE_WEBHOOK_PAYLOAD)}'`);
    expect(snippet).toContain(`printf 'v2.%s.%s.%s' "$TS" "$EID" "$BODY" | openssl dgst -sha256 -hmac "$SECRET"`);
    expect(snippet).toContain(`curl -s -X POST 'http://127.0.0.1:8765/api/v1/triggers/webhooks/orders/deliver'`);
    expect(snippet).toContain('-H "X-Webhook-Timestamp: $TS"');
    expect(snippet).toContain('-H "X-Webhook-Event-ID: $EID"');
    expect(snippet).toContain('-H "X-Webhook-Signature: $SIG"');
    expect(snippet).toContain('-d "$BODY"');
    expect(snippet).toContain('NEXURA_WEBHOOK_SECRET');
    // A quote in the body cannot break out of the shell string.
    expect(buildCurlSnippet({ deliveryUrl: url, payload: { q: "it's" } })).toContain(`BODY='{"q":"it'\\''s"}'`);
  });

  it('python snippet canonicalises with json.dumps(sort_keys, separators) and signs v2', () => {
    const snippet = buildPythonSnippet({ deliveryUrl: 'http://h/deliver', payload: { b: [true, null], a: 'x' } });
    expect(snippet).toContain('body = json.dumps(payload, sort_keys=True, separators=(",", ":"))');
    expect(snippet).toContain('signed = f"v2.{ts}.{event_id}.{body}".encode("utf-8")');
    expect(snippet).toContain('hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()');
    expect(snippet).toContain('data=body.encode("utf-8")');
    expect(snippet).toContain('"b": [\n        True,\n        None,\n    ],');
    expect(snippet).toContain('secret = os.environ["NEXURA_WEBHOOK_SECRET"]');
  });

  it('snippets never embed the secret, so the endpoint card can show them after dismiss', async () => {
    const user = userEvent.setup();
    wrap(<TriggersView />);
    await registerWebhook(user);
    const panel = await screen.findByTestId('webhook-secret-panel');
    expect(within(panel).getByTestId('webhook-secret-snippet')).not.toHaveTextContent(SECRET);
    await user.click(within(panel).getByTestId('webhook-secret-snippet-python'));
    expect(within(panel).getByTestId('webhook-secret-snippet')).toHaveTextContent('json.dumps(payload, sort_keys=True');
    expect(within(panel).getByTestId('webhook-secret-snippet')).not.toHaveTextContent(SECRET);
    await user.click(within(panel).getByTestId('webhook-secret-dismiss'));
    expect(screen.getByTestId('webhook-signing-help')).toHaveTextContent('How to sign deliveries to orders');
  });

  it('delivery URL encodes the endpoint id and strips the /api base suffix', () => {
    expect(webhookDeliveryUrl('http://x:1', 'a b')).toBe('http://x:1/api/v1/triggers/webhooks/a%20b/deliver');
  });
});

describe('B39: Triggers entry point from the workflow page', () => {
  it('WorkflowDetail opens #/triggers/{workflowId}', async () => {
    const user = userEvent.setup();
    workflows.get.mockResolvedValue({
      workflow_id: 'wf b/1',
      name: 'Flow',
      revision: 1,
      spec: { name: 'Flow', entry_node: 'a', nodes: { a: { id: 'a', handler: 'h' } } },
    });
    wrap(<WorkflowDetail workflowId="wf b/1" onBack={() => {}} onOpenWorkflow={() => {}} />);
    await user.click(await screen.findByTestId('workflow-open-triggers'));
    expect(window.location.hash).toBe(triggersHref('wf b/1'));
    expect(parseHash(window.location.hash)).toMatchObject({ tab: 'triggers', triggersWorkflowId: 'wf b/1' });
    expect(parseHash('#/triggers')).toMatchObject({ tab: 'triggers', triggersWorkflowId: null });
  });

  it('the preselected workflow is chosen in both forms and the lists show only its triggers', async () => {
    triggers.listSchedules.mockResolvedValue({
      schedules: [
        { schedule_id: 's-a', workflow_id: 'wf-a', timezone: 'UTC', interval_seconds: 60, missed_run_policy: 'fire_once' },
        { schedule_id: 's-b', workflow_id: 'wf-b', timezone: 'UTC', interval_seconds: 60, missed_run_policy: 'fire_once' },
      ],
    });
    const user = userEvent.setup();
    wrap(<TriggersView initialWorkflowId="wf-b" />);
    expect(await screen.findByTestId('triggers-workflow-filter')).toHaveTextContent('wf-b');
    expect(screen.getByTestId('triggers-show-all')).toHaveAttribute('href', '#/triggers');
    expect(await screen.findByTestId('schedule-item-s-b')).toBeInTheDocument();
    expect(screen.queryByTestId('schedule-item-s-a')).toBeNull();

    await user.click(screen.getByTestId('btn-open-create-schedule'));
    const schedulePicker = screen.getByTestId('select-new-schedule-wfid') as HTMLSelectElement;
    await waitFor(() => expect(schedulePicker).not.toBeDisabled());
    expect(schedulePicker.value).toBe('wf-b');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    await user.click(screen.getByTestId('tab-webhooks'));
    await user.click(screen.getByTestId('btn-open-register-webhook'));
    expect((screen.getByTestId('select-new-webhook-wfid') as HTMLSelectElement).value).toBe('wf-b');
  });
});
