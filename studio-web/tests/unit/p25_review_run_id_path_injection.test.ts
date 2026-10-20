// P25 review regression (promoted from .autopilot/p25/repros): the run id from `#/runs/{id}` is URL-decoded by parseHash and then passed to
// runs.get(), which interpolates it into the request path without encoding. A crafted id (from a
// link, or a dataset's metadata.source_run_id) makes RunPage fetch a different API resource
// ("../workflows/wf-1" -> GET /api/v1/workflows/wf-1) or a different run ("run-1#x",
// "run-1?x=1" -> GET /api/v1/runs/run-1) and render it as the requested run.
// Real NexuraClient + stubbed fetch (no network); RunVisualizer stubbed.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { NexuraClient } from '@liceron/nexura-sdk';

const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8765', workspaceId: 'w' });
const api = {
  manager: { client },
  connection: { status: 'connected', workspace: 'w', principal: 'p' },
};
vi.mock('../../src/api/context', () => ({ useApi: () => api }));
vi.mock('../../src/features/runs/RunVisualizer', () => ({
  RunVisualizer: ({ runId }: { runId: string }) => createElement('div', { 'data-testid': 'stub-run-id' }, runId),
}));

import { I18nProvider } from '../../src/i18n';
import { RunPage } from '../../src/features/runs/RunPage';
import { parseHash, runHref } from '../../src/app/routes';

const RUN = {
  run_id: 'run-1', workflow_id: 'wf-other', spec_hash: 'h', status: 'completed', step_count: 1,
  input_state: {}, current_state: {}, created_at: 1, updated_at: 1,
};
const WORKFLOW = { workflow_id: 'wf-1', name: 'Secret workflow', status: 'completed', spec: {} };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const url = new URL(String(input));
  if (url.pathname === '/api/v1/runs/run-1') return json(RUN);
  if (url.pathname === '/api/v1/workflows/wf-1') return json(WORKFLOW);
  return json({ detail: { code: 'RUN_NOT_FOUND', message: 'Run not found.' } }, 404);
});

beforeEach(() => {
  fetchMock.mockClear();
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function openFromHash(id: string) {
  // The id travels the real route: link helper -> hash -> parseHash -> RunPage.
  const routed = parseHash(runHref(id)).runId as string;
  expect(routed).toBe(id);
  render(createElement(I18nProvider, null, createElement(RunPage, { runId: routed })));
}

describe('P25 review: run ids from the URL are one path segment', () => {
  for (const id of ['../workflows/wf-1', 'run-1#x', 'run-1?x=1']) {
    it(`#/runs/${encodeURIComponent(id)} shows "not found", not another resource`, async () => {
      openFromHash(id);
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      // Every request RunPage makes must address exactly the run named in the URL.
      for (const [input] of fetchMock.mock.calls) {
        const url = new URL(String(input));
        expect(url.pathname).toBe(`/api/v1/runs/${encodeURIComponent(id)}`);
        expect(url.search).toBe('');
      }
      expect(await screen.findByTestId('run-page-notFound')).toBeInTheDocument();
      expect(screen.queryByTestId('run-page')).toBeNull();
    });
  }
});
