// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
// Pure comparison/filter logic for the Studio evals view. The server (SDK
// EvalReport.compare) decides what is a regression; this joins that verdict with the two
// stored reports so the UI can show them side by side.
import type {
  CaseResult,
  CaseStatus,
  CompareResponse,
  EvalReportJson,
} from './studioEvalsTypes';

export type RowChange = 'regression' | 'improvement' | 'unchanged' | 'new' | 'missing';

export interface CaseView {
  status: CaseStatus;
  /** Mean score per scorer over repeats. */
  scores: Record<string, number>;
  output_text: string;
  reasons: Record<string, string>;
  error: string | null;
}

export interface ScorerDelta {
  scorer: string;
  baseline: number | null;
  candidate: number | null;
  change: 'regression' | 'improvement' | 'unchanged';
}

export interface ComparisonRow {
  name: string;
  change: RowChange;
  baseline: CaseView | null;
  candidate: CaseView | null;
  scorers: ScorerDelta[];
  notes: string[];
}

const STATUS_RANK: Record<CaseStatus, number> = { passed: 0, failed: 1, paused: 2, error: 3 };

/** Group repeats of one case into one view: worst status, mean scores, first reasons. */
export function viewOfCase(runs: CaseResult[]): CaseView {
  let status: CaseStatus = 'passed';
  const sums: Record<string, number> = {};
  const counts: Record<string, number> = {};
  const reasons: Record<string, string> = {};
  for (const run of runs) {
    if (STATUS_RANK[run.status] > STATUS_RANK[status]) status = run.status;
    for (const s of run.scores) {
      sums[s.name] = (sums[s.name] ?? 0) + s.value;
      counts[s.name] = (counts[s.name] ?? 0) + 1;
      if (!s.passed && s.reason && !(s.name in reasons)) reasons[s.name] = s.reason;
    }
  }
  const scores: Record<string, number> = {};
  for (const k of Object.keys(sums)) scores[k] = sums[k] / counts[k];
  return {
    status,
    scores,
    reasons,
    output_text: runs[0]?.output_text ?? '',
    error: runs.find((r) => r.error)?.error?.message ?? null,
  };
}

export function groupByCase(report: EvalReportJson | undefined): Map<string, CaseResult[]> {
  const out = new Map<string, CaseResult[]>();
  for (const r of report?.results ?? []) {
    const list = out.get(r.name);
    if (list) list.push(r);
    else out.set(r.name, [r]);
  }
  return out;
}

export function buildComparisonRows(
  baseline: EvalReportJson | undefined,
  candidate: EvalReportJson | undefined,
  verdict: Pick<CompareResponse, 'regressions' | 'improvements'>,
): ComparisonRow[] {
  const base = groupByCase(baseline);
  const cand = groupByCase(candidate);
  const names = [...base.keys()];
  for (const n of cand.keys()) if (!base.has(n)) names.push(n);
  const regressedCases = new Map<string, string[]>();
  const improvedCases = new Map<string, string[]>();
  const regressedScorers = new Set<string>();
  const improvedScorers = new Set<string>();
  for (const c of verdict.regressions) {
    regressedCases.set(c.case, [...(regressedCases.get(c.case) ?? []), describe(c)]);
    if (c.scorer) regressedScorers.add(`${c.case}\u0000${c.scorer}`);
  }
  for (const c of verdict.improvements) {
    improvedCases.set(c.case, [...(improvedCases.get(c.case) ?? []), describe(c)]);
    if (c.scorer) improvedScorers.add(`${c.case}\u0000${c.scorer}`);
  }
  return names.map((name): ComparisonRow => {
    const b = base.has(name) ? viewOfCase(base.get(name)!) : null;
    const c = cand.has(name) ? viewOfCase(cand.get(name)!) : null;
    let change: RowChange = 'unchanged';
    if (regressedCases.has(name)) change = 'regression';
    else if (!b) change = 'new';
    else if (!c) change = 'missing';
    else if (improvedCases.has(name)) change = 'improvement';
    const scorerNames = [...new Set([...Object.keys(b?.scores ?? {}), ...Object.keys(c?.scores ?? {})])];
    const scorers = scorerNames.map((scorer): ScorerDelta => {
      const key = `${name}\u0000${scorer}`;
      return {
        scorer,
        baseline: b && scorer in b.scores ? b.scores[scorer] : null,
        candidate: c && scorer in c.scores ? c.scores[scorer] : null,
        change: regressedScorers.has(key)
          ? 'regression'
          : improvedScorers.has(key)
            ? 'improvement'
            : 'unchanged',
      };
    });
    return {
      name,
      change,
      baseline: b,
      candidate: c,
      scorers,
      notes: [...(regressedCases.get(name) ?? []), ...(improvedCases.get(name) ?? [])],
    };
  });
}

function describe(c: { scorer: string | null; baseline: number | string; candidate: number | string; detail: string }): string {
  const where = c.scorer ?? 'case';
  return `${where}: ${c.baseline} -> ${c.candidate}${c.detail ? ` (${c.detail})` : ''}`;
}

export interface RowFilter {
  change?: RowChange | 'all' | 'changed';
  query?: string;
}

export function filterRows(rows: ComparisonRow[], filter: RowFilter): ComparisonRow[] {
  const q = (filter.query ?? '').trim().toLowerCase();
  return rows.filter((row) => {
    if (filter.change === 'changed' && (row.change === 'unchanged')) return false;
    if (filter.change && filter.change !== 'all' && filter.change !== 'changed' && row.change !== filter.change) {
      return false;
    }
    return !q || row.name.toLowerCase().includes(q);
  });
}

export function countChanges(rows: ComparisonRow[]): Record<RowChange, number> {
  const out: Record<RowChange, number> = { regression: 0, improvement: 0, unchanged: 0, new: 0, missing: 0 };
  for (const r of rows) out[r.change] += 1;
  return out;
}

export interface ResultFilter {
  status?: CaseStatus | 'all';
  scorer?: string | 'all';
  /** Only rows where this scorer did not pass. */
  failingOnly?: boolean;
  tag?: string;
  query?: string;
}

export function filterResults(results: CaseResult[], f: ResultFilter): CaseResult[] {
  const q = (f.query ?? '').trim().toLowerCase();
  return results.filter((r) => {
    if (f.status && f.status !== 'all' && r.status !== f.status) return false;
    if (f.tag && !(r.metadata?.tags ?? []).includes(f.tag)) return false;
    if (f.scorer && f.scorer !== 'all') {
      const s = r.scores.find((x) => x.name === f.scorer);
      if (!s) return false;
      if (f.failingOnly && s.passed) return false;
    } else if (f.failingOnly && r.scores.every((s) => s.passed) && r.status !== 'error') {
      return false;
    }
    return !q || r.name.toLowerCase().includes(q) || r.output_text.toLowerCase().includes(q);
  });
}

export function pct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}
