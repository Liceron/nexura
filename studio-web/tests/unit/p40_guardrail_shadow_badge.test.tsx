// P40 item 1: the agent editor's Guardrails section shows a guardrail policy in shadow mode
// with a "reporting only" badge (English and Hindi) instead of the enforced one, and a note
// for shadow rules inside an enforced policy. No network; the section is rendered directly.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { AgentGuardrailOptions } from '@liceron/nexura-sdk';
import { I18nProvider } from '../../src/i18n';
import { AgentGuardrailsSection } from '../../src/features/agents/AgentGuardrails';
import { emptyDraft } from '../../src/features/agents/agentForm';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';

type View = AgentGuardrailOptions['policy'] & { mode?: string; shadow_rules?: number };

function options(view: Partial<View>): AgentGuardrailOptions {
  return {
    available: true,
    install_hint: null,
    defaults: [],
    presets: [],
    policy: {
      configured: true,
      enforced: true,
      detail: 'summary',
      exceptions: [],
      policy: { digest: 'abcdef0123456789abcdef', file: 'policy.yaml' },
      ...view,
    } as View,
  };
}

function show(opts: AgentGuardrailOptions) {
  return render(
    <I18nProvider>
      <AgentGuardrailsSection draft={emptyDraft('gpt-x')} setDraft={vi.fn()} options={opts} models={[]} error={null} />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
});

describe('guardrail policy shadow mode badge', () => {
  it('shows the reporting-only badge for a policy in shadow mode', () => {
    show(options({ mode: 'shadow', shadow_rules: 3 }));
    const badge = screen.getByTestId('agent-guardrails-policy-shadow');
    expect(badge.textContent).toBe('Shadow mode: reporting only, nothing is blocked');
    expect(badge.getAttribute('data-mode')).toBe('shadow');
    const note = screen.getByTestId('agent-guardrails-policy');
    expect(note.textContent).not.toContain(en.agents.guardrailsEnforced);
    expect(note.textContent).toContain('runs in shadow mode');
    expect(note.textContent).toContain('policy.yaml');
    expect(screen.queryByTestId('agent-guardrails-policy-shadow-rules')).toBeNull();
  });

  it('shows it in Hindi', () => {
    localStorage.setItem('nexura_locale', 'hi');
    show(options({ mode: 'shadow', shadow_rules: 1 }));
    expect(screen.getByTestId('agent-guardrails-policy-shadow').textContent).toBe(hi.agents.guardrailsShadow);
    expect(hi.agents.guardrailsShadow).not.toBe(en.agents.guardrailsShadow);
    expect(screen.getByTestId('agent-guardrails-policy').textContent).toContain('शैडो मोड');
  });

  it('keeps the enforced badge for an enforce policy and counts its shadow rules', () => {
    show(options({ mode: 'enforce', shadow_rules: 2 }));
    expect(screen.queryByTestId('agent-guardrails-policy-shadow')).toBeNull();
    expect(screen.getByTestId('agent-guardrails-policy').textContent).toContain(en.agents.guardrailsEnforced);
    expect(screen.getByTestId('agent-guardrails-policy-shadow-rules').textContent).toContain(
      '2 rules run in shadow mode',
    );
  });

  it('treats a server without the mode field as enforce', () => {
    show(options({}));
    expect(screen.queryByTestId('agent-guardrails-policy-shadow')).toBeNull();
    expect(screen.queryByTestId('agent-guardrails-policy-shadow-rules')).toBeNull();
    expect(screen.getByTestId('agent-guardrails-policy').textContent).toContain(en.agents.guardrailsEnforced);
  });

  it('shows no policy badge when the policy is not installed by the server', () => {
    show(options({ mode: 'shadow', enforced: false }));
    expect(screen.queryByTestId('agent-guardrails-policy')).toBeNull();
    expect(screen.queryByTestId('agent-guardrails-policy-shadow')).toBeNull();
  });
});
