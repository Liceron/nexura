// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import React, { useState } from 'react';
import { useApi } from '../../api/context';
import { Modal } from '../../components/Modal';
import { Input } from '../../components/Input';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { useI18n } from '../../i18n';
import { REGISTRY_TEMPLATES } from './templates';
import type { WorkflowTemplate } from './types';
import type { WorkflowSpec, ValidationIssue } from '@liceron/nexura-sdk';
import { DescribePanel } from '../builder-assistant';
import { formatApiError } from '../../api/errors';

interface CreateWorkflowModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: (workflowId: string) => void;
  initialTemplateId?: string | null;
  /**
   * Additional templates listed after the registry ones. Not used by Studio itself; unit tests pass
   * fixtures such as INCOMPATIBLE_TEST_TEMPLATE here so test-only templates never reach users.
   */
  extraTemplates?: WorkflowTemplate[];
}

export function CreateWorkflowModal({
  open,
  onClose,
  onCreated,
  initialTemplateId,
  extraTemplates,
}: CreateWorkflowModalProps) {
  const { manager } = useApi();
  const { t } = useI18n();

  const [selectedTemplateId, setSelectedTemplateId] = useState<string>(
    initialTemplateId || 'order-triage-support'
  );
  const [name, setName] = useState<string>(t('workflows.defaultName'));
  const [nameTouched, setNameTouched] = useState<boolean>(false);
  const [isValidating, setIsValidating] = useState<boolean>(false);
  const [validationErrors, setValidationErrors] = useState<ValidationIssue[]>([]);
  const [generalError, setGeneralError] = useState<string | null>(null);
  const [describing, setDescribing] = useState<boolean>(false);

  const allTemplates = [...REGISTRY_TEMPLATES, ...(extraTemplates ?? [])];

  const handleTemplateChange = (templateId: string) => {
    setSelectedTemplateId(templateId);
    setValidationErrors([]);
    setGeneralError(null);
    if (!nameTouched) {
      if (templateId === 'blank') {
        setName(t('workflows.blankName'));
      } else {
        const tmpl = allTemplates.find((t) => t.id === templateId);
        if (tmpl) setName(tmpl.name);
      }
    }
  };

  const getSpecToCreate = (): WorkflowSpec => {
    if (selectedTemplateId === 'blank') {
      return {
        name,
        entry_node: 'start',
        nodes: {
          start: {
            id: 'start',
            handler: 'synthetic_order_lookup',
            transitions: {},
            requires_approval: false,
          },
        },
      };
    }
    const tmpl = allTemplates.find((t) => t.id === selectedTemplateId);
    if (tmpl) {
      return {
        ...tmpl.spec,
        name,
      };
    }
    throw new Error(t('workflows.unknownTemplate'));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsValidating(true);
    setValidationErrors([]);
    setGeneralError(null);

    try {
      const spec = getSpecToCreate();

      // Authoritative pre-validation against installed module registry
      const valReport = await manager.client.workflows.validate(spec);
      if (!valReport.valid) {
        setValidationErrors(valReport.errors);
        return;
      }

      // Create workflow
      const created = await manager.client.workflows.create(spec, { name });
      onCreated(created.workflow_id);
      onClose();
    } catch (err: unknown) {
      setGeneralError(formatApiError(err, t));
    } finally {
      setIsValidating(false);
    }
  };

  if (describing) {
    return (
      <Modal open={open} onClose={onClose} title={t('workflows.describeIt')}>
        <div style={{ marginBottom: 12 }}>
          <button
            type="button"
            data-testid="describe-back"
            onClick={() => setDescribing(false)}
            style={{ fontSize: 13, color: 'var(--accent-primary)' }}
          >
            {t('workflows.describeBack')}
          </button>
        </div>
        <DescribePanel onCreated={onCreated} onClose={onClose} />
      </Modal>
    );
  }

  return (
    <Modal open={open} onClose={onClose} title={t('workflows.createModalTitle')}>
      <div style={{ marginBottom: 12 }}>
        <Button
          type="button"
          variant="secondary"
          onClick={() => setDescribing(true)}
          data-testid="create-describe-it"
        >
          {t('workflows.describeIt')}
        </Button>
      </div>
      <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <Input
          label={t('workflows.workflowName')}
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setNameTouched(true);
          }}
          required
        />

        <div>
          <a
            href="#/templates"
            data-testid="create-browse-gallery"
            onClick={onClose}
            style={{ fontSize: 13, color: 'var(--accent-primary)' }}
          >
            {t('workflows.browseGallery')}
          </a>
        </div>

        <div>
          <label
            style={{
              display: 'block',
              fontSize: 13,
              fontWeight: 500,
              color: 'var(--text-secondary)',
              marginBottom: 6,
            }}
          >
            {t('workflows.selectTemplate')}
          </label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 220, overflowY: 'auto' }}>
            {allTemplates.map((tmpl) => (
              <label
                key={tmpl.id}
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 10,
                  padding: 10,
                  borderRadius: 'var(--radius-sm)',
                  border: `1px solid ${
                    selectedTemplateId === tmpl.id
                      ? 'var(--brand-primary)'
                      : 'var(--border-subtle)'
                  }`,
                  backgroundColor:
                    selectedTemplateId === tmpl.id
                      ? 'rgba(37, 99, 235, 0.05)'
                      : 'var(--bg-surface)',
                  cursor: 'pointer',
                }}
              >
                <input
                  type="radio"
                  name="template"
                  value={tmpl.id}
                  checked={selectedTemplateId === tmpl.id}
                  onChange={() => handleTemplateChange(tmpl.id)}
                  style={{ marginTop: 3 }}
                />
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
                    <span style={{ fontWeight: 600, fontSize: 13 }}>{t(`examples.${tmpl.id}.title`, tmpl.title)}</span>
                    {tmpl.is_synthetic && <Badge variant="info">{t('ui.syntheticFixture')}</Badge>}
                    {tmpl.category === 'testing' && <Badge variant="warning">{t('ui.incompatibleTest')}</Badge>}
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                    {t(`examples.${tmpl.id}.description`, tmpl.description)}
                  </div>
                </div>
              </label>
            ))}

            <label
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 10,
                padding: 10,
                borderRadius: 'var(--radius-sm)',
                border: `1px solid ${
                  selectedTemplateId === 'blank'
                    ? 'var(--brand-primary)'
                    : 'var(--border-subtle)'
                }`,
                backgroundColor:
                  selectedTemplateId === 'blank'
                    ? 'rgba(37, 99, 235, 0.05)'
                    : 'var(--bg-surface)',
                cursor: 'pointer',
              }}
            >
              <input
                type="radio"
                name="template"
                value="blank"
                checked={selectedTemplateId === 'blank'}
                onChange={() => handleTemplateChange('blank')}
                style={{ marginTop: 3 }}
              />
              <div>
                <span style={{ fontWeight: 600, fontSize: 13 }}>{t('workflows.blankWorkflow')}</span>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                  {t('ui.startWithAMinimalSingleNodeGraph')}
                </div>
              </div>
            </label>
          </div>
        </div>

        {validationErrors.length > 0 && (
          <div
            style={{
              padding: 12,
              backgroundColor: 'var(--status-danger-bg)',
              border: '1px solid var(--status-danger)',
              borderRadius: 'var(--radius-md)',
              color: 'var(--status-danger)',
              fontSize: 13,
            }}
          >
            <strong>{t('ui.templateValidationFailed')}</strong>
            <ul style={{ margin: '6px 0 0 16px', padding: 0 }}>
              {validationErrors.map((err, i) => (
                <li key={i}>
                  [{err.code}] {err.message} ({err.node_id ? t('workflows.issueNode').replace('{id}', err.node_id) : t('ui.graph')})
                </li>
              ))}
            </ul>
          </div>
        )}

        {generalError && (
          <div
            style={{
              padding: 12,
              backgroundColor: 'var(--status-danger-bg)',
              border: '1px solid var(--status-danger)',
              borderRadius: 'var(--radius-md)',
              color: 'var(--status-danger)',
              fontSize: 13,
            }}
          >
            <strong>{t('ui.error2')}</strong> {generalError}
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 8 }}>
          <Button type="button" variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" variant="primary" loading={isValidating} data-testid="create-workflow-submit-btn">
            {isValidating ? t('ui.validating') : t('workflows.validateAndCreate')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
