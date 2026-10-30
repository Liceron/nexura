// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useState } from 'react';
import { useApi } from '../../api/context';
import type { SkillList, SkillSummary, SkillVersions, SkillVersionView } from './types';
import { formatApiError } from '../../api/errors';

/** List skills (optionally by label) and act on one. Errors come back as readable text. */
export function useSkills(label: string) {
  const { manager, connection } = useApi();
  const [items, setItems] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await manager.client.request<SkillList>('GET', '/skills', {
        params: { label: label || undefined },
      });
      setItems(res.items);
      setError(null);
    } catch (err: unknown) {
      setError(formatApiError(err));
    } finally {
      setLoading(false);
    }
  }, [manager, label]);

  useEffect(() => {
    if (connection.status !== 'connected') return;
    manager.clearCache();
    refresh();
  }, [manager, connection.status, connection.workspace, refresh]);

  const versions = useCallback(
    (name: string) =>
      manager.client.request<SkillVersions>('GET', `/skills/${encodeURIComponent(name)}/versions`),
    [manager],
  );

  const create = useCallback(
    async (markdown: string): Promise<SkillVersionView> => {
      const created = await manager.client.request<SkillVersionView>('POST', '/skills', {
        body: { markdown },
      });
      await refresh();
      return created;
    },
    [manager, refresh],
  );

  const setLabel = useCallback(
    async (name: string, labelName: string, version: number): Promise<void> => {
      await manager.client.request(
        'PUT',
        `/skills/${encodeURIComponent(name)}/labels/${encodeURIComponent(labelName)}`,
        { body: { version } },
      );
      await refresh();
    },
    [manager, refresh],
  );

  const remove = useCallback(
    async (name: string): Promise<void> => {
      await manager.client.request('DELETE', `/skills/${encodeURIComponent(name)}`);
      await refresh();
    },
    [manager, refresh],
  );

  return { items, loading, error, refresh, versions, create, setLabel, remove };
}
