// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { createContext, useContext, useEffect, useRef, useState, useCallback, useMemo } from 'react';
import type { CredentialSource } from '../app/host';
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
  credentials,
  children,
}: {
  initialBaseUrl?: string;
  initialWorkspace?: string;
  /** The hosting page's sign-in (see `app/host.ts`); replaces the operator-token flow. */
  credentials?: CredentialSource;
  children: React.ReactNode;
}) {
  const manager = useMemo(() => {
    const created = new NexuraApiManager(initialBaseUrl, initialWorkspace);
    // An operator token signed in earlier in this tab (remote single-operator mode), only when it
    // was accepted by this exact API origin; another origin's token is deleted, never sent.
    // A hosted Studio uses only the host's credential.
    const stored = credentials ? null : readStoredOperatorToken(apiOrigin(initialBaseUrl));
    if (stored) created.setAuthToken(stored);
    return created;
  }, [initialBaseUrl, initialWorkspace, credentials]);

  const [connection, setConnection] = useState<ConnectionInfo>({
    status: 'connecting',
    baseUrl: initialBaseUrl,
    workspace: initialWorkspace,
    principal: 'default',
  });

  // A hosted Studio asks its host for a fresh credential at most once per identity until the
  // service accepts one (a forced refresh always returns a new token, so the bound cannot be
  // per token), and the reply reconnects.
  const refreshRequestedFor = useRef<string | null>(null);
  const hostIdentity = useRef<string | null>(null);
  const awaitingRefresh = useRef(false);

  // Hosted, the host owns sign-in: never offer the operator-token form.
  const show = useCallback((info: ConnectionInfo) => {
    setConnection(credentials ? { ...info, operatorTokenMode: false } : info);
  }, [credentials]);

  // A stored operator token the server refuses (rotated on restart, or wrong) is forgotten at
  // once, so it is not sent again on every request (P38 C4 m4).
  const forgetRefusedToken = useCallback((info: ConnectionInfo) => {
    if (credentials) {
      if (info.status === 'connected') refreshRequestedFor.current = null;
      if (info.credentialRefused && hostIdentity.current && refreshRequestedFor.current !== hostIdentity.current) {
        refreshRequestedFor.current = hostIdentity.current;
        awaitingRefresh.current = true;
        credentials.requestRefresh();
      }
      return;
    }
    if (info.credentialRefused && manager.hasAuthToken) {
      storeOperatorToken(null);
      manager.setAuthToken(null);
    }
  }, [manager, credentials]);

  const refreshConnection = useCallback(async () => {
    setConnection((prev) => ({ ...prev, status: 'connecting', error: undefined }));
    const info = await manager.pingService();
    forgetRefusedToken(info);
    show(info);
  }, [manager, forgetRefusedToken, show]);

  const changeWorkspace = useCallback((ws: string) => {
    manager.setWorkspace(ws);
    setConnection((prev) => ({ ...prev, workspace: ws }));
    refreshConnection();
  }, [manager, refreshConnection]);

  const changeBaseUrl = useCallback((url: string) => {
    // Hosted, the host fixes the API address its credential is meant for.
    if (credentials) return;
    // The operator token belongs to the origin it was given for; another origin never gets it.
    if (manager.setBaseUrl(url)) storeOperatorToken(null);
    setConnection((prev) => ({ ...prev, baseUrl: url }));
    refreshConnection();
  }, [manager, refreshConnection, credentials]);

  const signInWithOperatorToken = useCallback(async (token: string) => {
    // Hosted, the host's credential is the only one; a pasted token is never used or stored.
    if (credentials) return;
    // No 'connecting' state here: the sign-in form stays on screen (busy) until the answer.
    manager.setAuthToken(token);
    const info = await manager.pingService();
    // Keep only a token the server accepted, bound to the origin that accepted it.
    storeOperatorToken(info.status === 'connected' ? token : null, apiOrigin(manager.baseUrl));
    if (info.status !== 'connected') manager.setAuthToken(null);
    setConnection(info);
  }, [manager, credentials]);

  // The connection settings' token field behaves like the sign-in card: a token is kept (in
  // sessionStorage) only when the server accepts it; an empty value signs out.
  const changeAuthToken = useCallback((token: string | null) => {
    if (credentials) return;
    if (token) {
      void signInWithOperatorToken(token);
      return;
    }
    storeOperatorToken(null);
    manager.setAuthToken(token);
    refreshConnection();
  }, [manager, refreshConnection, signInWithOperatorToken, credentials]);

  useEffect(() => {
    let mounted = true;
    const connect = () => manager.pingService().then((info) => {
      if (!mounted) return;
      forgetRefusedToken(info);
      show(info);
    });
    if (!credentials) {
      void connect();
      return () => {
        mounted = false;
        manager.abortAllRequests('Component unmounted');
      };
    }
    // Hosted: stay 'connecting' until the host supplies its first credential.
    manager.hostSuppliesCredential = true;
    let identity: string | null | undefined;
    let current: string | null = null;
    const reconnect = () => {
      if (mounted) setConnection((prev) => ({ ...prev, status: 'connecting', error: undefined, workspace: manager.workspace }));
      void connect();
    };
    const unsubscribe = credentials.subscribe((next) => {
      const workspaceChanged = Boolean(next.workspace && next.workspace !== manager.workspace);
      if (workspaceChanged) manager.setWorkspace(next.workspace as string);
      const sameIdentity = identity !== undefined && identity === next.identity;
      if (sameIdentity && next.token && current) {
        // A refreshed token for the same user: later requests use it, cached data stays.
        current = next.token;
        manager.rotateAuthToken(next.token);
        // It reconnects only when it answers a refusal (or the workspace changed).
        if (awaitingRefresh.current || workspaceChanged) {
          awaitingRefresh.current = false;
          reconnect();
        }
        return;
      }
      identity = next.identity;
      current = next.token;
      hostIdentity.current = next.identity;
      awaitingRefresh.current = false;
      refreshRequestedFor.current = null;
      // Drafts and other per-principal state follow the host's user, not 'default'.
      manager.setPrincipal(next.identity ?? 'default');
      manager.setAuthToken(next.token);
      reconnect();
    });
    return () => {
      mounted = false;
      unsubscribe();
      manager.abortAllRequests('Component unmounted');
    };
  }, [manager, forgetRefusedToken, credentials, show]);

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
