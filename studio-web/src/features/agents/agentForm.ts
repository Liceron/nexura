// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { NexuraApiError } from '@liceron/nexura-sdk';
import type {
  AgentFieldError,
  AgentGuardrailsSpec,
  AgentRecord,
  AgentServerLimits,
  AgentSpec,
  GuardrailPresetName,
  GuardrailPresetRef,
} from '@liceron/nexura-sdk';

/** Palette module that runs a saved agent in a workflow. */
export const AGENT_NODE_MODULE_ID = 'nexura.agents.agent';
/** Eval target ids of saved agents are `agent:<agent_id>`. */
export const SAVED_AGENT_TARGET_PREFIX = 'agent:';
/** The offline fixture: needs no key and costs nothing. */
export const OFFLINE_MODEL = 'fake-deterministic';

// Same rules as nexura_runtime.agents_spec (the server checks again on save).
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const LABEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const CONTROL_CHARS = /[\u0000-\u0008\u000b-\u001f\u007f]/;
export const MAX_DESCRIPTION_CHARS = 500;
export const MAX_OUTPUT_SCHEMA_CHARS = 16_000;
/** Presets a new agent starts with (zero cost, no network; P39 G6 design section 10). */
export const DEFAULT_GUARDRAIL_PRESETS: GuardrailPresetName[] = ['pii_redact', 'injection_fast'];
export const GUARDRAIL_PRESET_NAMES: GuardrailPresetName[] = ['pii_block', 'pii_redact', 'injection_fast', 'safety_local', 'judge'];
export const STREAM_GUARDS = ['auto', 'hold', 'chunked', 'passthrough'] as const;
/** Presets that check the answer: its streamed text cannot be passed through unchecked. */
export const OUTPUT_PRESETS: GuardrailPresetName[] = ['pii_redact', 'safety_local', 'judge'];
export type StreamGuardChoice = (typeof STREAM_GUARDS)[number];
export const MAX_JUDGE_POLICY_CHARS = 4_000;

/** Server bounds used until `GET /agents/options` answers (the server's defaults). */
export const DEFAULT_SERVER_LIMITS: AgentServerLimits = {
  max_agents_per_workspace: 100,
  max_spec_chars: 64_000,
  max_instructions_chars: 20_000,
  max_tools: 20,
  max_knowledge_collections: 10,
  max_skills: 20,
  max_turns: 20,
  max_tool_calls: 50,
  max_timeout_seconds: 300,
};

/** The editor's form state; numbers and the output schema stay text until saved. */
export interface AgentDraft {
  name: string;
  description: string;
  instructions: string;
  model: string;
  tools: string[];
  knowledge: string[];
  skills: string[];
  skills_label: string;
  output_schema: string;
  max_turns: string;
  max_tool_calls: string;
  timeout_seconds: string;
  /** Guardrail presets switched on, in the order they were chosen. */
  guardrail_presets: GuardrailPresetName[];
  /** True while the saved agent has no guardrails field and nothing was switched on. */
  guardrails_unset: boolean;
  judge_policy: string;
  /** A chat model name for the judge; '' uses the agent's own model. */
  judge_model: string;
  /** An Ollama guard tag for safety_local; '' uses the server's default. */
  safety_model: string;
  stream_guard: StreamGuardChoice;
  /** Options of saved presets the form does not edit (kinds, threshold), kept as they are. */
  guardrail_extras: Partial<Record<GuardrailPresetName, GuardrailPresetRef>>;
}

/** Editor fields a field error can point at. */
export type DraftField = keyof AgentDraft | 'guardrails' | 'form';
export type DraftErrors = Partial<Record<DraftField, string>>;

export function emptyDraft(
  model: string = OFFLINE_MODEL,
  guardrails: GuardrailPresetName[] = DEFAULT_GUARDRAIL_PRESETS,
): AgentDraft {
  return {
    name: '',
    description: '',
    instructions: '',
    model,
    tools: [],
    knowledge: [],
    skills: [],
    skills_label: 'production',
    output_schema: '',
    max_turns: '6',
    max_tool_calls: '10',
    timeout_seconds: '60',
    guardrail_presets: [...guardrails],
    guardrails_unset: false,
    judge_policy: '',
    judge_model: '',
    safety_model: '',
    stream_guard: 'auto',
    guardrail_extras: {},
  };
}

function guardrailFields(spec: AgentGuardrailsSpec | null | undefined) {
  const refs = spec?.presets ?? [];
  const judge = refs.find((r) => r.preset === 'judge');
  const safety = refs.find((r) => r.preset === 'safety_local');
  const extras: Partial<Record<GuardrailPresetName, GuardrailPresetRef>> = {};
  for (const ref of refs) if (ref.preset !== 'judge' && ref.preset !== 'safety_local') extras[ref.preset] = { ...ref };
  const mode = spec?.stream_guard ?? 'auto';
  return {
    guardrail_presets: refs.map((r) => r.preset),
    guardrails_unset: spec == null,
    judge_policy: judge?.policy ?? '',
    judge_model: judge?.model ?? '',
    safety_model: safety?.model ?? '',
    stream_guard: (STREAM_GUARDS as readonly string[]).includes(mode) ? (mode as StreamGuardChoice) : 'auto',
    guardrail_extras: extras,
  };
}

/** The spec's `guardrails` value for a draft: `null` for an agent that never had any. */
export function guardrailsOfDraft(draft: AgentDraft): AgentGuardrailsSpec | null {
  if (draft.guardrails_unset && draft.guardrail_presets.length === 0 && draft.stream_guard === 'auto') return null;
  const presets: GuardrailPresetRef[] = draft.guardrail_presets.map((preset) => {
    if (preset === 'judge') {
      const ref: GuardrailPresetRef = { preset, policy: draft.judge_policy.trim() };
      if (draft.judge_model) ref.model = draft.judge_model;
      return ref;
    }
    if (preset === 'safety_local') return draft.safety_model ? { preset, model: draft.safety_model } : { preset };
    return draft.guardrail_extras[preset] ? { ...draft.guardrail_extras[preset], preset } : { preset };
  });
  return { presets, stream_guard: draft.stream_guard };
}

export function draftFromRecord(record: AgentRecord): AgentDraft {
  const s = record.spec;
  return {
    name: s.name,
    description: s.description ?? '',
    instructions: s.instructions ?? '',
    model: s.model ?? OFFLINE_MODEL,
    tools: [...(s.tools ?? [])],
    knowledge: [...(s.knowledge ?? [])],
    skills: [...(s.skills ?? [])],
    skills_label: s.skills_label || 'production',
    output_schema: s.output_schema ? JSON.stringify(s.output_schema, null, 2) : '',
    max_turns: String(s.limits?.max_turns ?? 6),
    max_tool_calls: String(s.limits?.max_tool_calls ?? 10),
    timeout_seconds: String(s.limits?.timeout_seconds ?? 60),
    ...guardrailFields(s.guardrails),
  };
}

function boundedNumber(
  text: string,
  min: number,
  max: number,
  integer: boolean,
): { value: number | null; error: string | null } {
  const trimmed = text.trim();
  const value = Number(trimmed);
  if (!trimmed || !Number.isFinite(value)) return { value: null, error: `Enter a number from ${min} to ${max}.` };
  if (integer && !Number.isInteger(value)) return { value: null, error: `Enter a whole number from ${min} to ${max}.` };
  if (value < min || value > max) return { value: null, error: `Must be from ${min} to ${max}.` };
  return { value, error: null };
}

/**
 * Check a draft against the server's bounds before saving. Returns the spec to send, or the
 * problems by field (the server checks everything again and may still refuse names).
 */
/** Characters as the server counts them (code points; JS `.length` counts UTF-16 units). */
export function charCount(text: string): number {
  return [...text].length;
}

/**
 * Length of `value` as the server measures it: Python `json.dumps(value, ensure_ascii=False)`,
 * which puts a space after `,` and `:` (JSON.stringify does not).
 */
export function pythonJsonLength(value: unknown): number {
  const render = (v: unknown): string => {
    if (Array.isArray(v)) return `[${v.map(render).join(', ')}]`;
    if (v && typeof v === 'object') {
      return `{${Object.entries(v as Record<string, unknown>)
        .filter(([, item]) => item !== undefined)
        .map(([k, item]) => `${JSON.stringify(k)}: ${render(item)}`)
        .join(', ')}}`;
    }
    return JSON.stringify(v ?? null);
  };
  return charCount(render(value));
}

export function validateDraft(
  draft: AgentDraft,
  limits: AgentServerLimits = DEFAULT_SERVER_LIMITS,
): { spec: AgentSpec | null; errors: DraftErrors } {
  const errors: DraftErrors = {};
  const name = draft.name.trim();
  if (!NAME_PATTERN.test(name)) {
    errors.name = 'Use 1-64 lowercase letters, digits, "_" or "-", starting with a letter or digit.';
  }
  if (charCount(draft.description) > MAX_DESCRIPTION_CHARS) {
    errors.description = `At most ${MAX_DESCRIPTION_CHARS} characters.`;
  } else if (CONTROL_CHARS.test(draft.description)) {
    errors.description = 'Remove control characters (tab and newline are allowed).';
  }
  if (charCount(draft.instructions) > limits.max_instructions_chars) {
    errors.instructions = `At most ${limits.max_instructions_chars} characters.`;
  }
  if (!draft.model) errors.model = 'Choose a model.';
  if (draft.tools.length > limits.max_tools) errors.tools = `At most ${limits.max_tools} tools.`;
  if (draft.knowledge.length > limits.max_knowledge_collections) {
    errors.knowledge = `At most ${limits.max_knowledge_collections} collections.`;
  }
  if (draft.skills.length > limits.max_skills) errors.skills = `At most ${limits.max_skills} skills.`;
  const label = draft.skills_label.trim() || 'production';
  if (!LABEL_PATTERN.test(label)) errors.skills_label = 'Use letters, digits, ".", "_" or "-" (up to 64).';

  let outputSchema: Record<string, unknown> | null = null;
  if (draft.output_schema.trim()) {
    try {
      const parsed: unknown = JSON.parse(draft.output_schema);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        errors.output_schema = 'The output schema must be a JSON object, for example {"type": "object"}.';
      } else if (pythonJsonLength(parsed) > MAX_OUTPUT_SCHEMA_CHARS) {
        errors.output_schema = `At most ${MAX_OUTPUT_SCHEMA_CHARS} JSON characters.`;
      } else {
        outputSchema = parsed as Record<string, unknown>;
      }
    } catch (err: unknown) {
      errors.output_schema = `Not valid JSON: ${err instanceof Error ? err.message : String(err)}`;
    }
  }

  const turns = boundedNumber(draft.max_turns, 1, limits.max_turns, true);
  if (turns.error) errors.max_turns = turns.error;
  const calls = boundedNumber(draft.max_tool_calls, 0, limits.max_tool_calls, true);
  if (calls.error) errors.max_tool_calls = calls.error;
  const timeout = boundedNumber(draft.timeout_seconds, 1, limits.max_timeout_seconds, false);
  if (timeout.error) errors.timeout_seconds = timeout.error;
  if (draft.stream_guard === 'passthrough' && draft.guardrail_presets.some((p) => OUTPUT_PRESETS.includes(p))) {
    errors.guardrails = 'Answers that a guardrail checks cannot stream unchecked: choose another stream mode.';
  }
  if (draft.guardrail_presets.includes('judge')) {
    if (!draft.judge_policy.trim()) errors.guardrails = 'The judge needs policy text: what to block and what to allow.';
    else if (charCount(draft.judge_policy) > MAX_JUDGE_POLICY_CHARS) {
      errors.guardrails = `The judge policy is at most ${MAX_JUDGE_POLICY_CHARS} characters.`;
    }
  }

  if (Object.keys(errors).length > 0) return { spec: null, errors };
  const spec: AgentSpec = {
    name,
    description: draft.description,
    instructions: draft.instructions,
    model: draft.model,
    tools: [...draft.tools],
    knowledge: [...draft.knowledge],
    skills: [...draft.skills],
    skills_label: label,
    output_schema: outputSchema,
    limits: {
      max_turns: turns.value as number,
      max_tool_calls: calls.value as number,
      timeout_seconds: timeout.value as number,
    },
    guardrails: guardrailsOfDraft(draft),
  };
  if (pythonJsonLength(spec) > limits.max_spec_chars) {
    return { spec: null, errors: { form: `The whole agent is over ${limits.max_spec_chars} JSON characters.` } };
  }
  return { spec, errors };
}

/** Which editor field a server field path (`name`, `tools.0`, `limits.max_turns`) belongs to. */
export function draftFieldOf(path: string): DraftField {
  const parts = path.split('.');
  if (parts[0] === 'limits' && parts[1]) {
    if (parts[1] === 'max_turns' || parts[1] === 'max_tool_calls' || parts[1] === 'timeout_seconds') return parts[1];
    return 'form';
  }
  const known: DraftField[] = [
    'name',
    'description',
    'instructions',
    'model',
    'tools',
    'knowledge',
    'skills',
    'skills_label',
    'output_schema',
    'guardrails',
  ];
  return known.includes(parts[0] as DraftField) ? (parts[0] as DraftField) : 'form';
}

/** Field problems in a refused save (`detail.field_errors`), by editor field. */
export function fieldErrorsFromApi(err: unknown): DraftErrors {
  const out: DraftErrors = {};
  if (!(err instanceof NexuraApiError)) return out;
  const details = err.details as { field_errors?: AgentFieldError[] } | undefined;
  for (const fe of details?.field_errors ?? []) {
    if (!fe || typeof fe.message !== 'string') continue;
    const field = draftFieldOf(String(fe.field ?? ''));
    out[field] = out[field] ? `${out[field]} ${fe.message}` : fe.message;
  }
  if (err.code === 'AGENT_NAME_TAKEN' && !out.name) out.name = 'Another agent in this workspace has this name.';
  return out;
}

export function isRevisionConflict(err: unknown): boolean {
  return err instanceof NexuraApiError && err.code === 'REVISION_CONFLICT';
}

/** Eval target id of a saved agent, or null for any other target id. */
export function savedAgentIdOfTarget(targetId: string): string | null {
  return targetId.startsWith(SAVED_AGENT_TARGET_PREFIX) ? targetId.slice(SAVED_AGENT_TARGET_PREFIX.length) : null;
}
