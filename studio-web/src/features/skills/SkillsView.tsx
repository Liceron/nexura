// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useAfterRender } from '../../app/useAfterRender';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useApi } from '../../api/context';
import { Alert } from '../../components/Alert';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import type { SkillVersions, SkillVersionView } from './types';
import { useSkills } from './useSkills';
import { formatApiError } from '../../api/errors';
import { getMyMembership } from '../../api/membership';
import { useCompactLayout } from '../../app/useCompactLayout';

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
  // Phones show one pane at a time; Back keeps the selected skill.
  const [phonePane, setPhonePane] = useState<'list' | 'detail'>('list');
  const ids = useId();
  const listRef = useRef<HTMLUListElement>(null);
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const afterRender = useAfterRender();
  // On phones the list is hidden once a row opens, so focus follows into the detail pane.
  const compact = useCompactLayout();
  const compactRef = useRef(compact);
  compactRef.current = compact;

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
      setPhonePane('detail');
      setConfirmRemove(false);
      setActionError(null);
      try {
        const d = await versions(name);
        setDetail(d);
        setVersion(keepVersion ?? d.latest_version);
        if (compactRef.current && keepVersion === undefined) afterRender(() => detailHeadingRef.current?.focus());
      } catch (err: unknown) {
        setActionError(formatApiError(err, t));
      }
    },
    [versions, afterRender],
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
      setPhonePane('list');
      setConfirmRemove(false);
      setNotice(t('skills.removed'));
    } catch (err: unknown) {
      setActionError(formatApiError(err, t));
    } finally {
      setBusy(false);
    }
  };

  const backToList = () => {
    setPhonePane('list');
    afterRender(() => listRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus());
  };
  const viewing = Boolean(detail && shown);

  return (
    <PageLayout data-testid="skills-view">
      <PageHeader
        title={t('skills.title')}
        actions={
          <Button
            variant={viewing && canEdit ? 'secondary' : 'primary'}
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
        }
        filters={
          <div className="resource-field" style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
            <label htmlFor={`${ids}-filter`}>{t('skills.filterLabel')}</label>
            <select
              id={`${ids}-filter`}
              data-testid="skills-label-filter"
              value={labelFilter}
              onChange={(e) => setLabelFilter(e.target.value)}
            >
              <option value="">{t('skills.allLabels')}</option>
              {allLabels.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </div>
        }
      />
      <details className="resource-help-disclosure" style={{ marginBottom: 16 }}>
        <summary>{t('skills.help')}</summary>
        <p>{t('skills.intro')}</p>
      </details>

      <div className="resource-notices">
        {error && <Alert variant="danger">{error}</Alert>}
        {notice && (
          <div role="status">
            <Alert variant="success" data-testid="skills-notice">
              {notice}
            </Alert>
          </div>
        )}
        {actionError && !createOpen && (
          <div role="alert">
            <Alert variant="danger" data-testid="skills-error">
              {actionError}
            </Alert>
          </div>
        )}
      </div>

      <div className={`studio-list-detail resource-layout--${phonePane === 'detail' && viewing ? 'detail' : 'browse'}`}>
        <div className="resource-list-pane">
          {loading ? (
            <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '16px 0' }}>
              <Spinner size={16} /> <span className="resource-help">{t('common.loading')}</span>
            </div>
          ) : items.length === 0 ? (
            <p data-testid="skills-empty" className="resource-empty">
              {t('skills.empty')}
            </p>
          ) : (
            <ul ref={listRef} className="resource-list" aria-label={t('skills.listLabel')}>
              {items.map((s) => (
                <li key={s.name}>
                  <button
                    type="button"
                    className="resource-row"
                    data-testid={`skill-row-${s.name}`}
                    aria-pressed={selected === s.name}
                    onClick={() => open(s.name)}
                  >
                    <span className="resource-row-name">{s.name}</span>
                    {s.description && <span className="resource-row-description">{s.description}</span>}
                    {Object.keys(s.labels).length > 0 && (
                      <span className="resource-row-meta">
                        {Object.entries(s.labels).map(([l, v]) => (
                          <span key={l} data-testid={`skill-label-${s.name}-${l}`}>
                            {l}: v{v}
                          </span>
                        ))}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="resource-detail" data-testid="skill-detail">
          {!detail || !shown ? (
            <p className="resource-empty">{t('skills.selectPrompt')}</p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 800 }}>
              <div>
                <Button type="button" variant="ghost" size="sm" className="resource-back" onClick={backToList}>
                  {t('skills.back')}
                </Button>
                <div className="resource-detail-header" style={{ marginBottom: 4 }}>
                  <h2 ref={detailHeadingRef} tabIndex={-1}>{detail.name}</h2>
                  {canRemove &&
                    (confirmRemove ? (
                      <Button variant="danger" size="sm" data-testid="skill-remove-confirm" loading={busy} onClick={doRemove}>
                        {t('skills.confirmRemove')}
                      </Button>
                    ) : (
                      <Button variant="ghost" size="sm" className="workflow-danger-action" data-testid="skill-remove" onClick={() => setConfirmRemove(true)}>
                        {t('skills.remove')}
                      </Button>
                    ))}
                </div>
                {shown.description && <p style={{ color: 'var(--text-secondary)' }}>{shown.description}</p>}
              </div>
              <div className="resource-inline-form">
                <div className="resource-field">
                  <label htmlFor={`${ids}-version`}>{t('skills.version')}</label>
                  <select
                    id={`${ids}-version`}
                    data-testid="skill-version-select"
                    value={version ?? ''}
                    onChange={(e) => setVersion(Number(e.target.value))}
                  >
                    {detail.versions.map((v) => (
                      <option key={v.version} value={v.version}>
                        v{v.version}
                        {v.labels.length ? ` (${v.labels.join(', ')})` : ''}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {shown.tools.length > 0 && (
                <div style={{ fontSize: 'var(--type-ui)' }}>
                  {t('skills.tools')}: <code>{shown.tools.join(', ')}</code>
                </div>
              )}
              <section aria-labelledby={`${ids}-instructions`}>
                <h3 id={`${ids}-instructions`} className="resource-label" style={{ marginBottom: 4 }}>{t('skills.instructions')}</h3>
                <pre data-testid="skill-text" className="resource-document">
                  {shown.instructions}
                </pre>
              </section>
              {Object.entries(shown.resources).map(([name, text]) => (
                <section key={name} aria-label={`${t('skills.resource')}: ${name}`}>
                  <h3 className="resource-label" style={{ marginBottom: 4 }}>
                    {t('skills.resource')}: {name}
                  </h3>
                  <pre data-testid={`skill-resource-${name}`} className="resource-document">
                    {text}
                  </pre>
                </section>
              ))}
              <form
                className="resource-inline-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void applyLabel();
                }}
              >
                <div className="resource-field">
                  <label htmlFor={`${ids}-label`}>{t('skills.labelName')}</label>
                  <input
                    id={`${ids}-label`}
                    data-testid="skill-label-input"
                    list={`${ids}-label-options`}
                    value={labelName}
                    maxLength={64}
                    onChange={(e) => setLabelName(e.target.value)}
                  />
                  <datalist id={`${ids}-label-options`}>
                    {allLabels.map((l) => (
                      <option key={l} value={l} />
                    ))}
                  </datalist>
                </div>
                <Button
                  type="submit"
                  variant={canEdit ? 'primary' : 'secondary'}
                  data-testid="skill-set-label"
                  disabled={!canEdit || busy}
                  title={canEdit ? undefined : t('skills.viewerNote')}
                >
                  {t('skills.setLabel').replace('{version}', String(version ?? ''))}
                </Button>
              </form>
              {!canEdit && <p className="resource-help">{t('skills.viewerNote')}</p>}
            </div>
          )}
        </div>
      </div>

      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title={t('skills.new')}
        maxWidth={640}
        footer={
          <>
            <Button type="button" variant="ghost" onClick={() => setCreateOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" form={`${ids}-create`} variant="primary" data-testid="skill-create-submit" loading={busy} disabled={!draft.trim()}>
              {t('skills.save')}
            </Button>
          </>
        }
      >
        <form id={`${ids}-create`} onSubmit={submitCreate} className="resource-field">
          <label htmlFor="skill-markdown">{t('skills.markdownLabel')}</label>
          <span id={`${ids}-paste-help`} className="resource-help">{t('skills.pasteHint')}</span>
          <textarea
            id="skill-markdown"
            data-testid="skill-markdown"
            aria-describedby={`${ids}-paste-help`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={14}
            spellCheck={false}
            className="resource-code-input"
          />
          {actionError && (
            <div role="alert">
              <Alert variant="danger" data-testid="skill-create-error">
                {actionError}
              </Alert>
            </div>
          )}
        </form>
      </Modal>
    </PageLayout>
  );
}
