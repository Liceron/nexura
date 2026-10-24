// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { useRef, useState } from 'react';
import { useApi } from '../../api/context';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { Input } from '../../components/Input';
import { useI18n } from '../../i18n';
import { TemplateGraph } from '../templates/TemplateGraph';
import type { TemplateDetail } from '../templates/types';
import { MAX_DESCRIPTION_CHARS, requestDraft, toDraftFailure } from './api';
import type { DraftFailure, DraftResponse } from './api';
import { formatApiError } from '../../api/errors';

interface DescribePanelProps {
  onCreated: (workflowId: string) => void;
  onClose: () => void;
}

const box: React.CSSProperties = {
  padding: 12,
  borderRadius: 'var(--radius-md)',
  fontSize: 13,
  border: '1px solid var(--border-subtle)',
};

/**
 * "Describe it": plain-language description in, DRAFT workflow out.
 *
 * The draft lives only in this component. It is not saved or run until the user presses
 * "Create from draft", which uses the normal validate-then-create path.
 */
export function DescribePanel({ onCreated, onClose }: DescribePanelProps) {
  const { manager } = useApi();
  const { t } = useI18n();
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [failure, setFailure] = useState<DraftFailure | null>(null);
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const latest = useRef(0);

  const tooLong = description.length > MAX_DESCRIPTION_CHARS;
  const canDraft = description.trim().length > 0 && !tooLong && !busy;

  const handleDraft = async () => {
    const ticket = ++latest.current;
    setBusy(true);
    setFailure(null);
    setCreateError(null);
    setDraft(null);
    try {
      const result = await requestDraft(manager.client, description);
      if (ticket !== latest.current) return;
      setDraft(result);
      setName(result.spec.name || '');
    } catch (err) {
      if (ticket !== latest.current) return;
      setFailure(toDraftFailure(err));
    } finally {
      if (ticket === latest.current) setBusy(false);
    }
  };

  const handleDiscard = () => {
    latest.current++;
    setDraft(null);
    setFailure(null);
    setCreateError(null);
    setBusy(false);
  };

  const handleCreate = async () => {
    if (!draft) return;
    setCreating(true);
    setCreateError(null);
    try {
      const spec = { ...draft.spec, name: name.trim() || draft.spec.name };
      const report = await manager.client.workflows.validate(spec);
      if (!report.valid) {
        setCreateError(report.errors.map((e) => `[${e.code}] ${e.message}`).join('; '));
        return;
      }
      const created = await manager.client.workflows.create(spec, { name: spec.name });
      onCreated(created.workflow_id);
      onClose();
    } catch (err) {
      setCreateError(formatApiError(err, t));
    } finally {
      setCreating(false);
    }
  };

  const graphSpec = draft ? (draft.spec as unknown as TemplateDetail['spec']) : null;

  return (
    <div
      data-testid="describe-panel"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
        maxHeight: '70vh',
        overflowY: 'auto',
        paddingRight: 4,
      }}
    >
      <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)' }}>
        {t('workflows.describeLabel')}
        <textarea
          data-testid="describe-input"
          aria-label={t('workflows.describeLabel')}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={t('workflows.describePlaceholder')}
          rows={4}
          style={{
            display: 'block',
            width: '100%',
            marginTop: 6,
            padding: 8,
            fontSize: 14,
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            background: 'var(--bg-surface)',
            color: 'var(--text-primary)',
            resize: 'vertical',
          }}
        />
      </label>
      <div
        data-testid="describe-count"
        style={{ fontSize: 12, color: tooLong ? 'var(--status-danger)' : 'var(--text-secondary)' }}
      >
        {description.length} / {MAX_DESCRIPTION_CHARS} {t('workflows.describeChars')}
      </div>

      <div style={{ display: 'flex', gap: 10 }}>
        <Button
          type="button"
          variant="primary"
          loading={busy}
          disabled={!canDraft}
          onClick={handleDraft}
          data-testid="describe-draft-btn"
        >
          {busy ? t('workflows.describeDrafting') : t('workflows.describeDraft')}
        </Button>
        <Button type="button" variant="ghost" onClick={onClose}>
          {t('common.cancel')}
        </Button>
      </div>

      {failure && (
        <div
          role="alert"
          data-testid="describe-error"
          style={{
            ...box,
            borderColor: 'var(--status-danger)',
            background: 'var(--status-danger-bg)',
            color: 'var(--status-danger)',
          }}
        >
          {failure.code === 'NO_MODEL_CREDENTIAL' ? (
            <>
              <div>{t('workflows.describeNoCredential')}</div>
              <a href="#/credentials" onClick={onClose} data-testid="describe-open-credentials">
                {t('workflows.describeOpenCredentials')}
              </a>
            </>
          ) : (
            <>
              <strong>[{failure.code}]</strong> {failure.message}
            </>
          )}
          {failure.warnings.length > 0 && (
            <ul style={{ margin: '6px 0 0 16px', padding: 0 }}>
              {failure.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {draft && graphSpec && (
        <div data-testid="describe-result" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div
            data-testid="describe-unsaved"
            style={{ ...box, background: 'var(--status-warning-bg)', borderColor: 'var(--status-warning)' }}
          >
            {t('workflows.describeUnsaved')}
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Badge variant={draft.validation.valid ? 'success' : 'warning'}>
              {draft.validation.valid ? t('workflows.describeValid') : t('workflows.describeInvalid')}
            </Badge>
          </div>
          <div data-testid="describe-explanation" style={box}>
            <strong>{t('workflows.describeExplanation')}</strong>
            <p style={{ margin: '6px 0 0' }}>{draft.explanation}</p>
          </div>
          {draft.warnings.length > 0 && (
            <div data-testid="describe-warnings" style={{ ...box, borderColor: 'var(--status-warning)' }}>
              <strong>{t('workflows.describeWarnings')}</strong>
              <ul style={{ margin: '6px 0 0 16px', padding: 0 }}>
                {draft.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
          <div style={{ maxHeight: 240, overflow: 'auto', border: '1px solid var(--border-subtle)' }}>
            <TemplateGraph spec={graphSpec} label={draft.spec.name} />
          </div>
          <Input
            label={t('workflows.workflowName')}
            value={name}
            onChange={(e) => setName(e.target.value)}
            data-testid="describe-name"
          />
          {createError && (
            <div role="alert" data-testid="describe-create-error" style={{ ...box, color: 'var(--status-danger)' }}>
              {createError}
            </div>
          )}
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
            <Button type="button" variant="secondary" onClick={handleDiscard} data-testid="describe-discard">
              {t('workflows.describeDiscard')}
            </Button>
            <Button
              type="button"
              variant="primary"
              loading={creating}
              disabled={!name.trim() && !draft.spec.name}
              onClick={handleCreate}
              data-testid="describe-create"
            >
              {t('workflows.describeCreate')}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
