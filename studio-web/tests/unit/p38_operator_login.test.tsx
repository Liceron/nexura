// P38 C4: Studio asks for the operator token when the service runs in remote single-operator
// mode, sends it as a bearer and keeps it in sessionStorage only (fetch is a local fake).
import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { Shell } from '../../src/app/Shell';
import { OPERATOR_TOKEN_STORAGE_KEY, storeOperatorToken } from '../../src/api/operatorToken';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';

const GOOD = 'Qx7-v2Lr_9mZpA4tKc8NfWj3bYsH6uDe1oRgTiVnXqE';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const authHeader = (init?: RequestInit): string | undefined => {
  const headers = init?.headers;
  if (!headers) return undefined;
  if (headers instanceof Headers) return headers.get('Authorization') ?? undefined;
  return (headers as Record<string, string>).Authorization;
};

let mode: 'token' | 'rate-limited' = 'token';
let fetchSpy: MockInstance<typeof fetch>;

describe('Operator token sign-in (remote single-operator mode)', () => {
  beforeEach(() => {
    mode = 'token';
    window.sessionStorage.clear();
    window.localStorage.clear();
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (mode === 'rate-limited') {
        return json({ detail: { code: 'TOO_MANY_AUTH_FAILURES', message: 'Too many failed authentication attempts.' } }, 429);
      }
      if (authHeader(init) !== `Bearer ${GOOD}`) {
        return json({ detail: { code: 'UNAUTHENTICATED', message: 'Authentication required: send the operator token as Authorization: Bearer <token>.' } }, 401) // the token provider's real message;
      }
      if (url.includes('/capabilities')) {
        return json({
          sdk_version: '0.4.0',
          active_profile: 'local-owner',
          supported_profiles: ['local-owner'],
          capabilities: { modules: true, validation: true },
        });
      }
      if (url.includes('/local-session')) return json({ detail: { code: 'SESSION_TOKEN_NOT_USED' } }, 404);
      return json({ items: [], total: 0, limit: 100, offset: 0, has_more: false });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
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

  it('signs in with the operator token and keeps it in sessionStorage only', async () => {
    const user = userEvent.setup();
    renderShell();
    const field = await screen.findByLabelText('Operator token', {}, { timeout: 5000 });
    expect(field).toHaveAttribute('type', 'password');

    await user.type(field, GOOD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => expect(screen.queryByLabelText('Operator token')).not.toBeInTheDocument(), { timeout: 5000 });
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBe(GOOD);
    expect(JSON.stringify({ ...window.localStorage })).not.toContain(GOOD);
    expect(window.location.href).not.toContain(GOOD);
    const sent = fetchSpy.mock.calls.filter(([, init]) => authHeader(init as RequestInit) === `Bearer ${GOOD}`);
    expect(sent.length).toBeGreaterThan(0);
  });

  it('reports a refused token and does not keep it', async () => {
    const user = userEvent.setup();
    renderShell();
    const field = await screen.findByLabelText('Operator token', {}, { timeout: 5000 });
    await user.type(field, 'not-the-right-token-but-long-enough-0123456789');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByTestId('operator-login-error')).toHaveTextContent('did not accept');
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByLabelText('Operator token')).toHaveValue('');
  });

  it('uses a token stored earlier in this tab', async () => {
    // Signed in earlier in this tab on this same API origin (the token is bound to it).
    storeOperatorToken(GOOD, window.location.origin);
    renderShell();
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const first = fetchSpy.mock.calls[0];
    expect(authHeader(first[1] as RequestInit)).toBe(`Bearer ${GOOD}`);
    await waitFor(() => expect(screen.queryByLabelText('Operator token')).not.toBeInTheDocument(), { timeout: 5000 });
  });

  it('explains a rate-limited address', async () => {
    mode = 'rate-limited';
    renderShell();
    expect(await screen.findByTestId('operator-login-error', {}, { timeout: 5000 })).toHaveTextContent('Too many failed attempts');
  });

  it('has the sign-in strings in English and Hindi', () => {
    const keys = Object.keys(en.operatorLogin).sort();
    expect(Object.keys(hi.operatorLogin).sort()).toEqual(keys);
    for (const key of keys) expect((hi.operatorLogin as Record<string, string>)[key]).toBeTruthy();
  });
});
