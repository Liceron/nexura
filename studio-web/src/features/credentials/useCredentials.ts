// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useState, useEffect, useCallback } from 'react';
import { useLatestT } from '../../i18n';
import { useApi } from '../../api/context';
import type {
  CredentialReferenceItem,
  WorkspaceMemberInfo,
  CurrentMembershipState,
  VerificationResult,
  WorkspaceRole,
  CreateCredentialOptions,
} from './types';
import * as membership from '../../api/membership';

export function useCredentials() {
  const t = useLatestT();
  const { manager, connection } = useApi();
  const [credentials, setCredentials] = useState<CredentialReferenceItem[]>([]);
  const [members, setMembers] = useState<WorkspaceMemberInfo[]>([]);
  const [membershipAvailability, setMembershipAvailability] = useState({ workspace: '', available: false });
  const [myMembership, setMyMembership] = useState<CurrentMembershipState>({
    workspace_id: connection.workspace,
    principal_id: connection.principal || 'default',
    role: 'admin',
    permissions: ['admin', 'approve', 'edit', 'export_credentials', 'run'],
    profile: (connection.profile === 'authenticated-shared-service' ? 'authenticated-shared-service' : 'local-owner'),
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verificationMap, setVerificationMap] = useState<Record<string, VerificationResult>>({});

  const loadCredentials = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await manager.client.credentials.list({ limit: 100 });
      setCredentials((res.items as unknown as CredentialReferenceItem[]) || []);
    } catch (err: any) {
      setError(err?.message || t('validationMessages.credentialLoad'));
    } finally {
      setLoading(false);
    }
  }, [manager, t]);

  const loadMembers = useCallback(async () => {
    try {
      const res = await membership.listMembers(manager.client, connection.workspace);
      setMembers(res as unknown as WorkspaceMemberInfo[]);
      setMembershipAvailability({ workspace: connection.workspace, available: true });
    } catch {
      // OSS has no members route (404). Clear the previous workspace's roster and UI.
      setMembers([]);
      setMembershipAvailability({ workspace: connection.workspace, available: false });
    }
  }, [manager, connection.workspace]);

  const loadMyMembership = useCallback(async () => {
    try {
      const res = await membership.getMyMembership(manager.client, connection.workspace);
      setMyMembership(res as unknown as CurrentMembershipState);
    } catch {
      // Fallback for local-owner default
      setMyMembership({
        workspace_id: connection.workspace,
        principal_id: connection.profile === 'authenticated-shared-service' ? connection.principal || 'local-owner' : 'local-owner',
        role: 'admin',
        permissions: ['admin', 'approve', 'edit', 'export_credentials', 'run'],
        profile: connection.profile === 'authenticated-shared-service' ? 'authenticated-shared-service' : 'local-owner',
      });
    }
  }, [manager, connection.workspace, connection.principal, connection.profile]);

  useEffect(() => {
    manager.clearCache();
    loadCredentials();
    loadMembers();
    loadMyMembership();
  }, [connection.workspace, loadCredentials, loadMembers, loadMyMembership, manager]);

  const createCredential = useCallback(
    async (payload: CreateCredentialOptions): Promise<CredentialReferenceItem> => {
      setError(null);
      try {
        const created = await manager.client.credentials.create(payload);
        await loadCredentials();
        return created as unknown as CredentialReferenceItem;
      } catch (err: any) {
        setError(err?.message || t('validationMessages.credentialCreate'));
        throw err;
      }
    },
    [manager, loadCredentials, t]
  );

  const deleteCredential = useCallback(
    async (credentialId: string): Promise<void> => {
      setError(null);
      try {
        await manager.client.credentials.delete(credentialId);
        await loadCredentials();
      } catch (err: any) {
        setError(err?.message || t('validationMessages.credentialDelete'));
        throw err;
      }
    },
    [manager, loadCredentials, t]
  );

  const verifyCredential = useCallback(
    async (credentialId: string): Promise<VerificationResult> => {
      try {
        const res = await manager.client.credentials.verify(credentialId);
        const result: VerificationResult = {
          credential_id: res.credential_id,
          status: res.status,
          message: res.message,
          checked_at: res.checked_at,
        };
        setVerificationMap((prev) => ({ ...prev, [credentialId]: result }));
        // Refresh to update last_verified_at in list
        await loadCredentials();
        return result;
      } catch (err: any) {
        const failureResult: VerificationResult = {
          credential_id: credentialId,
          status: 'invalid',
          message: err?.message || t('validationMessages.credentialVerify'),
          checked_at: Date.now() / 1000,
        };
        setVerificationMap((prev) => ({ ...prev, [credentialId]: failureResult }));
        return failureResult;
      }
    },
    [manager, loadCredentials, t]
  );

  const revokeCredential = useCallback(
    async (credentialId: string): Promise<void> => {
      setError(null);
      try {
        await manager.client.credentials.revoke(credentialId);
        await loadCredentials();
      } catch (err: any) {
        setError(err?.message || t('validationMessages.credentialRevoke'));
        throw err;
      }
    },
    [manager, loadCredentials, t]
  );

  const addMember = useCallback(
    async (principalId: string, role: WorkspaceRole): Promise<void> => {
      setError(null);
      try {
        await membership.addMember(manager.client, connection.workspace, principalId, role);
        await loadMembers();
      } catch (err: any) {
        setError(err?.message || t('validationMessages.memberAdd'));
        throw err;
      }
    },
    [manager, connection.workspace, loadMembers, t]
  );

  const updateMemberRole = useCallback(
    async (principalId: string, role: WorkspaceRole): Promise<void> => {
      setError(null);
      try {
        await membership.updateMemberRole(manager.client, connection.workspace, principalId, role);
        await loadMembers();
      } catch (err: any) {
        setError(err?.message || t('validationMessages.memberRole'));
        throw err;
      }
    },
    [manager, connection.workspace, loadMembers, t]
  );

  const removeMember = useCallback(
    async (principalId: string): Promise<void> => {
      setError(null);
      try {
        await membership.removeMember(manager.client, connection.workspace, principalId);
        await loadMembers();
      } catch (err: any) {
        setError(err?.message || t('validationMessages.memberRemove'));
        throw err;
      }
    },
    [manager, connection.workspace, loadMembers, t]
  );

  return {
    credentials,
    members,
    membersAvailable: membershipAvailability.workspace === connection.workspace && membershipAvailability.available,
    myMembership,
    loading,
    error,
    verificationMap,
    loadCredentials,
    loadMembers,
    loadMyMembership,
    createCredential,
    deleteCredential,
    verifyCredential,
    revokeCredential,
    addMember,
    updateMemberRole,
    removeMember,
  };
}
