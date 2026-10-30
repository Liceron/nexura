// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron

/**
 * Turn the Studio's API base URL into the service origin a third party should call (for
 * example a webhook sender). Moved from the publish feature's `snippet.ts` when publishing left
 * the open-source edition (EDITIONS_PLAN step 4); the triggers feature uses it.
 */
export function deriveEndpoint(baseUrl: string, pageOrigin: string): string {
  const stripped = baseUrl.replace(/\/+$/, '').replace(/\/api(\/v1)?$/, '');
  if (/^https?:\/\//i.test(stripped)) return stripped;
  return `${pageOrigin.replace(/\/+$/, '')}${stripped && !stripped.startsWith('/') ? '/' : ''}${stripped}`;
}
