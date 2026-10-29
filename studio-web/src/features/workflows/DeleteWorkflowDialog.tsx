// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Modal } from '../../components/Modal';
import { Button } from '../../components/Button';
import { useI18n } from '../../i18n';

export function DeleteWorkflowDialog({ open, name, busy, error, onClose, onConfirm }: {
  open: boolean; name: string; busy: boolean; error?: string | null; onClose: () => void; onConfirm: () => Promise<void>;
}) {
  const { t } = useI18n();
  return <Modal open={open} onClose={() => { if (!busy) onClose(); }} title={t('workflows.deleteWorkflow')} closeDisabled={busy}>
    <p>{t('workflows.confirmDelete')} “{name}”</p>
    {error && <div role="alert" className="inline-notice inline-notice--danger">{error}</div>}
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 16 }}>
      <Button type="button" disabled={busy} onClick={onClose}>{t('common.cancel')}</Button>
      <Button type="button" variant="danger" loading={busy} onClick={() => void onConfirm()}>{t('common.delete')}</Button>
    </div>
  </Modal>;
}
