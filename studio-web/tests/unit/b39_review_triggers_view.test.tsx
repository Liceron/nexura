// B39 review regressions (promoted from .autopilot/b39/repros): TriggersView with a mocked
// API context (no network): viewer gating, routed workflow preselection, filtered test target.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';

const triggers = {
  listSchedules: vi.fn(),
  listWebhooks: vi.fn(),
  listJobs: vi.fn(),
  listOutbox: vi.fn(),
  registerWebhook: vi.fn(),
  createSchedule: vi.fn(),
};
const workflows = { list: vi.fn() };
const members = { getMyMembership: vi.fn() };
const api = {
  manager: { baseUrl: 'http://127.0.0.1:8765', client: {
      triggers,
      workflows,
      members,
      // EDITIONS_PLAN step 7: Studio reads the role through request(); served by the fake above.
      request: vi.fn((_method: string, path: string) =>
        path.endsWith('/my-membership') ? members.getMyMembership() : undefined,
      ),
    },
  },
  connection: { status: 'connected', workspace: 'w', principal: 'p' },
};
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

import { I18nProvider } from '../../src/i18n';
import { TriggersView } from '../../src/features/triggers/TriggersView';
import { installRequestMembershipSource } from './membershipSource';

// PKG-03: open-source Studio has no member client; the role comes from an extension's source.
installRequestMembershipSource();

const wrap = (ui: ReactNode) => render(<I18nProvider>{ui}</I18nProvider>);
const wf = (id: string, name: string) => ({
  workflow_id: id, name, revision: 1, entry_node: 'a', node_count: 1, created_at: 1, updated_at: 1,
});
const hook = (endpoint_id: string, workflow_id: string) => ({
  endpoint_id, workspace_id: 'w', workflow_id, replay_window_seconds: 300, is_active: true,
});

beforeEach(() => {
  vi.clearAllMocks();
  triggers.listSchedules.mockResolvedValue({ schedules: [] });
  triggers.listWebhooks.mockResolvedValue({ webhooks: [], total: 0 });
  triggers.listJobs.mockResolvedValue({ jobs: [], total: 0 });
  triggers.listOutbox.mockResolvedValue({ outbox: [], guarantee: 'DRY_RUN_ONLY' });
  triggers.registerWebhook.mockResolvedValue({ success: true, endpoint_id: 'x', workflow_id: 'wf-a', secret: 's' });
  triggers.createSchedule.mockResolvedValue({ success: true });
  workflows.list.mockResolvedValue({ items: [wf('wf-a', 'Alpha'), wf('wf-b', 'Beta')], total: 2, limit: 100, offset: 0, has_more: false });
  members.getMyMembership.mockResolvedValue({ workspace_id: 'w', principal_id: 'p', role: 'admin' });
});

describe('B39 review: viewers', () => {
  it('a viewer cannot submit the register-webhook or create-schedule forms', async () => {
    // The server requires edit (+run) to register; Agents/Credentials gate on the membership role.
    members.getMyMembership.mockResolvedValue({ workspace_id: 'w', principal_id: 'p', role: 'viewer' });
    const user = userEvent.setup();
    wrap(<TriggersView />);
    await waitFor(() => expect(workflows.list).toHaveBeenCalled());

    const openSchedule = await screen.findByTestId('btn-open-create-schedule');
    if (!(openSchedule as HTMLButtonElement).disabled) {
      await user.click(openSchedule);
      const submit = screen.getByTestId('btn-submit-create-schedule');
      // Wait until the picker is ready, so "disabled" is not just the loading state.
      await waitFor(() => expect(screen.getByTestId('select-new-schedule-wfid')).not.toHaveTextContent('Loading'));
      expect(submit).toBeDisabled();
      await user.click(screen.getByRole('button', { name: 'Cancel' }));
    }

    await user.click(screen.getByTestId('tab-webhooks'));
    const openWebhook = screen.getByTestId('btn-open-register-webhook');
    if (!(openWebhook as HTMLButtonElement).disabled) {
      await user.click(openWebhook);
      await waitFor(() => expect(screen.getByTestId('select-new-webhook-wfid')).not.toHaveTextContent('Loading'));
      expect(screen.getByTestId('btn-submit-register-webhook')).toBeDisabled();
    }
    expect(members.getMyMembership).toHaveBeenCalled();
  });
});

describe('B39 review: preselected workflow', () => {
  it('a routed workflow id missing from the complete workspace list is not offered or preselected', async () => {
    // has_more: false, so the list is complete: 'wf-other' is not in this workspace
    // (another workspace's id, a deleted workflow, or a typo in #/triggers/{id}).
    const user = userEvent.setup();
    wrap(<TriggersView initialWorkflowId="wf-other" />);
    await user.click(await screen.findByTestId('tab-webhooks'));
    await user.click(screen.getByTestId('btn-open-register-webhook'));
    const picker = screen.getByTestId('select-new-webhook-wfid') as HTMLSelectElement;
    await waitFor(() => expect(picker).not.toHaveTextContent('Loading'));
    const values = within(picker).queryAllByRole('option').map((o) => (o as HTMLOptionElement).value);
    expect(values).not.toContain('wf-other');
    expect(picker.value).not.toBe('wf-other');
  });

  it('opened for one workflow, the signing help and test target default to that workflow\'s webhook', async () => {
    // fetchWebhooks picks webhooks[0] of the unfiltered list, which belongs to wf-a here.
    triggers.listWebhooks.mockResolvedValue({ webhooks: [hook('ep-a', 'wf-a'), hook('ep-b', 'wf-b')], total: 2 });
    const user = userEvent.setup();
    wrap(<TriggersView initialWorkflowId="wf-b" />);
    await user.click(await screen.findByTestId('tab-webhooks'));
    expect(await screen.findByTestId('webhook-item-ep-b')).toBeInTheDocument();
    expect(screen.queryByTestId('webhook-item-ep-a')).toBeNull();
    expect((screen.getByTestId('select-test-webhook') as HTMLSelectElement).value).toBe('ep-b');
    expect(screen.getByTestId('webhook-signing-help')).toHaveTextContent('ep-b');
  });
});
