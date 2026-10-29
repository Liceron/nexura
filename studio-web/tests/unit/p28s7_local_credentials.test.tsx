import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const api = vi.hoisted(() => ({
  connection: { workspace: 'default', profile: 'local-owner', principal: 'forged-user' },
  manager: {
    clearCache: vi.fn(),
    client: {
      request: vi.fn(),
      credentials: { list: vi.fn(async () => ({ items: [] })), create: vi.fn() },
      modules: { list: vi.fn(async () => ({ items: [] })) },
    },
  },
}));
vi.mock('../../src/api/context', () => ({ useApi: () => api }));
vi.mock('../../src/i18n', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/i18n')>();
  return { ...actual, useI18n: () => ({ t: actual.englishT }), useT: () => actual.englishT };
});

import { CredentialsView } from '../../src/features/credentials/CredentialsView';
import { setMembershipSource } from '../../src/api/membership';
import { requestMembershipSource } from './membershipSource';

describe('P28 step 7 local credentials', () => {
  beforeEach(() => {
    api.connection.workspace = 'default';
    api.manager.client.request.mockReset();
    api.manager.client.credentials.list.mockClear();
  });
  afterEach(() => setMembershipSource(null));

  it('keeps local-owner admin credentials and removes members UI on OSS 404', async () => {
    // PKG-03: open-source Studio has no member client and sends no member request at all; with
    // no membership source registered it is the local owner (admin) without a Members tab.
    api.manager.client.request.mockRejectedValue({ statusCode: 404 });
    render(<CredentialsView />);
    await waitFor(() => expect(api.manager.client.credentials.list).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId('current-role-badge').textContent).toContain('Admin'));
    expect(api.manager.client.request).not.toHaveBeenCalled();
    expect(screen.getByTestId('current-role-badge').textContent).toContain('Admin');
    expect(screen.getByTestId('credentials-view').textContent).toContain('Local Owner');
    expect(screen.queryByTestId('tab-members')).toBeNull();
    expect(screen.queryByTestId('new-member-id-input')).toBeNull();
    expect(screen.queryByTestId('add-member-button')).toBeNull();
    expect(screen.getByTestId('tab-credentials')).toBeTruthy();
  });

  it('preserves extension members UI, then clears it when the workspace route is absent', async () => {
    // The closed edition registers a membership source; this one answers through request().
    setMembershipSource(requestMembershipSource);
    api.manager.client.request.mockImplementation(async (_method: string, path: string) => {
      if (path.includes('/other/')) throw { statusCode: 404 };
      return path.endsWith('/members') ? [] : {
        workspace_id: 'default', principal_id: 'local-owner', role: 'admin',
        permissions: ['admin', 'edit', 'run'], profile: 'local-owner',
      };
    });
    const view = render(<CredentialsView />);
    fireEvent.click(await screen.findByTestId('tab-members'));
    expect(screen.getByTestId('add-member-button')).toBeTruthy();
    api.connection.workspace = 'other';
    view.rerender(<CredentialsView />);
    await waitFor(() => expect(screen.queryByTestId('tab-members')).toBeNull());
    expect(screen.queryByTestId('add-member-button')).toBeNull();
    expect(screen.getByTestId('current-role-badge').textContent).toContain('Admin');
  });

  it('does not show an old verification time on a revoked credential', async () => {
    api.manager.client.request.mockRejectedValue({ statusCode: 404 });
    const base = { workspace_id: 'default', credential_type: 'api_key', provider: 'gemini', description: '', created_at: 1, last_verified_at: 1_800_000_000 };
    api.manager.client.credentials.list.mockResolvedValueOnce({ items: [
      { ...base, credential_id: 'old-key', status: 'revoked' },
      { ...base, credential_id: 'live-key', status: 'active' },
    ] } as never);
    render(<CredentialsView />);
    expect((await screen.findByTestId('cred-row-old-key')).textContent).not.toContain('Verified');
    expect(screen.getByTestId('verification-none-old-key').textContent).toBe('—');
    expect(screen.getByTestId('cred-row-live-key').textContent).toContain('Verified');
  });

  it('says which characters a credential ID may use before sending it', async () => {
    api.manager.client.request.mockRejectedValue({ statusCode: 404 });
    render(<CredentialsView />);
    fireEvent.click(await screen.findByTestId('register-credential-button'));
    fireEvent.change(screen.getByTestId('cred-id-input'), { target: { value: 'my key!' } });
    fireEvent.change(screen.getByTestId('cred-secret-input'), { target: { value: 'not-a-real-secret' } });
    fireEvent.submit(screen.getByTestId('register-credential-form'));
    expect((await screen.findByTestId('credential-form-error')).textContent).toContain('letters, digits');
    expect(screen.getByTestId('cred-id-input')).toHaveAttribute('aria-invalid', 'true');
    expect(api.manager.client.credentials.create).not.toHaveBeenCalled();
  });
});
