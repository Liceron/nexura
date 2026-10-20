// P25: the Shell renders #/runs/{id} through the real API client (fetch is a local fake).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { Shell } from '../../src/app/Shell';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

describe('Shell #/runs/{id} route', () => {
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
      if (url.includes('/runs/missing-run')) {
        return json({ detail: { code: 'RUN_NOT_FOUND', message: "Run 'missing-run' not found." } }, 404);
      }
      if (url.includes('/runs/foreign-run')) {
        return json(
          { detail: { code: 'CROSS_WORKSPACE_ACCESS_DENIED', message: "Cross-workspace access denied: run 'foreign-run'." } },
          403,
        );
      }
      if (url.includes('/runs')) return json({ items: [], total: 0, limit: 100, offset: 0, has_more: false });
      return json({});
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.location.hash = '';
  });

  const renderShell = () =>
    render(
      <I18nProvider>
        <ApiProvider>
          <Shell />
        </ApiProvider>
      </I18nProvider>,
    );

  it('shows a not-found notice for an unknown run (no crash)', async () => {
    window.location.hash = '#/runs/missing-run';
    renderShell();
    const notice = await screen.findByTestId('run-page-notFound', {}, { timeout: 5000 });
    expect(notice).toHaveTextContent('Run not found');
    expect(notice).toHaveTextContent('missing-run');
  });

  it('shows a denied notice for a run of another workspace', async () => {
    window.location.hash = '#/runs/foreign-run';
    renderShell();
    expect(await screen.findByTestId('run-page-denied', {}, { timeout: 5000 })).toHaveTextContent('Run access denied');
  });
});
