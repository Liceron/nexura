// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useCallback, useRef, useState, type CSSProperties } from 'react';
import { useApi } from '../../api/context';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { useI18n } from '../../i18n';
import type { StudioDatasetSummary, SaveFromRunResponse } from '../evaluations/studioEvalsTypes';
import {
  datasetOutputKey,
  getDataset,
  isSafeDatasetId,
  listWorkflowTestDatasets,
  saveRunAsTest,
  suggestDatasetId,
  useCanEdit,
} from '../evaluations/workflowTests';
import { formatApiError } from '../../api/errors';

const NEW = '__new__';
const field: CSSProperties = {
  padding: '6px 10px',
  borderRadius: 'var(--radius-md)',
  border: '1px solid var(--border-subtle)',
  background: 'var(--bg-surface)',
  color: 'var(--text-primary)',
  fontSize: 13,
};
const labelStyle: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, color: 'var(--text-secondary)' };

interface SaveAsTestButtonProps {
  runId: string;
  workflowId: string | null | undefined;
  status: string;
}

/** "Save as test" on a completed workflow run: appends the run as a case of a test dataset. */
export function SaveAsTestButton({ runId, workflowId, status }: SaveAsTestButtonProps) {
  // Only completed runs of a saved workflow qualify; nothing (not even the role) is fetched otherwise.
  if (status !== 'completed' || !workflowId) return null;
  return <SaveAsTestControl runId={runId} workflowId={workflowId} />;
}

function SaveAsTestControl({ runId, workflowId }: { runId: string; workflowId: string }) {
  const { manager } = useApi();
  const { t } = useI18n();
  const canEdit = useCanEdit();
  const [open, setOpen] = useState(false);
  const [datasets, setDatasets] = useState<StudioDatasetSummary[]>([]);
  const [choice, setChoice] = useState(NEW);
  const [newId, setNewId] = useState('');
  const [newName, setNewName] = useState('');
  const [caseName, setCaseName] = useState('');
  const [outputKey, setOutputKey] = useState('');
  const [recordPath, setRecordPath] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<SaveFromRunResponse | null>(null);

  // The latest pick; a slower fetch for an earlier pick must not set its output key.
  const latestPick = useRef(NEW);
  const pick = useCallback(
    async (id: string) => {
      latestPick.current = id;
      setChoice(id);
      setError(null);
      setOutputKey('');
      if (id === NEW) return;
      try {
        const key = datasetOutputKey(await getDataset(manager.client, id)) ?? '';
        if (latestPick.current === id) setOutputKey(key);
      } catch (err) {
        if (latestPick.current === id) setError(formatApiError(err, t));
      }
    },
    [manager],
  );

  const openModal = useCallback(async () => {
    setOpen(true);
    setSaved(null);
    setError(null);
    setCaseName('');
    setOutputKey('');
    setRecordPath(false);
    setNewId(suggestDatasetId(workflowId));
    setNewName(`${workflowId} ${t('workflowTests.tab', 'Tests').toLowerCase()}`);
    try {
      const items = await listWorkflowTestDatasets(manager.client, workflowId);
      setDatasets(items);
      await pick(items.length > 0 ? items[0].dataset_id : NEW);
    } catch (err) {
      setDatasets([]);
      setChoice(NEW);
      setError(formatApiError(err, t));
    }
  }, [manager, workflowId, pick, t]);

  const creating = choice === NEW;
  const idInvalid = creating && !isSafeDatasetId(newId.trim());
  const nameMissing = creating && !newName.trim();

  const submit = async () => {
    if (idInvalid || nameMissing) return;
    setBusy(true);
    setError(null);
    try {
      const res = await saveRunAsTest(manager.client, creating ? newId.trim() : choice, {
        run_id: runId,
        case_name: caseName,
        output_key: outputKey,
        dataset_name: creating ? newName : undefined,
        record_path: recordPath,
      });
      setSaved(res);
    } catch (err) {
      setError(formatApiError(err, t));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button
        data-testid="save-as-test"
        variant="secondary"
        size="sm"
        disabled={!canEdit}
        title={canEdit ? t('workflowTests.saveHint') : t('workflowTests.viewerNote')}
        onClick={() => void openModal()}
      >
        {t('workflowTests.saveAsTest')}
      </Button>
      <Modal open={open} onClose={() => setOpen(false)} title={t('workflowTests.saveTitle')} maxWidth={520}>
        <div data-testid="save-as-test-dialog" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {saved ? (
            <div data-testid="save-as-test-result" role="status" style={{ fontSize: 13, display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div>
                {t('workflowTests.savedCase')}: <strong>{saved.case.name}</strong>
              </div>
              <div>
                {t('workflowTests.dataset')}: <strong>{saved.name}</strong> ({saved.dataset_id}, {t('workflowTests.colRevision')}{' '}
                {saved.revision}, {saved.case_count} {t('workflowTests.colCases').toLowerCase()})
              </div>
              <div data-testid="save-as-test-redactions">
                {t('workflowTests.redactions')}: {saved.redactions}
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <Button variant="primary" onClick={() => setOpen(false)}>
                  {t('common.close', 'Close')}
                </Button>
              </div>
            </div>
          ) : (
            <>
              <label style={labelStyle}>
                {t('workflowTests.dataset')}
                <select
                  data-testid="save-as-test-dataset"
                  style={field}
                  value={choice}
                  onChange={(e) => void pick(e.target.value)}
                >
                  {datasets.map((d) => (
                    <option key={d.dataset_id} value={d.dataset_id}>
                      {d.name || d.dataset_id} ({d.case_count})
                    </option>
                  ))}
                  <option value={NEW}>{t('workflowTests.newDataset')}</option>
                </select>
              </label>
              {creating && (
                <>
                  <label style={labelStyle}>
                    {t('workflowTests.newDatasetId')}
                    <input
                      data-testid="save-as-test-new-id"
                      style={field}
                      value={newId}
                      onChange={(e) => setNewId(e.target.value)}
                      aria-invalid={idInvalid}
                    />
                  </label>
                  {idInvalid && (
                    <span style={{ fontSize: 12, color: 'var(--status-danger)' }}>{t('workflowTests.badDatasetId')}</span>
                  )}
                  <label style={labelStyle}>
                    {t('workflowTests.newDatasetName')}
                    <input
                      data-testid="save-as-test-new-name"
                      style={field}
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                    />
                  </label>
                </>
              )}
              <label style={labelStyle}>
                {t('workflowTests.caseName')}
                <input
                  data-testid="save-as-test-case-name"
                  style={field}
                  value={caseName}
                  maxLength={120}
                  placeholder={runId}
                  onChange={(e) => setCaseName(e.target.value)}
                />
              </label>
              <label style={labelStyle}>
                {t('workflowTests.outputKey')}
                <input
                  data-testid="save-as-test-output-key"
                  style={field}
                  value={outputKey}
                  onChange={(e) => setOutputKey(e.target.value)}
                />
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
                <input
                  type="checkbox"
                  data-testid="save-as-test-record-path"
                  checked={recordPath}
                  onChange={(e) => setRecordPath(e.target.checked)}
                />
                {t('workflowTests.recordPath')}
              </label>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{t('workflowTests.secretsNote')}</div>
              {error && (
                <div data-testid="save-as-test-error" role="alert" style={{ fontSize: 12, color: 'var(--status-danger)' }}>
                  {error}
                </div>
              )}
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                <Button variant="secondary" onClick={() => setOpen(false)}>
                  {t('common.cancel')}
                </Button>
                <Button
                  data-testid="save-as-test-submit"
                  variant="primary"
                  loading={busy}
                  disabled={!canEdit || idInvalid || nameMissing}
                  onClick={() => void submit()}
                >
                  {t('workflowTests.save')}
                </Button>
              </div>
            </>
          )}
        </div>
      </Modal>
    </>
  );
}
