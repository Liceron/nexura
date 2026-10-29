// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/**
 * A template's sample input, remembered for the workflow created from it so the Run dialog
 * starts with input that exercises the template (for example an amount above the approval
 * threshold). Kept in this browser only, scoped to the workspace; nothing secret is stored:
 * sample inputs are the template's own synthetic data.
 */

const PREFIX = 'nexura.sampleInput';

function key(workspace: string, workflowId: string): string {
  // JSON keeps the two ids apart whatever characters they contain.
  return `${PREFIX}:${JSON.stringify([workspace, workflowId])}`;
}

export function rememberSampleInput(
  workspace: string,
  workflowId: string,
  input: Record<string, unknown>,
): void {
  try {
    window.localStorage.setItem(key(workspace, workflowId), JSON.stringify(input));
  } catch {
    // Storage full or unavailable: the Run dialog falls back to its generic example.
  }
}

export function sampleInputFor(
  workspace: string,
  workflowId: string,
): Record<string, unknown> | null {
  try {
    const raw = window.localStorage.getItem(key(workspace, workflowId));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}
