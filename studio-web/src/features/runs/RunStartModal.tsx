// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useT } from '../../i18n';
import { useState, useMemo } from 'react';
import { Modal } from '../../components/Modal';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { useApi } from '../../api/context';
import type { WorkflowSpec, ValidationIssue } from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';

interface RunStartModalProps {
  open: boolean;
  workflowId: string;
  workflowName: string;
  serverRevision: number;
  currentSpec: WorkflowSpec;
  hasUnsavedDraft: boolean;
  onClose: () => void;
  onLaunchRun: (initialState: Record<string, unknown>, asyncExecution: boolean) => Promise<void>;
}

export function RunStartModal({
  open,
  workflowName,
  serverRevision,
  currentSpec,
  hasUnsavedDraft,
  onClose,
  onLaunchRun,
}: RunStartModalProps) {
  const t = useT();
  const { manager } = useApi();

  const [initialStateText, setInitialStateText] = useState<string>(
    JSON.stringify(
      {
        order_id: 'ORD-501',
        ticket_id: 'TICKET-501',
        customer_tier: 'premium',
      },
      null,
      2
    )
  );
  const [asyncExecution, setAsyncExecution] = useState(false);
  const [isValidating, setIsValidating] = useState(false);
  const [validationIssues, setValidationIssues] = useState<ValidationIssue[] | null>(null);
  const [isLaunching, setIsLaunching] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);

  // Parse state text
  const { parsedState, parseError } = useMemo(() => {
    if (!initialStateText.trim()) {
      return { parsedState: {}, parseError: null };
    }
    try {
      const parsed = JSON.parse(initialStateText);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return { parsedState: null, parseError: t('runStart.objectError') };
      }
      return { parsedState: parsed as Record<string, unknown>, parseError: null };
    } catch (err: unknown) {
      return {
        parsedState: null,
        parseError: t('approvalReview.jsonError') + ': ' + formatApiError(err, t),
      };
    }
  }, [initialStateText, t]);

  if (!open) return null;

  const handleValidateGraph = async () => {
    setIsValidating(true);
    setValidationIssues(null);
    setLaunchError(null);
    try {
      const report = await manager.client.workflows.validate(currentSpec);
      if (report.valid) {
        setValidationIssues([]);
      } else {
        setValidationIssues(report.errors || []);
      }
    } catch (err: unknown) {
      setLaunchError(formatApiError(err, t));
    } finally {
      setIsValidating(false);
    }
  };

  const handleFormatJson = () => {
    if (parsedState) {
      setInitialStateText(JSON.stringify(parsedState, null, 2));
    }
  };

  const handleLaunch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (parseError || !parsedState) return;

    setIsLaunching(true);
    setLaunchError(null);
    try {
      await onLaunchRun(parsedState, asyncExecution);
      onClose();
    } catch (err: unknown) {
      setLaunchError(formatApiError(err, t));
    } finally {
      setIsLaunching(false);
    }
  };

  const hasValidationErrors = validationIssues !== null && validationIssues.length > 0;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('runStart.title').replace('{name}', workflowName)}
      maxWidth={540}
    >
      <form onSubmit={handleLaunch} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {/* Authoritative Revision Notice */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '8px 12px',
            backgroundColor: 'var(--bg-canvas)',
            borderRadius: 'var(--radius-sm)',
            fontSize: 12,
            border: '1px solid var(--border-subtle)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ color: 'var(--text-secondary)' }}>{t('ui.targetServerRevision')}</span>
            <Badge variant="neutral">{t('ui.rev3') + " "}{serverRevision}</Badge>
          </div>
          {hasUnsavedDraft && (
            <Badge variant="warning">{t('ui.unsavedEditsOnCanvas')}</Badge>
          )}
        </div>

        {/* Warning if uncommitted draft exists */}
        {hasUnsavedDraft && (
          <div
            role="alert"
            style={{
              padding: 10,
              backgroundColor: 'var(--status-warning-bg)',
              color: 'var(--text-primary)',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--status-warning)',
              fontSize: 12,
              lineHeight: 1.5,
            }}
          >
            <Icon name="warning" /> <strong>{t('ui.notice')}</strong> {t('ui.youHaveUncommittedLocalEditsOnTheCanvasRunningThisWorkflowExecutesAuthoritativ')}{' '}
            <code>{serverRevision}</code>{t('ui.pleaseSaveYourCanvasFirstCtrlSIfYouWantYourLatestChangesIncluded')}
          </div>
        )}

        {/* Initial State JSON Input */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <label htmlFor="initial-state-json" style={{ fontSize: 13, fontWeight: 600 }}>
              {t('ui.initialStateJson')}
            </label>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={handleFormatJson}
              disabled={Boolean(parseError)}
            >
              {t('ui.formatJson')}
            </Button>
          </div>
          <textarea
            id="initial-state-json"
            rows={7}
            value={initialStateText}
            onChange={(e) => setInitialStateText(e.target.value)}
            aria-label={t('ui.initialStateJsonInput')}
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              padding: 10,
              borderRadius: 'var(--radius-sm)',
              border: `1px solid ${parseError ? 'var(--status-danger)' : 'var(--border-subtle)'}`,
              backgroundColor: 'var(--bg-canvas)',
              color: 'var(--text-primary)',
            }}
          />
          {parseError && (
            <div style={{ fontSize: 12, color: 'var(--status-danger)' }}>{parseError}</div>
          )}
        </div>

        {/* Validate Before Run Section */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
              {t('ui.checkGraphRoutesMappingsAndHandlerContractsBeforeRunning')}
            </span>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              loading={isValidating}
              onClick={handleValidateGraph}
            >
              {t('ui.validateGraph')}
            </Button>
          </div>

          {validationIssues !== null && (
            <div>
              {validationIssues.length === 0 ? (
                <div
                  style={{
                    padding: '6px 10px',
                    backgroundColor: 'var(--status-success-bg)',
                    color: 'var(--status-success)',
                    borderRadius: 'var(--radius-sm)',
                    fontSize: 12,
                  }}
                >
                  <Icon name="check" /> {t('ui.graphValidationPassedNoStructuralOrSchemaIssuesFound')}
                </div>
              ) : (
                <div
                  style={{
                    padding: '8px 10px',
                    backgroundColor: 'var(--status-danger-bg)',
                    color: 'var(--status-danger)',
                    borderRadius: 'var(--radius-sm)',
                    fontSize: 12,
                  }}
                >
                  <strong>{t('ui.validationFailed')}{validationIssues.length} {t('ui.errors')}</strong>
                  <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                    {validationIssues.map((issue, idx) => (
                      <li key={idx}>
                        {issue.node_id ? `[${issue.node_id}] ` : ''}
                        {issue.message}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Asynchronous start option */}
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 13,
            cursor: 'pointer',
            padding: '4px 0',
          }}
        >
          <input
            type="checkbox"
            checked={asyncExecution}
            onChange={(e) => setAsyncExecution(e.target.checked)}
          />
          <span>{t('ui.executeInBackgroundAsynchronousStart')}</span>
        </label>

        {launchError && (
          <div
            style={{
              padding: 10,
              backgroundColor: 'var(--status-danger-bg)',
              color: 'var(--status-danger)',
              borderRadius: 'var(--radius-sm)',
              fontSize: 12,
            }}
          >
            {launchError}
          </div>
        )}

        {/* Footer actions */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 4 }}>
          <Button type="button" variant="secondary" onClick={onClose}>
            {t('ui.cancel')}
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={isLaunching}
            disabled={Boolean(parseError) || hasValidationErrors}
          >
            {t('ui.launchRun')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
