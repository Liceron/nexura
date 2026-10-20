// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { NavTab } from './Sidebar';
import { studioExtensions, type StudioExtensionRegistry } from './extensions';

/** What the Studio hash route selects (P25 adds run pages). */
export interface ParsedRoute {
  tab: NavTab;
  /** `#/workflows/{id}` and `#/workflows/{id}/runs/{runId}`. */
  workflowId: string | null;
  /** Run open in the workflow page's Run Console: `#/workflows/{id}/runs/{runId}`. */
  workflowRunId: string | null;
  /** Standalone run page: `#/runs/{runId}`. */
  runId: string | null;
  /** `#/traces/{runId}`. */
  traceRunId: string | null;
  /** B39: `#/triggers/{workflowId}` opens the triggers view preselected for that workflow. */
  triggersWorkflowId: string | null;
}

const SIMPLE: Record<string, NavTab> = {
  '#/templates': 'templates',
  '#/approvals': 'approvals',
  '#/playground': 'playground',
  '#/chat': 'playground',
  '#/knowledge': 'knowledge',
  '#/skills': 'skills',
  '#/agents': 'agents',
  '#/credentials': 'credentials',
  '#/tools': 'tools',
  '#/integrations': 'tools',
  '#/evaluations': 'evaluations',
  '#/triggers': 'triggers',
  '#/onboarding': 'onboarding',
  '#/system': 'system',
};

/** decodeURIComponent that keeps a malformed escape as typed instead of throwing. */
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * Parse `window.location.hash`; anything unknown is the workflow list. Hashes registered by
 * Studio extensions are matched after the built-in ones, so they can never replace them.
 */
export function parseHash(
  rawHash: string,
  extensions: StudioExtensionRegistry = studioExtensions,
): ParsedRoute {
  const hash = rawHash || '#/workflows';
  const base: ParsedRoute = {
    tab: 'workflows',
    workflowId: null,
    workflowRunId: null,
    runId: null,
    traceRunId: null,
    triggersWorkflowId: null,
  };

  if (hash === '#/traces' || hash.startsWith('#/traces/')) {
    const id = hash.replace('#/traces', '').replace(/^\//, '').trim();
    return { ...base, tab: 'traces', traceRunId: id ? safeDecode(id) : null };
  }
  if (hash.startsWith('#/triggers/')) {
    const id = hash.slice('#/triggers/'.length).trim();
    return { ...base, tab: 'triggers', triggersWorkflowId: id ? safeDecode(id) : null };
  }
  if (hash === '#/runs' || hash.startsWith('#/runs/')) {
    const id = hash.slice('#/runs/'.length).trim();
    if (hash !== '#/runs' && id) return { ...base, runId: safeDecode(id) };
    return { ...base, tab: 'traces' }; // the run list lives on the traces page
  }
  if (hash.startsWith('#/workflows/')) {
    const rest = hash.slice('#/workflows/'.length).trim();
    const marker = rest.indexOf('/runs/');
    if (marker === 0) return base; // "#/workflows//runs/x": no workflow id
    if (marker > 0) {
      const runId = rest.slice(marker + '/runs/'.length).trim();
      return { ...base, workflowId: rest.slice(0, marker), workflowRunId: runId ? safeDecode(runId) : null };
    }
    if (rest) return { ...base, workflowId: rest };
  }
  const tab = SIMPLE[hash] ?? extensions.routeTab(hash);
  return tab ? { ...base, tab } : base;
}

/** Hash of the standalone run page. */
export function runHref(runId: string): string {
  return `#/runs/${encodeURIComponent(runId)}`;
}

/** Hash of a workflow page with the run open in its Run Console (reload keeps it). */
export function workflowRunHref(workflowId: string, runId: string): string {
  return `#/workflows/${workflowId}/runs/${encodeURIComponent(runId)}`;
}

/** B39: hash of the triggers view preselected for one workflow. */
export function triggersHref(workflowId: string): string {
  return `#/triggers/${encodeURIComponent(workflowId)}`;
}
