// Test helper: a membership source like the one a closed-edition Studio extension registers.
//
// Open-source Studio has no member client (audit finding PKG-03): without a registered source
// every membership call fails and Studio is the local owner (admin). Tests that drive role
// gating (viewer, editor, a slow membership answer) or the Members tab install this source; it
// answers through the fake client's `request`, which the tests already serve, so their fakes
// and assertions stay as they were.
import {
  setMembershipSource,
  type MemberItem,
  type MemberRole,
  type MembershipInfo,
  type MembershipSource,
} from '../../src/api/membership';

const ws = (workspace: string): string => `/workspaces/${encodeURIComponent(workspace)}`;

export const requestMembershipSource: MembershipSource = {
  getMyMembership: (client, workspace) =>
    client.request<MembershipInfo>('GET', `${ws(workspace)}/my-membership`),
  listMembers: (client, workspace) => client.request<MemberItem[]>('GET', `${ws(workspace)}/members`),
  addMember: (client, workspace, principalId: string, role: MemberRole) =>
    client.request<MemberItem>('POST', `${ws(workspace)}/members`, { body: { principal_id: principalId, role } }),
  updateMemberRole: (client, workspace, principalId: string, role: MemberRole) =>
    client.request<MemberItem>('PUT', `${ws(workspace)}/members/${encodeURIComponent(principalId)}`, {
      body: { role },
    }),
  removeMember: (client, workspace, principalId: string) =>
    client.request('DELETE', `${ws(workspace)}/members/${encodeURIComponent(principalId)}`),
};

/** Use `requestMembershipSource` for this test file. */
export function installRequestMembershipSource(): void {
  setMembershipSource(requestMembershipSource);
}
