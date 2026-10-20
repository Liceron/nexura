import { describe, it, expect } from 'vitest';
import {
  caseOf,
  draftOf,
  emptyDraft,
  exportDatasetJson,
  parseDatasetImport,
  validateCases,
} from '../../src/features/evaluations/evals_dataset_json';

const cases = [
  { name: 'a', input: 'hi', expected_output: 'hello', expected_trajectory: ['search', { name: 'send', args: { to: 'x' } }] },
  { name: 'b', input: { order_id: 'O1' } },
];

describe('dataset JSON', () => {
  it('round-trips export and import', () => {
    const text = exportDatasetJson({ dataset_id: 'd', name: 'D', description: '', cases });
    const parsed = parseDatasetImport(text);
    expect(parsed.ok).toBe(true);
    expect(parsed.dataset?.cases).toEqual(cases);
    expect(parsed.dataset?.dataset_id).toBe('d');
  });

  it('accepts a bare array of cases', () => {
    expect(parseDatasetImport(JSON.stringify(cases)).ok).toBe(true);
  });

  it.each([
    ['not json', '{oops'],
    ['wrong format', JSON.stringify({ format: 'other', cases })],
    ['empty', JSON.stringify({ cases: [] })],
    ['no input', JSON.stringify([{ name: 'x' }])],
    ['duplicate', JSON.stringify([{ name: 'x', input: 'a' }, { name: 'x', input: 'b' }])],
    ['bad trajectory', JSON.stringify([{ name: 'x', input: 'a', expected_trajectory: 'search' }])],
    ['scalar', '3'],
  ])('rejects %s', (_label, text) => {
    const r = parseDatasetImport(text);
    expect(r.ok).toBe(false);
    expect(r.errors.length).toBeGreaterThan(0);
  });

  it('rejects oversize datasets and inputs', () => {
    expect(validateCases(Array.from({ length: 201 }, (_, i) => ({ name: `c${i}`, input: 'x' })))[0]).toContain('at most');
    expect(validateCases([{ name: 'x', input: 'y'.repeat(8001) }])[0]).toContain('over');
  });
});

describe('case drafts', () => {
  it('turns drafts into cases and back', () => {
    const c = caseOf({ ...emptyDraft(), name: ' q ', input: 'text', expected_output: 'ans', trajectory: 'a\nb {"k":1}\n', tags: 'x, y' });
    expect(c).toEqual({
      name: 'q',
      input: 'text',
      expected_output: 'ans',
      expected_trajectory: ['a', { name: 'b', args: { k: 1 } }],
      tags: ['x', 'y'],
    });
    expect(caseOf(draftOf(c), c)).toEqual(c);
  });

  it('keeps JSON object inputs as objects and drops empty expectations', () => {
    const c = caseOf({ ...emptyDraft(), name: 'w', input: '{"order_id":"O1"}' });
    expect(c.input).toEqual({ order_id: 'O1' });
    expect('expected_output' in c).toBe(false);
    expect('expected_trajectory' in c).toBe(false);
  });
});
