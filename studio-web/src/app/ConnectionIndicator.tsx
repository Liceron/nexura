// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { useId, useState } from 'react';
import { useApi } from '../api/context';
import { Modal } from '../components/Modal';
import { Input } from '../components/Input';
import { Button } from '../components/Button';
import { useI18n } from '../i18n';

export function ConnectionIndicator() {
  const { connection, changeBaseUrl, changeAuthToken, refreshConnection } = useApi();
  const { t } = useI18n();
  const [modalOpen, setModalOpen] = useState(false);
  const [editUrl, setEditUrl] = useState(connection.baseUrl);
  const [editToken, setEditToken] = useState('');
  const descriptionId = useId();
  const close = () => { setEditToken(''); setModalOpen(false); };

  const statusColour: Record<string, string> = {
    connected: 'var(--text-secondary)',
    connecting: 'var(--status-warning)',
    offline: 'var(--status-danger)',
    unauthorized: 'var(--status-danger)',
  };

  const statusLabel: Record<string, string> = {
    connected: t('connection.connected'),
    connecting: t('connection.connecting'),
    offline: t('connection.offline'),
    unauthorized:
      connection.httpStatus === 403
        ? t('connection.forbidden')
        : connection.httpStatus === 429
          ? t('connection.rateLimitedStatus')
          : t('connection.unauthorized'),
  };

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    changeBaseUrl(editUrl.trim());
    if (editToken.trim()) {
      changeAuthToken(editToken.trim());
    }
    close();
  };

  return (
    <>
      <button
        onClick={() => {
          setEditUrl(connection.baseUrl);
          setEditToken('');
          setModalOpen(true);
        }}
        aria-label={`${t('header.connection')}: ${t(`connection.${connection.status}`)}`}
        aria-describedby={descriptionId}
        aria-haspopup="dialog"
        type="button"
        className="studio-utility-button studio-connection"
        style={{ color: statusColour[connection.status] }}
      >
        <span className="studio-status-dot" aria-hidden="true" />
        {statusLabel[connection.status] || connection.status}
      </button>
      <span id={descriptionId} className="studio-sr-only">
        {t('system.activeProfile')}: {connection.profile || t('common.notAvailable')}.{' '}
        {t('connection.serverUrl')}: {connection.baseUrl}.{' '}
        {t('shell.latency')}: {connection.latencyMs == null ? t('common.notAvailable') : `${connection.latencyMs} ms`}.
      </span>

      <Modal
        open={modalOpen}
        onClose={close}
        title={t('connection.settings')}
      >
        <form onSubmit={handleSave}>
          <dl className="studio-connection-details">
            <dt>{t('shell.connectionStatus')}</dt><dd>{statusLabel[connection.status]}</dd>
            <dt>{t('system.activeProfile')}</dt><dd>{connection.profile || t('common.notAvailable')}</dd>
            <dt>{t('shell.latency')}</dt><dd>{connection.latencyMs == null ? t('common.notAvailable') : `${connection.latencyMs} ms`}</dd>
          </dl>
          <Input
            label={t('connection.serverUrl')}
            value={editUrl}
            onChange={(e) => setEditUrl(e.target.value)}
            placeholder={t('ui.http1270018765OrApi')}
            helperText={t('ui.endpointHelp')}
            required
          />

          <Input
            label={t('ui.serviceAuthTokenOptional')}
            type="password"
            value={editToken}
            onChange={(e) => setEditToken(e.target.value)}
            placeholder={t('ui.bearerTokenOrApiKey')}
            helperText={t('ui.authHelp')}
          />

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 20 }}>
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                refreshConnection();
              }}
            >
              {t('connection.retry')}
            </Button>
            <Button type="submit" variant="primary">
              {t('connection.save')}
            </Button>
          </div>
        </form>
      </Modal>
    </>
  );
}
