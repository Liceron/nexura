import { describe, it, expect } from 'vitest';
import {
  buildAnswer,
  canForkCheckpoint,
  describeApprovalDetails,
  describeForkedFrom,
  describeInterrupt,
  formatCheckpointTime,
  parseJsonObject,
  schemaTemplate,
  subRunLabel,
  validateAgainstSchema,
} from '../../src/features/runs/durable';

const SCHEMA = {
  type: 'object',
  properties: { region: { type: 'string', enum: ['EU', 'US'] }, qty: { type: 'integer', minimum: 1 } },
  required: ['region'],
};

describe('parseJsonObject', () => {
  it('treats empty text as no value', () => {
    expect(parseJsonObject('  ')).toEqual({ value: undefined, error: null });
  });
  it('parses objects and rejects arrays, scalars and syntax errors', () => {
    expect(parseJsonObject('{"a":1}').value).toEqual({ a: 1 });
    expect(parseJsonObject('[1]').error).toMatch(/JSON object/);
    expect(parseJsonObject('3').error).toMatch(/JSON object/);
    expect(parseJsonObject('{bad').error).toMatch(/JSON syntax error/);
  });
});

describe('describeInterrupt', () => {
  it('reads question and schema', () => {
    expect(describeInterrupt({ question: 'Where?', schema: SCHEMA })).toEqual({ question: 'Where?', schema: SCHEMA });
    expect(describeInterrupt('plain')).toEqual({ question: 'plain', schema: null });
    expect(describeInterrupt({ json_schema: { type: 'string' } }).schema).toEqual({ type: 'string' });
    expect(describeInterrupt(null)).toEqual({ question: null, schema: null });
    expect(describeInterrupt({ question: 3, schema: 'x' })).toEqual({ question: null, schema: null });
  });
});

describe('validateAgainstSchema', () => {
  it('accepts a conforming value', () => {
    expect(validateAgainstSchema({ region: 'EU', qty: 2 }, SCHEMA)).toEqual([]);
  });
  it('reports missing, wrong type, enum and range errors with paths', () => {
    expect(validateAgainstSchema({}, SCHEMA)).toEqual(['$.region: required']);
    expect(validateAgainstSchema({ region: 'ASIA' }, SCHEMA)[0]).toMatch(/\$\.region: must be one of/);
    expect(validateAgainstSchema({ region: 'EU', qty: 1.5 }, SCHEMA)[0]).toMatch(/expected integer/);
    expect(validateAgainstSchema({ region: 'EU', qty: 0 }, SCHEMA)[0]).toMatch(/>= 1/);
    expect(validateAgainstSchema('x', SCHEMA)[0]).toMatch(/expected object, got string/);
  });
  it('checks array items and string length', () => {
    expect(validateAgainstSchema([1, 'a'], { type: 'array', items: { type: 'number' } })).toEqual([
      '$[1]: expected number, got string',
    ]);
    expect(validateAgainstSchema('ab', { type: 'string', minLength: 3 })[0]).toMatch(/at least 3/);
  });
});

describe('buildAnswer', () => {
  it('parses JSON, falls back to text, and sends raw text for string schemas', () => {
    expect(buildAnswer('{"a":1}', null)).toEqual({ value: { a: 1 }, errors: [] });
    expect(buildAnswer('eu-west', null)).toEqual({ value: 'eu-west', errors: [] });
    expect(buildAnswer('42', { type: 'string' })).toEqual({ value: '42', errors: [] });
  });
  it('blocks answers that do not satisfy the schema', () => {
    const bad = buildAnswer('{"region":"ASIA"}', SCHEMA);
    expect(bad.value).toBeUndefined();
    expect(bad.errors.length).toBe(1);
    expect(buildAnswer('{"region":"US"}', SCHEMA).errors).toEqual([]);
  });
});

describe('schemaTemplate', () => {
  it('builds a skeleton for object schemas only', () => {
    expect(JSON.parse(schemaTemplate(SCHEMA))).toEqual({ region: '', qty: 0 });
    expect(schemaTemplate({ type: 'string' })).toBe('');
    expect(schemaTemplate(null)).toBe('');
  });
});

describe('checkpoint helpers', () => {
  it('formats seconds and milliseconds, tolerates missing', () => {
    expect(formatCheckpointTime(0)).toBe('1970-01-01 00:00:00 UTC');
    expect(formatCheckpointTime(1_700_000_000)).toBe(formatCheckpointTime(1_700_000_000_000));
    expect(formatCheckpointTime(undefined)).toBe('unknown time');
  });
  it('cannot fork the end of a run', () => {
    expect(canForkCheckpoint({ current_node_id: 'a' })).toBe(true);
    expect(canForkCheckpoint({ current_node_id: null })).toBe(false);
  });
  it('describes forked_from', () => {
    expect(describeForkedFrom({ workflow_run_id: 'run-1', checkpoint_index: 2, state_changed: true })).toEqual({
      parentRunId: 'run-1',
      checkpointIndex: 2,
      stateChanged: true,
    });
    expect(describeForkedFrom(null)).toBeNull();
    expect(describeForkedFrom({ checkpoint_index: 1 })).toBeNull();
  });
});

describe('sub-run helpers', () => {
  it('labels children relative to the parent', () => {
    expect(subRunLabel('run-1/s#2', 'run-1')).toBe('s#2');
    expect(subRunLabel('other/s#2', 'run-1')).toBe('other/s#2');
  });
});

describe('describeApprovalDetails', () => {
  const items = Array.from({ length: 25 }, (_, i) => ({ operation: 'post_message', params: { text: `m${i}` } }));
  it('shows the first 20 items and counts the rest', () => {
    const v = describeApprovalDetails({
      details: { map_over: 'items', item_count: 25, items, max_writes_per_approval: 50 },
    });
    expect(v?.itemCount).toBe(25);
    expect(v?.shownItems).toHaveLength(20);
    expect(v?.moreCount).toBe(5);
    expect(v?.maxWrites).toBe(50);
    expect(v?.mapOver).toBe('items');
  });
  it('has no "more" for short lists and returns null without details', () => {
    expect(describeApprovalDetails({ details: { item_count: 2, items: items.slice(0, 2) } })?.moreCount).toBe(0);
    expect(describeApprovalDetails({ approval_id: 'a' })).toBeNull();
    expect(describeApprovalDetails(null)).toBeNull();
  });
});

describe('P13 review fixes', () => {
  it('matches string enum values before JSON parsing', () => {
    expect(buildAnswer('1', { enum: ['1', '2'] })).toEqual({ value: '1', errors: [] });
    expect(buildAnswer('1', { enum: [1, 2] })).toEqual({ value: 1, errors: [] });
  });
  it('multi-type schemas keep text as string unless another listed type matches', () => {
    expect(buildAnswer('123', { type: ['string', 'null'] }).value).toBe('123');
    expect(buildAnswer('null', { type: ['string', 'null'] }).value).toBe(null);
    expect(buildAnswer('5', { type: ['string', 'boolean'] }).value).toBe('5');
  });
  it('required ignores inherited keys', () => {
    expect(buildAnswer('{}', { type: 'object', required: ['toString'] }).errors.length).toBeGreaterThan(0);
  });
  it('item_count beyond the listed items adds no phantom more-count', () => {
    const v = describeApprovalDetails({ details: { items: [1, 2], item_count: 1000 } });
    expect(v?.moreCount).toBe(0);
    expect(v?.listedCount).toBe(2);
  });
});
