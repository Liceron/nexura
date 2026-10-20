// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { AgentRecord } from '@liceron/nexura-sdk';
import { useApi } from '../../api/context';
import { formatApiError } from '../../api/errors';
import { Alert } from '../../components/Alert';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import {
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

const fieldStyle = {
  padding: '6px 10px',
  borderRadius: 'var(--radius-md)',
  border: '1px solid var(--border-subtle)',
  backgroundColor: 'var(--bg-surface)',
  color: 'var(--text-primary)',
  fontSize: 13,
  width: '100%',
  boxSizing: 'border-box',
} as const;

const monoStyle = { ...fieldStyle, fontFamily: 'var(--font-mono, monospace)', resize: 'vertical' } as const;

const labelStyle = { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, fontWeight: 600 } as const;
const helpStyle = { fontSize: 11, fontWeight: 400, color: 'var(--text-secondary)' } as const;
const checkRow = { display: 'flex', gap: 6, fontSize: 12, alignItems: 'baseline', fontWeight: 400 } as const;

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
      } catch (err: unknown) {
        if (request === openRequest.current) setActionError(formatApiError(err, t));
      }
    },
    [get],
  );

  const startNew = () => {
    openRequest.current += 1; // a pending open must not replace the new draft
    resetMessages();
    setNotice(null);
    setRecord(null);
    setDraft(emptyDraft(defaultModel));
    setMode('new');
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
      <span role="alert" data-testid={`agent-error-${field}`} style={{ fontSize: 11, fontWeight: 400, color: 'var(--status-danger)' }}>
        {errors[field]}
      </span>
    ) : null;

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

  return (
    <div data-testid="agents-view" style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, margin: 0 }}>{t('agents.title')}</h1>
        <div style={{ flex: 1 }} />
        <Button
          variant="primary"
          data-testid="agents-new"
          disabled={!canEdit}
          title={canEdit ? undefined : t('agents.viewerNote')}
          onClick={startNew}
        >
          {t('agents.new')}
        </Button>
      </div>
      <p style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)' }}>{t('agents.intro')}</p>

      {error && <Alert variant="danger">{error}</Alert>}
      {optionsError && (
        <Alert variant="warning" data-testid="agents-options-error">
          {optionsError}
        </Alert>
      )}
      {notice && (
        <Alert variant="success" data-testid="agents-notice">
          {notice}
        </Alert>
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
        <Alert variant="danger" data-testid="agents-error">
          {actionError}
        </Alert>
      )}

      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <Card style={{ flex: '1 1 280px', minWidth: 260 }}>
          {loading ? (
            <Spinner size={18} />
          ) : items.length === 0 ? (
            <div data-testid="agents-empty" style={{ color: 'var(--text-secondary)', fontSize: 13, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <span>{t('agents.empty')}</span>
              <span>{t('agents.modelsNote')}</span>
            </div>
          ) : (
            <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
              {items.map((a) => {
                const active = record?.agent_id === a.agent_id && mode === 'edit';
                const gated = a.approval_tools?.length ?? 0;
                return (
                  <li key={a.agent_id}>
                    <button
                      type="button"
                      data-testid={`agent-row-${a.name}`}
                      aria-pressed={active}
                      onClick={() => void open(a.agent_id)}
                      style={{
                        width: '100%',
                        textAlign: 'left',
                        padding: 10,
                        borderRadius: 'var(--radius-md)',
                        border: `1px solid ${active ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                        backgroundColor: 'transparent',
                        color: 'var(--text-primary)',
                        cursor: 'pointer',
                      }}
                    >
                      <div style={{ fontWeight: 600, fontSize: 14 }}>{a.name}</div>
                      <div style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '2px 0 6px' }}>{a.description}</div>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        <Badge variant="info" data-testid={`agent-row-model-${a.name}`}>
                          {a.model}
                        </Badge>
                        <Badge variant="neutral" data-testid={`agent-row-tools-${a.name}`}>
                          {t('agents.toolCount').replace('{count}', String(a.tool_count))}
                        </Badge>
                        {gated > 0 && (
                          <Badge variant="warning" data-testid={`agent-row-approval-${a.name}`}>
                            {t('agents.approvalCount').replace('{count}', String(gated))}
                          </Badge>
                        )}
                        <Badge variant="neutral">{t('agents.revision').replace('{revision}', String(a.revision))}</Badge>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>

        <Card className="studio-detail-card" style={{ flex: '2 1 420px', minWidth: 320 }} data-testid="agent-detail">
          {mode === 'none' ? (
            <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{t('agents.selectPrompt')}</div>
          ) : (
            <form onSubmit={save} noValidate style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <h2 style={{ margin: 0, fontSize: 16 }}>
                  {mode === 'new' ? t('agents.new') : record?.spec.name}
                </h2>
                {record && mode === 'edit' && (
                  <Badge variant="neutral" data-testid="agent-revision">
                    {t('agents.revision').replace('{revision}', String(record.revision))}
                  </Badge>
                )}
                <div style={{ flex: 1 }} />
                {canRemove && mode === 'edit' && record &&
                  (confirmRemove ? (
                    <Button type="button" variant="danger" size="sm" data-testid="agent-delete-confirm" loading={busy} onClick={doRemove}>
                      {t('agents.confirmRemove')}
                    </Button>
                  ) : (
                    <Button type="button" variant="ghost" size="sm" data-testid="agent-delete" onClick={() => setConfirmRemove(true)}>
                      {t('agents.remove')}
                    </Button>
                  ))}
              </div>
              {!canEdit && (
                <div data-testid="agent-read-only" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                  {t('agents.viewerNote')}
                </div>
              )}
              {record && mode === 'edit' && (
                <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                  {t('agents.useHint')} <code data-testid="agent-id">{record.agent_id}</code>
                </div>
              )}

              <fieldset disabled={!canEdit || busy} style={{ border: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 12 }}>
                <label style={labelStyle}>
                  {t('agents.name')}
                  <input
                    data-testid="agent-name"
                    value={draft.name}
                    maxLength={64}
                    onChange={(e) => set('name', e.target.value)}
                    style={fieldStyle}
                  />
                  <span style={helpStyle}>{t('agents.nameHelp')}</span>
                  {fieldError('name')}
                </label>
                <label style={labelStyle}>
                  {t('agents.description')}
                  <input
                    data-testid="agent-description"
                    value={draft.description}
                    onChange={(e) => set('description', e.target.value)}
                    style={fieldStyle}
                  />
                  {fieldError('description')}
                </label>
                <label style={labelStyle}>
                  {t('agents.instructions')}
                  <textarea
                    data-testid="agent-instructions"
                    value={draft.instructions}
                    rows={6}
                    spellCheck={false}
                    onChange={(e) => set('instructions', e.target.value)}
                    style={monoStyle}
                  />
                  <span style={helpStyle}>
                    {t('agents.charCount')
                      .replace('{count}', String(draft.instructions.length))
                      .replace('{max}', String(limits.max_instructions_chars))}
                  </span>
                  {fieldError('instructions')}
                </label>
                <label style={labelStyle}>
                  {t('agents.model')}
                  <select data-testid="agent-model" value={draft.model} onChange={(e) => set('model', e.target.value)} style={fieldStyle}>
                    {modelChoices.length === 0 && <option value={draft.model}>{draft.model}</option>}
                    {modelChoices.map((m) => (
                      <option key={m.name} value={m.name}>
                        {m.name}
                        {m.offline ? ` (${t('agents.offline')})` : ''}
                        {'missing' in m ? ` (${t('agents.modelMissing')})` : ''}
                      </option>
                    ))}
                  </select>
                  <span style={helpStyle} data-testid="agent-models-note">{t('agents.modelsNote')}</span>
                  {fieldError('model')}
                </label>

                <fieldset data-testid="agent-tools" style={{ border: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <legend style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    {t('agents.tools')} ({draft.tools.length}/{limits.max_tools})
                  </legend>
                  <span style={helpStyle}>{t('agents.toolsHelp')}</span>
                  {options && toolOptions.length === 0 && missingTools.length === 0 && (
                    <span style={helpStyle}>{t('agents.toolsEmpty')}</span>
                  )}
                  {toolOptions.map((o) => (
                    <label key={o.module_id} style={checkRow}>
                      <input
                        type="checkbox"
                        data-testid={`agent-tool-${o.module_id}`}
                        checked={draft.tools.includes(o.module_id)}
                        onChange={() => toggle('tools', o.module_id)}
                      />
                      <span>
                        <strong>{o.title || o.module_id}</strong> <code>{o.module_id}</code>{' '}
                        {o.requires_approval && approvalBadge(`agent-tool-approval-${o.module_id}`)}
                        <span style={{ display: 'block', color: 'var(--text-secondary)' }}>{o.description}</span>
                      </span>
                    </label>
                  ))}
                  {missingTools.map((id) => (
                    <label key={id} style={{ ...checkRow, color: 'var(--status-warning)' }}>
                      <input type="checkbox" checked data-testid={`agent-tool-${id}`} onChange={() => toggle('tools', id)} />
                      <span>
                        <code>{id}</code> ({t('agents.toolMissing')}){' '}
                        {(toolPolicy.get(id) ?? true) && approvalBadge(`agent-tool-approval-${id}`)}
                      </span>
                    </label>
                  ))}
                  {fieldError('tools')}
                </fieldset>

                <fieldset data-testid="agent-knowledge" style={{ border: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <legend style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    {t('agents.knowledge')} ({draft.knowledge.length}/{limits.max_knowledge_collections})
                  </legend>
                  {(options?.knowledge_collections ?? []).map((c) => (
                    <label key={c} style={checkRow}>
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
                    <label key={c} style={{ ...checkRow, color: 'var(--status-warning)' }}>
                      <input type="checkbox" checked data-testid={`agent-knowledge-${c}`} onChange={() => toggle('knowledge', c)} />
                      <span>
                        <code>{c}</code> ({t('agents.notInWorkspace')})
                      </span>
                    </label>
                  ))}
                  {fieldError('knowledge')}
                </fieldset>

                <fieldset data-testid="agent-skills" style={{ border: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <legend style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    {t('agents.skills')} ({draft.skills.length}/{limits.max_skills})
                  </legend>
                  {options && (options.skills ?? []).length === 0 && missingSkills.length === 0 && (
                    <span style={helpStyle}>{t('agents.skillsEmpty')}</span>
                  )}
                  {(options?.skills ?? []).map((s) => (
                    <label key={s} style={checkRow}>
                      <input type="checkbox" data-testid={`agent-skill-${s}`} checked={draft.skills.includes(s)} onChange={() => toggle('skills', s)} />
                      <code>{s}</code>
                    </label>
                  ))}
                  {missingSkills.map((s) => (
                    <label key={s} style={{ ...checkRow, color: 'var(--status-warning)' }}>
                      <input type="checkbox" checked data-testid={`agent-skill-${s}`} onChange={() => toggle('skills', s)} />
                      <span>
                        <code>{s}</code> ({t('agents.notInWorkspace')})
                      </span>
                    </label>
                  ))}
                  {fieldError('skills')}
                  <label style={{ ...labelStyle, marginTop: 4 }}>
                    {t('agents.skillsLabel')}
                    <input
                      data-testid="agent-skills-label"
                      value={draft.skills_label}
                      maxLength={64}
                      onChange={(e) => set('skills_label', e.target.value)}
                      style={fieldStyle}
                    />
                    <span style={helpStyle}>{t('agents.skillsLabelHelp')}</span>
                    {fieldError('skills_label')}
                  </label>
                </fieldset>

                <label style={labelStyle}>
                  {t('agents.outputSchema')}
                  <textarea
                    data-testid="agent-output-schema"
                    value={draft.output_schema}
                    rows={4}
                    spellCheck={false}
                    placeholder='{"type": "object", "properties": {"answer": {"type": "string"}}}'
                    onChange={(e) => set('output_schema', e.target.value)}
                    style={monoStyle}
                  />
                  <span style={helpStyle}>{t('agents.outputSchemaHelp')}</span>
                  {fieldError('output_schema')}
                </label>

                <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 10 }}>
                  <label style={labelStyle}>
                    {t('agents.maxTurns')}
                    <input
                      type="number"
                      data-testid="agent-max-turns"
                      min={1}
                      max={limits.max_turns}
                      step={1}
                      value={draft.max_turns}
                      onChange={(e) => set('max_turns', e.target.value)}
                      style={fieldStyle}
                    />
                    <span style={helpStyle}>{`1-${limits.max_turns}`}</span>
                    {fieldError('max_turns')}
                  </label>
                  <label style={labelStyle}>
                    {t('agents.maxToolCalls')}
                    <input
                      type="number"
                      data-testid="agent-max-tool-calls"
                      min={0}
                      max={limits.max_tool_calls}
                      step={1}
                      value={draft.max_tool_calls}
                      onChange={(e) => set('max_tool_calls', e.target.value)}
                      style={fieldStyle}
                    />
                    <span style={helpStyle}>{`0-${limits.max_tool_calls}`}</span>
                    {fieldError('max_tool_calls')}
                  </label>
                  <label style={labelStyle}>
                    {t('agents.timeoutSeconds')}
                    <input
                      type="number"
                      data-testid="agent-timeout"
                      min={1}
                      max={limits.max_timeout_seconds}
                      value={draft.timeout_seconds}
                      onChange={(e) => set('timeout_seconds', e.target.value)}
                      style={fieldStyle}
                    />
                    <span style={helpStyle}>{`1-${limits.max_timeout_seconds}`}</span>
                    {fieldError('timeout_seconds')}
                  </label>
                </div>
                {fieldError('form')}
              </fieldset>

              {canEdit && (
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                  {mode === 'new' && (
                    <Button type="button" onClick={() => { resetMessages(); setMode('none'); }}>
                      {t('common.cancel')}
                    </Button>
                  )}
                  <Button type="submit" variant="primary" data-testid="agent-save" loading={busy}>
                    {mode === 'new' ? t('agents.create') : t('agents.save')}
                  </Button>
                </div>
              )}
            </form>
          )}
        </Card>
      </div>
    </div>
  );
}
