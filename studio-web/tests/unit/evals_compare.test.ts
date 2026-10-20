import { describe, it, expect } from 'vitest';
import {
  buildComparisonRows,
  countChanges,
  filterResults,
  filterRows,
  groupByCase,
  pct,
  viewOfCase,
} from '../../src/features/evaluations/evals_compare';
import type { CaseResult, EvalReportJson } from '../../src/features/evaluations/studioEvalsTypes';

function result(
  name: string,
  status: CaseResult['status'],
  scores: Array<[string, number, boolean, string?]>,
  extra: Partial<CaseResult> = {},
): CaseResult {
  return {
    name,
    repeat: 0,
    status,
    scores: scores.map(([n, value, passed, reason]) => ({ name: n, value, passed, reason: reason ?? '' })),
    output_text: `out-${name}`,
    error: null,
    latency_seconds: 0.1,
    agent_path: [],
    metadata: { tags: [] },
    ...extra,
  };
}
const report = (results: CaseResult[]): EvalReportJson => ({
  target: 't',
  repeats: 1,
  scorer_names: ['exact_match', 'tool_trajectory'],
  results,
});

const baseline = report([
  result('a', 'passed', [['exact_match', 1, true], ['tool_trajectory', 1, true]]),
  result('b', 'failed', [['exact_match', 0, false, 'output differs'], ['tool_trajectory', 1, true]]),
  result('c', 'passed', [['exact_match', 1, true]]),
  result('gone', 'passed', [['exact_match', 1, true]]),
]);
const candidate = report([
  result('a', 'failed', [['exact_match', 1, true], ['tool_trajectory', 0.5, false, 'missing: router']]),
  result('b', 'passed', [['exact_match', 1, true], ['tool_trajectory', 1, true]]),
  result('c', 'passed', [['exact_match', 1, true]]),
  result('fresh', 'passed', [['exact_match', 1, true]]),
]);
// What the server (SDK EvalReport.compare) returns for the two reports above.
const verdict = {
  regressions: [
    { case: 'a', scorer: 'tool_trajectory', baseline: 1, candidate: 0.5, detail: '' },
    { case: 'gone', scorer: null, baseline: 'passed', candidate: 'missing', detail: 'case not in report' },
  ],
  improvements: [{ case: 'b', scorer: 'exact_match', baseline: 0, candidate: 1, detail: '' }],
};

describe('buildComparisonRows', () => {
  const rows = buildComparisonRows(baseline, candidate, verdict);
  const byName = Object.fromEntries(rows.map((r) => [r.name, r]));

  it('classifies cases from the server verdict', () => {
    expect(byName.a.change).toBe('regression');
    expect(byName.b.change).toBe('improvement');
    expect(byName.c.change).toBe('unchanged');
    expect(byName.fresh.change).toBe('new');
  });

  it('a case missing from the candidate is a regression (SDK rule), not silently dropped', () => {
    expect(byName.gone.change).toBe('regression');
    expect(byName.gone.candidate).toBeNull();
    expect(byName.gone.notes[0]).toContain('case not in report');
  });

  it('marks only the regressed scorer and keeps both sides values', () => {
    const t = byName.a.scorers.find((s) => s.scorer === 'tool_trajectory')!;
    expect(t).toMatchObject({ baseline: 1, candidate: 0.5, change: 'regression' });
    expect(byName.a.scorers.find((s) => s.scorer === 'exact_match')!.change).toBe('unchanged');
    expect(byName.b.scorers.find((s) => s.scorer === 'exact_match')!.change).toBe('improvement');
  });

  it('counts and filters', () => {
    expect(countChanges(rows)).toEqual({ regression: 2, improvement: 1, unchanged: 1, new: 1, missing: 0 });
    expect(filterRows(rows, { change: 'regression' }).map((r) => r.name)).toEqual(['a', 'gone']);
    expect(filterRows(rows, { change: 'changed' }).map((r) => r.name)).not.toContain('c');
    expect(filterRows(rows, { change: 'all', query: 'FRE' }).map((r) => r.name)).toEqual(['fresh']);
  });

  it('handles undefined reports', () => {
    expect(buildComparisonRows(undefined, undefined, { regressions: [], improvements: [] })).toEqual([]);
  });
});

describe('viewOfCase', () => {
  it('averages scores over repeats and keeps the worst status', () => {
    const v = viewOfCase([
      result('x', 'passed', [['s', 1, true]]),
      result('x', 'failed', [['s', 0, false, 'bad']]),
    ]);
    expect(v.status).toBe('failed');
    expect(v.scores.s).toBe(0.5);
    expect(v.reasons.s).toBe('bad');
  });

  it('surfaces errors', () => {
    const v = viewOfCase([result('x', 'error', [], { error: { type: 'E', message: 'boom' } })]);
    expect(v.status).toBe('error');
    expect(v.error).toBe('boom');
  });

  it('groups repeats by case name', () => {
    const g = groupByCase(report([result('x', 'passed', []), result('x', 'passed', []), result('y', 'passed', [])]));
    expect(g.get('x')).toHaveLength(2);
  });
});

describe('filterResults', () => {
  const results = [
    result('ok', 'passed', [['exact_match', 1, true]], { metadata: { tags: ['orders'] } }),
    result('bad', 'failed', [['exact_match', 0, false, 'nope']], { output_text: 'Refund text' }),
    result('crash', 'error', [], { error: { type: 'E', message: 'x' } }),
  ];
  it('filters by status, tag, text and failing', () => {
    expect(filterResults(results, { status: 'failed' }).map((r) => r.name)).toEqual(['bad']);
    expect(filterResults(results, { tag: 'orders' }).map((r) => r.name)).toEqual(['ok']);
    expect(filterResults(results, { query: 'refund' }).map((r) => r.name)).toEqual(['bad']);
    expect(filterResults(results, { failingOnly: true }).map((r) => r.name)).toEqual(['bad', 'crash']);
    expect(filterResults(results, { scorer: 'exact_match', failingOnly: true }).map((r) => r.name)).toEqual(['bad']);
    expect(filterResults(results, { status: 'all' })).toHaveLength(3);
  });
  it('formats percentages', () => {
    expect(pct(0.5)).toBe('50.0%');
  });
});
