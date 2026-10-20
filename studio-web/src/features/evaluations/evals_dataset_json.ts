// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// Dataset editor helpers: import/export JSON and case validation (mirrors server limits;
// the server remains authoritative).
import { NexuraApiError } from '@liceron/nexura-sdk';
import { englishT, type Translate } from '../../i18n';
import type { EvalCase } from './studioEvalsTypes';

export const DATASET_FORMAT = 'nexura.eval-dataset';
export const MAX_CASES = 200;
export const MAX_TEXT = 8000;

export interface ImportResult {
  ok: boolean;
  errors: string[];
  dataset?: { dataset_id?: string; name: string; description: string; cases: EvalCase[] };
}

export function validateCases(cases: unknown, t: Translate = englishT): string[] {
  const errors: string[] = [];
  if (!Array.isArray(cases) || cases.length === 0) return [t('validationMessages.datasetNeedsCase')];
  if (cases.length > MAX_CASES) return [t('validationMessages.datasetCaseLimit').replace('{v1}', String(MAX_CASES))];
  const seen = new Set<string>();
  cases.forEach((raw, i) => {
    const where = `cases[${i}]`;
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
      errors.push(t('validationMessages.caseObject').replace('{v1}', String(where)));
      return;
    }
    const c = raw as Record<string, unknown>;
    if (typeof c.name !== 'string' || !c.name.trim() || c.name.length > 120) {
      errors.push(t('validationMessages.caseNameLength').replace('{v1}', String(where)));
    } else if (seen.has(c.name.trim())) {
      errors.push(t('validationMessages.caseNameDuplicate').replace('{v1}', String(where)).replace('{v2}', String(c.name)));
    } else {
      seen.add(c.name.trim());
    }
    const input = c.input;
    if (typeof input === 'string') {
      if (!input.trim()) errors.push(t('validationMessages.caseInputRequired').replace('{v1}', String(where)));
      else if (input.length > MAX_TEXT) errors.push(t('validation.caseInputLength').replace('{path}', where).replace('{max}', String(MAX_TEXT)));
    } else if (typeof input !== 'object' || input === null || Array.isArray(input) || !Object.keys(input).length) {
      errors.push(t('validationMessages.caseInputType').replace('{v1}', String(where)));
    }
    const traj = c.expected_trajectory;
    if (traj !== undefined && traj !== null) {
      const okStep = (s: unknown) =>
        (typeof s === 'string' && s.trim() !== '') ||
        (typeof s === 'object' && s !== null && typeof (s as { name?: unknown }).name === 'string');
      if (!Array.isArray(traj) || !traj.every(okStep)) {
        errors.push(t('validationMessages.caseTrajectory').replace('{v1}', String(where)));
      }
    }
  });
  return errors;
}

export function parseDatasetImport(text: string, t: Translate = englishT): ImportResult {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (e) {
    return { ok: false, errors: [t('validationMessages.invalidJson').replace('{v1}', String((e as Error).message))] };
  }
  // Accept the exported document or a bare array of cases.
  const doc = Array.isArray(raw) ? { cases: raw } : (raw as Record<string, unknown>);
  if (typeof doc !== 'object' || doc === null) return { ok: false, errors: [t('validationMessages.jsonObjectOrArray')] };
  if (doc.format !== undefined && doc.format !== DATASET_FORMAT) {
    return { ok: false, errors: [t('validationMessages.unsupportedFormat').replace('{v1}', String(String(doc.format))).replace('{v2}', String(DATASET_FORMAT))] };
  }
  const errors = validateCases(doc.cases, t);
  if (errors.length) return { ok: false, errors };
  return {
    ok: true,
    errors: [],
    dataset: {
      dataset_id: typeof doc.dataset_id === 'string' ? doc.dataset_id : undefined,
      name: typeof doc.name === 'string' ? doc.name : '',
      description: typeof doc.description === 'string' ? doc.description : '',
      cases: doc.cases as EvalCase[],
    },
  };
}

export function exportDatasetJson(ds: {
  dataset_id: string;
  name: string;
  description: string;
  cases: EvalCase[];
}): string {
  return JSON.stringify(
    {
      format: DATASET_FORMAT,
      format_version: 1,
      dataset_id: ds.dataset_id,
      name: ds.name,
      description: ds.description,
      cases: ds.cases,
    },
    null,
    2,
  );
}

/** Editable form of a case: text fields instead of nested JSON. */
export interface CaseDraft {
  name: string;
  input: string;
  expected_output: string;
  trajectory: string;
  tags: string;
}

export function emptyDraft(): CaseDraft {
  return { name: '', input: '', expected_output: '', trajectory: '', tags: '' };
}

export function draftOf(c: EvalCase): CaseDraft {
  return {
    name: c.name,
    input: typeof c.input === 'string' ? c.input : JSON.stringify(c.input),
    expected_output:
      c.expected_output === undefined || c.expected_output === null
        ? ''
        : typeof c.expected_output === 'string'
          ? c.expected_output
          : JSON.stringify(c.expected_output),
    trajectory: (c.expected_trajectory ?? [])
      .map((s) => (typeof s === 'string' ? s : s.args ? `${s.name} ${JSON.stringify(s.args)}` : s.name))
      .join('\n'),
    tags: (c.tags ?? []).join(', '),
  };
}

/** Draft back to a case. Input that parses as a JSON object stays an object (workflow state). */
export function caseOf(d: CaseDraft, base?: EvalCase): EvalCase {
  const trimmed = d.input.trim();
  let input: EvalCase['input'] = d.input;
  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) input = parsed;
    } catch {
      /* keep as text */
    }
  }
  const out: EvalCase = { ...(base ?? {}), name: d.name.trim(), input };
  const expected = d.expected_output;
  if (expected.trim() === '') delete out.expected_output;
  else if (base && typeof base.expected_output === 'object' && base.expected_output !== null && expected === JSON.stringify(base.expected_output)) {
    out.expected_output = base.expected_output;
  } else out.expected_output = expected;
  const steps = d.trajectory
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      const space = line.indexOf(' ');
      if (space > 0 && line.slice(space).trim().startsWith('{')) {
        try {
          return { name: line.slice(0, space), args: JSON.parse(line.slice(space)) as Record<string, unknown> };
        } catch {
          return line;
        }
      }
      return line;
    });
  if (steps.length) out.expected_trajectory = steps;
  else delete out.expected_trajectory;
  out.tags = d.tags
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
  return out;
}

/**
 * True when pasted or loaded text is an OpenAI Evals export rather than a Nexura dataset:
 * JSONL rows (`{"item": {...}}` or bare objects, one per line), a JSON array of `{item}` rows
 * or of `openai/evals` registry rows (`{"input": ..., "ideal": ...}`), a single such row, or an
 * inline run source `{"type": "file_content", "content": [...]}`. The conversion itself is done
 * by the service (`POST /evaluations/studio/datasets/import-openai`), the same code as
 * `nexura-runtime evals import-openai`, so Studio and the CLI cannot drift apart.
 */
export function looksLikeOpenAiEvals(text: string): boolean {
  const trimmed = text.replace(/^﻿/, '').trim();
  if (!trimmed) return false;
  const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
  // A registry row has `ideal`, which a Nexura case never has (its field is `expected_output`).
  const isRow = (r: unknown): boolean => isObj(r) && (isObj(r.item) || ('input' in r && 'ideal' in r));
  try {
    const whole: unknown = JSON.parse(trimmed);
    if (Array.isArray(whole)) return whole.length > 0 && whole.every(isRow);
    if (!isObj(whole)) return false;
    if (whole.format !== undefined || 'cases' in whole) return false;
    return whole.type === 'file_content' || isRow(whole);
  } catch {
    /* not one JSON document: maybe JSONL */
  }
  const lines = trimmed.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) return false;
  try {
    return isObj(JSON.parse(lines[0]));
  } catch {
    return false;
  }
}

/** Line problems of a refused OpenAI import (`detail.field_errors`), as display text. */
export function importFieldErrors(err: unknown): string[] {
  if (!(err instanceof NexuraApiError)) return [];
  const details = err.details as { field_errors?: Array<{ field?: unknown; error?: unknown }> } | undefined;
  return (details?.field_errors ?? [])
    .filter((fe) => fe && typeof fe.error === 'string')
    .map((fe) => `${String(fe.field ?? '')}: ${String(fe.error)}`);
}

/** Server cases carry `null` for absent optional fields; the editor uses absent keys. */
export function withoutNulls(cases: EvalCase[]): EvalCase[] {
  return cases.map((c) => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(c)) if (v !== null) out[k] = v;
    return out as unknown as EvalCase;
  });
}
