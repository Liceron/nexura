// P38 C4 review: Studio repros (from .autopilot/p38/c4_repros). Each asserts the secure behaviour; all three failed before the review fixes.
import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider, useApi } from '../../src/api/context';
import { Shell } from '../../src/app/Shell';
import { OPERATOR_TOKEN_ORIGIN_STORAGE_KEY, OPERATOR_TOKEN_STORAGE_KEY, storeOperatorToken } from '../../src/api/operatorToken';

const GOOD = 'Qx7-v2Lr_9mZpA4tKc8NfWj3bYsH6uDe1oRgTiVnXqE';
const ROTATED = 'Rr1-NEW_TOKEN_after_restart_0123456789abcdef';
let serverToken = GOOD;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const authOf = (init?: RequestInit) =>
  init?.headers ? (init.headers as Record<string, string>).Authorization : undefined;

let fetchSpy: MockInstance<typeof fetch>;

beforeEach(() => {
  serverToken = GOOD;
  window.sessionStorage.clear();
  fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input);
    if (authOf(init) !== `Bearer ${serverToken}`) {
      return json({ detail: { code: 'UNAUTHENTICATED', message: 'Authentication required: send the operator token as Authorization: Bearer <token>.' } }, 401) // the token provider's real message;
    }
    if (url.includes('/capabilities')) {
      return json({ sdk_version: '0.4.0', active_profile: 'local-owner', supported_profiles: ['local-owner'], capabilities: {} });
    }
    if (url.includes('/local-session')) return json({ detail: { code: 'SESSION_TOKEN_NOT_USED' } }, 404);
    return json({ items: [], total: 0, limit: 100, offset: 0, has_more: false });
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

const renderShell = () =>
  render(<I18nProvider><ApiProvider><Shell /></ApiProvider></I18nProvider>);

describe('C4 review repros (Studio)', () => {
  it('a signed-in Studio sends no request without the bearer (fetchSessionToken)', async () => {
    storeOperatorToken(GOOD, window.location.origin); // signed in earlier on this origin
    renderShell();
    await waitFor(() => expect(screen.queryByLabelText('Operator token')).not.toBeInTheDocument(), { timeout: 5000 });
    await waitFor(() => expect(fetchSpy.mock.calls.some(([u]) => String(u).includes('/local-session'))).toBe(true));
    const bare = fetchSpy.mock.calls.filter(([, init]) => !authOf(init as RequestInit));
    // Each one is a counted failure and a WARNING log line on the server.
    expect(bare.map(([u]) => String(u))).toEqual([]);
  });

  it('a stored token the server now refuses (rotated) is cleared from sessionStorage', async () => {
    storeOperatorToken(GOOD, window.location.origin); // signed in earlier on this origin
    serverToken = ROTATED; // service restarted with a new token
    renderShell();
    await screen.findByLabelText('Operator token', {}, { timeout: 5000 });
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('changing the server URL does not send the operator token to another origin', async () => {
    storeOperatorToken(GOOD, window.location.origin); // signed in earlier on this origin
    let api: ReturnType<typeof useApi> | null = null;
    const Grab = () => { api = useApi(); return null; };
    render(<I18nProvider><ApiProvider><Grab /></ApiProvider></I18nProvider>);
    await waitFor(() => expect(api).not.toBeNull());
    await act(async () => { api!.changeBaseUrl('https://attacker.example'); });
    await waitFor(() => expect(fetchSpy.mock.calls.some(([u]) => String(u).startsWith('https://attacker.example'))).toBe(true));
    const leaked = fetchSpy.mock.calls.filter(
      ([u, init]) => String(u).startsWith('https://attacker.example') && authOf(init as RequestInit) === `Bearer ${GOOD}`,
    );
    expect(leaked).toHaveLength(0);
  });

  // Round 5 review B1 (repro moved from .autopilot/p36/round5_repros/r5_token_origin.test.tsx).
  it('a token accepted by https://remote.example is not sent to /api after a reload', async () => {
    const REMOTE = 'remote-operator-token-sentinel-0123456789';
    fetchSpy.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.startsWith('https://remote.example') && authOf(init) !== `Bearer ${REMOTE}`) {
        return json({ detail: { code: 'UNAUTHENTICATED' } }, 401);
      }
      if (url.includes('/capabilities')) return json({ sdk_version: '0.4.0', active_profile: 'local-owner', supported_profiles: ['local-owner'], capabilities: {} });
      if (url.includes('/local-session')) return json({ detail: { code: 'SESSION_TOKEN_NOT_USED' } }, 404);
      return json({ items: [] });
    });
    let api: ReturnType<typeof useApi> | null = null;
    const Grab = () => { api = useApi(); return null; };
    const first = render(<I18nProvider><ApiProvider><Grab /></ApiProvider></I18nProvider>);
    await waitFor(() => expect(api).not.toBeNull());
    await act(async () => { api!.changeBaseUrl('https://remote.example/api'); });
    await act(async () => { await api!.signInWithOperatorToken(REMOTE); });
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBe(REMOTE);
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_ORIGIN_STORAGE_KEY)).toBe('https://remote.example');
    first.unmount();

    // Reload: the base URL is not persisted, so Studio starts at '/api' again (this page's origin).
    fetchSpy.mockClear();
    render(<I18nProvider><ApiProvider><Grab /></ApiProvider></I18nProvider>);
    await waitFor(() => expect(fetchSpy.mock.calls.length).toBeGreaterThan(0));
    const leaked = fetchSpy.mock.calls.filter(([u, init]) => !String(u).startsWith('https://remote.example') && authOf(init as RequestInit) === `Bearer ${REMOTE}`);
    expect(leaked.map(([u]) => String(u))).toEqual([]);
    // The other origin's token is deleted, not kept for later.
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('a stored token without a recorded origin is never sent', async () => {
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, GOOD);
    renderShell();
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    expect(fetchSpy.mock.calls.filter(([, init]) => authOf(init as RequestInit) === `Bearer ${GOOD}`)).toHaveLength(0);
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
  });

  // Round 5 review M3: only the operator-token provider's refusal asks for the operator token.
  it('a 403 shows the service error and the real status, not the token prompt', async () => {
    fetchSpy.mockImplementation(async () => json({ detail: { code: 'FORBIDDEN', message: 'Your role cannot read this workspace.' } }, 403));
    renderShell();
    expect(await screen.findByTestId('access-refused-error', {}, { timeout: 5000 })).toHaveTextContent('Your role cannot read this workspace.');
    expect(screen.getByRole('heading', { level: 1, name: 'Access refused' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Operator token')).toBeNull();
    expect(screen.getByLabelText(/^Service connection status:/)).toHaveTextContent('Forbidden (403)');
  });

  it('a shared-service 401 (no operator token involved) shows its own message', async () => {
    fetchSpy.mockImplementation(async () => json({ detail: { code: 'UNAUTHENTICATED', message: 'An authenticated principal is required.' } }, 401));
    renderShell();
    expect(await screen.findByTestId('access-refused-error', {}, { timeout: 5000 })).toHaveTextContent('An authenticated principal is required.');
    expect(screen.queryByLabelText('Operator token')).toBeNull();
  });

  it('the operator-token provider 401 asks for the operator token', async () => {
    fetchSpy.mockImplementation(async () => json({ detail: { code: 'UNAUTHENTICATED', message: 'Authentication required: send the operator token as Authorization: Bearer <token>.' } }, 401));
    renderShell();
    expect(await screen.findByLabelText('Operator token', {}, { timeout: 5000 })).toBeInTheDocument();
  });
});
