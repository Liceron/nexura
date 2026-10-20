// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useApi } from '../../api/context';
import { Alert } from '../../components/Alert';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { Modal } from '../../components/Modal';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import type { SkillVersions, SkillVersionView } from './types';
import { useSkills } from './useSkills';
import { formatApiError } from '../../api/errors';
import { getMyMembership } from '../../api/membership';

const fieldStyle = {
  padding: '6px 10px',
  borderRadius: 'var(--radius-md)',
  border: '1px solid var(--border-subtle)',
  backgroundColor: 'var(--bg-surface)',
  color: 'var(--text-primary)',
  fontSize: 13,
} as const;

const textStyle = {
  margin: 0,
  padding: 12,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  fontSize: 12,
  fontFamily: 'var(--font-mono, monospace)',
  backgroundColor: 'var(--bg-canvas)',
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-md)',
} as const;

const SAMPLE = `---
name: refunds
description: How to handle refund requests.
tools: [lookup_order]
---
# Refunds

Refund only within 30 days.`;

export function SkillsView() {
  const { t } = useI18n();
  const { manager, connection } = useApi();
  const [labelFilter, setLabelFilter] = useState('');
  const { items, loading, error, versions, create, setLabel, remove } = useSkills(labelFilter);
  const [role, setRole] = useState('admin');
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillVersions | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [labelName, setLabelName] = useState('production');
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);

  useEffect(() => {
    getMyMembership(manager.client, connection.workspace)
      .then((m) => setRole(String((m as unknown as { role?: string }).role ?? 'admin')))
      .catch(() => setRole('admin'));
  }, [manager, connection.workspace]);
  const canEdit = role === 'admin' || role === 'editor';
  const canRemove = role === 'admin';

  const allLabels = useMemo(() => {
    const names = new Set<string>(['production']);
    for (const s of items) Object.keys(s.labels).forEach((l) => names.add(l));
    return [...names].sort();
  }, [items]);

  const open = useCallback(
    async (name: string, keepVersion?: number) => {
      setSelected(name);
      setConfirmRemove(false);
      setActionError(null);
      try {
        const d = await versions(name);
        setDetail(d);
        setVersion(keepVersion ?? d.latest_version);
      } catch (err: unknown) {
        setActionError(formatApiError(err, t));
      }
    },
    [versions],
  );

  const shown: SkillVersionView | undefined = detail?.versions.find((v) => v.version === version);

  const submitCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setActionError(null);
    try {
      const created = await create(draft);
      setCreateOpen(false);
      setDraft('');
      setNotice(t('skills.created').replace('{name}', created.name).replace('{version}', String(created.version)));
      await open(created.name);
    } catch (err: unknown) {
      setActionError(formatApiError(err, t));
    } finally {
      setBusy(false);
    }
  };

  const applyLabel = async () => {
    if (!selected || version === null || !labelName.trim()) return;
    setBusy(true);
    setActionError(null);
    try {
      await setLabel(selected, labelName.trim(), version);
      setNotice(t('skills.labelSet').replace('{label}', labelName.trim()).replace('{version}', String(version)));
      await open(selected, version);
    } catch (err: unknown) {
      setActionError(formatApiError(err, t));
    } finally {
      setBusy(false);
    }
  };

  const doRemove = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await remove(selected);
      setSelected(null);
      setDetail(null);
      setConfirmRemove(false);
      setNotice(t('skills.removed'));
    } catch (err: unknown) {
      setActionError(formatApiError(err, t));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div data-testid="skills-view" style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, margin: 0 }}>{t('skills.title')}</h1>
        <div style={{ flex: 1 }} />
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          {t('skills.filterLabel')}{' '}
          <select
            data-testid="skills-label-filter"
            value={labelFilter}
            onChange={(e) => setLabelFilter(e.target.value)}
            style={fieldStyle}
          >
            <option value="">{t('skills.allLabels')}</option>
            {allLabels.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <Button
          variant="primary"
          data-testid="skills-new"
          disabled={!canEdit}
          title={canEdit ? undefined : t('skills.viewerNote')}
          onClick={() => {
            setDraft(SAMPLE);
            setActionError(null);
            setCreateOpen(true);
          }}
        >
          {t('skills.new')}
        </Button>
      </div>
      <p style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)' }}>{t('skills.intro')}</p>

      {error && <Alert variant="danger">{error}</Alert>}
      {notice && (
        <Alert variant="success" data-testid="skills-notice">
          {notice}
        </Alert>
      )}
      {actionError && !createOpen && (
        <Alert variant="danger" data-testid="skills-error">
          {actionError}
        </Alert>
      )}

      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <Card style={{ flex: '1 1 280px', minWidth: 260 }}>
          {loading ? (
            <Spinner size={18} />
          ) : items.length === 0 ? (
            <div data-testid="skills-empty" style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
              {t('skills.empty')}
            </div>
          ) : (
            <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
              {items.map((s) => (
                <li key={s.name}>
                  <button
                    type="button"
                    data-testid={`skill-row-${s.name}`}
                    aria-pressed={selected === s.name}
                    onClick={() => open(s.name)}
                    style={{
                      width: '100%',
                      textAlign: 'left',
                      padding: 10,
                      borderRadius: 'var(--radius-md)',
                      border: `1px solid ${selected === s.name ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                      backgroundColor: 'transparent',
                      color: 'var(--text-primary)',
                      cursor: 'pointer',
                    }}
                  >
                    <div style={{ fontWeight: 600, fontSize: 14 }}>{s.name}</div>
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '2px 0 6px' }}>
                      {s.description}
                    </div>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {Object.entries(s.labels).map(([l, v]) => (
                        <Badge key={l} variant={l === 'production' ? 'success' : 'neutral'} data-testid={`skill-label-${s.name}-${l}`}>
                          {l}: v{v}
                        </Badge>
                      ))}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="studio-detail-card" style={{ flex: '2 1 380px', minWidth: 320 }} data-testid="skill-detail">
          {!detail || !shown ? (
            <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{t('skills.selectPrompt')}</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <h2 style={{ margin: 0, fontSize: 16 }}>{detail.name}</h2>
                <select
                  aria-label={t('skills.version')}
                  data-testid="skill-version-select"
                  value={version ?? ''}
                  onChange={(e) => setVersion(Number(e.target.value))}
                  style={fieldStyle}
                >
                  {detail.versions.map((v) => (
                    <option key={v.version} value={v.version}>
                      v{v.version}
                      {v.labels.length ? ` (${v.labels.join(', ')})` : ''}
                    </option>
                  ))}
                </select>
                <div style={{ flex: 1 }} />
                {canRemove &&
                  (confirmRemove ? (
                    <Button variant="danger" size="sm" data-testid="skill-remove-confirm" loading={busy} onClick={doRemove}>
                      {t('skills.confirmRemove')}
                    </Button>
                  ) : (
                    <Button variant="ghost" size="sm" data-testid="skill-remove" onClick={() => setConfirmRemove(true)}>
                      {t('skills.remove')}
                    </Button>
                  ))}
              </div>
              <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{shown.description}</div>
              {shown.tools.length > 0 && (
                <div style={{ fontSize: 12 }}>
                  {t('skills.tools')}: <code>{shown.tools.join(', ')}</code>
                </div>
              )}
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t('skills.instructions')}</div>
                <pre data-testid="skill-text" style={textStyle}>
                  {shown.instructions}
                </pre>
              </div>
              {Object.entries(shown.resources).map(([name, text]) => (
                <div key={name}>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    {t('skills.resource')}: {name}
                  </div>
                  <pre data-testid={`skill-resource-${name}`} style={textStyle}>
                    {text}
                  </pre>
                </div>
              ))}
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <input
                  aria-label={t('skills.labelName')}
                  data-testid="skill-label-input"
                  list="skill-label-options"
                  value={labelName}
                  maxLength={64}
                  onChange={(e) => setLabelName(e.target.value)}
                  style={fieldStyle}
                />
                <datalist id="skill-label-options">
                  {allLabels.map((l) => (
                    <option key={l} value={l} />
                  ))}
                </datalist>
                <Button
                  data-testid="skill-set-label"
                  disabled={!canEdit || busy}
                  title={canEdit ? undefined : t('skills.viewerNote')}
                  onClick={applyLabel}
                >
                  {t('skills.setLabel').replace('{version}', String(version ?? ''))}
                </Button>
              </div>
            </div>
          )}
        </Card>
      </div>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title={t('skills.new')} maxWidth={640}>
        <form onSubmit={submitCreate} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <label htmlFor="skill-markdown" style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('skills.pasteHint')}
          </label>
          <textarea
            id="skill-markdown"
            data-testid="skill-markdown"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={14}
            spellCheck={false}
            style={{ ...fieldStyle, fontFamily: 'var(--font-mono, monospace)', resize: 'vertical' }}
          />
          {actionError && (
            <Alert variant="danger" data-testid="skill-create-error">
              {actionError}
            </Alert>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button type="button" onClick={() => setCreateOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" variant="primary" data-testid="skill-create-submit" loading={busy} disabled={!draft.trim()}>
              {t('skills.save')}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
