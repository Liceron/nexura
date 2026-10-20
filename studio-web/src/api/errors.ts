// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { englishT, type Translate } from '../i18n';
import { NexuraApiError } from '@liceron/nexura-sdk';

/** A plain-language lead for error codes a user can act on. */
const CODE_LEADS: Record<string, string> = {
  RESOURCE_SCOPE_DENIED: 'apiErrors.scope',
  REVISION_CONFLICT: 'apiErrors.conflict',
  RATE_LIMITED: 'apiErrors.rate',
  VALIDATION_ERROR: 'apiErrors.validation',
};

const STATUS_LEADS: Record<number, string> = {
  401: 'apiErrors.auth',
  403: 'apiErrors.role',
  404: 'apiErrors.missing',
  409: 'apiErrors.state',
  413: 'apiErrors.size',
  429: 'apiErrors.rate',
};

/**
 * Text to show a user for an error from the service (or anything else thrown).
 *
 * Service errors keep the server's message and code (support and tests rely on the code),
 * with a plain lead for statuses and codes a user can act on. Other errors show their message.
 */
export function formatApiError(err: unknown, t: Translate = englishT): string {
  if (err instanceof NexuraApiError) {
    const text = err.message.replace(/^\[\d+\]\s+[A-Za-z0-9_]+:\s*/, '');
    const lead = CODE_LEADS[err.code] ?? STATUS_LEADS[err.statusCode];
    return lead ? `${t(lead)} ${text} (${err.code})` : `${text} (${err.code})`;
  }
  return err instanceof Error ? err.message : String(err);
}
