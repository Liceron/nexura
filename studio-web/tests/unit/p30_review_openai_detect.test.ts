// P30 review regression (promoted from .autopilot/p30/repros): Studio's detector must route every
// shape the server converter accepts.
import { describe, expect, it } from 'vitest';
import { looksLikeOpenAiEvals } from '../../src/features/evaluations/evals_dataset_json';

describe('looksLikeOpenAiEvals (P30 review)', () => {
  it('detects a JSON array of openai/evals registry rows (the converter accepts it)', () => {
    const text = JSON.stringify([
      { input: [{ role: 'user', content: 'Capital of France?' }], ideal: 'Paris' },
      { input: [{ role: 'user', content: '2+2?' }], ideal: '4' },
    ]);
    expect(looksLikeOpenAiEvals(text)).toBe(true);
  });

  it('detects a single registry row', () => {
    expect(looksLikeOpenAiEvals('{"input": [{"role": "user", "content": "hi"}], "ideal": "hello"}')).toBe(true);
  });
});
