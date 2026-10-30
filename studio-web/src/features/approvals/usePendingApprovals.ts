// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useState, useEffect, useCallback, useRef } from 'react';
import { useApi } from '../../api/context';
import type { PendingApprovalItem, DecisionType, DecisionResult, ExactApprovalRequest } from './types';
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import { NexuraApiError } from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';
import { payloadJson } from './ExactAction';

/** Fired on window after a decision so other hook instances (the sidebar badge) refresh. */
export const APPROVALS_CHANGED_EVENT = 'nexura:approvals-changed';

/** Sidebar approvals badge refresh interval. Ticks are skipped while offline or hidden. */
export const APPROVALS_BADGE_POLL_MS = 30_000;

export interface UsePendingApprovalsOptions {
  /** Fetch only while true (default true). Shell passes false while the service is offline. */
  enabled?: boolean;
  /** Re-fetch every N ms while enabled and the page is visible. Omit for a one-off fetch. */
  pollIntervalMs?: number;
}

export function usePendingApprovals(options: UsePendingApprovalsOptions = {}) {
  const { enabled = true, pollIntervalMs } = options;
  const { manager, connection } = useApi();
  const [approvals, setApprovals] = useState<PendingApprovalItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [submittingRunId, setSubmittingRunId] = useState<string | null>(null);
  // Server total of pending approvals (the list holds one page of at most 100).
  const [total, setTotal] = useState<number>(0);
  // Set after a 401/403: the caller cannot list runs, so background polling stops.
  const [denied, setDenied] = useState<boolean>(false);
  const isMountedRef = useRef<boolean>(true);
  const instanceRef = useRef<object>({});
  // Keep only API-supplied snapshots. A fresh request for the same paused node can
  // be compared during this inbox session; no history is invented after reload.
  const snapshots = useRef(new Map<string, ExactApprovalRequest>());
  const previous = useRef(new Map<string, ExactApprovalRequest>());

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const fetchApprovals = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await manager.client.runs.list({
        status: 'pending_approval',
        limit: 100,
      });

      if (!isMountedRef.current) return;

      const items: PendingApprovalItem[] = [];
      for (const r of response.items) {
        if (r.status === 'pending_approval' && r.pending_approval?.approval_id) {
          const key = `${r.run_id}:${r.pending_approval.node_id}`;
          const old = snapshots.current.get(key);
          const next = r.pending_approval as ExactApprovalRequest;
          if (old && old.approval_id !== next.approval_id && old.input_state && next.input_state && payloadJson(old.input_state) !== payloadJson(next.input_state)) {
            previous.current.set(key, old);
          }
          snapshots.current.set(key, r.pending_approval);
          items.push({
            approvalId: r.pending_approval.approval_id,
            runId: r.run_id,
            workflowId: r.workflow_id || '',
            nodeId: r.pending_approval.node_id || r.current_node_id || '',
            createdAt: r.created_at,
            currentState: r.current_state || {},
            spec: (r as unknown as { spec?: WorkflowSpec }).spec,
            runRecord: r,
            previousApproval: previous.current.get(key),
          });
        }
      }

      setApprovals(items);
      setTotal(Math.max(items.length, typeof response.total === 'number' ? response.total : 0));
      setDenied(false);
    } catch (err: unknown) {
      if (!isMountedRef.current) return;
      const msg = formatApiError(err);
      setError(msg);
      if (err instanceof NexuraApiError && (err.statusCode === 401 || err.statusCode === 403)) {
        // No access (any more): show nothing rather than a stale count, and stop polling.
        setApprovals([]);
        setTotal(0);
        setDenied(true);
      }
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
      }
    }
  }, [manager]);

  useEffect(() => {
    snapshots.current.clear();
    previous.current.clear();
  }, [connection.workspace]);

  useEffect(() => {
    if (!enabled) return;
    fetchApprovals();
  }, [enabled, fetchApprovals, connection.workspace]);

  // Optional background refresh (the Shell's sidebar badge). Stops while disabled (offline) and
  // skips ticks while the tab is hidden, so an idle Studio does not keep hitting the service.
  useEffect(() => {
    if (!enabled || denied || !pollIntervalMs || pollIntervalMs <= 0) return;
    const timer = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      void fetchApprovals();
    }, pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, denied, pollIntervalMs, fetchApprovals]);

  useEffect(() => {
    if (!enabled) return;
    const onChanged = (event: Event) => {
      if ((event as CustomEvent<object>).detail === instanceRef.current) return; // already refreshed
      void fetchApprovals();
    };
    window.addEventListener(APPROVALS_CHANGED_EVENT, onChanged);
    return () => window.removeEventListener(APPROVALS_CHANGED_EVENT, onChanged);
  }, [enabled, fetchApprovals]);

  const decideApproval = useCallback(
    async (
      runId: string,
      approvalId: string,
      decision: DecisionType,
      stateUpdate?: Record<string, unknown>
    ): Promise<DecisionResult> => {
      if (submittingRunId) {
        return { success: false, error: 'A decision is already being submitted.' };
      }

      setSubmittingRunId(runId);
      try {
        const updated = await manager.client.runs.decide(runId, {
          approval_id: approvalId,
          decision,
          state_update: stateUpdate,
        });

        // Refresh list after decision
        await fetchApprovals();
        window.dispatchEvent(new CustomEvent(APPROVALS_CHANGED_EVENT, { detail: instanceRef.current }));
        return { success: true, run: updated };
      } catch (err: unknown) {
        const msg = formatApiError(err);
        const raw = err instanceof Error ? err.message : String(err); // keeps "[409] CODE"
        const isConflict =
          raw.includes('409') || raw.includes('410') || raw.includes('APPROVAL_EXPIRED') ||
          raw.includes('APPROVAL_ALREADY_CLAIMED') ||
          raw.includes('NOT_PENDING_APPROVAL') ||
          raw.includes('RUN_ALREADY_TERMINAL');

        // Always re-fetch snapshot to reconcile truthful state
        await fetchApprovals();
        window.dispatchEvent(new CustomEvent(APPROVALS_CHANGED_EVENT, { detail: instanceRef.current }));

        return {
          success: false,
          error: msg,
          conflict: isConflict,
        };
      } finally {
        if (isMountedRef.current) {
          setSubmittingRunId(null);
        }
      }
    },
    [manager, submittingRunId, fetchApprovals]
  );

  return {
    approvals,
    count: total,
    loading,
    error,
    refresh: fetchApprovals,
    decideApproval,
    isSubmitting: Boolean(submittingRunId),
    submittingRunId,
  };
}
