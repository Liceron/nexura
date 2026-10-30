// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
/** Studio inside a host's frame: the host's credential, rotation, refusal and no chrome. */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { ApiProvider, useApi } from '../../src/api/context';
import { I18nProvider } from '../../src/i18n';
import { Shell } from '../../src/app/Shell';
import { StudioHostContext, type CredentialSource, type HostCredentials } from '../../src/app/host';

function hostSource() {
  let listener: ((credentials: HostCredentials) => void) | null = null;
  const source: CredentialSource & { send(c: HostCredentials): void; refreshes: number } = {
    refreshes: 0,
    subscribe(next) {
      listener = next;
      return () => { listener = null; };
    },
    requestRefresh() { source.refreshes += 1; },
    send(c) { act(() => listener?.(c)); },
  };
  return source;
}

function capabilities(): Response {
  return new Response(JSON.stringify({ active_profile: 'authenticated-shared-service', sdk_version: '0.4.0' }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}

function refused(): Response {
  return new Response(JSON.stringify({ error: { code: 'UNAUTHENTICATED', message: 'Authentication required.' } }), {
    status: 401, headers: { 'Content-Type': 'application/json' },
  });
}

function authHeader(call: unknown[]): string | null {
  const init = call[1] as RequestInit | undefined;
  return new Headers(init?.headers).get('Authorization');
}

function Status() {
  const { connection, manager } = useApi();
  return <div data-testid="status">{connection.status}:{manager.workspace}</div>;
}

afterEach(() => { vi.restoreAllMocks(); sessionStorage.clear(); });

describe('Studio hosted in another page', () => {
  it('waits for the host credential, then sends it as a bearer token', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => capabilities());
    const source = hostSource();
    render(<ApiProvider initialBaseUrl="https://api.example.test/api" credentials={source}><Status /></ApiProvider>);
    expect(screen.getByTestId('status').textContent).toBe('connecting:default');
    expect(fetchSpy).not.toHaveBeenCalled();

    source.send({ token: 'id-token-1', identity: 'firebase:p:alice', workspace: 'team' });
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('connected:team'));
    expect(fetchSpy.mock.calls.map(authHeader)).toContain('Bearer id-token-1');
    // The host's credential is never written to the operator-token storage.
    expect(JSON.stringify({ ...sessionStorage })).not.toContain('id-token-1');
  });

  it('rotates a refreshed token for the same identity without reconnecting', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => capabilities());
    const source = hostSource();
    render(<ApiProvider initialBaseUrl="/api" credentials={source}><Status /></ApiProvider>);
    source.send({ token: 'id-token-1', identity: 'firebase:p:alice' });
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('connected:default'));
    const pings = fetchSpy.mock.calls.length;

    source.send({ token: 'id-token-2', identity: 'firebase:p:alice' });
    expect(screen.getByTestId('status').textContent).toBe('connected:default');
    expect(fetchSpy.mock.calls.length).toBe(pings);

    // A different identity reconnects with its own credential.
    source.send({ token: 'id-token-bob', identity: 'firebase:p:bob' });
    await waitFor(() => expect(fetchSpy.mock.calls.map(authHeader)).toContain('Bearer id-token-bob'));
  });

  it('recovers when the refreshed token for the same user is accepted', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
      new Headers(init?.headers).get('Authorization') === 'Bearer expired' ? refused() : capabilities());
    const source = hostSource();
    render(<ApiProvider initialBaseUrl="/api" credentials={source}><Status /></ApiProvider>);
    source.send({ token: 'expired', identity: 'firebase:p:alice' });
    await waitFor(() => expect(source.refreshes).toBe(1));

    // The host answers with a new token for the same user: Studio reconnects with it.
    source.send({ token: 'renewed', identity: 'firebase:p:alice' });
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('connected:default'));
    expect(source.refreshes).toBe(1);
  });

  it('asks for a refresh at most once per user while the service keeps refusing', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => refused());
    const source = hostSource();
    render(<ApiProvider initialBaseUrl="/api" credentials={source}><Status /></ApiProvider>);
    source.send({ token: 'stale', identity: 'firebase:p:alice' });
    await waitFor(() => expect(source.refreshes).toBe(1));

    // Each forced refresh yields a different token; none of them asks again.
    source.send({ token: 'new-1', identity: 'firebase:p:alice' });
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('unauthorized:default'));
    source.send({ token: 'new-2', identity: 'firebase:p:alice' });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(source.refreshes).toBe(1);

    // Another user gets its own single refresh.
    source.send({ token: 'bob', identity: 'firebase:p:bob' });
    await waitFor(() => expect(source.refreshes).toBe(2));
  });

  it.each([
    ['401', refused],
    ['429', () => new Response(JSON.stringify({ error: { code: 'TOO_MANY_AUTH_FAILURES', message: 'later' } }), {
      status: 429, headers: { 'Content-Type': 'application/json' },
    })],
  ])('never offers the operator-token form when hosted (%s)', async (_code, reply) => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => reply());
    const source = hostSource();
    const { container } = render(
      <I18nProvider>
        <StudioHostContext.Provider value={{ embedded: true }}>
          <ApiProvider initialBaseUrl="/api" credentials={source}><Shell /></ApiProvider>
        </StudioHostContext.Provider>
      </I18nProvider>,
    );
    source.send({ token: 'refused', identity: 'firebase:p:alice' });
    await waitFor(() => expect(container.querySelector('[data-testid="access-refused"]')).not.toBeNull());
    expect(container.querySelector('input[type="password"]')).toBeNull();
  });

  it('ignores operator-token sign-in and address changes when hosted, and uses the host user as principal', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => capabilities());
    const source = hostSource();
    let api: ReturnType<typeof useApi> | null = null;
    function Grab() { api = useApi(); return <Status />; }
    render(<ApiProvider initialBaseUrl="/api" credentials={source}><Grab /></ApiProvider>);
    source.send({ token: 'id-token-1', identity: 'firebase:p:alice' });
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('connected:default'));
    expect(api!.connection.principal).toBe('firebase:p:alice');

    await act(async () => { await api!.signInWithOperatorToken('pasted'); });
    act(() => { api!.changeAuthToken('pasted'); api!.changeBaseUrl('https://elsewhere.example.test'); });
    expect(api!.manager.baseUrl).toBe('/api');
    expect(fetchSpy.mock.calls.map(authHeader)).not.toContain('Bearer pasted');
    expect(JSON.stringify({ ...sessionStorage })).not.toContain('pasted');
  });

  it('renders pages without the header and sidebar when embedded', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => capabilities());
    const source = hostSource();
    const { container } = render(
      <I18nProvider>
        <StudioHostContext.Provider value={{ embedded: true }}>
          <ApiProvider initialBaseUrl="/api" credentials={source}><Shell /></ApiProvider>
        </StudioHostContext.Provider>
      </I18nProvider>,
    );
    expect(container.querySelector('.studio-shell-embedded')).not.toBeNull();
    expect(container.querySelector('.studio-header')).toBeNull();
    expect(container.querySelector('nav')).toBeNull();
  });
});
