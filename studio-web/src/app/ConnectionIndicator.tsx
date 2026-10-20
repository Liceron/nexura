// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { useState } from 'react';
import { useApi } from '../api/context';
import { Badge, BadgeVariant } from '../components/Badge';
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

  const statusVariant: Record<string, BadgeVariant> = {
    connected: 'success',
    connecting: 'warning',
    offline: 'danger',
    unauthorized: 'danger',
  };

  const statusLabel: Record<string, string> = {
    connected: connection.profile
      ? `${connection.profile} (${connection.latencyMs ?? 0}ms)`
      : t('connection.connected'),
    connecting: t('connection.connecting'),
    offline: t('connection.offline'),
    unauthorized: t('connection.unauthorized'),
  };

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    changeBaseUrl(editUrl.trim());
    if (editToken.trim()) {
      changeAuthToken(editToken.trim());
    }
    setModalOpen(false);
  };

  return (
    <>
      <button
        onClick={() => {
          setEditUrl(connection.baseUrl);
          setModalOpen(true);
        }}
        aria-label={`${t('header.connection')}: ${t(`connection.${connection.status}`)}`}
        style={{
          display: 'inline-flex',
          minWidth: 0,
          maxWidth: '100%',
          overflowWrap: 'anywhere',
          alignItems: 'center',
          gap: 6,
          padding: '4px 8px',
          borderRadius: 'var(--radius-md)',
          backgroundColor: 'var(--bg-surface-elevated)',
          border: '1px solid var(--border-subtle)',
          cursor: 'pointer',
        }}
      >
        <Badge variant={statusVariant[connection.status] || 'neutral'}>
          {statusLabel[connection.status] || connection.status}
        </Badge>
      </button>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title={t('connection.settings')}
      >
        <form onSubmit={handleSave}>
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
