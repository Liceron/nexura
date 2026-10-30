// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
// P39 G6: the agent editor's Guardrails section (presets only, no code) and the effect class
// badge of a tool. The operator's service policy is shown read-only above the presets; a policy
// in shadow mode (P40) gets a "reporting only" badge instead of the enforced one.
import { useId } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import type { AgentGuardrailOptions, ChatModelInfo, GuardrailPolicyView, GuardrailPresetName } from '@liceron/nexura-sdk';
import { Badge } from '../../components/Badge';
import type { BadgeVariant } from '../../components/Badge';
import { useI18n } from '../../i18n';
import { GUARDRAIL_PRESET_NAMES, MAX_JUDGE_POLICY_CHARS, OUTPUT_PRESETS, STREAM_GUARDS, charCount } from './agentForm';
import type { AgentDraft, StreamGuardChoice } from './agentForm';

const COST_VARIANT: Record<string, BadgeVariant> = { none: 'success', local: 'info', model: 'warning' };
const EFFECT_VARIANT: Record<string, BadgeVariant> = { pure: 'success', idempotent: 'info', non_idempotent: 'warning' };

/** The durable effect class of a module effect policy, as the server maps it; null when unknown. */
export function effectClassOf(policy?: string | null): string | null {
  const classes: Record<string, string> = {
    pure: 'pure',
    read: 'pure',
    idempotent_write: 'idempotent',
    non_idempotent_write: 'non_idempotent',
  };
  return policy ? (classes[policy] ?? null) : null;
}

/** A tool's durable effect class (`pure`, `idempotent`, `non_idempotent`); nothing when unknown. */
export function EffectClassBadge({ effectClass, testId }: { effectClass?: string | null; testId?: string }) {
  const { t } = useI18n();
  if (!effectClass) return null;
  const known = effectClass in EFFECT_VARIANT;
  return (
    <Badge
      variant={EFFECT_VARIANT[effectClass] ?? 'neutral'}
      data-testid={testId}
      data-effect-class={effectClass}
      title={t('agents.effectTitle')}
    >
      {known ? t(`agents.effect.${effectClass}`) : effectClass}
    </Badge>
  );
}

/** The policy view with its P40 mode fields (optional: older servers do not send them). */
type PolicyViewWithMode = GuardrailPolicyView & { mode?: string; shadow_rules?: number };

interface Props {
  draft: AgentDraft;
  setDraft: Dispatch<SetStateAction<AgentDraft>>;
  options?: AgentGuardrailOptions;
  models: ChatModelInfo[];
  error: ReactNode;
}

export function AgentGuardrailsSection({ draft, setDraft, options, models, error }: Props) {
  const { t } = useI18n();
  const ids = useId();
  const infos = new Map((options?.presets ?? []).map((p) => [p.name, p]));
  const names = options?.presets?.length ? options.presets.map((p) => p.name) : GUARDRAIL_PRESET_NAMES;
  // Only what the server says is enforced (locked for every run) gets the badge (M2).
  const view = options?.policy as PolicyViewWithMode | undefined;
  const policy = view?.enforced ? view.policy : null;
  // Shadow mode: the server reports every decision of the policy and applies none.
  const shadow = view?.mode === 'shadow';
  const shadowRules = typeof view?.shadow_rules === 'number' ? view.shadow_rules : 0;
  const chosen = new Set(draft.guardrail_presets);
  // Streaming past checked answers is not offered: an output preset or an enforced policy.
  const noPassthrough = OUTPUT_PRESETS.some((p) => chosen.has(p)) || Boolean(policy);
  const judgeModels = models.filter((m) => !m.offline);
  const safetyModels = infos.get('safety_local')?.models ?? [];

  const toggle = (name: GuardrailPresetName) =>
    setDraft((d) => ({
      ...d,
      guardrails_unset: false,
      guardrail_presets: d.guardrail_presets.includes(name)
        ? d.guardrail_presets.filter((p) => p !== name)
        : [...d.guardrail_presets, name],
    }));
  const set = <K extends keyof AgentDraft>(key: K, value: AgentDraft[K]) =>
    setDraft((d) => ({ ...d, guardrails_unset: false, [key]: value }));

  return (
    <fieldset data-testid="agent-guardrails" className="resource-section">
      <legend>{t('agents.guardrails')}</legend>
      <span className="resource-help">{t('agents.guardrailsHelp')}</span>
      {policy && (
        <div className="resource-help" data-testid="agent-guardrails-policy" role="note">
          {shadow ? (
            <Badge variant="warning" data-testid="agent-guardrails-policy-shadow" data-mode="shadow">
              {t('agents.guardrailsShadow')}
            </Badge>
          ) : (
            <Badge variant="info">{t('agents.guardrailsEnforced')}</Badge>
          )}{' '}
          {t(shadow ? 'agents.guardrailsPolicyShadow' : 'agents.guardrailsPolicy')
            .replace('{name}', policy.name ?? policy.file ?? t('agents.guardrailsPolicyUnnamed'))
            .replace('{digest}', policy.digest.slice(0, 12))}{' '}
          {!shadow && shadowRules > 0 && (
            <span data-testid="agent-guardrails-policy-shadow-rules">
              {t('agents.guardrailsShadowRules').replace('{count}', String(shadowRules))}{' '}
            </span>
          )}
          {policy.rules && (
            <span data-testid="agent-guardrails-policy-rules">
              {t('agents.guardrailsPolicyRules').replace('{rules}', String(policy.rules.length))}
            </span>
          )}
          <span className="resource-check-detail" data-testid="agent-guardrails-policy-exceptions">
            {t('agents.guardrailsPolicyExceptions')}
          </span>
        </div>
      )}
      {options && !options.available && (
        <span className="resource-help" data-testid="agent-guardrails-unavailable">
          {t('agents.guardrailsUnavailable').replace('{hint}', options.install_hint ?? '')}
        </span>
      )}
      <div className="resource-checks">
        {names.map((name) => {
          const info = infos.get(name);
          return (
            <label key={name} className="resource-check">
              <input
                type="checkbox"
                data-testid={`agent-guardrail-${name}`}
                checked={chosen.has(name)}
                disabled={options ? !options.available : false}
                onChange={() => toggle(name)}
              />
              <span>
                <strong>{t(`agents.guardrailsPreset.${name}`)}</strong> <code>{name}</code>{' '}
                {info && (
                  <Badge variant={COST_VARIANT[info.cost] ?? 'neutral'} data-testid={`agent-guardrail-cost-${name}`}>
                    {t(`agents.guardrailsCost.${info.cost}`)}
                  </Badge>
                )}
                <span className="resource-check-detail">{t(`agents.guardrailsPresetHelp.${name}`)}</span>
              </span>
            </label>
          );
        })}
      </div>

      {chosen.has('safety_local') && (
        <div className="resource-field">
          <label htmlFor={`${ids}-safety`}>{t('agents.guardrailsSafetyModel')}</label>
          <select
            id={`${ids}-safety`}
            data-testid="agent-guardrail-safety-model"
            value={draft.safety_model}
            onChange={(e) => set('safety_model', e.target.value)}
          >
            <option value="">
              {t('agents.guardrailsDefaultModel').replace('{model}', infos.get('safety_local')?.default_model ?? 'llama-guard3:1b')}
            </option>
            {safetyModels.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
            {draft.safety_model && !safetyModels.includes(draft.safety_model) && (
              <option value={draft.safety_model}>{draft.safety_model}</option>
            )}
          </select>
          <span className="resource-help">{t('agents.guardrailsSafetyHelp')}</span>
        </div>
      )}

      {chosen.has('judge') && (
        <>
          <div className="resource-field">
            <label htmlFor={`${ids}-judge-policy`}>{t('agents.guardrailsJudgePolicy')}</label>
            <textarea
              id={`${ids}-judge-policy`}
              data-testid="agent-guardrail-judge-policy"
              rows={4}
              value={draft.judge_policy}
              aria-describedby={`${ids}-judge-help`}
              onChange={(e) => set('judge_policy', e.target.value)}
            />
            <span id={`${ids}-judge-help`} className="resource-help">
              {t('agents.guardrailsJudgePolicyHelp')}{' '}
              {t('agents.charCount')
                .replace('{count}', String(charCount(draft.judge_policy)))
                .replace('{max}', String(MAX_JUDGE_POLICY_CHARS))}
            </span>
          </div>
          <div className="resource-field">
            <label htmlFor={`${ids}-judge-model`}>{t('agents.guardrailsJudgeModel')}</label>
            <select
              id={`${ids}-judge-model`}
              data-testid="agent-guardrail-judge-model"
              value={draft.judge_model}
              onChange={(e) => set('judge_model', e.target.value)}
            >
              <option value="">{t('agents.guardrailsJudgeOwnModel')}</option>
              {judgeModels.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.label || m.name}
                </option>
              ))}
              {draft.judge_model && !judgeModels.some((m) => m.name === draft.judge_model) && (
                <option value={draft.judge_model}>{draft.judge_model}</option>
              )}
            </select>
            <span className="resource-help" data-testid="agent-guardrail-judge-capacity">
              {t('agents.guardrailsJudgeCapacity')}
            </span>
          </div>
        </>
      )}

      <div className="resource-field">
        <label htmlFor={`${ids}-stream`}>{t('agents.guardrailsStreamGuard')}</label>
        <select
          id={`${ids}-stream`}
          data-testid="agent-guardrail-stream"
          value={draft.stream_guard}
          onChange={(e) => set('stream_guard', e.target.value as StreamGuardChoice)}
        >
          {STREAM_GUARDS.map((mode) => (
            <option key={mode} value={mode} disabled={mode === 'passthrough' && noPassthrough}>
              {t(`agents.guardrailsStream.${mode}`)}
            </option>
          ))}
        </select>
        {noPassthrough && (
          <span className="resource-help" data-testid="agent-guardrail-stream-note">
            {t('agents.guardrailsStreamChecked')}
          </span>
        )}
      </div>
      {error}
    </fieldset>
  );
}
