// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { useState } from 'react';
import { useApi } from '../api/context';
import { Modal } from '../components/Modal';
import { Input } from '../components/Input';
import { Button } from '../components/Button';
import { useI18n } from '../i18n';

export function WorkspaceSelector() {
  const { connection, changeWorkspace } = useApi();
  const { t } = useI18n();
  const [modalOpen, setModalOpen] = useState(false);
  const [targetWs, setTargetWs] = useState(connection.workspace);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (targetWs.trim()) {
      changeWorkspace(targetWs.trim());
      setModalOpen(false);
    }
  };

  return (
    <>
      <button
        onClick={() => {
          setTargetWs(connection.workspace);
          setModalOpen(true);
        }}
        aria-label={`${t('header.workspace')}: ${connection.workspace}`}
        style={{
          display: 'inline-flex',
          minWidth: 0,
          maxWidth: '100%',
          overflowWrap: 'anywhere',
          alignItems: 'center',
          gap: 6,
          padding: '4px 10px',
          borderRadius: 'var(--radius-md)',
          backgroundColor: 'var(--bg-surface-elevated)',
          border: '1px solid var(--border-subtle)',
          color: 'var(--text-primary)',
          fontSize: 13,
          fontWeight: 500,
          cursor: 'pointer',
        }}
      >
        <span className="studio-workspace-prefix" style={{ color: 'var(--text-muted)' }}>{t('ui.workspace')}</span>
        <span style={{ color: 'var(--accent-primary)', fontWeight: 600, minWidth: 0 }}>
          {connection.workspace}
        </span>
      </button>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title={t('connection.workspace')}
      >
        <form onSubmit={handleSubmit}>
          <Input
            label={t('ui.workspaceIdentifier')}
            value={targetWs}
            onChange={(e) => setTargetWs(e.target.value)}
            placeholder={t('ui.eGDefaultTenantAlphaSandbox')}
            helperText={t('ui.workspaceHelp')}
            required
          />

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 20 }}>
            <Button
              type="button"
              variant="secondary"
              onClick={() => setModalOpen(false)}
            >
              {t('common.cancel')}
            </Button>
            <Button type="submit" variant="primary">
              {t('common.confirm')}
            </Button>
          </div>
        </form>
      </Modal>
    </>
  );
}
