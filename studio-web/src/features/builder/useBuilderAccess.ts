// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import { useApi } from '../../api/context';
import { getMyMembership } from '../../api/membership';

/** Shared-service failures fail closed; the standalone service has a local owner. */
export function useBuilderAccess() {
  const { manager, connection } = useApi();
  const key = JSON.stringify([connection.workspace, connection.principal]);
  const [access, setAccess] = useState({ key, edit: false, run: false });
  useEffect(() => {
    let current = true;
    getMyMembership(manager.client, connection.workspace).then(member => {
      if (current) setAccess({ key, edit: member.role !== 'viewer', run: member.role !== 'viewer' && (member.profile === 'local-owner' || member.permissions.includes('run')) });
    }).catch(error => {
      const localOwner = error instanceof Error && error.name === 'MembershipUnavailableError';
      if (current) setAccess({ key, edit: localOwner, run: localOwner });
    });
    return () => { current = false; };
  }, [manager, connection.workspace, key]);
  return access.key === key ? access : { edit: false, run: false };
}
