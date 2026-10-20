// PKG-03: open-source Studio has no member client. Workspace membership comes from a Studio
// extension's `membership` source (the closed edition registers one); without one every call
// fails with MembershipUnavailableError and Studio is the local owner.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  getMembershipSource,
  getMyMembership,
  listMembers,
  addMember,
  updateMemberRole,
  removeMember,
  MembershipUnavailableError,
  setMembershipSource,
  type MembershipSource,
} from '../../src/api/membership';
import { registerStudioExtensions } from '../../src/app/bootstrap';
import { StudioExtensionError, StudioExtensionRegistry } from '../../src/app/extensions';

const client = { request: vi.fn() };

function fakeSource(role: 'admin' | 'viewer'): MembershipSource {
  return {
    getMyMembership: vi.fn(async (_c, workspace: string) => ({
      workspace_id: workspace, principal_id: 'p', role, permissions: [], profile: 'authenticated-shared-service' as const,
    })),
    listMembers: vi.fn(async () => []),
    addMember: vi.fn(),
    updateMemberRole: vi.fn(),
    removeMember: vi.fn(),
  };
}

afterEach(() => {
  setMembershipSource(null);
  client.request.mockReset();
});

describe('membership without a source (open-source default)', () => {
  it('rejects every call with MembershipUnavailableError and sends no request', async () => {
    expect(getMembershipSource()).toBeNull();
    const calls = [
      getMyMembership(client, 'w'),
      listMembers(client, 'w'),
      addMember(client, 'w', 'p', 'viewer'),
      updateMemberRole(client, 'w', 'p', 'editor'),
      removeMember(client, 'w', 'p'),
    ];
    for (const call of calls) {
      await expect(call).rejects.toBeInstanceOf(MembershipUnavailableError);
    }
    await expect(getMyMembership(client, 'w')).rejects.toMatchObject({ statusCode: 404 });
    expect(client.request).not.toHaveBeenCalled();
  });

  it('registering extensions without a source leaves none', () => {
    setMembershipSource(fakeSource('admin'));
    registerStudioExtensions([{ id: 'acme.reports' }], new StudioExtensionRegistry());
    expect(getMembershipSource()).toBeNull();
  });
});

describe('membership source from an extension', () => {
  it('is used for every call once registered', async () => {
    const source = fakeSource('viewer');
    const registry = registerStudioExtensions([{ id: 'acme.members', membership: source }], new StudioExtensionRegistry());
    expect(registry.membership()).toBe(source);
    expect(getMembershipSource()).toBe(source);
    await expect(getMyMembership(client, 'ws-1')).resolves.toMatchObject({ role: 'viewer', workspace_id: 'ws-1' });
    expect(source.getMyMembership).toHaveBeenCalledWith(client, 'ws-1');
    await listMembers(client, 'ws-1');
    expect(source.listMembers).toHaveBeenCalledWith(client, 'ws-1');
  });

  it('refuses a second source and an incomplete one', () => {
    const registry = new StudioExtensionRegistry();
    registry.register({ id: 'acme.members', membership: fakeSource('admin') });
    expect(() => registry.register({ id: 'other.members', membership: fakeSource('viewer') })).toThrow(StudioExtensionError);
    const partial = { getMyMembership: fakeSource('admin').getMyMembership } as unknown as MembershipSource;
    expect(() => new StudioExtensionRegistry().register({ id: 'bad.members', membership: partial })).toThrow(
      /membership needs the methods/,
    );
  });
});
