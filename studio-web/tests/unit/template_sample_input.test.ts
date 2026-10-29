import { afterEach, describe, expect, it } from 'vitest';
import { rememberSampleInput, sampleInputFor } from '../../src/features/templates/sampleInput';

/**
 * QA 2026-10-27: a workflow made from the approval template never paused, because the Run dialog
 * started with a generic input that has no amount. The template's sample input is now remembered
 * for the new workflow, per workspace, and the dialog starts with it.
 */
describe('template sample input', () => {
  afterEach(() => window.localStorage.clear());

  it('round-trips per workspace and workflow', () => {
    rememberSampleInput('default', 'wf-1', { title: 'Buy tickets', amount: 2500 });
    expect(sampleInputFor('default', 'wf-1')).toEqual({ title: 'Buy tickets', amount: 2500 });
    expect(sampleInputFor('other', 'wf-1')).toBeNull();
    expect(sampleInputFor('default', 'wf-2')).toBeNull();
  });

  it('ignores stored values that are not objects', () => {
    const stored = 'nexura.sampleInput:["default","wf-1"]';
    window.localStorage.setItem(stored, '[1, 2]');
    expect(sampleInputFor('default', 'wf-1')).toBeNull();
    window.localStorage.setItem(stored, '{not json');
    expect(sampleInputFor('default', 'wf-1')).toBeNull();
  });

  it('keeps workspace and workflow ids from colliding', () => {
    rememberSampleInput('a.b', 'c', { x: 1 });
    expect(sampleInputFor('a', 'b.c')).toBeNull();
  });
});
