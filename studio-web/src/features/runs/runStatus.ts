// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type { BadgeVariant } from '../../components/Badge';

/**
 * Run statuses the service stores (src/nexura_runtime/service/api/runs.py): running at start,
 * then pending_approval or waiting_for_input while paused, and one of the terminal states.
 */
export const RUN_STATUSES = [
  'running',
  'pending_approval',
  'waiting_for_input',
  'completed',
  'failed',
  'rejected',
  'cancelled',
  'interrupted',
] as const;

export function runStatusVariant(status: string): BadgeVariant {
  switch (status) {
    case 'completed':
      return 'success';
    case 'failed':
    case 'rejected':
      return 'danger';
    case 'running':
      return 'info';
    case 'pending_approval':
    case 'waiting_for_input':
    case 'interrupted':
      return 'warning';
    default:
      return 'neutral';
  }
}

/** Translated status label; unknown statuses show as the server sent them. */
export function runStatusLabel(t: (key: string, fallback?: string) => string, status: string): string {
  return t('traces.status_' + status, status);
}

/** "Showing 1-20 of 45" (i18n has no interpolation, so placeholders are replaced here). */
export function pageRange(t: (key: string, fallback?: string) => string, offset: number, count: number, total: number): string {
  return t('runs.pageRange')
    .replace('{from}', String(count === 0 ? 0 : offset + 1))
    .replace('{to}', String(count === 0 ? 0 : offset + count))
    .replace('{total}', String(total));
}
