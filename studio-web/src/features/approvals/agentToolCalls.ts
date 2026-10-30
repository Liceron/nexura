// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type { PendingApproval } from '@liceron/nexura-sdk';

/** `pending_approval.details.kind` of a saved agent's tool calls waiting for approval. */
export const AGENT_TOOL_CALLS_KIND = 'agent_tool_calls';

export interface AgentToolCallView {
  tool: string;
  /** The call's arguments as indented JSON text (shown as text, never as markup). */
  argumentsText: string;
}

export interface AgentToolCallsView {
  agentId: string | null;
  agentName: string;
  agentRevision: number | null;
  calls: AgentToolCallView[];
}

function argumentsText(value: unknown): string {
  if (value === undefined) return '{}';
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/**
 * The agent, revision and exact calls an approval covers, or null when the approval is not a
 * saved agent's tool calls. Only the named fields are read: the internal paused-run snapshot
 * (`resume`) and the call digests are never part of the view.
 */
export function describeAgentToolCalls(
  pending: Partial<PendingApproval> | null | undefined,
): AgentToolCallsView | null {
  const d = pending?.details as Record<string, unknown> | undefined;
  if (!d || typeof d !== 'object' || d.kind !== AGENT_TOOL_CALLS_KIND) return null;
  const calls = Array.isArray(d.calls) ? d.calls : [];
  return {
    agentId: typeof d.agent_id === 'string' ? d.agent_id : null,
    agentName: typeof d.agent_name === 'string' ? d.agent_name : '',
    agentRevision: typeof d.agent_revision === 'number' ? d.agent_revision : null,
    calls: calls
      .filter((c): c is Record<string, unknown> => Boolean(c) && typeof c === 'object')
      .map((c) => ({ tool: String(c.tool ?? ''), argumentsText: argumentsText(c.arguments) })),
  };
}
