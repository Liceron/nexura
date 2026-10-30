// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { Component, ErrorInfo, ReactNode } from 'react';
import { Card } from '../components/Card';
import { Button } from '../components/Button';
import { useI18n } from '../i18n';

interface Props {
  children: ReactNode;
  onGoToWorkflows?: () => void;
}

function ErrorFallback({ error, onGoToWorkflows }: { error?: Error; onGoToWorkflows?: () => void }) {
  const { t } = useI18n();
  const reloadFirst = !onGoToWorkflows || error?.name === 'ChunkLoadError' ||
    /dynamically imported module|Loading chunk/i.test(error?.message ?? '');
  const reloadButton = (
    <Button variant={reloadFirst ? 'primary' : 'secondary'} onClick={() => window.location.reload()}>
      {t('errors.reload')}
    </Button>
  );
  return (
    <div role="alert" style={{ padding: 40, display: 'flex', justifyContent: 'center' }}>
      <Card elevated style={{ maxWidth: 520, width: '100%', textAlign: 'center' }}>
        <h2 style={{ color: 'var(--status-danger)', marginBottom: 12 }}>
          {t('errors.title')}
        </h2>
        <p style={{ color: 'var(--text-secondary)', marginBottom: 20 }}>
          {t(onGoToWorkflows ? 'errors.pageDescription' : 'errors.description')}
        </p>
        <div style={{ display: 'flex', justifyContent: 'center', gap: 10, flexWrap: 'wrap' }}>
          {reloadFirst && reloadButton}
          {onGoToWorkflows && (
            <Button variant={reloadFirst ? 'secondary' : 'primary'} onClick={onGoToWorkflows}>
              {t('errors.goToWorkflows')}
            </Button>
          )}
          {!reloadFirst && reloadButton}
        </div>
      </Card>
    </div>
  );
}

interface State {
  hasError: boolean;
  error?: Error;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // Log cleanly to console without leaking sensitive state
    console.error('ErrorBoundary caught:', error.message, errorInfo.componentStack);
  }

  public render() {
    if (this.state.hasError) {
      return <ErrorFallback error={this.state.error} onGoToWorkflows={this.props.onGoToWorkflows && (() => {
        this.setState({ hasError: false, error: undefined });
        this.props.onGoToWorkflows?.();
      })} />;
    }

    return this.props.children;
  }
}
