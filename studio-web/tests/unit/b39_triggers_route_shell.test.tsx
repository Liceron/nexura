// B39: the Shell routes #/triggers/{workflowId} to the triggers view preselected for that
// workflow, through the real API client (fetch is a local fake, no network).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { Shell } from '../../src/app/Shell';
import { triggersHref } from '../../src/app/routes';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const wf = (id: string, name: string) => ({
  workflow_id: id, name, revision: 1, entry_node: 'a', node_count: 1, created_at: 1, updated_at: 1,
});

describe('Shell #/triggers/{workflowId} route', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/capabilities')) {
        return json({
          sdk_version: '0.4.0',
          active_profile: 'local-owner',
          supported_profiles: ['local-owner'],
          capabilities: { modules: true, validation: true, downloadable_export: true, live_events: true },
        });
      }
      if (url.includes('/triggers/schedules')) return json({ schedules: [], total: 0 });
      if (url.includes('/triggers/webhooks')) return json({ webhooks: [], total: 0 });
      if (url.includes('/triggers/jobs')) return json({ jobs: [], total: 0 });
      if (url.includes('/triggers/outbox')) return json({ outbox: [], guarantee: 'DRY_RUN_ONLY' });
      if (url.includes('/workflows')) {
        return json({ items: [wf('wf-a', 'Alpha'), wf('wf/b', 'Beta')], total: 2, limit: 100, offset: 0, has_more: false });
      }
      if (url.includes('/approvals') || url.includes('/runs')) {
        return json({ items: [], total: 0, limit: 100, offset: 0, has_more: false });
      }
      return json({});
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.location.hash = '';
  });

  it('opens the triggers view with the routed workflow preselected', async () => {
    window.location.hash = triggersHref('wf/b');
    render(
      <I18nProvider>
        <ApiProvider>
          <Shell />
        </ApiProvider>
      </I18nProvider>,
    );
    expect(await screen.findByTestId('triggers-workflow-filter', {}, { timeout: 5000 })).toHaveTextContent('wf/b');
    const user = userEvent.setup();
    await user.click(screen.getByTestId('tab-webhooks'));
    await user.click(screen.getByTestId('btn-open-register-webhook'));
    const picker = screen.getByTestId('select-new-webhook-wfid') as HTMLSelectElement;
    await waitFor(() => expect(picker).not.toBeDisabled());
    expect(picker.value).toBe('wf/b');
  });
});
