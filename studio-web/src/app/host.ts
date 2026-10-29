// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/**
 * How a page that hosts Studio (for example inside its own navigation frame) configures it:
 * Studio without its header and sidebar, a fixed API base URL and workspace, and the credential
 * the host signs its user in with. Without host options Studio behaves exactly as before.
 */
import { createContext, useContext } from 'react';

export interface HostCredentials {
  /** Bearer credential for the service, or null when the host's user is signed out. */
  token: string | null;
  /**
   * Stable id of the signed-in identity. A new token for the same identity only rotates the
   * credential; a different identity also drops in-flight requests and cached data.
   */
  identity: string | null;
  /** Workspace the host's user works in. */
  workspace?: string;
}

export interface CredentialSource {
  /** Calls `listener` with every credential the host supplies; returns the unsubscribe. */
  subscribe(listener: (credentials: HostCredentials) => void): () => void;
  /** Asks the host for a fresh credential after the service refused the current one. */
  requestRefresh(): void;
}

export interface StudioHostOptions {
  /** Render pages only: the host draws the navigation around Studio. */
  embedded?: boolean;
  apiBaseUrl?: string;
  workspace?: string;
  credentials?: CredentialSource;
}

export const StudioHostContext = createContext<{ embedded: boolean }>({ embedded: false });

export function useStudioHost(): { embedded: boolean } {
  return useContext(StudioHostContext);
}
