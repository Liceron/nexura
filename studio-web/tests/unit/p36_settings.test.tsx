// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// Round 5 (operational and settings pages). Explicit local mock API: no service, provider,
// model or network calls.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../src/i18n';

const SECRET = 'sk_unit_sentinel_should_never_render_42';

const fixture = vi.hoisted(() => {
  const credentials: Array<Record<string, unknown>> = [];
  const client = {
    modules: { list: vi.fn(async () => ({ items: [] })) },
    credentials: {
      list: vi.fn(async () => ({ items: credentials })),
      create: vi.fn(async (body: Record<string, unknown>) => {
        credentials.push({ credential_id: body.credential_id, credential_type: body.credential_type, provider: body.provider, description: body.description, status: 'active', masked_hint: 'sk_…42', created_at: 1 });
        return credentials[credentials.length - 1];
      }),
      delete: vi.fn(), verify: vi.fn(), revoke: vi.fn(),
    },
    integrations: {
      getStatus: vi.fn(async () => ({ mcp: { available: true }, openapi: { available: true } })),
      discoverMcp: vi.fn(async () => ({ tools: [
        { name: 'math_add', description: 'Add', input_schema: {}, digest: 'abcdef0123456789', effect_policy: 'pure' },
        { name: 'send_mail', description: 'Send', input_schema: {}, digest: '0123456789abcdef', effect_policy: 'non_idempotent_write' },
      ] })),
      testRunMcp: vi.fn(async () => ({ status: 'success', content: [{ text: '42' }], is_fixture: true, execution_label: 'Fixture Dry-Run (Local Mock Transport)', duration_ms: 3 })),
      discoverOpenAPI: vi.fn(async () => ({ operations: [] })),
      register: vi.fn(async () => ({})),
    },
    workflows: { list: vi.fn(async () => ({ items: [{ workflow_id: 'wf-a', name: 'Alpha', revision: 1 }], total: 1, has_more: false })) },
    triggers: {
      listSchedules: vi.fn(async () => ({ schedules: [{ schedule_id: 's1', workflow_id: 'wf-a', timezone: 'Asia/Kolkata', interval_seconds: 60, missed_run_policy: 'skip', paused: false }] })),
      listWebhooks: vi.fn(async () => ({ webhooks: [{ endpoint_id: 'ep1', workflow_id: 'wf-a', replay_window_seconds: 300 }], total: 1 })),
      listJobs: vi.fn(async () => ({ jobs: [] })),
      listOutbox: vi.fn(async () => ({ outbox: [], guarantee: 'DRY_RUN_ONLY' })),
      registerWebhook: vi.fn(),
      rotateDeliveryToken: vi.fn(),
      deleteWebhook: vi.fn(async () => ({ deleted: true })),
    },
    request: vi.fn(async () => ({ registrations: [] })),
  };
  const api = {
    manager: { client, clearCache: vi.fn(), baseUrl: 'http://127.0.0.1:8765' },
    connection: { status: 'connected', workspace: 'local-r5', principal: 'owner', profile: 'local-owner', baseUrl: '/api', sdkVersion: '0.4.0', capabilities: undefined as unknown },
    refreshConnection: vi.fn(),
  };
  return { api, client, credentials };
});
vi.mock('../../src/api/context', () => ({ useApi: () => fixture.api }));
vi.mock('../../src/api/membership', () => ({
  getMyMembership: async () => ({ workspace_id: 'local-r5', principal_id: 'owner', role: 'admin', permissions: [], profile: 'local-owner' }),
  listMembers: async () => { throw new Error('no members route'); },
  addMember: vi.fn(), updateMemberRole: vi.fn(), removeMember: vi.fn(),
}));

const { CredentialsView } = await import('../../src/features/credentials/CredentialsView');
const { SystemView } = await import('../../src/features/system/SystemView');
const { IntegrationsView } = await import('../../src/features/integrations/IntegrationsView');
const { TriggersView } = await import('../../src/features/triggers/TriggersView');

const show = (node: React.ReactNode) => render(<I18nProvider>{node}</I18nProvider>);
const primaries = () => Array.from(document.querySelectorAll('.studio-button--primary'));

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.clearAllMocks();
  fixture.credentials.length = 0;
  fixture.api.connection.capabilities = undefined;
  (fixture.api.manager as { hasAuthToken?: boolean }).hasAuthToken = false;
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: !query.includes('max-width'), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
});
afterEach(cleanup);

describe('Credentials', () => {
  it('keeps the secret write-only: masked, never echoed, cleared on close and absent after saving', async () => {
    const user = userEvent.setup();
    show(<CredentialsView />);
    expect(await screen.findByRole('heading', { level: 1, name: 'Credentials' })).toBeInTheDocument();
    expect(primaries()).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Add credential' }));
    const dialog = screen.getByRole('dialog', { name: 'Add credential' });
    const secret = within(dialog).getByLabelText('Secret value');
    expect(secret).toHaveAttribute('type', 'password');
    expect(secret).toHaveAccessibleDescription('Enter the secret to store on this service. Workflows use its reference.');
    await user.type(secret, SECRET);
    // Closing purges the transient value; reopening starts empty.
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await user.click(screen.getByRole('button', { name: 'Add credential' }));
    const reopened = screen.getByRole('dialog', { name: 'Add credential' });
    expect(within(reopened).getByLabelText('Secret value')).toHaveValue('');

    await user.type(within(reopened).getByLabelText('Reference'), 'unit_key');
    await user.type(within(reopened).getByLabelText('Secret value'), SECRET);
    await user.click(within(reopened).getByRole('button', { name: 'Add credential' }));
    await screen.findByTestId('cred-row-unit_key');
    expect(fixture.client.credentials.create).toHaveBeenCalledWith(expect.objectContaining({ credential_id: 'unit_key', secret: SECRET }));
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).not.toContain(SECRET);
    expect(screen.getByTestId('cred-hint-unit_key')).toHaveTextContent('sk_…42');
    expect(screen.getByTestId('status-unit_key')).toHaveTextContent('Active');
  });

  it('explains storage in a disclosure and names the tables', async () => {
    show(<CredentialsView />);
    const help = await screen.findByTestId('credential-storage-help');
    expect(within(help).getByText('How credentials are stored')).toBeInTheDocument();
    expect(help).toHaveTextContent('never stores the secret in browser storage');
    fireEvent.click(screen.getByRole('tab', { name: 'Permissions' }));
    expect(screen.getByRole('table', { name: 'Role permissions' })).toBeInTheDocument();
    // A read-only tab has no filled primary action.
    expect(primaries()).toHaveLength(0);
  });
});

describe('System', () => {
  it('reports only what the service said: missing flags are "Not reported", never Supported', async () => {
    show(<SystemView />);
    const table = screen.getByRole('table', { name: 'Capabilities' });
    for (const row of within(table).getAllByRole('row').slice(1)) expect(row).toHaveTextContent('Not reported');
    expect(screen.queryByText('Supported')).toBeNull();
    expect(primaries()).toHaveLength(0);
  });

  it('shows reported true/false states and lists extension flags by name', async () => {
    fixture.api.connection.capabilities = { sdk_version: '0.4.0', active_profile: 'local-owner', capabilities: { modules: true, validation: false, live_events: true, sandbox_execution: true } };
    show(<SystemView />);
    expect(screen.getByTestId('capability-modules')).toHaveTextContent('Supported');
    expect(screen.getByTestId('capability-validation')).toHaveTextContent('Not available');
    expect(screen.getByTestId('capability-downloadable_export')).toHaveTextContent('Not reported');
    expect(screen.getByTestId('capability-sandbox_execution')).toHaveTextContent('sandbox_execution');
    expect(screen.getByText('0.4.0')).toBeInTheDocument();
  });
});

describe('Tools', () => {
  it('marks reads and writes at each operation and moves the primary from Connect to Run test', async () => {
    const user = userEvent.setup();
    show(<IntegrationsView />);
    // The page discovers the default local fixture on load (existing behaviour), so the
    // selected tool's Run test is the primary and Connect steps back to secondary.
    const write = await screen.findByTestId('mcp-tool-send_mail');
    const connect = screen.getByRole('button', { name: 'Connect' });
    expect(connect).not.toHaveClass('studio-button--primary');
    expect(write).toHaveTextContent('Writes (not safe to retry)');
    expect(screen.getByTestId('mcp-tool-math_add')).toHaveTextContent('No side effects');
    expect(primaries().map((b) => b.textContent)).toEqual(['Run test']);
    expect(screen.getByLabelText('Arguments (JSON)')).toHaveAccessibleDescription(/Nothing is sent to a real service/);
    await user.click(screen.getByRole('button', { name: 'Run test' }));
    expect(await screen.findByTestId('tool-run-label')).toHaveTextContent('Fixture Dry-Run (Local Mock Transport)');
    expect(screen.getByTestId('tool-run-status')).toHaveTextContent('Success');
    // Reconnecting keeps working from the secondary Connect action.
    await user.click(connect);
    await waitFor(() => expect(fixture.client.integrations.discoverMcp).toHaveBeenCalledTimes(2));
  });

  it('makes Connect the only primary while no source is connected', async () => {
    fixture.client.integrations.getStatus.mockResolvedValueOnce({ mcp: { available: false, missing_reason: 'disabled' }, openapi: { available: false } } as never);
    show(<IntegrationsView />);
    const connect = await screen.findByRole('button', { name: 'Connect' });
    await waitFor(() => expect(screen.getByText('MCP is unavailable')).toBeInTheDocument());
    expect(primaries()).toEqual([connect]);
    expect(fixture.client.integrations.discoverMcp).not.toHaveBeenCalled();
  });
});

describe('Triggers', () => {
  it('gives each tab its own primary and keeps the extra webhook tests under a disclosure', async () => {
    const user = userEvent.setup();
    show(<TriggersView />);
    expect(await screen.findByTestId('schedule-item-s1')).toHaveTextContent('Asia/Kolkata');
    expect(screen.getByTestId('schedule-status-s1')).toHaveTextContent('Active');
    expect(primaries().map((b) => b.textContent)).toEqual(['New schedule']);
    await user.click(screen.getByRole('tab', { name: /^Webhooks/ }));
    expect(primaries().map((b) => b.textContent)).toEqual(['New webhook']);
    const more = screen.getByTestId('webhook-more-tests');
    expect(more).not.toHaveAttribute('open');
    expect(within(more).getByTestId('btn-test-webhook-bad-sig')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: /^Outbox/ }));
    expect(primaries().map((b) => b.textContent)).toEqual(['Simulate message']);
    expect(screen.getByTestId('outbox-mode')).toHaveTextContent('DRY_RUN_ONLY');
    const channel = screen.getByRole('group', { name: 'Show channel' });
    expect(screen.getByRole('group', { name: 'Channel' })).toContainElement(screen.getByRole('radio', { name: 'Email' }));
    expect(within(channel).getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true');
    await act(async () => { await Promise.resolve(); });
  });

  it('labels every schedule form field', async () => {
    const user = userEvent.setup();
    show(<TriggersView />);
    await user.click(await screen.findByRole('button', { name: 'New schedule' }));
    const dialog = screen.getByRole('dialog', { name: 'New schedule' });
    for (const label of ['Schedule ID *', 'Target workflow *', 'IANA time zone *', 'Missed run policy']) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    await waitFor(() => expect(within(dialog).getByLabelText('Target workflow *')).not.toBeDisabled());
  });
});

describe('Triggers delivery tokens (remote token mode)', () => {
  const TOKEN = 'dlv_unit_token_sentinel_0123456789abcdefghij';
  const ROTATED = 'dlv_rotated_token_sentinel_zyxwvutsrqponmlkji';
  const leaks = (value: string) => document.body.innerHTML.includes(value) || (JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).includes(value);

  it('local mode: no delivery token and no rotate action', async () => {
    fixture.client.triggers.registerWebhook.mockResolvedValue({ endpoint_id: 'ep2', workflow_id: 'wf-a', secret: 'whsec_local', replay_window_seconds: 300 });
    const user = userEvent.setup();
    show(<TriggersView />);
    await user.click(await screen.findByRole('tab', { name: /^Webhooks/ }));
    expect(screen.queryByTestId('btn-rotate-delivery-token-ep1')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'New webhook' }));
    await user.type(screen.getByTestId('input-new-webhook-id'), 'ep2');
    await waitFor(() => expect(screen.getByTestId('select-new-webhook-wfid')).not.toBeDisabled());
    await user.click(screen.getByTestId('btn-submit-register-webhook'));
    const panel = await screen.findByTestId('webhook-secret-panel');
    expect(within(panel).queryByTestId('webhook-secret-delivery-token')).toBeNull();
    expect(screen.queryByTestId('btn-rotate-delivery-token-ep1')).toBeNull();
  });

  it('shows the token returned at registration once, masked, with copy; never stored', async () => {
    fixture.client.triggers.registerWebhook.mockResolvedValue({ endpoint_id: 'ep2', workflow_id: 'wf-a', secret: 'whsec_tok', replay_window_seconds: 300, delivery_token: TOKEN });
    const user = userEvent.setup();
    // After setup: user-event installs its own clipboard stub.
    const writeText = vi.fn(async (_text: string) => undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    show(<TriggersView />);
    await user.click(await screen.findByRole('tab', { name: /^Webhooks/ }));
    await user.click(screen.getByRole('button', { name: 'New webhook' }));
    await user.type(screen.getByTestId('input-new-webhook-id'), 'ep2');
    await waitFor(() => expect(screen.getByTestId('select-new-webhook-wfid')).not.toBeDisabled());
    await user.click(screen.getByTestId('btn-submit-register-webhook'));
    const panel = await screen.findByTestId('webhook-secret-panel');
    const value = within(panel).getByTestId('webhook-secret-delivery-token-value');
    expect(value).not.toHaveTextContent(TOKEN);
    expect(leaks(TOKEN)).toBe(false);
    expect(panel).toHaveTextContent('This delivery token is shown once');
    await user.click(within(panel).getByTestId('webhook-secret-delivery-token-copy'));
    expect(writeText).toHaveBeenCalledWith(TOKEN);
    expect(within(panel).getByTestId('webhook-secret-delivery-token-copy')).toHaveTextContent('Copied');
    const reveal = within(panel).getByTestId('webhook-secret-delivery-token-reveal');
    await user.click(reveal);
    expect(reveal).toHaveAttribute('aria-pressed', 'true');
    expect(value).toHaveTextContent(TOKEN);
    expect(JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).not.toContain(TOKEN);
    // Registration revealed token mode, so each webhook now offers rotation.
    expect(screen.getByTestId('btn-rotate-delivery-token-ep1')).toBeInTheDocument();
    await user.click(within(panel).getByRole('button', { name: 'I have saved the secret and token, dismiss' }));
    expect(screen.queryByTestId('webhook-secret-panel')).toBeNull();
    expect(leaks(TOKEN)).toBe(false);
  });

  it('rotates only after confirmation and shows the new token once', async () => {
    // Studio is sending an operator token to this service (signed in, token mode).
    (fixture.api.manager as { hasAuthToken?: boolean }).hasAuthToken = true;
    fixture.client.triggers.rotateDeliveryToken.mockResolvedValue({ endpoint_id: 'ep1', delivery_token: ROTATED, delivery_token_header: 'Authorization: Bearer <delivery_token>' });
    const user = userEvent.setup();
    show(<TriggersView />);
    await user.click(await screen.findByRole('tab', { name: /^Webhooks/ }));
    const rotate = await screen.findByTestId('btn-rotate-delivery-token-ep1');
    expect(rotate).toHaveAccessibleName('Rotate delivery token ep1');
    await user.click(rotate);
    expect(fixture.client.triggers.rotateDeliveryToken).not.toHaveBeenCalled();
    expect(screen.getByText('A new delivery token is issued and the current one stops working immediately.')).toBeInTheDocument();
    await user.click(screen.getByTestId('btn-rotate-delivery-token-ep1-confirm'));
    await waitFor(() => expect(fixture.client.triggers.rotateDeliveryToken).toHaveBeenCalledWith('ep1'));
    const panel = await screen.findByTestId('delivery-token-panel');
    expect(within(panel).getByRole('heading', { name: 'New delivery token for "ep1": copy it now' })).toBeInTheDocument();
    expect(within(panel).getByTestId('rotated-delivery-token-value')).not.toHaveTextContent(ROTATED);
    expect(panel).toHaveTextContent('Authorization: Bearer <delivery_token>');
    expect(leaks(ROTATED)).toBe(false);
    // The panel owns the page's primary action until dismissed.
    expect(primaries().map((b) => b.textContent)).toEqual(['I have saved the token, dismiss']);
    await user.click(within(panel).getByRole('button', { name: 'I have saved the token, dismiss' }));
    expect(screen.queryByTestId('delivery-token-panel')).toBeNull();
    expect(leaks(ROTATED)).toBe(false);
    expect(primaries().map((b) => b.textContent)).toEqual(['New webhook']);
  });

  it('shows the service error when rotation is refused', async () => {
    // Studio is sending an operator token to this service (signed in, token mode).
    (fixture.api.manager as { hasAuthToken?: boolean }).hasAuthToken = true;
    fixture.client.triggers.rotateDeliveryToken.mockRejectedValue(new Error('DELIVERY_TOKENS_NOT_USED'));
    const user = userEvent.setup();
    show(<TriggersView />);
    await user.click(await screen.findByRole('tab', { name: /^Webhooks/ }));
    await user.click(await screen.findByTestId('btn-rotate-delivery-token-ep1'));
    await user.click(screen.getByTestId('btn-rotate-delivery-token-ep1-confirm'));
    // The reason is shown on the webhook's own row, and the confirmation closes.
    expect(await within(screen.getByTestId('webhook-item-ep1')).findByTestId('rotate-error-ep1')).toHaveTextContent('DELIVERY_TOKENS_NOT_USED');
    expect(screen.queryByTestId('btn-rotate-delivery-token-ep1-confirm')).toBeNull();
    expect(screen.queryByTestId('delivery-token-panel')).toBeNull();
  });
});

describe('Round 5 review fixes', () => {
  const SIGNING = 'whsec_unit_signing_sentinel_0123456789abcdef';
  const register = async (user: ReturnType<typeof userEvent.setup>, id: string) => {
    await user.click(await screen.findByRole('tab', { name: /^Webhooks/ }));
    await user.click(screen.getByRole('button', { name: 'New webhook' }));
    await user.type(screen.getByTestId('input-new-webhook-id'), id);
    await waitFor(() => expect(screen.getByTestId('select-new-webhook-wfid')).not.toBeDisabled());
    await user.click(screen.getByTestId('btn-submit-register-webhook'));
    return screen.findByTestId('webhook-secret-panel');
  };

  it('M2: the signing secret is masked until Show, and Copy reads it from memory', async () => {
    fixture.client.triggers.registerWebhook.mockResolvedValue({ endpoint_id: 'ep1', workflow_id: 'wf-a', secret: SIGNING, replay_window_seconds: 300 });
    const user = userEvent.setup();
    show(<TriggersView />);
    const panel = await register(user, 'ep1');
    expect(document.body.innerHTML).not.toContain(SIGNING);
    expect(within(panel).getByText('The signing secret is hidden. Use Show to display it, or Copy to copy it.')).toBeInTheDocument();
    const writeText = vi.fn(async (_text: string) => undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    await user.click(within(panel).getByTestId('webhook-secret-copy'));
    expect(writeText).toHaveBeenCalledWith(SIGNING);
    expect(document.body.innerHTML).not.toContain(SIGNING);
    await user.click(within(panel).getByTestId('webhook-secret-reveal'));
    expect(within(panel).getByTestId('webhook-secret-value')).toHaveTextContent(SIGNING);
  });

  it('M1: deleting a webhook clears its one-time panel (secret, token and snippets)', async () => {
    fixture.client.triggers.registerWebhook.mockResolvedValue({ endpoint_id: 'ep1', workflow_id: 'wf-a', secret: SIGNING, replay_window_seconds: 300, delivery_token: 'dlv_unit_delete_case_token_0123456789' });
    const user = userEvent.setup();
    show(<TriggersView />);
    const panel = await register(user, 'ep1');
    await user.click(within(panel).getByTestId('webhook-secret-reveal'));
    expect(panel).toHaveTextContent(SIGNING);
    await user.click(screen.getByTestId('btn-delete-webhook-ep1'));
    await user.click(screen.getByTestId('btn-delete-webhook-ep1-confirm'));
    await waitFor(() => expect(fixture.client.triggers.deleteWebhook).toHaveBeenCalledWith('ep1'));
    await waitFor(() => expect(screen.queryByTestId('webhook-secret-panel')).toBeNull());
    expect(document.body.innerHTML).not.toContain(SIGNING);
    expect(screen.queryByTestId('webhook-secret-snippet')).toBeNull();
  });

  it('minor 10: with a one-time value on screen, leaving Triggers asks first; staying inside does not', async () => {
    const { confirmNavigation } = await import('../../src/app/navigationGuard');
    fixture.client.triggers.registerWebhook.mockResolvedValue({ endpoint_id: 'ep1', workflow_id: 'wf-a', secret: SIGNING, replay_window_seconds: 300 });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const user = userEvent.setup();
    show(<TriggersView />);
    expect(confirmNavigation('#/workflows')).toBe(true);
    await register(user, 'ep1');
    expect(confirmNavigation('#/triggers/wf-a')).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
    expect(confirmNavigation('#/workflows')).toBe(false);
    expect(confirm).toHaveBeenCalledWith('The signing secret or delivery token on this page will not be shown again. Leave anyway?');
    await user.click(screen.getByTestId('webhook-secret-dismiss'));
    expect(confirmNavigation('#/workflows')).toBe(true);
  });

  it('minor 1/2: the typed secret never reaches the DOM; a taken reference shows one plain error at the top', async () => {
    fixture.client.credentials.create.mockRejectedValueOnce(Object.assign(new Error('[409] CREDENTIAL_EXISTS: exists'), { code: 'CREDENTIAL_EXISTS', statusCode: 409 }));
    const user = userEvent.setup();
    show(<CredentialsView />);
    await user.click(await screen.findByRole('button', { name: 'Add credential' }));
    const dialog = screen.getByRole('dialog', { name: 'Add credential' });
    await user.type(within(dialog).getByLabelText('Reference'), 'taken_key');
    await user.type(within(dialog).getByLabelText('Secret value'), SECRET);
    expect(document.body.innerHTML).not.toContain(SECRET);
    await user.click(within(dialog).getByRole('button', { name: 'Add credential' }));
    const error = await within(dialog).findByTestId('credential-form-error');
    expect(error).toHaveTextContent('A credential with this reference already exists.');
    expect(within(dialog).getByLabelText('Reference')).toHaveAttribute('aria-invalid', 'true');
    // Only one copy of the error: the page banner behind the dialog stays quiet.
    expect(screen.getAllByText(/already exists|CREDENTIAL_EXISTS/)).toHaveLength(1);
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it('minor 5: a tool without an effect policy says "Not reported" instead of crashing', async () => {
    fixture.client.integrations.discoverMcp.mockResolvedValueOnce({ tools: [{ name: 'legacy_tool', description: 'Old', input_schema: {}, digest: 'abcdef0123456789' }] } as never);
    show(<IntegrationsView />);
    expect(await screen.findByTestId('mcp-tool-legacy_tool')).toHaveTextContent('Not reported');
  });
});

