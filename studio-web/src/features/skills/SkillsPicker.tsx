// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import { useApi } from '../../api/context';
import { useI18n } from '../../i18n';
import type { SkillList, SkillSummary } from './types';

const selectStyle = {
  width: '100%',
  padding: '6px 8px',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--border-subtle)',
  backgroundColor: 'var(--bg-surface)',
  color: 'var(--text-primary)',
  fontSize: 12,
} as const;

/** Whether a module's config schema takes workspace skills (agent nodes do). */
export function acceptsSkills(schema: { properties?: Record<string, unknown> } | undefined | null): boolean {
  return Boolean(schema?.properties && 'skills' in schema.properties);
}

/** Checkbox list of the workspace's skills; names already chosen but missing stay visible. */
export function SkillsPicker({ value, onChange }: { value: unknown; onChange: (names: string[]) => void }) {
  const { manager } = useApi();
  const { t } = useI18n();
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [failed, setFailed] = useState(false);
  const chosen = Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];

  useEffect(() => {
    let alive = true;
    manager.client
      .request<SkillList>('GET', '/skills')
      .then((res) => alive && setSkills(res.items))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [manager]);

  const known = new Set((skills ?? []).map((s) => s.name));
  const missing = chosen.filter((n) => skills !== null && !known.has(n));
  const toggle = (name: string) =>
    onChange(chosen.includes(name) ? chosen.filter((n) => n !== name) : [...chosen, name]);

  return (
    <fieldset data-testid="skills-picker" style={{ border: 'none', margin: '0 0 10px', padding: 0 }}>
      <legend style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t('skills.pickerTitle')}</legend>
      {failed && <div style={{ fontSize: 'var(--type-meta)', color: 'var(--status-danger)' }}>{t('skills.pickerError')}</div>}
      {skills !== null && skills.length === 0 && missing.length === 0 && (
        <div style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>{t('skills.pickerEmpty')}</div>
      )}
      {(skills ?? []).map((s) => (
        <label key={s.name} style={{ display: 'flex', gap: 6, fontSize: 12, alignItems: 'baseline' }}>
          <input
            type="checkbox"
            data-testid={`skills-pick-${s.name}`}
            checked={chosen.includes(s.name)}
            onChange={() => toggle(s.name)}
          />
          <span>
            <strong>{s.name}</strong> <span style={{ color: 'var(--text-secondary)' }}>{s.description}</span>
          </span>
        </label>
      ))}
      {missing.map((n) => (
        <label key={n} style={{ display: 'flex', gap: 6, fontSize: 12, color: 'var(--status-warning)' }}>
          <input type="checkbox" checked onChange={() => toggle(n)} data-testid={`skills-pick-${n}`} />
          <span>
            {n} ({t('skills.pickerMissing')})
          </span>
        </label>
      ))}
    </fieldset>
  );
}

/** Which label of each chosen skill the agent reads (default production). */
export function SkillsLabelSelect({ value, onChange }: { value: unknown; onChange: (label: string) => void }) {
  const { t } = useI18n();
  const current = typeof value === 'string' && value ? value : 'production';
  return (
    <label style={{ display: 'block', fontSize: 12, marginBottom: 10 }}>
      <span style={{ fontWeight: 600 }}>{t('skills.pickerLabel')}</span>
      <input
        data-testid="skills-label-input"
        value={current}
        maxLength={64}
        onChange={(e) => onChange(e.target.value.trim() || 'production')}
        style={selectStyle}
      />
      <span style={{ color: 'var(--text-secondary)' }}>{t('skills.pickerLabelHelp')}</span>
    </label>
  );
}
