// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import type { Translate } from '../../i18n';
import { formatApiError } from '../../api/errors';
import { sampleInputFor } from '../templates/sampleInput';

export function defaultRunInput(workspace: string, workflowId: string): Record<string, unknown> {
  return sampleInputFor(workspace, workflowId) ?? {
    order_id: 'ORD-501', ticket_id: 'TICKET-501', customer_tier: 'premium',
  };
}

export function parseRunInput(text: string, t: Translate) {
  if (!text.trim()) return { parsedState: {} as Record<string, unknown>, parseError: null };
  try {
    const parsed: unknown = JSON.parse(text);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { parsedState: null, parseError: t('runStart.objectError') };
    }
    return { parsedState: parsed as Record<string, unknown>, parseError: null };
  } catch (error) {
    return { parsedState: null, parseError: t('approvalReview.jsonError') + ': ' + formatApiError(error, t) };
  }
}

/** Only offer shorthand for one unambiguous, flat entry-node text mapping. */
export function workflowMessageKey(spec: WorkflowSpec): string | null {
  const entry = spec.nodes?.[spec.entry_node];
  const mapping = entry?.input_mapping as Record<string, unknown> | undefined;
  const pairs = Object.entries(mapping ?? {});
  if (pairs.length !== 1) return null;
  const [port, key] = pairs[0];
  return ['message', 'prompt', 'query', 'question', 'text', 'input'].includes(port) &&
    typeof key === 'string' && /^[a-zA-Z_][\w]*$/.test(key) ? key : null;
}
