// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { expect, it } from 'vitest';
import { formatToolResult } from '../../src/features/chat/formatToolResult';
import { parseHash } from '../../src/app/routes';

it('tool output removes null fields while preserving useful zero and false values', () => {
  expect(formatToolResult('{"result":42,"error":null,"cached":false,"usage":{"tokens":0,"cost":null}}')).toBe('result: 42\ncached: false\nusage: tokens: 0');
  expect(formatToolResult([null, { answer: 'hello', empty: null }])).toBe('answer: hello');
  expect(formatToolResult('Plain text')).toBe('Plain text');
});

it('agent hash context does not change the selected Studio page', () => {
  expect(parseHash('#/playground?agent=a1').tab).toBe('playground');
  expect(parseHash('#/evaluations?agent=a1').tab).toBe('evaluations');
});

it('decodes only the top-level tool result and keeps nested strings verbatim', () => {
  const value = { id: '9007199254740993', status: 'null', nested: { json: '{"answer":1}', text: '"quoted"' }, items: ['true', '001', null] };
  const expected = 'id: 9007199254740993\nstatus: null\nnested: json: {"answer":1}\ntext: "quoted"\nitems: true\n001';
  expect(formatToolResult(JSON.stringify(value))).toBe(expected);
  expect(formatToolResult(value)).toBe(expected);
  expect(formatToolResult(JSON.stringify('9007199254740993'))).toBe('9007199254740993');
  expect(formatToolResult(JSON.stringify('null'))).toBe('null');
});
