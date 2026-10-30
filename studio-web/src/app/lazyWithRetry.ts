// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { lazy, type ComponentType, type LazyExoticComponent } from 'react';

/**
 * `React.lazy` that retries a failed page import once before giving up.
 *
 * A code-split page can fail to load transiently (a busy dev server, a dropped connection,
 * or a Studio update while a tab is open). One retry turns those into a short delay instead
 * of the error screen; a real failure still reaches the error boundary.
 */
export function lazyWithRetry<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>,
  retryDelayMs = 500,
): LazyExoticComponent<T> {
  return lazy(() =>
    load().catch(
      () =>
        new Promise<{ default: T }>((resolve, reject) => {
          setTimeout(() => load().then(resolve, reject), retryDelayMs);
        }),
    ),
  );
}
