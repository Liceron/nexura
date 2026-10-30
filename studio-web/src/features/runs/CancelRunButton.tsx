// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import { useApi } from '../../api/context';
import { getMyMembership } from '../../api/membership';
import { formatApiError } from '../../api/errors';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { APPROVALS_CHANGED_EVENT } from '../approvals/usePendingApprovals';
import { useT } from '../../i18n';

/** Shared by the run console and the approval/input queues. */
export function CancelRunButton({ runId, status, onCancelled }: {
  runId: string; status: string; onCancelled: () => void | Promise<unknown>;
}) {
  const { manager, connection } = useApi();
  const t = useT();
  const [access, setAccess] = useState<{ workspace: string; principal?: string; allowed: boolean } | null>(null);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const cancellable = ['running', 'pending_approval', 'waiting_for_input', 'paused'].includes(status);
  useEffect(() => {
    if (!cancellable) return;
    let current = true;
    getMyMembership(manager.client, connection.workspace).then(m => {
      if (current) setAccess({ workspace: connection.workspace, principal: connection.principal, allowed: m.role !== 'viewer' && m.permissions.includes('run') });
    }).catch(() => { if (current) setAccess(null); });
    return () => { current = false; };
  }, [manager, connection.workspace, connection.principal, cancellable]);
  useEffect(() => { setOpen(false); setReason(''); setError(''); }, [runId, connection.workspace, connection.principal]);
  if (!cancellable ||
      (access?.workspace === connection.workspace && access.principal === connection.principal && !access.allowed)) return null;
  return <>
    <Button variant="danger" size="sm" onClick={() => { setError(''); setReason(''); setOpen(true); }}>{t('livefixUi.cancelRun')}</Button>
    <Modal open={open} title={t('livefixUi.cancelRunTitle').replace('{id}', runId)} closeDisabled={busy} onClose={() => { if (!busy) setOpen(false); }}>
      <p>{t('livefixUi.cancelRunConfirm').replace('{id}', runId)}</p>
      <label>{t('livefixUi.reason')}<input value={reason} disabled={busy} onChange={e => setReason(e.target.value)} /></label>
      {error && <p role="alert">{error}</p>}
      <div className="inspection-actions">
        <Button disabled={busy} onClick={() => setOpen(false)}>{t('livefixUi.keepRun')}</Button>
        <Button variant="danger" loading={busy} onClick={async () => {
          setBusy(true); setError('');
          try {
            await manager.client.runs.cancel(runId, reason.trim() || undefined);
            window.dispatchEvent(new Event(APPROVALS_CHANGED_EVENT));
            setOpen(false);
            await onCancelled();
          } catch (cause) { setError(formatApiError(cause, t)); }
          finally { setBusy(false); }
        }}>{t('livefixUi.confirmCancellation')}</Button>
      </div>
    </Modal>
  </>;
}
