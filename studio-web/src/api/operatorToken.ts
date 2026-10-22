// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
//
// P38 C4: the operator token of a service in remote single-operator mode
// (`nexura-runtime serve --auth token`). Studio sends it as `Authorization: Bearer` (the
// client's `apiKey`). It is kept in sessionStorage only: this tab, until the tab is closed.
// Never localStorage, the URL or the console.
//
// The token is bound to the API origin that accepted it (Round 5 review B1): the origin is stored
// beside it, and a token is only ever read back for exactly that origin. A token whose origin does
// not match (or has no recorded origin) is deleted rather than sent anywhere.

export const OPERATOR_TOKEN_STORAGE_KEY = 'nexura_operator_token';
export const OPERATOR_TOKEN_ORIGIN_STORAGE_KEY = 'nexura_operator_token_origin';

function clear(): void {
  try {
    window.sessionStorage.removeItem(OPERATOR_TOKEN_STORAGE_KEY);
    window.sessionStorage.removeItem(OPERATOR_TOKEN_ORIGIN_STORAGE_KEY);
  } catch {
    // sessionStorage unavailable: nothing was stored.
  }
}

/** The stored token, only when it was accepted by exactly `origin`; otherwise it is deleted. */
export function readStoredOperatorToken(origin: string): string | null {
  try {
    const value = window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY);
    if (!value || !value.trim()) return null;
    const boundTo = window.sessionStorage.getItem(OPERATOR_TOKEN_ORIGIN_STORAGE_KEY);
    if (!origin || !boundTo || boundTo !== origin) {
      clear(); // another origin's token is never sent here, and is not kept either
      return null;
    }
    return value;
  } catch {
    return null; // sessionStorage unavailable: the operator signs in again after a reload.
  }
}

/** Keep `token` for `origin` (the API origin that accepted it), or forget it when null. */
export function storeOperatorToken(token: string | null, origin?: string): void {
  if (!token || !origin) {
    clear();
    return;
  }
  try {
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, token);
    window.sessionStorage.setItem(OPERATOR_TOKEN_ORIGIN_STORAGE_KEY, origin);
  } catch {
    // sessionStorage unavailable: the token stays in memory for this page only.
  }
}
