// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { BuilderNodeData } from './types';
import en from '../../i18n/en.json';
import { useI18n } from '../../i18n';

/** Reducers the engine accepts (mirrors REDUCERS in src/nexura_runtime/workflow.py). */
export const REDUCER_CHOICES = ['append', 'extend', 'sum', 'merge', 'max', 'min'] as const;

export const DEFAULT_MAP_AS = 'item';
export const DEFAULT_MAX_CONCURRENCY = 10;
export const MAX_CONCURRENCY_LIMIT = 100;

export type StepKind = 'step' | 'parallel' | 'map' | 'subworkflow';

/** Which kind of step a node is, derived from the real spec fields (no separate flag). */
export function stepKind(d: Partial<BuilderNodeData>): StepKind {
  if (d.subgraph || d.pendingKind === 'subworkflow') return 'subworkflow';
  if (d.parallel) return 'parallel';
  if (d.map_over !== undefined) return 'map';
  return 'step';
}

export function isComposite(d: Partial<BuilderNodeData>): boolean {
  return stepKind(d) !== 'step';
}

/** True for steps that run no handler of their own (parallel and sub-workflow). */
export function hasNoOwnHandler(d: Partial<BuilderNodeData>): boolean {
  const k = stepKind(d);
  return k === 'parallel' || k === 'subworkflow';
}

export type CompositeField = 'handler' | 'parallel' | 'map_over' | 'map_as' | 'max_concurrency' | 'reducers' | 'subgraph';

export interface CompositeIssue {
  field: CompositeField;
  /** i18n key under builder.errors */
  key: string;
}

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Client-side mirror of the engine's composite rules (_validate_composite). It only gives
 * early inline feedback; the validate endpoint stays authoritative.
 */
export function compositeIssues(d: Partial<BuilderNodeData>): CompositeIssue[] {
  const out: CompositeIssue[] = [];
  const kind = stepKind(d);
  if (kind === 'step') return out;
  if (kind === 'parallel') {
    const names = d.parallel || [];
    if (names.length < 1) out.push({ field: 'parallel', key: 'parallelNeedsOne' });
    if (new Set(names).size !== names.length || names.some((n) => !n)) {
      out.push({ field: 'parallel', key: 'parallelDistinct' });
    }
  }
  if (kind === 'subworkflow' && !d.subgraph) out.push({ field: 'subgraph', key: 'subgraphMissing' });
  if (kind === 'map' && !(d.handler || d.module_id)) {
    out.push({ field: 'handler', key: 'mapNeedsHandler' });
  }
  if (d.map_over !== undefined) {
    if (!d.map_over.trim()) out.push({ field: 'map_over', key: 'mapOverRequired' });
    const as = d.map_as ?? DEFAULT_MAP_AS;
    if (!IDENT.test(as)) out.push({ field: 'map_as', key: 'mapAsInvalid' });
  }
  if (d.max_concurrency !== undefined) {
    const c = d.max_concurrency;
    if (!Number.isInteger(c) || c < 1 || c > MAX_CONCURRENCY_LIMIT) {
      out.push({ field: 'max_concurrency', key: 'concurrencyRange' });
    }
  }
  if (d.reducers && Object.keys(d.reducers).length > 0) {
    if (kind === 'subworkflow' && d.map_over === undefined) {
      out.push({ field: 'reducers', key: 'reducersNeedFanOut' });
    }
    const bad = Object.values(d.reducers).some((r) => !(REDUCER_CHOICES as readonly string[]).includes(r));
    const emptyKey = Object.keys(d.reducers).some((k) => !k.trim());
    if (bad || emptyKey) out.push({ field: 'reducers', key: 'reducerInvalid' });
  }
  return out;
}

type Dict = Record<string, unknown>;

function lookup(dict: Dict, path: string): string | undefined {
  let cur: unknown = dict;
  for (const part of path.split('.')) {
    if (cur && typeof cur === 'object' && part in (cur as Dict)) cur = (cur as Dict)[part];
    else return undefined;
  }
  return typeof cur === 'string' ? cur : undefined;
}

/** Translator for builder strings; falls back to English when no I18nProvider is mounted. */
export function useBuilderT(): (key: string, vars?: Record<string, string | number> | string) => string {
  let translate: ((key: string) => string) | null = null;
  try {
    const ctx = useI18n();
    translate = (k: string) => ctx.t(k);
  } catch {
    translate = null;
  }
  return (key, vars) => {
    const raw = translate ? translate(key) : key;
    let text = raw === key ? (lookup(en as Dict, key) ?? (typeof vars === 'string' ? vars : key)) : raw;
    if (vars && typeof vars !== 'string') {
      for (const [k, v] of Object.entries(vars)) text = text.replace(`{${k}}`, String(v));
    }
    return text;
  };
}
