// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import { isPotentialSecret } from '../modules/types';
import { formatApiError } from '../../api/errors';

export interface WorkflowDraft {
  workflowId: string;
  workspace: string;
  principal: string;
  savedAt: number;
  spec: WorkflowSpec;
  serverRevision: number;
}

/**
 * Recursively scans any object/array for raw secrets or API keys.
 */
export function deepScanForSecrets(obj: unknown): boolean {
  if (obj === null || obj === undefined) return false;
  if (typeof obj === 'string') {
    return isPotentialSecret(obj);
  }
  if (Array.isArray(obj)) {
    return obj.some((item) => deepScanForSecrets(item));
  }
  if (typeof obj === 'object') {
    return Object.values(obj as Record<string, unknown>).some((val) =>
      deepScanForSecrets(val)
    );
  }
  return false;
}

/**
 * Returns a strictly tenant- and principal-scoped storage key for drafts.
 * Ensures drafts from one workspace/principal can NEVER leak to another.
 */
export function getDraftStorageKey(
  workspace: string,
  principal: string | undefined,
  workflowId: string
): string {
  const ws = (workspace || 'default').trim().toLowerCase();
  const pr = (principal || 'default').trim().toLowerCase();
  const wf = workflowId.trim();
  return `nexura_draft_${ws}_${pr}_${wf}`;
}

export interface SaveDraftResult {
  saved: boolean;
  secretDetected: boolean;
  error?: string;
}

/**
 * Safely saves an in-memory workflow spec draft into localStorage.
 * Enforces:
 * 1. Zero secret persistence: If a potential raw secret is detected, rejects
 *    or strips secret storage so raw API keys are never written to disk.
 * 2. Scoped to workspace & principal.
 */
export function saveWorkflowDraft(
  workspace: string,
  principal: string | undefined,
  workflowId: string,
  spec: WorkflowSpec,
  serverRevision: number
): SaveDraftResult {
  if (typeof window === 'undefined' || !window.localStorage) {
    return { saved: false, secretDetected: false, error: 'localStorage unavailable' };
  }

  // Security guard: never persist raw secrets into local browser storage
  const hasSecret = deepScanForSecrets(spec);
  if (hasSecret) {
    return {
      saved: false,
      secretDetected: true,
      error: 'Cannot save local draft: specification contains raw secret or API key.',
    };
  }

  const effectivePrincipal = principal || 'default';
  const key = getDraftStorageKey(workspace, effectivePrincipal, workflowId);
  const draft: WorkflowDraft = {
    workflowId,
    workspace,
    principal: effectivePrincipal,
    savedAt: Date.now(),
    spec,
    serverRevision,
  };

  try {
    localStorage.setItem(key, JSON.stringify(draft));
    return { saved: true, secretDetected: false };
  } catch (err: unknown) {
    return {
      saved: false,
      secretDetected: false,
      error: formatApiError(err),
    };
  }
}

/**
 * Safely retrieves an existing draft for a specific workspace and principal.
 * Enforces strict isolation: returns null if the draft's stored tenant
 * does not match the active caller context.
 */
export function loadWorkflowDraft(
  workspace: string,
  principal: string | undefined,
  workflowId: string
): WorkflowDraft | null {
  if (typeof window === 'undefined' || !window.localStorage) return null;

  const effectivePrincipal = principal || 'default';
  const key = getDraftStorageKey(workspace, effectivePrincipal, workflowId);
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;

    const parsed: WorkflowDraft = JSON.parse(raw);
    // Strict isolation validation
    if (
      parsed.workspace !== workspace ||
      (parsed.principal || 'default') !== effectivePrincipal ||
      parsed.workflowId !== workflowId
    ) {
      return null;
    }

    // Guard against corrupted or secretly injected storage
    if (deepScanForSecrets(parsed.spec)) {
      clearWorkflowDraft(workspace, effectivePrincipal, workflowId);
      return null;
    }

    return parsed;
  } catch {
    return null;
  }
}

/**
 * Clears the stored draft for this specific workflow, workspace, and principal.
 */
export function clearWorkflowDraft(
  workspace: string,
  principal: string | undefined,
  workflowId: string
): void {
  if (typeof window === 'undefined' || !window.localStorage) return;
  const key = getDraftStorageKey(workspace, principal || 'default', workflowId);
  try {
    localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

/**
 * Checks whether an uncommitted local draft exists.
 */
export function hasWorkflowDraft(
  workspace: string,
  principal: string | undefined,
  workflowId: string
): boolean {
  return loadWorkflowDraft(workspace, principal, workflowId) !== null;
}
