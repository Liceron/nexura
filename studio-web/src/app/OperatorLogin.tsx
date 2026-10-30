// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import React, { useId, useState } from 'react';
import { useApi } from '../api/context';
import { Button } from '../components/Button';
import { Input } from '../components/Input';
import { PageHeader, PageLayout } from '../components/PageHeader';
import { useI18n } from '../i18n';

/**
 * P38 C4: shown when the operator-token provider refuses Studio (401/429). A service in remote
 * single-operator mode (`nexura-runtime serve --auth token`) needs the operator token as a bearer
 * on every request; the operator pastes it here once per tab. It is kept in sessionStorage only,
 * bound to the API origin that accepted it, and dropped when the server refuses it.
 */
export function OperatorLogin() {
  const { connection, refreshConnection, signInWithOperatorToken } = useApi();
  const { t } = useI18n();
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const errorId = useId();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const value = token.trim();
    if (!value) return;
    setBusy(true);
    try {
      await signInWithOperatorToken(value);
    } finally {
      setBusy(false);
      setAttempted(true);
      setToken('');
    }
  };

  const error = connection.rateLimited
    ? t('operatorLogin.rateLimited')
    : attempted
      ? t('operatorLogin.rejected')
      : undefined;

  return (
    <PageLayout width="form" className="access-card">
      <PageHeader title={t('operatorLogin.title')} description={t('operatorLogin.description')} />
      {error && (
        <div role="alert" id={errorId} data-testid="operator-login-error" className="inline-notice inline-notice--danger" style={{ marginBottom: 16 }}>
          <span>{error}</span>
        </div>
      )}
      <form onSubmit={handleSubmit} className="resource-form" style={{ gap: 16 }}>
        <Input
          label={t('operatorLogin.tokenLabel')}
          type="password"
          name="operator-token"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          helperText={t('operatorLogin.storageNote')}
          aria-describedby={error ? errorId : undefined}
          aria-invalid={attempted && !connection.rateLimited ? true : undefined}
          required
        />
        <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
          <Button type="submit" variant="primary" loading={busy} disabled={!token.trim()} className="access-card-submit">
            {busy ? t('operatorLogin.signingIn') : t('operatorLogin.submit')}
          </Button>
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setAttempted(false);
              void refreshConnection();
            }}
            disabled={busy}
          >
            {t('connection.retry')}
          </Button>
        </div>
      </form>
    </PageLayout>
  );
}

/**
 * A refusal that is not the operator-token provider's (a 403, or a 401 from a shared service):
 * show the service's own message and a retry, never a token prompt that does not apply.
 */
export function AccessRefused() {
  const { connection, refreshConnection } = useApi();
  const { t } = useI18n();
  return (
    <PageLayout width="form" className="access-card" data-testid="access-refused">
      <PageHeader
        title={connection.httpStatus === 403 ? t('connection.forbiddenTitle') : t('connection.unauthorizedTitle')}
        description={connection.httpStatus === 403 ? t('connection.forbiddenDescription') : t('connection.unauthorizedDescription')}
      />
      {connection.error && (
        <div role="alert" className="inline-notice inline-notice--danger" style={{ marginBottom: 16 }} data-testid="access-refused-error">
          <span>{connection.error}</span>
        </div>
      )}
      <Button variant="primary" onClick={() => void refreshConnection()}>
        {t('connection.retry')}
      </Button>
    </PageLayout>
  );
}
