// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useAfterRender } from '../../app/useAfterRender';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { AgentRecord } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { formatApiError } from '../../api/errors';
import { Alert } from '../../components/Alert';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { PageHeader, PageLayout } from '../../components/PageHeader';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { AgentGuardrailsSection, EffectClassBadge } from './AgentGuardrails';
import {
  DEFAULT_GUARDRAIL_PRESETS,
  DEFAULT_SERVER_LIMITS,
  OFFLINE_MODEL,
  draftFromRecord,
  emptyDraft,
  fieldErrorsFromApi,
  isRevisionConflict,
  validateDraft,
  type AgentDraft,
  type DraftErrors,
  type DraftField,
} from './agentForm';
import { useAgents } from './useAgents';
import { getMyMembership } from '../../api/membership';
import { useCompactLayout } from '../../app/useCompactLayout';

type Mode = 'none' | 'new' | 'edit';

export function AgentsView() {
  const { t } = useI18n();
  const { manager, connection } = useApi();
  const { items, limits: listLimits, options, loading, error, optionsError, get, create, update, remove } = useAgents();
  // Unknown until the membership answers: nothing is editable before then.
  const [role, setRole] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>('none');
  const [record, setRecord] = useState<AgentRecord | null>(null);
  const [draft, setDraft] = useState<AgentDraft>(() => emptyDraft());
  const [errors, setErrors] = useState<DraftErrors>({});
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  // Phones show one pane at a time; the hidden editor stays mounted so a draft survives Back.
  const [phonePane, setPhonePane] = useState<'list' | 'detail'>('list');
  const ids = useId();
  const listRef = useRef<HTMLUListElement>(null);
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const afterRender = useAfterRender();
  const newButtonRef = useRef<HTMLButtonElement>(null);
  // On phones the list is hidden once a row opens, so focus follows into the detail pane.
  const compact = useCompactLayout();
  const compactRef = useRef(compact);
  compactRef.current = compact;

  // Same role source as the Skills page (a server without memberships is the local owner).
  // A slower answer for a previous workspace is ignored.
  useEffect(() => {
    let current = true;
    setRole(null);
    getMyMembership(manager.client, connection.workspace)
      .then((m) => current && setRole(String((m as unknown as { role?: string }).role ?? 'admin')))
      .catch(() => current && setRole('admin'));
    return () => {
      current = false;
    };
  }, [manager, connection.workspace]);

  // Another workspace: close whatever agent of the previous one was open.
  useEffect(() => {
    setMode('none');
    setRecord(null);
    setDraft(emptyDraft());
    setErrors({});
    setActionError(null);
    setConflict(false);
    setConfirmRemove(false);
    setPhonePane('list');
  }, [connection.workspace]);
  const canEdit = role === 'admin' || role === 'editor';
  const canRemove = role === 'admin';

  const limits = options?.limits ?? listLimits ?? DEFAULT_SERVER_LIMITS;
  const models = options?.models ?? [];
  const defaultModel = models.find((m) => m.default)?.name ?? models[0]?.name ?? OFFLINE_MODEL;
  const toolOptions = options?.tools ?? [];
  const toolPolicy = useMemo(() => {
    const map = new Map<string, boolean>();
    for (const p of record?.tools ?? []) map.set(p.module_id, p.requires_approval);
    for (const o of toolOptions) map.set(o.module_id, o.requires_approval);
    return map;
  }, [record, toolOptions]);

  const resetMessages = () => {
    setErrors({});
    setActionError(null);
    setConflict(false);
    setConfirmRemove(false);
  };

  // Only the newest open (or New) may fill the editor; a slower earlier one is dropped.
  const openRequest = useRef(0);
  const open = useCallback(
    async (agentId: string) => {
      const request = ++openRequest.current;
      resetMessages();
      try {
        const loaded = await get(agentId);
        if (request !== openRequest.current) return;
        setRecord(loaded);
        setDraft(draftFromRecord(loaded));
        setMode('edit');
        setPhonePane('detail');
        if (compactRef.current) afterRender(() => detailHeadingRef.current?.focus());
      } catch (err: unknown) {
        if (request === openRequest.current) setActionError(formatApiError(err, t));
      }
    },
    [get, afterRender],
  );

  const startNew = () => {
    openRequest.current += 1; // a pending open must not replace the new draft
    resetMessages();
    setNotice(null);
    setRecord(null);
    setDraft(emptyDraft(defaultModel, options?.guardrails?.defaults ?? DEFAULT_GUARDRAIL_PRESETS));
    setMode('new');
    setPhonePane('detail');
    afterRender(() => detailHeadingRef.current?.focus());
  };

  const set = <K extends keyof AgentDraft>(key: K, value: AgentDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const toggle = (key: 'tools' | 'knowledge' | 'skills', value: string) =>
    setDraft((d) => ({
      ...d,
      [key]: d[key].includes(value) ? d[key].filter((v) => v !== value) : [...d[key], value],
    }));

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canEdit) return;
    setNotice(null);
    setActionError(null);
    setConflict(false);
    const checked = validateDraft(draft, limits);
    setErrors(checked.errors);
    if (!checked.spec) return;
    setBusy(true);
    try {
      const saved =
        mode === 'edit' && record
          ? await update(record.agent_id, checked.spec, record.revision)
          : await create(checked.spec);
      setRecord(saved);
      setDraft(draftFromRecord(saved));
      setMode('edit');
      setNotice(
        t(mode === 'new' ? 'agents.created' : 'agents.saved')
          .replace('{name}', saved.spec.name)
          .replace('{revision}', String(saved.revision)),
      );
    } catch (err: unknown) {
      if (isRevisionConflict(err)) setConflict(true);
      setErrors(fieldErrorsFromApi(err));
      setActionError(formatApiError(err, t));
    } finally {
      setBusy(false);
    }
  };

  const doRemove = async () => {
    if (!record) return;
    setBusy(true);
    setActionError(null);
    try {
      await remove(record.agent_id, record.revision);
      setRecord(null);
      setMode('none');
      setPhonePane('list');
      setConfirmRemove(false);
      setNotice(t('agents.removed').replace('{name}', record.spec.name));
    } catch (err: unknown) {
      if (isRevisionConflict(err)) setConflict(true);
      setActionError(formatApiError(err, t));
      setConfirmRemove(false);
    } finally {
      setBusy(false);
    }
  };

  const fieldError = (field: DraftField) =>
    errors[field] ? (
      <span role="alert" id={`${ids}-${field}-error`} data-testid={`agent-error-${field}`} className="resource-error">
        {errors[field]}
      </span>
    ) : null;
  const describedBy = (field: DraftField, help?: boolean) =>
    [help ? `${ids}-${field}-help` : null, errors[field] ? `${ids}-${field}-error` : null].filter(Boolean).join(' ') || undefined;
  const field = (name: DraftField, label: string, control: ReactNode, help?: ReactNode) => (
    <div className="resource-field">
      <label htmlFor={`${ids}-${name}`}>{label}</label>
      {control}
      {help && <span id={`${ids}-${name}-help`} className="resource-help">{help}</span>}
      {fieldError(name)}
    </div>
  );

  const modelChoices = models.some((m) => m.name === draft.model) || !draft.model
    ? models
    : [...models, { name: draft.model, label: draft.model, provider: '', offline: false, streaming: false, default: false, missing: true }];
  const knownTools = new Set(toolOptions.map((o) => o.module_id));
  const missingTools = draft.tools.filter((id) => !knownTools.has(id));
  const knownCollections = new Set(options?.knowledge_collections ?? []);
  const missingCollections = draft.knowledge.filter((id) => !knownCollections.has(id));
  const knownSkills = new Set(options?.skills ?? []);
  const missingSkills = draft.skills.filter((n) => !knownSkills.has(n));

  const approvalBadge = (testId: string) => (
    <Badge variant="warning" data-testid={testId}>
      {t('agents.needsApproval')}
    </Badge>
  );

  const editing = mode !== 'none';
  const backToList = () => {
    setPhonePane('list');
    afterRender(() => listRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus());
  };

  return (
    <PageLayout data-testid="agents-view">
      <PageHeader
        title={t('agents.title')}
        actions={
          <Button
            ref={newButtonRef}
            variant={editing && canEdit ? 'secondary' : 'primary'}
            data-testid="agents-new"
            disabled={!canEdit}
            title={canEdit ? undefined : t('agents.viewerNote')}
            onClick={startNew}
          >
            {t('agents.new')}
          </Button>
        }
      />
      <details className="resource-help-disclosure" style={{ marginTop: -16, marginBottom: 16 }}>
        <summary>{t('agents.help')}</summary>
        <p>{t('agents.intro')}</p>
        <p>{t('agents.modelsNote')}</p>
      </details>

      <div className="resource-notices">
        {error && <Alert variant="danger">{error}</Alert>}
        {optionsError && (
          <Alert variant="warning" data-testid="agents-options-error">
            {optionsError}
          </Alert>
        )}
        {notice && (
          <div role="status">
            <Alert variant="success" data-testid="agents-notice">
              {notice}
            </Alert>
          </div>
        )}
        {conflict && (
          <Alert variant="warning" data-testid="agent-conflict">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <span>{t('agents.conflict')}</span>
              {record && (
                <Button size="sm" data-testid="agent-conflict-reload" onClick={() => void open(record.agent_id)}>
                  {t('agents.reload')}
                </Button>
              )}
            </div>
          </Alert>
        )}
        {actionError && (
          <div role="alert">
            <Alert variant="danger" data-testid="agents-error">
              {actionError}
            </Alert>
          </div>
        )}
      </div>

      <div className={`studio-list-detail resource-layout--${phonePane === 'detail' && editing ? 'detail' : 'browse'}`}>
        <div className="resource-list-pane">
          {loading ? (
            <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '16px 0' }}>
              <Spinner size={16} /> <span className="resource-help">{t('common.loading')}</span>
            </div>
          ) : items.length === 0 ? (
            <div data-testid="agents-empty" className="resource-empty">
              <strong>{t('agents.empty')}</strong>
              <span>{t('agents.modelsNote')}</span>
            </div>
          ) : (
            <ul ref={listRef} className="resource-list" aria-label={t('agents.listLabel')}>
              {items.map((a) => {
                const active = record?.agent_id === a.agent_id && mode === 'edit';
                const gated = a.approval_tools?.length ?? 0;
                return (
                  <li key={a.agent_id}>
                    <button
                      type="button"
                      className="resource-row"
                      data-testid={`agent-row-${a.name}`}
                      aria-pressed={active}
                      onClick={() => void open(a.agent_id)}
                    >
                      <span className="resource-row-name">{a.name}</span>
                      {a.description && <span className="resource-row-description">{a.description}</span>}
                      <span className="resource-row-meta">
                        <span data-testid={`agent-row-model-${a.name}`}>{a.model}</span>
                        <span data-testid={`agent-row-tools-${a.name}`}>
                          {t('agents.toolCount').replace('{count}', String(a.tool_count))}
                        </span>
                        {gated > 0 && (
                          <span className="resource-row-attention" data-testid={`agent-row-approval-${a.name}`}>
                            {t('agents.approvalCount').replace('{count}', String(gated))}
                          </span>
                        )}
                        <span>{t('agents.revision').replace('{revision}', String(a.revision))}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div className="resource-detail" data-testid="agent-detail">
          {mode === 'none' ? (
            <p className="resource-empty">{t('agents.selectPrompt')}</p>
          ) : (
            <form onSubmit={save} noValidate className="resource-form" aria-labelledby={`${ids}-title`}>
              <div>
                <Button type="button" variant="ghost" size="sm" className="resource-back" onClick={backToList}>
                  {t('agents.back')}
                </Button>
                <div className="resource-detail-header" style={{ marginBottom: 4 }}>
                  <h2 id={`${ids}-title`} ref={detailHeadingRef} tabIndex={-1}>
                    {mode === 'new' ? t('agents.new') : record?.spec.name}
                  </h2>
                  {canRemove && mode === 'edit' && record &&
                    (confirmRemove ? (
                      <Button type="button" variant="danger" size="sm" data-testid="agent-delete-confirm" loading={busy} onClick={doRemove}>
                        {t('agents.confirmRemove')}
                      </Button>
                    ) : (
                      <Button type="button" variant="ghost" size="sm" className="workflow-danger-action" data-testid="agent-delete" onClick={() => setConfirmRemove(true)}>
                        {t('agents.remove')}
                      </Button>
                    ))}
                </div>
                {record && mode === 'edit' && (
                  <div className="inspection-meta">
                    <span data-testid="agent-revision">{t('agents.revision').replace('{revision}', String(record.revision))}</span>
                    <span>
                      {t('agents.useHint')} <code data-testid="agent-id">{record.agent_id}</code>
                    </span>
                  </div>
                )}
                {!canEdit && (
                  <p data-testid="agent-read-only" className="resource-help" style={{ marginTop: 8 }}>
                    {t('agents.viewerNote')}
                  </p>
                )}
              </div>

              <fieldset disabled={!canEdit || busy} className="resource-form" style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }}>
                <section className="resource-section" aria-labelledby={`${ids}-identity`}>
                  <h3 id={`${ids}-identity`}>{t('agents.sectionIdentity')}</h3>
                  {field('name', t('agents.name'),
                    <input
                      id={`${ids}-name`}
                      data-testid="agent-name"
                      value={draft.name}
                      maxLength={64}
                      aria-invalid={errors.name ? true : undefined}
                      aria-describedby={describedBy('name', true)}
                      onChange={(e) => set('name', e.target.value)}
                    />, t('agents.nameHelp'))}
                  {field('description', t('agents.description'),
                    <input
                      id={`${ids}-description`}
                      data-testid="agent-description"
                      value={draft.description}
                      aria-invalid={errors.description ? true : undefined}
                      aria-describedby={describedBy('description')}
                      onChange={(e) => set('description', e.target.value)}
                    />)}
                </section>

                <section className="resource-section" aria-labelledby={`${ids}-instructions-title`}>
                  <h3 id={`${ids}-instructions-title`}>{t('agents.instructions')}</h3>
                  <div className="resource-field">
                    <textarea
                      id={`${ids}-instructions`}
                      data-testid="agent-instructions"
                      aria-labelledby={`${ids}-instructions-title`}
                      value={draft.instructions}
                      rows={8}
                      spellCheck={false}
                      aria-invalid={errors.instructions ? true : undefined}
                      aria-describedby={describedBy('instructions', true)}
                      onChange={(e) => set('instructions', e.target.value)}
                    />
                    <span id={`${ids}-instructions-help`} className="resource-help">
                      {t('agents.charCount')
                        .replace('{count}', String(draft.instructions.length))
                        .replace('{max}', String(limits.max_instructions_chars))}
                    </span>
                    {fieldError('instructions')}
                  </div>
                </section>

                <section className="resource-section" aria-labelledby={`${ids}-model-title`}>
                  <h3 id={`${ids}-model-title`}>{t('agents.sectionModel')}</h3>
                  {field('model', t('agents.model'),
                    <select
                      id={`${ids}-model`}
                      data-testid="agent-model"
                      value={draft.model}
                      aria-invalid={errors.model ? true : undefined}
                      aria-describedby={describedBy('model', true)}
                      onChange={(e) => set('model', e.target.value)}
                    >
                      {modelChoices.length === 0 && <option value={draft.model}>{draft.model}</option>}
                      {modelChoices.map((m) => (
                        <option key={m.name} value={m.name}>
                          {m.name}
                          {m.offline ? ` (${t('agents.offline')})` : ''}
                          {'missing' in m ? ` (${t('agents.modelMissing')})` : ''}
                        </option>
                      ))}
                    </select>,
                    <span data-testid="agent-models-note">{t('agents.modelsNote')}</span>)}
                  <div className="resource-limits">
                    {field('max_turns', t('agents.maxTurns'),
                      <input
                        id={`${ids}-max_turns`}
                        type="number"
                        data-testid="agent-max-turns"
                        min={1}
                        max={limits.max_turns}
                        step={1}
                        value={draft.max_turns}
                        aria-invalid={errors.max_turns ? true : undefined}
                        aria-describedby={describedBy('max_turns', true)}
                        onChange={(e) => set('max_turns', e.target.value)}
                      />, `1-${limits.max_turns}`)}
                    {field('max_tool_calls', t('agents.maxToolCalls'),
                      <input
                        id={`${ids}-max_tool_calls`}
                        type="number"
                        data-testid="agent-max-tool-calls"
                        min={0}
                        max={limits.max_tool_calls}
                        step={1}
                        value={draft.max_tool_calls}
                        aria-invalid={errors.max_tool_calls ? true : undefined}
                        aria-describedby={describedBy('max_tool_calls', true)}
                        onChange={(e) => set('max_tool_calls', e.target.value)}
                      />, `0-${limits.max_tool_calls}`)}
                    {field('timeout_seconds', t('agents.timeoutSeconds'),
                      <input
                        id={`${ids}-timeout_seconds`}
                        type="number"
                        data-testid="agent-timeout"
                        min={1}
                        max={limits.max_timeout_seconds}
                        value={draft.timeout_seconds}
                        aria-invalid={errors.timeout_seconds ? true : undefined}
                        aria-describedby={describedBy('timeout_seconds', true)}
                        onChange={(e) => set('timeout_seconds', e.target.value)}
                      />, `1-${limits.max_timeout_seconds}`)}
                  </div>
                </section>

                <fieldset data-testid="agent-tools" className="resource-section">
                  <legend>
                    {t('agents.tools')} ({draft.tools.length}/{limits.max_tools})
                  </legend>
                  <span className="resource-help">{t('agents.toolsHelp')}</span>
                  {options && toolOptions.length === 0 && missingTools.length === 0 && (
                    <span className="resource-help">{t('agents.toolsEmpty')}</span>
                  )}
                  <div className="resource-checks">
                    {toolOptions.map((o) => (
                      <label key={o.module_id} className="resource-check">
                        <input
                          type="checkbox"
                          data-testid={`agent-tool-${o.module_id}`}
                          checked={draft.tools.includes(o.module_id)}
                          onChange={() => toggle('tools', o.module_id)}
                        />
                        <span>
                          <strong>{o.title || o.module_id}</strong> <code>{o.module_id}</code>{' '}
                          {o.requires_approval && approvalBadge(`agent-tool-approval-${o.module_id}`)}{' '}
                          <EffectClassBadge effectClass={o.effect_class} testId={`agent-tool-effect-${o.module_id}`} />
                          <span className="resource-check-detail">{o.description}</span>
                        </span>
                      </label>
                    ))}
                    {missingTools.map((id) => (
                      <label key={id} className="resource-check resource-check--missing">
                        <input type="checkbox" checked data-testid={`agent-tool-${id}`} onChange={() => toggle('tools', id)} />
                        <span>
                          <code>{id}</code> ({t('agents.toolMissing')}){' '}
                          {(toolPolicy.get(id) ?? true) && approvalBadge(`agent-tool-approval-${id}`)}
                        </span>
                      </label>
                    ))}
                  </div>
                  {fieldError('tools')}
                </fieldset>

                <fieldset data-testid="agent-knowledge" className="resource-section">
                  <legend>
                    {t('agents.knowledge')} ({draft.knowledge.length}/{limits.max_knowledge_collections})
                  </legend>
                  <div className="resource-checks">
                    {(options?.knowledge_collections ?? []).map((c) => (
                      <label key={c} className="resource-check">
                        <input
                          type="checkbox"
                          data-testid={`agent-knowledge-${c}`}
                          checked={draft.knowledge.includes(c)}
                          onChange={() => toggle('knowledge', c)}
                        />
                        <code>{c}</code>
                      </label>
                    ))}
                    {missingCollections.map((c) => (
                      <label key={c} className="resource-check resource-check--missing">
                        <input type="checkbox" checked data-testid={`agent-knowledge-${c}`} onChange={() => toggle('knowledge', c)} />
                        <span>
                          <code>{c}</code> ({t('agents.notInWorkspace')})
                        </span>
                      </label>
                    ))}
                  </div>
                  {fieldError('knowledge')}
                </fieldset>

                <fieldset data-testid="agent-skills" className="resource-section">
                  <legend>
                    {t('agents.skills')} ({draft.skills.length}/{limits.max_skills})
                  </legend>
                  {options && (options.skills ?? []).length === 0 && missingSkills.length === 0 && (
                    <span className="resource-help">{t('agents.skillsEmpty')}</span>
                  )}
                  <div className="resource-checks">
                    {(options?.skills ?? []).map((s) => (
                      <label key={s} className="resource-check">
                        <input type="checkbox" data-testid={`agent-skill-${s}`} checked={draft.skills.includes(s)} onChange={() => toggle('skills', s)} />
                        <code>{s}</code>
                      </label>
                    ))}
                    {missingSkills.map((s) => (
                      <label key={s} className="resource-check resource-check--missing">
                        <input type="checkbox" checked data-testid={`agent-skill-${s}`} onChange={() => toggle('skills', s)} />
                        <span>
                          <code>{s}</code> ({t('agents.notInWorkspace')})
                        </span>
                      </label>
                    ))}
                  </div>
                  {fieldError('skills')}
                  {field('skills_label', t('agents.skillsLabel'),
                    <input
                      id={`${ids}-skills_label`}
                      data-testid="agent-skills-label"
                      value={draft.skills_label}
                      maxLength={64}
                      aria-invalid={errors.skills_label ? true : undefined}
                      aria-describedby={describedBy('skills_label', true)}
                      onChange={(e) => set('skills_label', e.target.value)}
                    />, t('agents.skillsLabelHelp'))}
                </fieldset>

                <AgentGuardrailsSection
                  draft={draft}
                  setDraft={setDraft}
                  options={options?.guardrails}
                  models={models}
                  error={fieldError('guardrails')}
                />

                <section className="resource-section" aria-labelledby={`${ids}-output-title`}>
                  <h3 id={`${ids}-output-title`}>{t('agents.sectionOutput')}</h3>
                  {field('output_schema', t('agents.outputSchema'),
                    <textarea
                      id={`${ids}-output_schema`}
                      data-testid="agent-output-schema"
                      value={draft.output_schema}
                      rows={4}
                      spellCheck={false}
                      placeholder='{"type": "object", "properties": {"answer": {"type": "string"}}}'
                      aria-invalid={errors.output_schema ? true : undefined}
                      aria-describedby={describedBy('output_schema', true)}
                      onChange={(e) => set('output_schema', e.target.value)}
                    />, t('agents.outputSchemaHelp'))}
                </section>
                {fieldError('form')}
              </fieldset>

              {canEdit && (
                <div className="resource-actions" style={{ justifyContent: 'flex-start' }}>
                  <Button type="submit" variant="primary" data-testid="agent-save" loading={busy}>
                    {mode === 'new' ? t('agents.create') : t('agents.save')}
                  </Button>
                  {mode === 'new' && (
                    <Button type="button" variant="ghost" onClick={() => {
                      resetMessages();
                      setMode('none');
                      setPhonePane('list');
                      requestAnimationFrame(() => newButtonRef.current?.focus());
                    }}>
                      {t('common.cancel')}
                    </Button>
                  )}
                </div>
              )}
            </form>
          )}
        </div>
      </div>
    </PageLayout>
  );
}
