// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
/**
 * Pure helpers for the durable-run screens (interrupt answers, checkpoint history, fork,
 * sub-runs). No React, no network: unit-tested in tests/unit/durable_runs.test.ts.
 */

import type { PendingApproval, RunRecord, SubRunDetail } from '@liceron/nexura-sdk';
import { englishT, type Translate } from '../../i18n';
import { formatApiError } from '../../api/errors';

export type JsonObject = Record<string, unknown>;

export interface JsonObjectResult {
  value?: JsonObject;
  error: string | null;
}

/** Parse text that must be a JSON object. Empty text is "no value" and is not an error. */
export function parseJsonObject(text: string, t: Translate = englishT): JsonObjectResult {
  if (!text.trim()) return { value: undefined, error: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (err: unknown) {
    return { error: t('validationMessages.jsonSyntax').replace('{v1}', String(formatApiError(err, t))) };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { error: t('validationMessages.stateObject') };
  }
  return { value: parsed as JsonObject, error: null };
}

// ---------------------------------------------------------------- interrupts

export interface InterruptInfo {
  question: string | null;
  schema: JsonObject | null;
}

function isRecord(v: unknown): v is JsonObject {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Read the question and optional JSON schema out of an interrupt() value. */
export function describeInterrupt(value: unknown): InterruptInfo {
  if (typeof value === 'string') return { question: value, schema: null };
  if (!isRecord(value)) return { question: null, schema: null };
  const question = typeof value.question === 'string' ? value.question : null;
  const raw = value.schema ?? value.json_schema ?? value.response_schema;
  return { question, schema: isRecord(raw) ? raw : null };
}

function typeOf(v: unknown): string {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

function matchesType(v: unknown, t: string): boolean {
  if (t === 'integer') return typeof v === 'number' && Number.isInteger(v);
  if (t === 'number') return typeof v === 'number' && Number.isFinite(v);
  return typeOf(v) === t;
}

/**
 * Validate against the common JSON Schema subset used for interrupt answers: type, enum,
 * required, properties, items, min/max, minLength/maxLength. Unknown keywords are ignored.
 * Returns human-readable errors; an empty list means valid.
 */
export function validateAgainstSchema(value: unknown, schema: JsonObject, path = '$', t: Translate = englishT): string[] {
  const errors: string[] = [];
  const type = schema.type;
  if (typeof type === 'string' || Array.isArray(type)) {
    const types = (Array.isArray(type) ? type : [type]).filter((x): x is string => typeof x === 'string');
    if (!types.some((t) => matchesType(value, t))) {
      errors.push(t('validation.schemaType').replace('{path}', path).replace('{types}', types.join(t('validation.or'))).replace('{actual}', typeOf(value)));
      return errors;
    }
  }
  if (Array.isArray(schema.enum) && !schema.enum.some((e) => JSON.stringify(e) === JSON.stringify(value))) {
    errors.push(t('validation.schemaEnum').replace('{path}', path).replace('{options}', schema.enum.map((e) => JSON.stringify(e)).join(', ')));
  }
  if (typeof value === 'string') {
    if (typeof schema.minLength === 'number' && value.length < schema.minLength) {
      errors.push(t('validationMessages.schemaMinLength').replace('{v1}', String(path)).replace('{v2}', String(schema.minLength)));
    }
    if (typeof schema.maxLength === 'number' && value.length > schema.maxLength) {
      errors.push(t('validationMessages.schemaMaxLength').replace('{v1}', String(path)).replace('{v2}', String(schema.maxLength)));
    }
  }
  if (typeof value === 'number') {
    if (typeof schema.minimum === 'number' && value < schema.minimum) {
      errors.push(t('validation.schemaMin').replace('{path}', path).replace('{limit}', String(schema.minimum)));
    }
    if (typeof schema.maximum === 'number' && value > schema.maximum) {
      errors.push(t('validation.schemaMax').replace('{path}', path).replace('{limit}', String(schema.maximum)));
    }
  }
  if (isRecord(value)) {
    if (Array.isArray(schema.required)) {
      for (const key of schema.required) {
        if (typeof key === 'string' && !Object.prototype.hasOwnProperty.call(value, key)) errors.push(t('validation.schemaRequired').replace('{path}', path).replace('{key}', key));
      }
    }
    if (isRecord(schema.properties)) {
      for (const [key, sub] of Object.entries(schema.properties)) {
        if (Object.prototype.hasOwnProperty.call(value, key) && isRecord(sub)) errors.push(...validateAgainstSchema(value[key], sub, `${path}.${key}`, t));
      }
    }
  }
  if (Array.isArray(value) && isRecord(schema.items)) {
    value.forEach((item, i) => {
      errors.push(...validateAgainstSchema(item, schema.items as JsonObject, `${path}[${i}]`, t));
    });
  }
  return errors;
}

export interface AnswerResult {
  value?: unknown;
  errors: string[];
}

/**
 * Turn the answer text into the value sent to POST /runs/{id}/input. With a schema whose
 * type is "string" the text is sent as-is; otherwise valid JSON is parsed and anything else
 * is sent as a string. A schema, when present, must be satisfied.
 */
export function buildAnswer(text: string, schema: JsonObject | null, t: Translate = englishT): AnswerResult {
  let value: unknown;
  const parsed = (): { ok: boolean; v?: unknown } => {
    try {
      return { ok: true, v: JSON.parse(text) };
    } catch {
      return { ok: false };
    }
  };
  if (!schema) {
    const p = parsed();
    value = p.ok ? p.v : text;
    return { value, errors: [] };
  }
  const types = (Array.isArray(schema.type) ? schema.type : schema.type !== undefined ? [schema.type] : []).filter(
    (x): x is string => typeof x === 'string',
  );
  if (Array.isArray(schema.enum)) {
    // Raw text wins when it equals a string enum value; JSON parsing only applies otherwise.
    if (schema.enum.some((e) => typeof e === 'string' && e === text)) {
      value = text;
    } else {
      const p = parsed();
      value = p.ok ? p.v : text;
    }
  } else if (types.length === 1 && types[0] === 'string') {
    value = text;
  } else if (types.includes('string')) {
    // Keep text as a string unless it parses exactly to another listed type.
    const p = parsed();
    value = p.ok && types.some((t) => t !== 'string' && matchesType(p.v, t)) ? p.v : text;
  } else {
    const p = parsed();
    value = p.ok ? p.v : text;
  }
  const errors = validateAgainstSchema(value, schema, '$', t);
  return errors.length ? { errors } : { value, errors: [] };
}

/** A starter answer for the form: an object skeleton for object schemas, else empty. */
export function schemaTemplate(schema: JsonObject | null): string {
  if (!schema || schema.type !== 'object' || !isRecord(schema.properties)) return '';
  const skeleton: JsonObject = {};
  for (const [key, sub] of Object.entries(schema.properties)) {
    const t = isRecord(sub) ? sub.type : undefined;
    skeleton[key] = t === 'number' || t === 'integer' ? 0 : t === 'boolean' ? false : t === 'array' ? [] : t === 'object' ? {} : '';
  }
  return JSON.stringify(skeleton, null, 2);
}

// ------------------------------------------------------------------- history

export function formatCheckpointTime(updatedAt: number | null | undefined, t: Translate = englishT): string {
  if (typeof updatedAt !== 'number' || !Number.isFinite(updatedAt)) return t('validationMessages.unknownTime');
  // The API reports epoch seconds; tolerate milliseconds.
  const ms = updatedAt < 1e12 ? updatedAt * 1000 : updatedAt;
  return new Date(ms).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC');
}

/** Fork is impossible at the end of a run (the API answers FORK_AT_END_OF_RUN). */
export function canForkCheckpoint(cp: { current_node_id?: string | null }): boolean {
  return cp.current_node_id !== null && cp.current_node_id !== undefined;
}

export interface ForkedFromInfo {
  parentRunId: string;
  checkpointIndex: number | null;
  stateChanged: boolean;
}

export function describeForkedFrom(raw: unknown): ForkedFromInfo | null {
  if (!isRecord(raw) || typeof raw.workflow_run_id !== 'string') return null;
  return {
    parentRunId: raw.workflow_run_id,
    checkpointIndex: typeof raw.checkpoint_index === 'number' ? raw.checkpoint_index : null,
    stateChanged: raw.state_changed === true,
  };
}

// ------------------------------------------------------------------ sub-runs

/** Child run ids look like `<parent>/<node>#<step>[.attempt][:item]`; show the part after the parent. */
export function subRunLabel(childId: string, parentId: string): string {
  return childId.startsWith(`${parentId}/`) ? childId.slice(parentId.length + 1) : childId;
}

/** The Sub-runs tab is shown only when the server says the run's spec has sub-workflow nodes. */
export function showSubRunsTab(run: Pick<RunRecord, 'has_subworkflows'> | null | undefined): boolean {
  return run?.has_subworkflows === true;
}

export interface SubRunStepRow {
  key: number;
  node: string;
  status: string;
  step: number;
  time: string;
}

export interface SubRunView {
  label: string;
  status: string;
  stepCount: number;
  currentNode: string;
  error: string | null;
  steps: SubRunStepRow[];
}

/** Flatten a child run detail into what the child view renders (no raw state, history is redacted server-side). */
export function describeSubRun(detail: SubRunDetail): SubRunView {
  return {
    label: subRunLabel(detail.run_id, detail.parent_run_id),
    status: detail.status,
    stepCount: detail.step_count,
    currentNode: detail.current_node_id ?? 'end',
    error: detail.error ?? null,
    steps: detail.steps.map((s) => ({
      key: s.index,
      node: s.node_id ?? 'end',
      status: s.status,
      step: s.step_count,
      time: formatCheckpointTime(s.updated_at),
    })),
  };
}

// ----------------------------------------------------------------- approvals

export const APPROVAL_ITEMS_SHOWN = 20;

export interface ApprovalDetailsView {
  mapOver: string | null;
  itemCount: number;
  shownItems: unknown[];
  moreCount: number;
  /** Number of items the server actually listed. */
  listedCount: number;
  maxWrites: number | null;
}

/** Read `pending_approval.details` (mapped module step). Returns null when absent or malformed. */
export function describeApprovalDetails(
  pending: Partial<PendingApproval> | null | undefined,
  limit = APPROVAL_ITEMS_SHOWN
): ApprovalDetailsView | null {
  const d = pending?.details;
  if (!isRecord(d)) return null;
  const items = Array.isArray(d.items) ? d.items : [];
  const itemCount = typeof d.item_count === 'number' ? d.item_count : items.length;
  return {
    mapOver: typeof d.map_over === 'string' ? d.map_over : null,
    itemCount,
    shownItems: items.slice(0, limit),
    moreCount: Math.max(0, Math.min(itemCount, items.length) - Math.min(items.length, limit)),
    listedCount: items.length,
    maxWrites: typeof d.max_writes_per_approval === 'number' ? d.max_writes_per_approval : null,
  };
}
