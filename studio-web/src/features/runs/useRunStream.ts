// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useState, useEffect, useRef, useCallback } from 'react';
import { useApi } from '../../api/context';
import type { RunRecord, RunEvent } from '@liceron/nexura-sdk';
import type { StreamStatus, NodeExecutionStatus, ReplayStep } from './types';
import {
  deduplicateEvents,
  extractNodeExecutionStatus,
  computeStateReplaySteps,
} from './events';
import { formatApiError } from '../../api/errors';
import { useLatestT } from '../../i18n';
import { APPROVALS_CHANGED_EVENT } from '../approvals/usePendingApprovals';

export interface UseRunStreamResult {
  run: RunRecord | null;
  events: RunEvent[];
  streamStatus: StreamStatus;
  maxSeq: number;
  loading: boolean;
  error: string | null;
  nodeStatuses: Map<string, NodeExecutionStatus>;
  replaySteps: ReplayStep[];
  cancelRun: (reason?: string) => Promise<RunRecord | null>;
  decideApproval: (
    decision: 'approve' | 'reject',
    stateUpdate?: Record<string, unknown>
  ) => Promise<RunRecord | null>;
  provideInput: (interruptId: string, value: unknown) => Promise<RunRecord>;
  refetchSnapshot: () => Promise<void>;
}

const TERMINAL_STATUSES = new Set([
  'completed',
  'failed',
  'cancelled',
  'rejected',
  'interrupted',
]);

export function useRunStream(runId: string | null): UseRunStreamResult {
  const t = useLatestT();
  const { manager, connection } = useApi();

  const [run, setRun] = useState<RunRecord | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('connecting');
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const maxSeqRef = useRef<number>(0);
  const isMountedRef = useRef<boolean>(true);
  const retryTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Fetch authoritative snapshot from database
  const fetchSnapshot = useCallback(async () => {
    if (!runId) return;
    try {
      const snapshot = await manager.client.runs.get(runId);
      if (!isMountedRef.current) return;
      setRun(snapshot);
      if (snapshot.events && snapshot.events.length > 0) {
        setEvents((prev) => {
          const merged = deduplicateEvents(prev, snapshot.events || []);
          const highest = merged.reduce((m, ev) => Math.max(m, ev.seq), 0);
          maxSeqRef.current = Math.max(maxSeqRef.current, highest);
          return merged;
        });
      }
    } catch (err: unknown) {
      if (!isMountedRef.current) return;
      setError(formatApiError(err, t));
    }
  }, [manager, runId, t]);

  // Main lifecycle and stream coordinator
  useEffect(() => {
    isMountedRef.current = true;
    if (!runId) {
      setRun(null);
      setEvents([]);
      setLoading(false);
      setStreamStatus('closed');
      return;
    }

    // Reset state for new run
    setLoading(true);
    setError(null);
    setEvents([]);
    maxSeqRef.current = 0;
    setStreamStatus('connecting');

    // Clean up previous reader / timer
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    if (retryTimeoutRef.current) {
      clearTimeout(retryTimeoutRef.current);
    }

    const abortCtrl = new AbortController();
    abortControllerRef.current = abortCtrl;

    async function startLifecycle() {
      try {
        // 1. Initial snapshot fetch
        const snapshot = await manager.client.runs.get(runId!);
        if (!isMountedRef.current || abortCtrl.signal.aborted) return;

        setRun(snapshot);
        setLoading(false);

        const initialEvents = snapshot.events || [];
        const merged = deduplicateEvents([], initialEvents);
        const highest = merged.reduce((m, ev) => Math.max(m, ev.seq), 0);
        maxSeqRef.current = highest;
        setEvents(merged);

        // If run is already terminal, no live SSE needed
        if (TERMINAL_STATUSES.has(snapshot.status)) {
          setStreamStatus('closed');
          return;
        }

        // 2. Open live event stream
        setStreamStatus('connected');
        await readStreamLoop(abortCtrl);
      } catch (err: unknown) {
        if (!isMountedRef.current || abortCtrl.signal.aborted) return;
        setLoading(false);
        setError(formatApiError(err, t));
        setStreamStatus('error');
      }
    }

    async function readStreamLoop(currentAbort: AbortController) {
      while (isMountedRef.current && !currentAbort.signal.aborted) {
        try {
          const generator = manager.client.runs.streamEvents(runId!, {
            cursor: maxSeqRef.current,
            live: true,
            signal: currentAbort.signal,
          });

          for await (const event of generator) {
            if (!isMountedRef.current || currentAbort.signal.aborted) break;

            if (event.type === 'RUN_STREAM_CLOSED') {
              setStreamStatus('closed');
              await fetchSnapshot();
              return;
            }

            maxSeqRef.current = Math.max(maxSeqRef.current, event.seq);
            setEvents((prev) => deduplicateEvents(prev, [event]));

            // If event indicates state update or node change, update local run snapshot
            if (event.type === 'NODE_STARTED') {
              setRun((prev) => (prev ? { ...prev, current_node_id: event.node_id } : prev));
            } else if (event.type === 'NODE_COMPLETED') {
              const stateUpdate = (event.data?.state_update as Record<string, unknown>) || {};
              setRun((prev) =>
                prev
                  ? {
                      ...prev,
                      current_state: { ...prev.current_state, ...stateUpdate },
                      step_count: prev.step_count + 1,
                    }
                  : prev
              );
            } else if (event.type === 'RUN_PENDING_APPROVAL') {
              setStreamStatus('connected');
              await fetchSnapshot();
            } else if (
              event.type === 'RUN_COMPLETED' ||
              event.type === 'RUN_FAILED' ||
              event.type === 'RUN_CANCELLED'
            ) {
              setStreamStatus('closed');
              await fetchSnapshot();
              return;
            }
          }

          // If stream ended naturally without terminal event, poll snapshot
          if (isMountedRef.current && !currentAbort.signal.aborted) {
            const check = await manager.client.runs.get(runId!);
            if (isMountedRef.current) {
              setRun(check);
              if (TERMINAL_STATUSES.has(check.status)) {
                setStreamStatus('closed');
                return;
              }
            }
          }
        } catch {
          if (currentAbort.signal.aborted || !isMountedRef.current) {
            return;
          }
          // Connection drop / reconnecting
          setStreamStatus('reconnecting');
          // Re-fetch snapshot on reconnect attempt to guarantee database alignment
          await fetchSnapshot();
          // Wait 500ms before retrying stream with updated cursor
          await new Promise((resolve) => {
            retryTimeoutRef.current = setTimeout(resolve, 500);
          });
        }
      }
    }

    startLifecycle();

    return () => {
      isMountedRef.current = false;
      abortCtrl.abort();
      if (retryTimeoutRef.current) {
        clearTimeout(retryTimeoutRef.current);
      }
    };
  }, [runId, connection.workspace, manager, fetchSnapshot, t]);

  // Cancel Run action
  const cancelRun = useCallback(
    async (reason?: string): Promise<RunRecord | null> => {
      if (!runId) return null;
      try {
        const cancelled = await manager.client.runs.cancel(runId, reason);
        if (isMountedRef.current) {
          setRun(cancelled);
          setStreamStatus('closed');
          if (cancelled.events) {
            setEvents((prev) => deduplicateEvents(prev, cancelled.events || []));
          }
        }
        return cancelled;
      } catch (err: unknown) {
        setError(formatApiError(err, t));
        return null;
      }
    },
    [manager, runId, t]
  );

  // Approval decision action
  const decideApproval = useCallback(
    async (
      decision: 'approve' | 'reject',
      stateUpdate?: Record<string, unknown>
    ): Promise<RunRecord | null> => {
      if (!runId || !run?.pending_approval?.approval_id) return null;
      try {
        const updated = await manager.client.runs.decide(runId, {
          approval_id: run.pending_approval.approval_id,
          decision,
          state_update: stateUpdate,
        });
        // The sidebar approvals badge counts pending approvals; tell it one was decided.
        window.dispatchEvent(new CustomEvent(APPROVALS_CHANGED_EVENT));
        if (isMountedRef.current) {
          setRun(updated);
          if (updated.events) {
            setEvents((prev) => deduplicateEvents(prev, updated.events || []));
          }
          if (decision === 'approve' && !TERMINAL_STATUSES.has(updated.status)) {
            setStreamStatus('connected');
          } else {
            setStreamStatus('closed');
          }
        }
        return updated;
      } catch (err: unknown) {
        setError(formatApiError(err, t));
        return null;
      }
    },
    [manager, runId, run, t]
  );

  // Answer a pending interrupt() input request
  const provideInput = useCallback(
    async (interruptId: string, value: unknown): Promise<RunRecord> => {
      if (!runId) throw new Error('No run selected');
      const updated = await manager.client.runs.provideInput(runId, interruptId, value);
      if (isMountedRef.current) {
        setRun(updated);
        if (updated.events) {
          setEvents((prev) => deduplicateEvents(prev, updated.events || []));
        }
        setStreamStatus(TERMINAL_STATUSES.has(updated.status) ? 'closed' : 'connected');
      }
      await fetchSnapshot();
      return updated;
    },
    [manager, runId, fetchSnapshot]
  );

  // Derived state calculations
  const nodeStatuses = extractNodeExecutionStatus(events, run?.current_node_id);
  const replaySteps = computeStateReplaySteps(run?.input_state || {}, events);

  return {
    run,
    events,
    streamStatus,
    maxSeq: maxSeqRef.current,
    loading,
    error,
    nodeStatuses,
    replaySteps,
    cancelRun,
    decideApproval,
    provideInput,
    refetchSnapshot: fetchSnapshot,
  };
}
