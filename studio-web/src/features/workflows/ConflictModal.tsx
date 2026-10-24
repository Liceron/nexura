// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useT } from '../../i18n';
import { useState } from 'react';
import { Modal } from '../../components/Modal';
import { Button } from '../../components/Button';
import { Input } from '../../components/Input';
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';

interface ConflictModalProps {
  open: boolean;
  workflowName: string;
  expectedRevision: number;
  localSpec: WorkflowSpec;
  onClose: () => void;
  onReloadServer: () => void;
  onSaveAsCopy: (copyName: string) => Promise<void>;
}

export function ConflictModal({
  open,
  workflowName,
  expectedRevision,
  localSpec,
  onClose,
  onReloadServer,
  onSaveAsCopy,
}: ConflictModalProps) {
  const t = useT();
  const [copyName, setCopyName] = useState(() => t('workflows.copySuffix').replace('{name}', workflowName));
  const [showCopyInput, setShowCopyInput] = useState(false);
  const [isCopying, setIsCopying] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);

  if (!open) return null;

  const localNodes = Object.keys(localSpec.nodes || {});
  const localEntry = localSpec.entry_node || t('common.none');

  const handleCopySubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!copyName.trim()) return;
    setIsCopying(true);
    setCopyError(null);
    try {
      await onSaveAsCopy(copyName.trim());
      onClose();
    } catch (err: unknown) {
      setCopyError(formatApiError(err, t));
    } finally {
      setIsCopying(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('ui.revisionConflictDetectedCas409')}
      maxWidth={540}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div
          style={{
            padding: 12,
            backgroundColor: 'var(--status-danger-bg)',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--status-danger)',
            fontSize: 13,
            color: 'var(--status-danger)',
            lineHeight: 1.5,
          }}
        >
          <Icon name="warning" /> <strong>{t('ui.concurrentModificationConflict')}</strong> {t('ui.theServerVersionOfThisWorkflowWasModifiedByAnotherEditorSessionAfterRevision')}{' '}
          <code>{expectedRevision}</code> {t('ui.wasLoaded')}<br />
          <span style={{ fontSize: 12, marginTop: 4, display: 'inline-block' }}>
            {t('ui.yourLocalEditsHaveBeenPreservedInThisEditorSessionAndWillNotBeSilentlyOverw')}
          </span>
        </div>

        {/* Local Edits Summary / Diff Preview */}
        <div
          style={{
            padding: 12,
            backgroundColor: 'var(--bg-canvas)',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            fontSize: 12,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 6, color: 'var(--text-primary)' }}>
            {t('ui.uncommittedLocalEditsSummary')}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, color: 'var(--text-secondary)' }}>
            <div>
              {t('ui.entryNode2') + " "}<code>{localEntry}</code>
            </div>
            <div>
              {t('ui.nodeCount') + " "}<strong>{localNodes.length}</strong> (
              {localNodes.slice(0, 4).join(', ')}
              {localNodes.length > 4 ? '...' : ''})
            </div>
            <div>
              {t('ui.targetExpectedRevision') + " "}<code>{expectedRevision}</code>
            </div>
          </div>
        </div>

        {/* Copy Name Input if Copy Mode Active */}
        {showCopyInput && (
          <form onSubmit={handleCopySubmit} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <Input
              label={t('ui.newWorkflowNameForCopy')}
              value={copyName}
              onChange={(e) => setCopyName(e.target.value)}
              placeholder={t('ui.eGMyWorkflowCopy')}
              autoFocus
            />
            {copyError && (
              <div style={{ color: 'var(--status-danger)', fontSize: 12 }}>{copyError}</div>
            )}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setShowCopyInput(false)}
                disabled={isCopying}
              >
                {t('ui.cancel')}
              </Button>
              <Button
                variant="primary"
                size="sm"
                type="submit"
                loading={isCopying}
              >
                {t('ui.confirmSaveAsCopy')}
              </Button>
            </div>
          </form>
        )}

        {/* Action Choices */}
        {!showCopyInput && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 4 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: 10,
                border: '1px solid var(--border-subtle)',
                borderRadius: 'var(--radius-sm)',
              }}
            >
              <div>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{t('ui.saveAsNewCopy')}</div>
                <div style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>
                  {t('ui.createAnIndependentCopyPreservingAllYourLocalEdits')}
                </div>
              </div>
              <Button
                variant="primary"
                size="sm"
                onClick={() => setShowCopyInput(true)}
              >
                {t('ui.saveAsCopy')}
              </Button>
            </div>

            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: 10,
                border: '1px solid var(--border-subtle)',
                borderRadius: 'var(--radius-sm)',
              }}
            >
              <div>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{t('ui.reloadFromServer')}</div>
                <div style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>
                  {t('ui.discardLocalEditsAndReloadTheLatestServerRevision')}
                </div>
              </div>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  onReloadServer();
                  onClose();
                }}
              >
                {t('ui.reloadServer')}
              </Button>
            </div>

            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: 10,
                border: '1px solid var(--border-subtle)',
                borderRadius: 'var(--radius-sm)',
              }}
            >
              <div>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{t('ui.keepLocalEdits')}</div>
                <div style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>
                  {t('ui.remainInEditorToInspectOrExportJsonManually')}
                </div>
              </div>
              <Button variant="secondary" size="sm" onClick={onClose}>
                {t('ui.keepLocal')}
              </Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
