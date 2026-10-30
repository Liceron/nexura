// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentOptions, AgentRecord, AgentServerLimits, AgentSpec, AgentSummary } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { formatApiError } from '../../api/errors';

/**
 * The workspace's saved agents and what a spec may name here. List and options errors come
 * back as readable text; create, save and delete throw so the page can map field errors.
 */
export function useAgents() {
  const { manager, connection } = useApi();
  const [items, setItems] = useState<AgentSummary[]>([]);
  const [limits, setLimits] = useState<AgentServerLimits | null>(null);
  const [options, setOptions] = useState<AgentOptions | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);

  // Only the newest list request may update the list: an older, slower one would hide an
  // agent created after it was sent.
  const listRequest = useRef(0);
  const refresh = useCallback(async () => {
    const request = ++listRequest.current;
    try {
      const res = await manager.client.agents.list();
      if (request !== listRequest.current) return;
      setItems(res.items);
      setLimits(res.limits);
      setError(null);
    } catch (err: unknown) {
      if (request === listRequest.current) setError(formatApiError(err));
    } finally {
      if (request === listRequest.current) setLoading(false);
    }
  }, [manager]);

  const refreshOptions = useCallback(async () => {
    try {
      setOptions(await manager.client.agents.options());
      setOptionsError(null);
    } catch (err: unknown) {
      setOptionsError(formatApiError(err));
    }
  }, [manager]);

  useEffect(() => {
    if (connection.status !== 'connected') return;
    void refresh();
    void refreshOptions();
  }, [connection.status, connection.workspace, refresh, refreshOptions]);

  const get = useCallback((agentId: string) => manager.client.agents.get(agentId), [manager]);

  const create = useCallback(
    async (spec: AgentSpec): Promise<AgentRecord> => {
      const created = await manager.client.agents.create(spec);
      await refresh();
      return created;
    },
    [manager, refresh],
  );

  const update = useCallback(
    async (agentId: string, spec: AgentSpec, expectedRevision: number): Promise<AgentRecord> => {
      const saved = await manager.client.agents.update(agentId, spec, expectedRevision);
      await refresh();
      return saved;
    },
    [manager, refresh],
  );

  const remove = useCallback(
    async (agentId: string, expectedRevision?: number): Promise<void> => {
      await manager.client.agents.delete(agentId, { expectedRevision });
      await refresh();
    },
    [manager, refresh],
  );

  return { items, limits, options, loading, error, optionsError, refresh, get, create, update, remove };
}

/** Saved agents of the workspace for pickers (node inspector, playground, red-team). */
export function useAgentList(enabled = true) {
  const { manager, connection } = useApi();
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const latest = useRef(0);
  const load = useCallback(async () => {
    const request = ++latest.current;
    try {
      const res = await manager.client.agents.list();
      if (request !== latest.current) return;
      setAgents(res.items);
      setError(null);
    } catch (err: unknown) {
      if (request !== latest.current) return;
      // Unknown, not empty: a failed load must not make a saved agent look deleted.
      setAgents(null);
      setError(formatApiError(err));
    }
  }, [manager]);

  useEffect(() => {
    if (!enabled) return;
    void load();
  }, [enabled, load, connection.workspace]);

  return { agents, error, reload: load };
}
