// B39 review regression (promoted from .autopilot/b39/repros): the "Show all triggers" link inside the triggers view remounts the view
// (Shell keys TriggersView on the routed workflow id), so the one-time secret is lost
// although the user never left the triggers view. Real API client, local fake fetch.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { Shell } from '../../src/app/Shell';
import { triggersHref } from '../../src/app/routes';

const SECRET = 'b39-review-secret-0123456789abcdef0123456789';
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const wf = (id: string, name: string) => ({
  workflow_id: id, name, revision: 1, entry_node: 'a', node_count: 1, created_at: 1, updated_at: 1,
});

describe('B39 review: Show all triggers keeps the secret panel', () => {
  let hooks: Array<Record<string, unknown>> = [];
  beforeEach(() => {
    hooks = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input instanceof Request ? input.url : input);
      const method = (input instanceof Request ? input.method : init?.method) ?? 'GET';
      if (url.includes('/capabilities')) {
        return json({
          sdk_version: '0.4.0',
          active_profile: 'local-owner',
          supported_profiles: ['local-owner'],
          capabilities: { modules: true, validation: true, downloadable_export: true, live_events: true },
        });
      }
      if (url.includes('/triggers/webhooks') && method === 'POST') {
        hooks = [{ endpoint_id: 'orders', workspace_id: 'default', workflow_id: 'wf-a', replay_window_seconds: 300, is_active: true }];
        return json({ success: true, endpoint_id: 'orders', workspace_id: 'default', workflow_id: 'wf-a', secret: SECRET, replay_window_seconds: 300, signature_scheme: 'v2' });
      }
      if (url.includes('/triggers/schedules')) return json({ schedules: [], total: 0 });
      if (url.includes('/triggers/webhooks')) return json({ webhooks: hooks, total: hooks.length });
      if (url.includes('/triggers/jobs')) return json({ jobs: [], total: 0 });
      if (url.includes('/triggers/outbox')) return json({ outbox: [], guarantee: 'DRY_RUN_ONLY' });
      if (url.includes('/workflows')) {
        return json({ items: [wf('wf-a', 'Alpha')], total: 1, limit: 100, offset: 0, has_more: false });
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

  it('the secret registered from #/triggers/{id} survives the in-view Show all link', async () => {
    window.location.hash = triggersHref('wf-a');
    render(
      <I18nProvider>
        <ApiProvider>
          <Shell />
        </ApiProvider>
      </I18nProvider>,
    );
    const user = userEvent.setup();
    await screen.findByTestId('triggers-workflow-filter', {}, { timeout: 5000 });
    await user.click(screen.getByTestId('tab-webhooks'));
    await user.click(screen.getByTestId('btn-open-register-webhook'));
    await user.type(screen.getByTestId('input-new-webhook-id'), 'orders');
    await waitFor(() => expect(screen.getByTestId('select-new-webhook-wfid')).not.toBeDisabled());
    await user.click(screen.getByTestId('btn-submit-register-webhook'));
    await user.click(await screen.findByTestId('webhook-secret-reveal'));
    expect(await screen.findByTestId('webhook-secret-value')).toHaveTextContent(SECRET);

    // What the link's href="#/triggers" does.
    expect(screen.getByTestId('triggers-show-all')).toHaveAttribute('href', '#/triggers');
    await act(async () => {
      window.location.hash = '#/triggers';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    await waitFor(() => expect(screen.queryByTestId('triggers-workflow-filter')).toBeNull());
    expect(screen.getByTestId('webhook-secret-value')).toHaveTextContent(SECRET);
  });
});
