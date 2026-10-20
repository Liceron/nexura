// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useT, englishT, type Translate } from '../../i18n';
import { useState, useMemo, useEffect } from 'react';
import { Modal } from '../../components/Modal';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';
import { useApi } from '../../api/context';
import type { WorkflowRecord, WorkflowSpec } from '@liceron/nexura-sdk';
import {
  exportWorkflowToJson,
  validateAndParseWorkflowImport,
  ImportValidationResult,
} from './codecs';
import { formatApiError } from '../../api/errors';

type Mode = 'bundle' | 'export' | 'import' | 'pyexport' | 'pyimport';

interface PythonExport {
  source: string;
  filename: string;
}

/** Messages (each with its line number) from a failed Python import. */
export function pythonImportErrors(err: unknown, t: Translate = englishT): string[] {
  const details = (err as { details?: unknown } | null)?.details;
  if (Array.isArray(details)) {
    const lines = details
      .map((d) => (d && typeof (d as { message?: unknown }).message === 'string' ? (d as { message: string }).message : ''))
      .filter(Boolean);
    if (lines.length > 0) return lines;
  }
  return [formatApiError(err, t)];
}

interface ImportExportModalProps {
  open: boolean;
  initialMode?: Mode;
  workflowId: string;
  workflowName: string;
  currentSpec: WorkflowSpec;
  onClose: () => void;
  onApplyImportedSpec?: (spec: WorkflowSpec) => void;
  onCreateFromImport?: (name: string, spec: WorkflowSpec) => Promise<void>;
  /** Called after the server created a workflow from pasted Python. */
  onImportedPython?: (record: WorkflowRecord) => void;
}

export function ImportExportModal({
  open,
  initialMode = 'export',
  workflowId,
  workflowName,
  currentSpec,
  onClose,
  onApplyImportedSpec,
  onCreateFromImport,
  onImportedPython,
}: ImportExportModalProps) {
  const t = useT();
  const { manager } = useApi();
  const client = manager.client;
  const [mode, setMode] = useState<Mode>(initialMode);
  const [copied, setCopied] = useState(false);
  const [downloadingBundle, setDownloadingBundle] = useState(false);

  useEffect(() => {
    if (open) {
      setMode(initialMode);
    }
  }, [open, initialMode]);

  // Import state
  const [importText, setImportText] = useState('');
  const [isImporting, setIsImporting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [archiveSuccess, setArchiveSuccess] = useState<string | null>(null);

  // Python round trip: the server generates / parses the code, it is never executed.
  const [pyExport, setPyExport] = useState<PythonExport | null>(null);
  const [pyExportError, setPyExportError] = useState<string | null>(null);
  const [pyCopied, setPyCopied] = useState(false);
  const [pyText, setPyText] = useState('');
  const [pyErrors, setPyErrors] = useState<string[]>([]);
  const [pyImporting, setPyImporting] = useState(false);

  useEffect(() => {
    if (!open || mode !== 'pyexport' || !workflowId) return;
    let cancelled = false;
    setPyExport(null);
    setPyExportError(null);
    client
      .request<PythonExport>('GET', `/workflows/${encodeURIComponent(workflowId)}/export/python`)
      .then((res) => {
        if (!cancelled) setPyExport(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setPyExportError(formatApiError(err, t));
      });
    return () => {
      cancelled = true;
    };
  }, [open, mode, workflowId, client]);

  // Export memo
  const exportData = useMemo(() => {
    return exportWorkflowToJson(workflowId, workflowName, currentSpec);
  }, [workflowId, workflowName, currentSpec]);

  // Import validation memo
  const validationResult: ImportValidationResult | null = useMemo(() => {
    if (!importText.trim()) return null;
    return validateAndParseWorkflowImport(importText, undefined, t);
  }, [importText, t]);

  if (!open) return null;

  const handleCopyPython = () => {
    if (!pyExport) return;
    navigator.clipboard.writeText(pyExport.source).then(() => {
      setPyCopied(true);
      setTimeout(() => setPyCopied(false), 2000);
    });
  };

  const handleDownloadPython = () => {
    if (!pyExport) return;
    const blob = new Blob([pyExport.source], { type: 'text/x-python' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = pyExport.filename || `workflow_${workflowId || 'export'}.py`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handlePythonFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (event) => {
      if (typeof event.target?.result === 'string') setPyText(event.target.result);
    };
    reader.readAsText(file);
  };

  const handleImportPython = async () => {
    setPyImporting(true);
    setPyErrors([]);
    try {
      const record = await client.request<WorkflowRecord>('POST', '/workflows/import/python', {
        body: { source: pyText },
      });
      if (onImportedPython) onImportedPython(record);
      onClose();
    } catch (err: unknown) {
      setPyErrors(pythonImportErrors(err, t));
    } finally {
      setPyImporting(false);
    }
  };

  const handleCopyExport = () => {
    navigator.clipboard.writeText(exportData.json).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  const handleDownloadExport = () => {
    const blob = new Blob([exportData.json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `workflow_${workflowId || 'export'}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handleDownloadBundle = async () => {
    try {
      setDownloadingBundle(true);
      setActionError(null);
      const buffer = await client.workflows.downloadExport(workflowId);
      const blob = new Blob([buffer], { type: 'application/zip' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `workflow_${workflowId || 'bundle'}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      setActionError(formatApiError(err, t));
    } finally {
      setDownloadingBundle(false);
    }
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    if (file.name.endsWith('.zip')) {
      const reader = new FileReader();
      reader.onload = async (event) => {
        try {
          setIsImporting(true);
          setActionError(null);
          setArchiveSuccess(null);
          const dataUrl = event.target?.result as string;
          const base64Str = dataUrl.split(',')[1];
          const imported = await client.workflows.import({
            archive_base64: base64Str,
            name: file.name.replace('.zip', ''),
          });
          setArchiveSuccess(t('workflowIO.archiveImported').replace('{name}', imported.name).replace('{revision}', String(imported.revision)));
          if (onCreateFromImport && imported.spec) {
            onClose();
          }
        } catch (err: unknown) {
          setActionError(formatApiError(err, t));
        } finally {
          setIsImporting(false);
        }
      };
      reader.readAsDataURL(file);
    } else {
      const reader = new FileReader();
      reader.onload = (event) => {
        const content = event.target?.result;
        if (typeof content === 'string') {
          setImportText(content);
        }
      };
      reader.readAsText(file);
    }
  };

  const handleApplyToCanvas = () => {
    if (!validationResult || !validationResult.valid || !validationResult.spec) return;
    if (onApplyImportedSpec) {
      onApplyImportedSpec(validationResult.spec);
      onClose();
    }
  };

  const handleCreateNewWorkflow = async () => {
    if (!validationResult || !validationResult.valid || !validationResult.spec) return;
    if (!onCreateFromImport) return;

    setIsImporting(true);
    setActionError(null);
    try {
      await onCreateFromImport(
        validationResult.workflowName || t('workflowIO.importedName').replace('{name}', workflowName),
        validationResult.spec
      );
      onClose();
    } catch (err: unknown) {
      setActionError(formatApiError(err, t));
    } finally {
      setIsImporting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('ui.workflowSpecificationImportExport')}
      maxWidth={680}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {/* Mode Selector Tabs */}
        <div role="tablist" style={{ display: 'flex', gap: 8, borderBottom: '1px solid var(--border-subtle)', paddingBottom: 8 }}>
          <Button
            variant={mode === 'export' ? 'primary' : 'secondary'}
            size="sm"
            role="tab"
            aria-selected={mode === 'export'}
            onClick={() => setMode('export')}
          >
            {t('ui.exportJson')}
          </Button>
          <Button
            variant={mode === 'bundle' ? 'primary' : 'secondary'}
            size="sm"
            role="tab"
            aria-selected={mode === 'bundle'}
            onClick={() => setMode('bundle')}
          >
            {t('ui.runnableSdkBundleZip')}
          </Button>
          <Button
            variant={mode === 'import' ? 'primary' : 'secondary'}
            size="sm"
            role="tab"
            aria-selected={mode === 'import'}
            onClick={() => setMode('import')}
          >
            {t('ui.importJson')}
          </Button>
          <Button
            variant={mode === 'pyexport' ? 'primary' : 'secondary'}
            size="sm"
            role="tab"
            aria-selected={mode === 'pyexport'}
            onClick={() => setMode('pyexport')}
          >
            {t('ui.exportAsPython')}
          </Button>
          <Button
            variant={mode === 'pyimport' ? 'primary' : 'secondary'}
            size="sm"
            role="tab"
            aria-selected={mode === 'pyimport'}
            onClick={() => setMode('pyimport')}
          >
            {t('ui.importFromPython')}
          </Button>
        </div>

        {/* 0. Bundle Mode */}
        {mode === 'bundle' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
              {t('ui.downloadAnImmutableSecretFreeRunnableBundleContaining') + " "}<code>workflow.json</code>,{' '}
              <code>requirements.txt</code>, <code>pyproject.toml</code>, <code>README.md</code>{t('ui.and')}{' '}
              <code>run.py</code>.
            </div>

            <div
              style={{
                padding: 12,
                borderRadius: 'var(--radius-sm)',
                backgroundColor: 'var(--bg-canvas)',
                border: '1px solid var(--border-subtle)',
                display: 'flex',
                flexDirection: 'column',
                gap: 8,
                fontSize: 12,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ fontWeight: 600 }}>{t('ui.workflowIdTarget')}</span>
                <code style={{ color: 'var(--accent-primary)' }}>{workflowId || 'current'}</code>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ fontWeight: 600 }}>{t('ui.runtimeRequirements')}</span>
                <code>Python &gt;= 3.12, nexura-runtime &gt;= 0.4.0</code>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ fontWeight: 600 }}>{t('ui.typescriptConsumer')}</span>
                <code>@liceron/nexura-sdk &gt;= 0.4.0</code>
              </div>
            </div>

            {exportData.hasSecrets ? (
              <div
                style={{
                  padding: 10,
                  backgroundColor: 'var(--status-warning-bg)',
                  color: 'var(--status-warning)',
                  fontSize: 12,
                  borderRadius: 'var(--radius-sm)',
                }}
              >
                <Icon name="lock" /> <strong>{t('ui.credentialReferenceInstructions')}</strong> {t('ui.rawSecretTokensAreScrubbedFromTheExportBundleBeforeRunning') + " "}<code>run.py</code>{t('ui.provideCredentialsViaEnvironmentVariablesMatchingReferencesEG') + " "}<code>export API_KEY=&quot;...&quot;</code>).
              </div>
            ) : (
              <div
                style={{
                  padding: 10,
                  backgroundColor: 'var(--status-success-bg)',
                  color: 'var(--status-success)',
                  fontSize: 12,
                  borderRadius: 'var(--radius-sm)',
                }}
              >
                <Icon name="check" /> <strong>{t('ui.selfContainedWorkflow')}</strong> {t('ui.noExternalCredentialsRequired')}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 4 }}>
              <Button
                variant="primary"
                size="md"
                disabled={downloadingBundle}
                onClick={handleDownloadBundle}
              >
                {downloadingBundle ? t('ui.generatingBundle') : t('ui.downloadRunnableBundleZip')}
              </Button>
            </div>
          </div>
        )}

        {/* 1. Export Mode */}
        {mode === 'export' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                {t('ui.downloadOrCopyVersionedJsonWithSchemaValidationAndLayoutCoordinates')}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <Button variant="secondary" size="sm" onClick={handleCopyExport}>
                  {copied ? <><Icon name="check" /> {t('ui.copied')}</> : t('ui.copyJson')}
                </Button>
                <Button variant="primary" size="sm" onClick={handleDownloadExport}>
                  {t('ui.downloadJson')}
                </Button>
              </div>
            </div>

            {exportData.hasSecrets && (
              <div
                style={{
                  padding: 8,
                  backgroundColor: 'var(--status-danger-bg)',
                  color: 'var(--status-danger)',
                  fontSize: 12,
                  borderRadius: 'var(--radius-sm)',
                }}
              >
                <Icon name="warning" /> <strong>{t('ui.securityWarning')}</strong> {t('ui.thisWorkflowSpecificationAppearsToContainRawSecretsOrTokensStripCredentialsBef')}
              </div>
            )}

            <textarea
              readOnly
              value={exportData.json}
              rows={14}
              aria-label={t('ui.exportedWorkflowJson')}
              style={{
                width: '100%',
                padding: 10,
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                backgroundColor: 'var(--bg-canvas)',
                color: 'var(--text-primary)',
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                resize: 'vertical',
              }}
            />
          </div>
        )}

        {/* 1b. Export as Python */}
        {mode === 'pyexport' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                {t('ui.plainPythonThatRebuildsThisWorkflowHandlersStayNamesFromYourRegistry')}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <Button variant="secondary" size="sm" disabled={!pyExport} onClick={handleCopyPython}>
                  {pyCopied ? <><Icon name="check" /> {t('ui.copied')}</> : t('ui.copyPython')}
                </Button>
                <Button variant="primary" size="sm" disabled={!pyExport} onClick={handleDownloadPython}>
                  {t('ui.downloadPy')}
                </Button>
              </div>
            </div>
            {pyExportError && (
              <div role="alert" style={{ color: 'var(--status-danger)', fontSize: 12 }}>
                {pyExportError}
              </div>
            )}
            <textarea
              readOnly
              value={pyExport ? pyExport.source : pyExportError ? '' : t('workflowIO.generatingPython')}
              rows={16}
              aria-label={t('ui.exportedWorkflowPython')}
              style={{
                width: '100%',
                padding: 10,
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                backgroundColor: 'var(--bg-canvas)',
                color: 'var(--text-primary)',
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                resize: 'vertical',
              }}
            />
          </div>
        )}

        {/* 2b. Import from Python */}
        {mode === 'pyimport' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                {t('ui.pastePythonWrittenByExportAsPythonTheServerReadsItWithoutRunningItAndCreate')}
              </div>
              <label
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  padding: '5px 10px',
                  borderRadius: 'var(--radius-sm)',
                  backgroundColor: 'var(--bg-canvas)',
                  border: '1px solid var(--border-subtle)',
                  fontSize: 12,
                  cursor: 'pointer',
                  fontWeight: 500,
                }}
              >
                {t('ui.choosePyFile')}<input type="file" accept=".py,text/x-python" onChange={handlePythonFile} style={{ display: 'none' }} />
              </label>
            </div>
            <textarea
              value={pyText}
              onChange={(e) => setPyText(e.target.value)}
              placeholder={t('ui.pasteWorkflowPythonHere')}
              rows={12}
              aria-label={t('ui.workflowPythonImportInput')}
              style={{
                width: '100%',
                padding: 10,
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                backgroundColor: 'var(--bg-canvas)',
                color: 'var(--text-primary)',
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                resize: 'vertical',
              }}
            />
            {pyErrors.length > 0 && (
              <div
                role="alert"
                aria-label={t('ui.pythonImportErrors')}
                style={{
                  padding: '8px 12px',
                  backgroundColor: 'var(--status-danger-bg)',
                  color: 'var(--status-danger)',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: 12,
                }}
              >
                <strong>{t('ui.pythonImportFailed')}</strong>
                <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                  {pyErrors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <Button
                variant="primary"
                size="sm"
                loading={pyImporting}
                disabled={!pyText.trim()}
                onClick={handleImportPython}
              >
                {t('ui.importPython')}
              </Button>
            </div>
          </div>
        )}

        {/* 2. Import Mode */}
        {mode === 'import' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                {t('ui.uploadOrPasteWorkflowSpecificationJson')}
              </div>
              <label
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  padding: '5px 10px',
                  borderRadius: 'var(--radius-sm)',
                  backgroundColor: 'var(--bg-canvas)',
                  border: '1px solid var(--border-subtle)',
                  fontSize: 12,
                  cursor: 'pointer',
                  fontWeight: 500,
                }}
              >
                <Icon name="folder" /> {t('ui.chooseFile')}<input
                  type="file"
                  accept=".json,application/json"
                  onChange={handleFileUpload}
                  style={{ display: 'none' }}
                />
              </label>
            </div>

            <textarea
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
              placeholder={t('ui.pasteWorkflowJsonHere')}
              rows={10}
              aria-label={t('ui.workflowJsonImportInput')}
              style={{
                width: '100%',
                padding: 10,
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                backgroundColor: 'var(--bg-canvas)',
                color: 'var(--text-primary)',
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                resize: 'vertical',
              }}
            />

            {/* Live Validation Feedback */}
            {validationResult && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {validationResult.valid ? (
                  <div
                    style={{
                      padding: '8px 12px',
                      backgroundColor: 'var(--status-success-bg)',
                      color: 'var(--status-success)',
                      borderRadius: 'var(--radius-sm)',
                      fontSize: 12,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                    }}
                  >
                    <span><Icon name="check" /> {t('ui.validWorkflowJson')}{Object.keys(validationResult.spec?.nodes || {}).length} {t('ui.nodes')}</span>
                    {validationResult.migrated && <Badge variant="info">{t('ui.autoMigrated')}</Badge>}
                  </div>
                ) : (
                  <div
                    style={{
                      padding: '8px 12px',
                      backgroundColor: 'var(--status-danger-bg)',
                      color: 'var(--status-danger)',
                      borderRadius: 'var(--radius-sm)',
                      fontSize: 12,
                    }}
                  >
                    <strong>{t('ui.importValidationFailed')}</strong>
                    <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                      {validationResult.errors.map((e, i) => (
                        <li key={i}>{e}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {validationResult.warnings.length > 0 && (
                  <div
                    style={{
                      padding: '8px 12px',
                      backgroundColor: 'var(--status-warning-bg)',
                      color: 'var(--status-warning)',
                      borderRadius: 'var(--radius-sm)',
                      fontSize: 12,
                    }}
                  >
                    <strong>{t('ui.warnings')}</strong>
                    <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                      {validationResult.warnings.map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            {archiveSuccess && (
              <div
                style={{
                  padding: '8px 12px',
                  backgroundColor: 'var(--status-success-bg)',
                  color: 'var(--status-success)',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: 12,
                }}
              >
                <Icon name="check" /> {archiveSuccess}
              </div>
            )}

            {actionError && (
              <div style={{ color: 'var(--status-danger)', fontSize: 12 }}>{actionError}</div>
            )}

            {/* Import Actions */}
            {validationResult && validationResult.valid && (
              <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 8 }}>
                {onApplyImportedSpec && (
                  <Button variant="secondary" size="sm" onClick={handleApplyToCanvas}>
                    {t('ui.applyToCanvas')}
                  </Button>
                )}
                {onCreateFromImport && (
                  <Button
                    variant="primary"
                    size="sm"
                    loading={isImporting}
                    onClick={handleCreateNewWorkflow}
                  >
                    {t('ui.createNewWorkflow')}
                  </Button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
