// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { NexuraClient, WorkflowSpec } from '@liceron/nexura-sdk';

export const MAX_DESCRIPTION_CHARS = 4000;

export interface DraftValidationIssue {
  node_id?: string | null;
  code: string;
  message: string;
}

export interface DraftResponse {
  spec: WorkflowSpec;
  explanation: string;
  warnings: string[];
  attempts: number;
  saved: false;
  model?: string;
  validation: {
    valid: boolean;
    errors: DraftValidationIssue[];
    warnings: DraftValidationIssue[];
  };
}

export interface DraftFailure {
  code: string;
  message: string;
  warnings: string[];
}

/** Ask the service for a draft. The service neither saves nor runs it. */
export async function requestDraft(
  client: NexuraClient,
  description: string,
  signal?: AbortSignal
): Promise<DraftResponse> {
  return client.request<DraftResponse>('POST', '/builder/draft', {
    body: { description },
    signal,
  });
}

/** Turn any thrown error into something the panel can show. */
export function toDraftFailure(err: unknown): DraftFailure {
  const e = err as { code?: unknown; message?: unknown; details?: { warnings?: unknown } };
  const warnings = Array.isArray(e?.details?.warnings)
    ? (e.details.warnings as unknown[]).filter((w): w is string => typeof w === 'string')
    : [];
  return {
    code: typeof e?.code === 'string' ? e.code : 'ERROR',
    message: typeof e?.message === 'string' ? e.message : String(err),
    warnings,
  };
}
