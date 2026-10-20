// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
export type SpanKind = 'run' | 'node' | 'agent' | 'model' | 'tool' | 'handoff' | 'retry' | 'error' | 'note';
export type SpanStatus = 'ok' | 'error' | 'running' | 'unknown';

export interface TraceSpan {
  span_id: string;
  parent_id: string | null;
  kind: SpanKind;
  name: string;
  status: SpanStatus;
  start_ms: number | null;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cost_usd: string | null;
  model_id?: string | null;
  module_id?: string | null;
  error: unknown;
  input: unknown;
  output: unknown;
  has_content: boolean;
}

export interface RunTrace {
  run_id: string;
  status: string;
  include_content: boolean;
  truncated: boolean;
  span_count: number;
  duration_ms: number | null;
  spans: TraceSpan[];
}

export interface PositionedSpan {
  span: TraceSpan;
  depth: number;
}

/** Depth-first order with indentation depth, so children follow their parent. */
export function orderSpans(spans: TraceSpan[]): PositionedSpan[] {
  const children = new Map<string | null, TraceSpan[]>();
  const known = new Set(spans.map((s) => s.span_id));
  for (const span of spans) {
    const parent = span.parent_id && known.has(span.parent_id) ? span.parent_id : null;
    const list = children.get(parent) ?? [];
    list.push(span);
    children.set(parent, list);
  }
  const out: PositionedSpan[] = [];
  const visit = (parent: string | null, depth: number) => {
    for (const span of children.get(parent) ?? []) {
      out.push({ span, depth });
      visit(span.span_id, depth + 1);
    }
  };
  visit(null, 0);
  return out;
}

/** Keep spans with the wanted status plus their ancestors, so the tree stays readable. */
export function filterSpans(spans: TraceSpan[], status: 'all' | 'error'): TraceSpan[] {
  if (status === 'all') return spans;
  const byId = new Map(spans.map((s) => [s.span_id, s]));
  const keep = new Set<string>();
  for (const span of spans) {
    if (span.status !== 'error') continue;
    let cur: TraceSpan | undefined = span;
    while (cur && !keep.has(cur.span_id)) {
      keep.add(cur.span_id);
      cur = cur.parent_id ? byId.get(cur.parent_id) : undefined;
    }
  }
  return spans.filter((s) => keep.has(s.span_id));
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return 'n/a';
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${ms.toFixed(ms < 10 ? 2 : 0)} ms`;
}

export function formatJson(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2) ?? '';
  } catch {
    return String(value);
  }
}
