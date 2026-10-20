// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { I18nProvider } from './i18n';
import { ApiProvider } from './api/context';
import { ErrorBoundary } from './app/ErrorBoundary';
import { Shell } from './app/Shell';

export function App() {
  return (
    <I18nProvider>
      <ErrorBoundary>
        <ApiProvider>
          <Shell />
        </ApiProvider>
      </ErrorBoundary>
    </I18nProvider>
  );
}
