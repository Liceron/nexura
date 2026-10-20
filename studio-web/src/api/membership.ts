// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/**
 * Workspace membership for Studio's role checks and the Members tab of the Credentials page.
 *
 * The open-source service has one principal, the local owner (role admin), and no member
 * routes, so open-source Studio has no member client (audit finding PKG-03): without a
 * registered source every call below rejects with `MembershipUnavailableError`, and every caller
 * treats that as "the local owner, admin" and hides the Members tab. The closed edition plugs its
 * member client in through a Studio extension's `membership` source (`setMembershipSource` is
 * called by `registerStudioExtensions`); no closed-edition code is part of this package.
 */
import type { NexuraClient } from '@liceron/nexura-sdk';

export type MemberRole = 'admin' | 'editor' | 'viewer';

export interface MembershipInfo {
  workspace_id: string;
  principal_id: string;
  role: MemberRole;
  permissions: string[];
  profile: 'local-owner' | 'authenticated-shared-service';
}

export interface MemberItem {
  workspace_id: string;
  principal_id: string;
  role: MemberRole;
  joined_at: number;
  updated_at: number;
}

export type RequestClient = Pick<NexuraClient, 'request'>;

/** Where Studio reads and changes workspace memberships (registered by an extension). */
export interface MembershipSource {
  getMyMembership(client: RequestClient, workspace: string): Promise<MembershipInfo>;
  listMembers(client: RequestClient, workspace: string): Promise<MemberItem[]>;
  addMember(client: RequestClient, workspace: string, principalId: string, role: MemberRole): Promise<MemberItem>;
  updateMemberRole(client: RequestClient, workspace: string, principalId: string, role: MemberRole): Promise<MemberItem>;
  removeMember(client: RequestClient, workspace: string, principalId: string): Promise<unknown>;
}

/** No membership source is registered: the open-source service has only the local owner. */
export class MembershipUnavailableError extends Error {
  readonly statusCode = 404;

  constructor() {
    super('Workspace members are not available: the open-source service has only the local owner.');
    this.name = 'MembershipUnavailableError';
  }
}

let source: MembershipSource | null = null;

/** Use `next` for every membership call (`null`: none, the open-source default). */
export function setMembershipSource(next: MembershipSource | null): void {
  source = next;
}

export function getMembershipSource(): MembershipSource | null {
  return source;
}

function current(): MembershipSource {
  if (source === null) throw new MembershipUnavailableError();
  return source;
}

export async function getMyMembership(client: RequestClient, workspace: string): Promise<MembershipInfo> {
  return current().getMyMembership(client, workspace);
}

export async function listMembers(client: RequestClient, workspace: string): Promise<MemberItem[]> {
  return current().listMembers(client, workspace);
}

export async function addMember(
  client: RequestClient,
  workspace: string,
  principalId: string,
  role: MemberRole,
): Promise<MemberItem> {
  return current().addMember(client, workspace, principalId, role);
}

export async function updateMemberRole(
  client: RequestClient,
  workspace: string,
  principalId: string,
  role: MemberRole,
): Promise<MemberItem> {
  return current().updateMemberRole(client, workspace, principalId, role);
}

export async function removeMember(client: RequestClient, workspace: string, principalId: string): Promise<unknown> {
  return current().removeMember(client, workspace, principalId);
}
