// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type { RunRecord } from '@liceron/nexura-sdk';
import { useI18n } from '../../i18n';

/**
 * Why a cancelled run was stopped when access ended (B40). The service sets `cancel_reason`
 * (`access_revoked`, `publication_revoked`) when a member is removed, an API key is revoked or a
 * publication is revoked; a run cancelled by hand has none.
 */
export function runCancelReason(run: RunRecord): string | null {
  if (run.status !== 'cancelled') return null;
  const reason = (run as RunRecord & { cancel_reason?: unknown }).cancel_reason;
  return typeof reason === 'string' && reason !== '' ? reason : null;
}

export function RunCancelReasonNotice({ run }: { run: RunRecord }) {
  const { t } = useI18n();
  const reason = runCancelReason(run);
  if (!reason) return null;
  return (
    <div
      role="status"
      data-testid="run-cancel-reason"
      style={{
        padding: 10,
        backgroundColor: 'var(--status-warning-bg)',
        color: 'var(--status-warning)',
        borderRadius: 'var(--radius-sm)',
        fontSize: 12,
      }}
    >
      <strong>{t('runs.cancelReasonTitle')}</strong>{' '}
      {t('runs.cancelReason_' + reason, reason)}
    </div>
  );
}
