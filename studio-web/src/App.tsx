// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { I18nProvider } from './i18n';
import { ApiProvider } from './api/context';
import { ErrorBoundary } from './app/ErrorBoundary';
import { Shell } from './app/Shell';
import type { CredentialSource } from './app/host';

export function App({ apiBaseUrl, workspace, credentials }: {
  apiBaseUrl?: string;
  workspace?: string;
  credentials?: CredentialSource;
} = {}) {
  return (
    <I18nProvider>
      <ErrorBoundary>
        <ApiProvider initialBaseUrl={apiBaseUrl} initialWorkspace={workspace} credentials={credentials}>
          <Shell />
        </ApiProvider>
      </ErrorBoundary>
    </I18nProvider>
  );
}
