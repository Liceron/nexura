// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { createContext, useContext, useEffect, useState, useCallback, useMemo } from 'react';
import { NexuraApiManager, ConnectionInfo } from './client';

interface ApiContextValue {
  manager: NexuraApiManager;
  connection: ConnectionInfo;
  refreshConnection: () => Promise<void>;
  changeWorkspace: (ws: string) => void;
  changeBaseUrl: (url: string) => void;
  changeAuthToken: (token: string | null) => void;
}

const ApiContext = createContext<ApiContextValue | null>(null);

export function ApiProvider({
  initialBaseUrl = '/api',
  initialWorkspace = 'default',
  children,
}: {
  initialBaseUrl?: string;
  initialWorkspace?: string;
  children: React.ReactNode;
}) {
  const manager = useMemo(() => new NexuraApiManager(initialBaseUrl, initialWorkspace), [initialBaseUrl, initialWorkspace]);

  const [connection, setConnection] = useState<ConnectionInfo>({
    status: 'connecting',
    baseUrl: initialBaseUrl,
    workspace: initialWorkspace,
    principal: 'default',
  });

  const refreshConnection = useCallback(async () => {
    setConnection((prev) => ({ ...prev, status: 'connecting', error: undefined }));
    const info = await manager.pingService();
    setConnection(info);
  }, [manager]);

  const changeWorkspace = useCallback((ws: string) => {
    manager.setWorkspace(ws);
    setConnection((prev) => ({ ...prev, workspace: ws }));
    refreshConnection();
  }, [manager, refreshConnection]);

  const changeBaseUrl = useCallback((url: string) => {
    manager.setBaseUrl(url);
    setConnection((prev) => ({ ...prev, baseUrl: url }));
    refreshConnection();
  }, [manager, refreshConnection]);

  const changeAuthToken = useCallback((token: string | null) => {
    manager.setAuthToken(token);
    refreshConnection();
  }, [manager, refreshConnection]);

  useEffect(() => {
    let mounted = true;
    manager.pingService().then((info) => {
      if (mounted) setConnection(info);
    });
    return () => {
      mounted = false;
      manager.abortAllRequests('Component unmounted');
    };
  }, [manager]);

  const value = useMemo(
    () => ({
      manager,
      connection,
      refreshConnection,
      changeWorkspace,
      changeBaseUrl,
      changeAuthToken,
    }),
    [manager, connection, refreshConnection, changeWorkspace, changeBaseUrl, changeAuthToken]
  );

  return <ApiContext.Provider value={value}>{children}</ApiContext.Provider>;
}

export function useApi(): ApiContextValue {
  const ctx = useContext(ApiContext);
  if (!ctx) {
    throw new Error('useApi must be used within an ApiProvider');
  }
  return ctx;
}
