// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { createContext, useContext, useEffect, useState, useCallback, useMemo } from 'react';
import { NexuraApiManager, ConnectionInfo, apiOrigin } from './client';
import { readStoredOperatorToken, storeOperatorToken } from './operatorToken';

interface ApiContextValue {
  manager: NexuraApiManager;
  connection: ConnectionInfo;
  refreshConnection: () => Promise<void>;
  changeWorkspace: (ws: string) => void;
  changeBaseUrl: (url: string) => void;
  changeAuthToken: (token: string | null) => void;
  /** Remote single-operator mode: use `token` and keep it in sessionStorage for this tab. */
  signInWithOperatorToken: (token: string) => Promise<void>;
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
  const manager = useMemo(() => {
    const created = new NexuraApiManager(initialBaseUrl, initialWorkspace);
    // An operator token signed in earlier in this tab (remote single-operator mode), only when it
    // was accepted by this exact API origin; another origin's token is deleted, never sent.
    const stored = readStoredOperatorToken(apiOrigin(initialBaseUrl));
    if (stored) created.setAuthToken(stored);
    return created;
  }, [initialBaseUrl, initialWorkspace]);

  const [connection, setConnection] = useState<ConnectionInfo>({
    status: 'connecting',
    baseUrl: initialBaseUrl,
    workspace: initialWorkspace,
    principal: 'default',
  });

  // A stored operator token the server refuses (rotated on restart, or wrong) is forgotten at
  // once, so it is not sent again on every request (P38 C4 m4).
  const forgetRefusedToken = useCallback((info: ConnectionInfo) => {
    if (info.credentialRefused && manager.hasAuthToken) {
      storeOperatorToken(null);
      manager.setAuthToken(null);
    }
  }, [manager]);

  const refreshConnection = useCallback(async () => {
    setConnection((prev) => ({ ...prev, status: 'connecting', error: undefined }));
    const info = await manager.pingService();
    forgetRefusedToken(info);
    setConnection(info);
  }, [manager, forgetRefusedToken]);

  const changeWorkspace = useCallback((ws: string) => {
    manager.setWorkspace(ws);
    setConnection((prev) => ({ ...prev, workspace: ws }));
    refreshConnection();
  }, [manager, refreshConnection]);

  const changeBaseUrl = useCallback((url: string) => {
    // The operator token belongs to the origin it was given for; another origin never gets it.
    if (manager.setBaseUrl(url)) storeOperatorToken(null);
    setConnection((prev) => ({ ...prev, baseUrl: url }));
    refreshConnection();
  }, [manager, refreshConnection]);

  const signInWithOperatorToken = useCallback(async (token: string) => {
    // No 'connecting' state here: the sign-in form stays on screen (busy) until the answer.
    manager.setAuthToken(token);
    const info = await manager.pingService();
    // Keep only a token the server accepted, bound to the origin that accepted it.
    storeOperatorToken(info.status === 'connected' ? token : null, apiOrigin(manager.baseUrl));
    if (info.status !== 'connected') manager.setAuthToken(null);
    setConnection(info);
  }, [manager]);

  // The connection settings' token field behaves like the sign-in card: a token is kept (in
  // sessionStorage) only when the server accepts it; an empty value signs out.
  const changeAuthToken = useCallback((token: string | null) => {
    if (token) {
      void signInWithOperatorToken(token);
      return;
    }
    storeOperatorToken(null);
    manager.setAuthToken(token);
    refreshConnection();
  }, [manager, refreshConnection, signInWithOperatorToken]);

  useEffect(() => {
    let mounted = true;
    manager.pingService().then((info) => {
      if (!mounted) return;
      forgetRefusedToken(info);
      setConnection(info);
    });
    return () => {
      mounted = false;
      manager.abortAllRequests('Component unmounted');
    };
  }, [manager, forgetRefusedToken]);

  const value = useMemo(
    () => ({
      manager,
      connection,
      refreshConnection,
      changeWorkspace,
      changeBaseUrl,
      changeAuthToken,
      signInWithOperatorToken,
    }),
    [manager, connection, refreshConnection, changeWorkspace, changeBaseUrl, changeAuthToken, signInWithOperatorToken]
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
