// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useEffect, useMemo, useState } from 'react';
import { InlineConfirm } from '../../components/InlineConfirm';
import type { CSSProperties } from 'react';
import { Alert } from '../../components/Alert';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { useStudioEvals } from './useStudioEvals';
import {
  buildComparisonRows,
  countChanges,
  filterResults,
  filterRows,
  pct,
} from './evals_compare';
import type { RowChange } from './evals_compare';
import {
  caseOf,
  draftOf,
  emptyDraft,
  exportDatasetJson,
  looksLikeOpenAiEvals,
  parseDatasetImport,
  withoutNulls,
  validateCases,
} from './evals_dataset_json';
import type { CaseDraft } from './evals_dataset_json';
import type { CaseStatus, EvalCase, OpenAiImportReport, StudioRun } from './studioEvalsTypes';
import { sourceRunIdOf } from './workflowTests';
import { runHref } from '../../app/routes';
import { savedAgentIdOfTarget } from '../agents/agentForm';

type Section = 'datasets' | 'run' | 'results' | 'compare' | 'redteam';

const field: CSSProperties = {
  padding: '6px 10px',
  borderRadius: 'var(--radius-md)',
  border: '1px solid var(--border-subtle)',
  background: 'var(--bg-surface)',
  color: 'var(--text-primary)',
  fontSize: 13,
};
const cell: CSSProperties = { padding: '6px 8px', textAlign: 'left', verticalAlign: 'top', fontSize: 12 };
const labelStyle: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, color: 'var(--text-secondary)' };

function statusVariant(s: CaseStatus): 'success' | 'danger' | 'warning' | 'neutral' {
  return s === 'passed' ? 'success' : s === 'failed' ? 'danger' : s === 'error' ? 'danger' : 'warning';
}
function changeVariant(c: RowChange): 'success' | 'danger' | 'warning' | 'neutral' | 'info' {
  return c === 'regression' || c === 'missing' ? 'danger' : c === 'improvement' ? 'success' : c === 'new' ? 'info' : 'neutral';
}
const fmt = (v: number | null) => (v === null ? '-' : v.toFixed(2));
function inputText(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v ?? '');
}

export function StudioEvalsPanel() {
  const { t } = useI18n();
  const api = useStudioEvals();
  const [section, setSection] = useState<Section>('datasets');

  // ---- dataset editor state
  const [datasetId, setDatasetId] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [cases, setCases] = useState<EvalCase[]>([]);
  const [revision, setRevision] = useState<number | undefined>(undefined);
  const [editing, setEditing] = useState<{ index: number; draft: CaseDraft } | null>(null);
  const [importText, setImportText] = useState('');
  const [importOpen, setImportOpen] = useState(false);
  // OpenAI Evals import (P30): field mapping, optional criteria, and the converter's report.
  const [oaiInputField, setOaiInputField] = useState('');
  const [oaiExpectedField, setOaiExpectedField] = useState('');
  const [oaiCriteria, setOaiCriteria] = useState('');
  const [oaiSkipInvalid, setOaiSkipInvalid] = useState(false);
  const [importReport, setImportReport] = useState<OpenAiImportReport | null>(null);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);

  // ---- run state
  const [runDatasetId, setRunDatasetId] = useState('');
  const [targetKey, setTargetKey] = useState('');
  const [outputKey, setOutputKey] = useState('');
  const [repeats, setRepeats] = useState(1);
  const [label, setLabel] = useState('');

  // ---- red-team state
  // "workflow:<id>" or "agent:<agent_id>" (a saved agent, P26).
  const [rtTarget, setRtTarget] = useState('');
  const [rtWriteTools, setRtWriteTools] = useState('');

  // ---- results filters
  const [status, setStatus] = useState<CaseStatus | 'all'>('all');
  const [failingOnly, setFailingOnly] = useState(false);
  const [query, setQuery] = useState('');

  // ---- compare state
  const [baseId, setBaseId] = useState('');
  const [candId, setCandId] = useState('');
  const [tolerance, setTolerance] = useState('0');
  const [minRate, setMinRate] = useState('');
  const [changeFilter, setChangeFilter] = useState<RowChange | 'all' | 'changed'>('all');
  const [rowQuery, setRowQuery] = useState('');

  useEffect(() => {
    const ds = api.dataset;
    if (ds) {
      setDatasetId(ds.dataset_id);
      setName(ds.name);
      setDescription(ds.description);
      setCases(ds.cases);
      setRevision(ds.revision);
      setEditing(null);
      setImportReport(null);
    }
  }, [api.dataset]);

  useEffect(() => {
    if (!runDatasetId && api.datasets.length) setRunDatasetId(api.datasets[0].dataset_id);
    if (!targetKey && api.targets.length) setTargetKey(`${api.targets[0].kind}:${api.targets[0].id}`);
  }, [api.datasets, api.targets, runDatasetId, targetKey]);

  useEffect(() => {
    if (api.runs.length >= 2) {
      setCandId((v) => v || api.runs[0].run_id);
      setBaseId((v) => v || api.runs[1].run_id);
    }
  }, [api.runs]);

  const startNew = () => {
    setDatasetId('');
    setName('');
    setDescription('');
    setCases([]);
    setRevision(undefined);
    setEditing(null);
    setFormErrors([]);
    setNotice(null);
    setImportReport(null);
  };

  const saveCase = () => {
    if (!editing) return;
    const next = [...cases];
    const built = caseOf(editing.draft, editing.index < cases.length ? cases[editing.index] : undefined);
    if (editing.index < cases.length) next[editing.index] = built;
    else next.push(built);
    const errors = validateCases(next, t);
    if (errors.length) {
      setFormErrors(errors);
      return;
    }
    setFormErrors([]);
    setCases(next);
    setEditing(null);
  };

  const saveDataset = async () => {
    setNotice(null);
    const errors = validateCases(cases, t);
    if (!datasetId.trim()) errors.unshift('Dataset id is required.');
    if (errors.length) {
      setFormErrors(errors);
      return;
    }
    setFormErrors([]);
    const saved = await api.saveDataset(datasetId.trim(), { name, description, cases, expected_revision: revision });
    if (saved) {
      const redactions = (saved as { redactions?: number }).redactions ?? 0;
      setNotice(redactions > 0 ? `${t('evalsStudio.saved')} ${t('evalsStudio.redactedNotice')}` : t('evalsStudio.saved'));
    }
  };

  const applyOpenAiImport = async () => {
    let criteria: unknown;
    if (oaiCriteria.trim()) {
      try {
        criteria = JSON.parse(oaiCriteria);
      } catch (e) {
        setFormErrors([`${t('evalsStudio.oaiCriteria')}: ${(e as Error).message}`]);
        return;
      }
    }
    const id = datasetId.trim() || 'openai-evals';
    const { result, errors } = await api.importOpenAi({
      content: importText,
      dataset_id: id,
      ...(oaiInputField.trim() ? { input_field: oaiInputField.trim() } : {}),
      ...(oaiExpectedField.trim() ? { expected_field: oaiExpectedField.trim() } : {}),
      ...(criteria !== undefined ? { testing_criteria: criteria } : {}),
      skip_invalid: oaiSkipInvalid,
    });
    if (!result) {
      setFormErrors(errors);
      return;
    }
    setFormErrors([]);
    setCases(withoutNulls(result.dataset.cases));
    if (!name) setName(result.dataset.name);
    if (!description) setDescription(result.dataset.description);
    if (!datasetId) setDatasetId(id);
    setImportReport(result.report);
    setImportOpen(false);
    setImportText('');
  };

  const applyImport = () => {
    setImportReport(null);
    if (looksLikeOpenAiEvals(importText)) {
      void applyOpenAiImport();
      return;
    }
    const parsed = parseDatasetImport(importText, t);
    if (!parsed.ok || !parsed.dataset) {
      setFormErrors(parsed.errors);
      return;
    }
    setFormErrors([]);
    setCases(parsed.dataset.cases);
    if (parsed.dataset.name) setName(parsed.dataset.name);
    if (parsed.dataset.description) setDescription(parsed.dataset.description);
    if (parsed.dataset.dataset_id && !datasetId) setDatasetId(parsed.dataset.dataset_id);
    setImportOpen(false);
    setImportText('');
  };

  const loadImportFile = (file: File | undefined) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setImportText(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => setFormErrors([`${file.name}: ${reader.error?.message ?? 'cannot read the file'}`]);
    reader.readAsText(file);
  };

  const doExport = () => {
    const text = exportDatasetJson({ dataset_id: datasetId, name, description, cases });
    setImportText(text);
    setImportOpen(true);
  };

  const startRun = async () => {
    const [kind, ...rest] = targetKey.split(':');
    const id = rest.join(':');
    const target: Record<string, unknown> = { kind, id };
    if (kind === 'workflow' && outputKey.trim()) target.output_key = outputKey.trim();
    const created = await api.runDataset({ dataset_id: runDatasetId, target, repeats, label });
    if (created) setSection('results');
  };

  const results = useMemo(
    () => filterResults(api.run?.report?.results ?? [], { status, failingOnly, query }),
    [api.run, status, failingOnly, query],
  );

  const rows = useMemo(
    () =>
      api.comparison
        ? buildComparisonRows(api.baselineRun?.report, api.candidateRun?.report, api.comparison)
        : [],
    [api.comparison, api.baselineRun, api.candidateRun],
  );
  const shownRows = useMemo(() => filterRows(rows, { change: changeFilter, query: rowQuery }), [rows, changeFilter, rowQuery]);
  const counts = useMemo(() => countChanges(rows), [rows]);

  const workflowTargets = api.targets.filter((x) => x.kind === 'workflow');
  const savedAgentTargets = api.targets.filter((x) => x.kind === 'agent' && x.saved && savedAgentIdOfTarget(x.id));
  const rtChoices = [
    ...workflowTargets.map((w) => `workflow:${w.id}`),
    ...savedAgentTargets.map((a) => `agent:${savedAgentIdOfTarget(a.id)}`),
  ];
  const rtSelected = rtChoices.includes(rtTarget) ? rtTarget : (rtChoices[0] ?? '');
  const runRedteamTarget = () => {
    const [kind, ...rest] = rtSelected.split(':');
    const id = rest.join(':');
    const writeTools = rtWriteTools.split(',').map((x) => x.trim()).filter(Boolean);
    void api.runRedteam(kind === 'agent' ? { agent_id: id } : { workflow_id: id }, writeTools);
  };

  const runName = (r: StudioRun) =>
    `${r.label || r.run_id} - ${r.target.id}${r.target.version ? `@${r.target.version}` : ''} - ${pct(r.summary.pass_rate)}`;

  const tabBtn = (id: Section, text: string) => (
    <button
      key={id}
      data-testid={`studio-evals-tab-${id}`}
      role="tab"
      aria-selected={section === id}
      onClick={() => setSection(id)}
      style={{
        background: 'none',
        border: 'none',
        borderBottom: section === id ? '2px solid var(--accent-primary)' : '2px solid transparent',
        color: section === id ? 'var(--accent-primary)' : 'var(--text-secondary)',
        fontWeight: section === id ? 600 : 500,
        fontSize: 13,
        padding: '6px 12px',
        cursor: 'pointer',
      }}
    >
      {text}
    </button>
  );

  return (
    <div data-testid="studio-evals-panel" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <p style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)' }}>{t('evalsStudio.subtitle')}</p>
      {api.error && (
        <Alert variant="danger" title={t('common.error')}>
          <span data-testid="studio-evals-error">{api.error}</span>
        </Alert>
      )}
      {formErrors.length > 0 && (
        <Alert variant="danger" title={t('evalsStudio.validation')}>
          <ul data-testid="studio-evals-form-errors" style={{ margin: 0, paddingLeft: 18 }}>
            {formErrors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </Alert>
      )}
      <div role="tablist" style={{ display: 'flex', gap: 8, borderBottom: '1px solid var(--border-subtle)' }}>
        {tabBtn('datasets', `${t('evalsStudio.tabDatasets')} (${api.datasets.length})`)}
        {tabBtn('run', t('evalsStudio.tabRun'))}
        {tabBtn('results', t('evalsStudio.tabResults'))}
        {tabBtn('compare', t('evalsStudio.tabCompare'))}
        {tabBtn('redteam', t('evalsStudio.tabRedteam'))}
        {api.busy && <Spinner size={14} />}
      </div>

      {section === 'datasets' && (
        <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gap: 16 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Button size="sm" variant="primary" data-testid="studio-evals-new-dataset" onClick={startNew}>
              {t('evalsStudio.newDataset')}
            </Button>
            {api.datasets.length === 0 && <span style={{ fontSize: 12 }}>{t('evalsStudio.noDatasets')}</span>}
            {api.datasets.map((d) => (
              <button
                key={d.dataset_id}
                data-testid={`studio-evals-dataset-${d.dataset_id}`}
                onClick={() => void api.openDataset(d.dataset_id)}
                style={{ ...field, textAlign: 'left', cursor: 'pointer' }}
              >
                <strong>{d.name || d.dataset_id}</strong>
                <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                  {d.dataset_id} - {d.case_count} {t('evalsStudio.cases')} - r{d.revision}
                </div>
              </button>
            ))}
          </div>
          <Card>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }} data-testid="studio-evals-editor">
              <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <label style={labelStyle}>
                  {t('evalsStudio.datasetId')}
                  <input data-testid="studio-evals-dataset-id" style={field} value={datasetId} disabled={revision !== undefined} onChange={(e) => setDatasetId(e.target.value)} />
                </label>
                <label style={labelStyle}>
                  {t('evalsStudio.datasetName')}
                  <input data-testid="studio-evals-dataset-name" style={field} value={name} onChange={(e) => setName(e.target.value)} />
                </label>
              </div>
              <label style={labelStyle}>
                {t('evalsStudio.description')}
                <input style={field} value={description} onChange={(e) => setDescription(e.target.value)} />
              </label>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <strong style={{ fontSize: 13 }}>
                  {t('evalsStudio.cases')} ({cases.length})
                </strong>
                <Button size="sm" data-testid="studio-evals-add-case" onClick={() => setEditing({ index: cases.length, draft: emptyDraft() })}>
                  {t('evalsStudio.addCase')}
                </Button>
                <Button size="sm" data-testid="studio-evals-import" onClick={() => { setImportText(''); setImportOpen(true); }}>
                  {t('evalsStudio.importJson')}
                </Button>
                <Button size="sm" data-testid="studio-evals-export" disabled={cases.length === 0} onClick={doExport}>
                  {t('evalsStudio.exportJson')}
                </Button>
              </div>
              {importOpen && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <textarea
                    data-testid="studio-evals-json"
                    aria-label={t('evalsStudio.json')}
                    style={{ ...field, minHeight: 140, fontFamily: 'monospace' }}
                    value={importText}
                    placeholder={t('evalsStudio.importPlaceholder')}
                    onChange={(e) => setImportText(e.target.value)}
                  />
                  <label style={labelStyle}>
                    {t('evalsStudio.importFile')}
                    <input
                      type="file"
                      data-testid="studio-evals-import-file"
                      accept=".json,.jsonl,application/json"
                      onChange={(e) => loadImportFile(e.target.files?.[0])}
                    />
                  </label>
                  {looksLikeOpenAiEvals(importText) && (
                    <div data-testid="studio-evals-oai-options" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                      <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{t('evalsStudio.oaiDetected')}</span>
                      <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                        <label style={labelStyle}>
                          {t('evalsStudio.oaiInputField')}
                          <input data-testid="studio-evals-oai-input-field" style={field} value={oaiInputField} placeholder={t('evalsStudio.oaiAutoDetect')} onChange={(e) => setOaiInputField(e.target.value)} />
                        </label>
                        <label style={labelStyle}>
                          {t('evalsStudio.oaiExpectedField')}
                          <input data-testid="studio-evals-oai-expected-field" style={field} value={oaiExpectedField} placeholder={t('evalsStudio.oaiAutoDetect')} onChange={(e) => setOaiExpectedField(e.target.value)} />
                        </label>
                      </div>
                      <label style={labelStyle}>
                        {t('evalsStudio.oaiCriteria')}
                        <textarea data-testid="studio-evals-oai-criteria" style={{ ...field, minHeight: 48, fontFamily: 'monospace' }} value={oaiCriteria} onChange={(e) => setOaiCriteria(e.target.value)} />
                      </label>
                      <label style={{ ...labelStyle, flexDirection: 'row', alignItems: 'center' }}>
                        <input type="checkbox" data-testid="studio-evals-oai-skip-invalid" checked={oaiSkipInvalid} onChange={(e) => setOaiSkipInvalid(e.target.checked)} />
                        {t('evalsStudio.oaiSkipInvalid')}
                      </label>
                    </div>
                  )}
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button size="sm" variant="primary" data-testid="studio-evals-apply-import" onClick={applyImport}>
                      {t('evalsStudio.applyImport')}
                    </Button>
                    <Button size="sm" onClick={() => setImportOpen(false)}>
                      {t('common.close')}
                    </Button>
                  </div>
                </div>
              )}
              {importReport && (
                <Alert variant={importReport.unmapped_criteria.length || importReport.skipped_lines.length ? 'warning' : 'info'} title={t('evalsStudio.oaiReportTitle')}>
                  <div data-testid="studio-evals-oai-report" style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
                    <span>
                      {importReport.cases} {t('evalsStudio.cases')} - {t('evalsStudio.oaiInputField')}: {importReport.input_field} - {t('evalsStudio.oaiExpectedField')}:{' '}
                      {importReport.expected_field ?? '-'}
                    </span>
                    {importReport.scorers.length > 0 && (
                      <span data-testid="studio-evals-oai-scorers">
                        {t('evalsStudio.oaiScorers')}: {importReport.scorers.map((sc) => String(sc.type)).join(', ')}
                      </span>
                    )}
                    {importReport.unmapped_criteria.length > 0 && (
                      <div data-testid="studio-evals-oai-unmapped">
                        {t('evalsStudio.oaiUnmapped')}
                        <ul style={{ margin: 0, paddingLeft: 18 }}>
                          {importReport.unmapped_criteria.map((u, i) => (
                            <li key={`${u.name}-${i}`}>
                              {u.name || u.type} ({u.type}): {u.reason}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {importReport.skipped_lines.length > 0 && (
                      <div data-testid="studio-evals-oai-skipped">
                        {t('evalsStudio.oaiSkipped')}
                        <ul style={{ margin: 0, paddingLeft: 18 }}>
                          {importReport.skipped_lines.map((l) => (
                            <li key={l.line}>
                              {t('evalsStudio.oaiLine')} {l.line}: {l.error}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {importReport.warnings.map((w) => (
                      <span key={w}>{w}</span>
                    ))}
                    <span>{t('evalsStudio.oaiNotSaved')}</span>
                  </div>
                </Alert>
              )}
              {editing && (
                <div data-testid="studio-evals-case-form" style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 10, border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)' }}>
                  <label style={labelStyle}>
                    {t('evalsStudio.caseName')}
                    <input data-testid="studio-evals-case-name" style={field} value={editing.draft.name} onChange={(e) => setEditing({ ...editing, draft: { ...editing.draft, name: e.target.value } })} />
                  </label>
                  <label style={labelStyle}>
                    {t('evalsStudio.caseInput')}
                    <textarea data-testid="studio-evals-case-input" style={{ ...field, minHeight: 56 }} value={editing.draft.input} onChange={(e) => setEditing({ ...editing, draft: { ...editing.draft, input: e.target.value } })} />
                  </label>
                  <label style={labelStyle}>
                    {t('evalsStudio.expectedOutput')}
                    <textarea data-testid="studio-evals-case-expected" style={{ ...field, minHeight: 40 }} value={editing.draft.expected_output} onChange={(e) => setEditing({ ...editing, draft: { ...editing.draft, expected_output: e.target.value } })} />
                  </label>
                  <label style={labelStyle}>
                    {t('evalsStudio.expectedTrajectory')}
                    <textarea data-testid="studio-evals-case-trajectory" style={{ ...field, minHeight: 40 }} placeholder={t('evalsStudio.trajectoryHint')} value={editing.draft.trajectory} onChange={(e) => setEditing({ ...editing, draft: { ...editing.draft, trajectory: e.target.value } })} />
                  </label>
                  <label style={labelStyle}>
                    {t('evalsStudio.tags')}
                    <input style={field} value={editing.draft.tags} onChange={(e) => setEditing({ ...editing, draft: { ...editing.draft, tags: e.target.value } })} />
                  </label>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button size="sm" variant="primary" data-testid="studio-evals-save-case" onClick={saveCase}>
                      {t('evalsStudio.saveCase')}
                    </Button>
                    <Button size="sm" onClick={() => setEditing(null)}>
                      {t('common.cancel')}
                    </Button>
                  </div>
                </div>
              )}
              <table data-testid="studio-evals-cases" style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th style={cell}>{t('evalsStudio.colCase')}</th>
                    <th style={cell}>{t('evalsStudio.caseInput')}</th>
                    <th style={cell}>{t('evalsStudio.expectedOutput')}</th>
                    <th style={cell}>{t('evalsStudio.expectedTrajectory')}</th>
                    <th style={cell} />
                  </tr>
                </thead>
                <tbody>
                  {cases.map((c, i) => (
                    <tr key={c.name} data-testid={`studio-evals-case-${c.name}`} style={{ borderTop: '1px solid var(--border-subtle)' }}>
                      <td style={cell}>{c.name}</td>
                      <td style={cell}>{inputText(c.input)}</td>
                      <td style={cell}>{c.expected_output === undefined ? '-' : inputText(c.expected_output)}</td>
                      <td style={cell}>{(c.expected_trajectory ?? []).map((s) => (typeof s === 'string' ? s : s.name)).join(' > ') || '-'}</td>
                      <td style={cell}>
                        <Button size="sm" data-testid={`studio-evals-edit-${c.name}`} onClick={() => setEditing({ index: i, draft: draftOf(c) })}>
                          {t('evalsStudio.editCase')}
                        </Button>{' '}
                        <Button size="sm" variant="danger" data-testid={`studio-evals-delete-${c.name}`} onClick={() => setCases(cases.filter((_, j) => j !== i))}>
                          {t('common.delete')}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Button variant="primary" data-testid="studio-evals-save-dataset" onClick={() => void saveDataset()}>
                  {t('evalsStudio.saveDataset')}
                </Button>
                {revision !== undefined && (
                  <InlineConfirm key={datasetId} consequence={t('confirmation.deleteDataset')} >
                    <Button variant="danger" data-testid="studio-evals-delete-dataset" onClick={() => api.deleteDataset(datasetId).then(startNew)}>
                      {t('evalsStudio.deleteDataset')}
                    </Button>
                  </InlineConfirm>
                )}
                {notice && <span data-testid="studio-evals-notice" style={{ fontSize: 12 }}>{notice}</span>}
              </div>
              <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{t('evalsStudio.secretsNote')}</span>
            </div>
          </Card>
        </div>
      )}

      {section === 'run' && (
        <Card>
          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <label style={labelStyle}>
              {t('evalsStudio.tabDatasets')}
              <select data-testid="studio-evals-run-dataset" style={field} value={runDatasetId} onChange={(e) => setRunDatasetId(e.target.value)}>
                {api.datasets.map((d) => (
                  <option key={d.dataset_id} value={d.dataset_id}>
                    {d.name || d.dataset_id} (r{d.revision})
                  </option>
                ))}
              </select>
            </label>
            <label style={labelStyle}>
              {t('evalsStudio.target')}
              <select data-testid="studio-evals-run-target" style={field} value={targetKey} onChange={(e) => setTargetKey(e.target.value)}>
                {api.targets.map((x) => (
                  <option key={`${x.kind}:${x.id}`} value={`${x.kind}:${x.id}`}>
                    {x.kind === 'workflow'
                      ? t('evalsStudio.targetWorkflow')
                      : x.saved
                        ? t('evalsStudio.targetSavedAgent')
                        : t('evalsStudio.targetAgent')}
                    : {x.name || x.id}
                    {x.version ? ` (${x.version})` : ''}
                  </option>
                ))}
              </select>
            </label>
            <label style={labelStyle}>
              {t('evalsStudio.outputKey')}
              <input data-testid="studio-evals-run-output-key" style={field} value={outputKey} onChange={(e) => setOutputKey(e.target.value)} />
            </label>
            <label style={labelStyle}>
              {t('evalsStudio.repeats')}
              <input type="number" min={1} max={5} style={field} value={repeats} onChange={(e) => setRepeats(Math.max(1, Math.min(5, Number(e.target.value) || 1)))} />
            </label>
            <label style={labelStyle}>
              {t('evalsStudio.runLabel')}
              <input data-testid="studio-evals-run-label" style={field} value={label} onChange={(e) => setLabel(e.target.value)} />
            </label>
          </div>
          <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{t('evalsStudio.syntheticNote')}</p>
          <Button variant="primary" data-testid="studio-evals-run" disabled={api.busy || !runDatasetId || !targetKey} onClick={() => void startRun()}>
            {api.busy ? t('evalsStudio.running') : t('evalsStudio.runEval')}
          </Button>
        </Card>
      )}

      {section === 'results' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <label style={labelStyle}>
            {t('evalsStudio.tabResults')}
            <select
              data-testid="studio-evals-run-picker"
              style={field}
              value={api.run?.run_id ?? ''}
              onChange={(e) => e.target.value && void api.openRun(e.target.value)}
            >
              <option value="">{t('evalsStudio.selectRun')}</option>
              {api.runs.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {runName(r)}
                </option>
              ))}
            </select>
          </label>
          {!api.run?.report && <span style={{ fontSize: 13 }}>{t('evalsStudio.noRuns')}</span>}
          {api.run?.report && (
            <>
              <div style={{ display: 'flex', gap: 16, fontSize: 13 }} data-testid="studio-evals-summary">
                <span>
                  {t('evalsStudio.passRate')}: <strong data-testid="studio-evals-pass-rate">{pct(api.run.summary.pass_rate)}</strong>
                </span>
                <span>
                  {t('evalsStudio.meanScore')}: <strong>{api.run.summary.mean_score.toFixed(2)}</strong>
                </span>
                <span>
                  {api.run.target.id}
                  {api.run.target.version ? `@${api.run.target.version}` : ''} - r{api.run.dataset_revision}
                  {api.run.target.synthetic ? ` - ${t('evalsStudio.synthetic')}` : ''}
                </span>
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <select data-testid="studio-evals-status-filter" aria-label={t('evalsStudio.filterStatus')} style={field} value={status} onChange={(e) => setStatus(e.target.value as CaseStatus | 'all')}>
                  <option value="all">{t('evalsStudio.filterAll')}</option>
                  <option value="passed">{t('evalsStudio.passed')}</option>
                  <option value="failed">{t('evalsStudio.failed')}</option>
                  <option value="error">{t('evalsStudio.error')}</option>
                  <option value="paused">{t('evalsStudio.paused')}</option>
                </select>
                <label style={{ fontSize: 12 }}>
                  <input type="checkbox" data-testid="studio-evals-failing-only" checked={failingOnly} onChange={(e) => setFailingOnly(e.target.checked)} /> {t('evalsStudio.failingOnly')}
                </label>
                <input data-testid="studio-evals-search" aria-label={t('evalsStudio.search')} placeholder={t('evalsStudio.search')} style={field} value={query} onChange={(e) => setQuery(e.target.value)} />
              </div>
              <table data-testid="studio-evals-results" style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th style={cell}>{t('evalsStudio.colCase')}</th>
                    <th style={cell}>{t('evalsStudio.colStatus')}</th>
                    <th style={cell}>{t('evalsStudio.colScores')}</th>
                    <th style={cell}>{t('evalsStudio.colOutput')}</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((r) => (
                    <tr key={`${r.name}-${r.repeat}`} data-testid={`studio-evals-result-${r.name}`} style={{ borderTop: '1px solid var(--border-subtle)' }}>
                      <td style={cell}>
                        {r.name}
                        <div style={{ color: 'var(--text-secondary)' }}>{inputText(r.metadata?.input)}</div>
                        {sourceRunIdOf(r.metadata) && (
                          <a href={runHref(sourceRunIdOf(r.metadata) as string)} data-testid="studio-evals-source-run">
                            {t('runs.openSourceRun')}
                          </a>
                        )}
                      </td>
                      <td style={cell}>
                        <Badge variant={statusVariant(r.status)}>{t(`evalsStudio.${r.status}`)}</Badge>
                      </td>
                      <td style={cell}>
                        {r.scores.map((s) => (
                          <div key={s.name}>
                            <Badge variant={s.passed ? 'success' : 'danger'}>{s.name} {s.value.toFixed(2)}</Badge>
                            {s.reason && <span style={{ marginLeft: 6 }}>{s.reason}</span>}
                          </div>
                        ))}
                        {r.error && <div style={{ color: 'var(--status-danger)' }}>{r.error.type}: {r.error.message}</div>}
                      </td>
                      <td style={cell}>{r.output_text}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}

      {section === 'compare' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 110px 130px auto', gap: 10, alignItems: 'end' }}>
            <label style={labelStyle}>
              {t('evalsStudio.baseline')}
              <select data-testid="studio-evals-base" style={field} value={baseId} onChange={(e) => setBaseId(e.target.value)}>
                <option value="">-</option>
                {api.runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>{runName(r)}</option>
                ))}
              </select>
            </label>
            <label style={labelStyle}>
              {t('evalsStudio.candidate')}
              <select data-testid="studio-evals-candidate" style={field} value={candId} onChange={(e) => setCandId(e.target.value)}>
                <option value="">-</option>
                {api.runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>{runName(r)}</option>
                ))}
              </select>
            </label>
            <label style={labelStyle}>
              {t('evalsStudio.tolerance')}
              <input data-testid="studio-evals-tolerance" style={field} value={tolerance} onChange={(e) => setTolerance(e.target.value)} />
            </label>
            <label style={labelStyle}>
              {t('evalsStudio.minPassRate')}
              <input data-testid="studio-evals-min-rate" style={field} placeholder="0.9" value={minRate} onChange={(e) => setMinRate(e.target.value)} />
            </label>
            <Button
              variant="primary"
              data-testid="studio-evals-compare"
              disabled={!baseId || !candId || api.busy}
              onClick={() => {
                const tol = Number(tolerance);
                const min = minRate.trim() === '' ? null : Number(minRate);
                if (!Number.isFinite(tol) || tol < 0 || tol > 1 || (min !== null && (!Number.isFinite(min) || min < 0 || min > 1))) {
                  setFormErrors([t('evalsStudio.invalidThreshold')]);
                  return;
                }
                setFormErrors([]);
                void api.compareRuns(baseId, candId, tol, min);
              }}
            >
              {t('evalsStudio.compare')}
            </Button>
          </div>
          {!api.comparison && <span style={{ fontSize: 13 }}>{t('evalsStudio.noComparison')}</span>}
          {api.comparison && (
            <>
              <div
                data-testid="studio-evals-gate"
                data-passed={api.comparison.gate.passed ? 'true' : 'false'}
                style={{ padding: 10, borderRadius: 'var(--radius-md)', border: '1px solid var(--border-subtle)', display: 'flex', gap: 12, alignItems: 'center' }}
              >
                <Badge variant={api.comparison.gate.passed ? 'success' : 'danger'}>
                  {api.comparison.gate.passed ? t('evalsStudio.gatePass') : t('evalsStudio.gateFail')}
                </Badge>
                <span style={{ fontSize: 12 }}>{api.comparison.gate.reasons.join('; ') || t('evalsStudio.gateClean')}</span>
                <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                  {t('evalsStudio.tolerance')} {api.comparison.gate.tolerance}
                </span>
              </div>
              <div style={{ display: 'flex', gap: 14, fontSize: 12 }} data-testid="studio-evals-counts">
                <span>{t('evalsStudio.passRate')}: {pct(api.comparison.baseline_summary.pass_rate)} {'->'} {pct(api.comparison.candidate_summary.pass_rate)}</span>
                <span data-testid="studio-evals-count-regression">{t('evalsStudio.regressions')}: {counts.regression}</span>
                <span>{t('evalsStudio.improvements')}: {counts.improvement}</span>
                <span>{t('evalsStudio.unchanged')}: {counts.unchanged}</span>
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <select data-testid="studio-evals-change-filter" aria-label={t('evalsStudio.filterChange')} style={field} value={changeFilter} onChange={(e) => setChangeFilter(e.target.value as RowChange | 'all' | 'changed')}>
                  <option value="all">{t('evalsStudio.filterAll')}</option>
                  <option value="changed">{t('evalsStudio.filterChanged')}</option>
                  <option value="regression">{t('evalsStudio.regressions')}</option>
                  <option value="improvement">{t('evalsStudio.improvements')}</option>
                  <option value="unchanged">{t('evalsStudio.unchanged')}</option>
                </select>
                <input aria-label={t('evalsStudio.search')} placeholder={t('evalsStudio.search')} style={field} value={rowQuery} onChange={(e) => setRowQuery(e.target.value)} />
              </div>
              <table data-testid="studio-evals-compare-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th style={cell}>{t('evalsStudio.colCase')}</th>
                    <th style={cell}>{t('evalsStudio.baseline')}</th>
                    <th style={cell}>{t('evalsStudio.candidate')}</th>
                    <th style={cell}>{t('evalsStudio.colScores')}</th>
                  </tr>
                </thead>
                <tbody>
                  {shownRows.map((row) => (
                    <tr
                      key={row.name}
                      data-testid={`studio-evals-row-${row.name}`}
                      data-change={row.change}
                      style={{
                        borderTop: '1px solid var(--border-subtle)',
                        background: row.change === 'regression' ? 'var(--status-danger-bg)' : row.change === 'improvement' ? 'var(--status-success-bg)' : undefined,
                      }}
                    >
                      <td style={cell}>
                        {row.name} <Badge variant={changeVariant(row.change)}>{t(`evalsStudio.change_${row.change}`)}</Badge>
                      </td>
                      <td style={cell}>
                        {row.baseline ? <Badge variant={statusVariant(row.baseline.status)}>{t(`evalsStudio.${row.baseline.status}`)}</Badge> : '-'}
                        <div>{row.baseline?.output_text}</div>
                      </td>
                      <td style={cell}>
                        {row.candidate ? <Badge variant={statusVariant(row.candidate.status)}>{t(`evalsStudio.${row.candidate.status}`)}</Badge> : '-'}
                        <div>{row.candidate?.output_text}</div>
                      </td>
                      <td style={cell}>
                        {row.scorers.map((s) => (
                          <div key={s.scorer} data-change={s.change} style={{ color: s.change === 'regression' ? 'var(--status-danger)' : s.change === 'improvement' ? 'var(--status-success)' : undefined }}>
                            {s.scorer}: {fmt(s.baseline)} {'->'} {fmt(s.candidate)}
                          </div>
                        ))}
                        {row.notes.map((n) => (
                          <div key={n} style={{ color: 'var(--text-secondary)' }}>{n}</div>
                        ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
      {section === 'redteam' && (
        <Card>
          <div data-testid="studio-evals-redteam" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <p style={{ margin: 0, fontSize: 12, color: 'var(--text-secondary)' }}>{t('evalsStudio.redteamIntro')}</p>
            {rtChoices.length === 0 ? (
              <span data-testid="studio-evals-redteam-empty">{t('evalsStudio.redteamNoTargets')}</span>
            ) : (
              <div className="studio-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                <label style={labelStyle}>
                  {t('evalsStudio.redteamTarget')}
                  <select data-testid="studio-evals-redteam-target" style={field} value={rtSelected} onChange={(e) => setRtTarget(e.target.value)}>
                    {workflowTargets.length > 0 && (
                      <optgroup label={t('evalsStudio.redteamWorkflow')}>
                        {workflowTargets.map((w) => (
                          <option key={w.id} value={`workflow:${w.id}`}>
                            {w.name || w.id}
                          </option>
                        ))}
                      </optgroup>
                    )}
                    {savedAgentTargets.length > 0 && (
                      <optgroup label={t('evalsStudio.targetSavedAgent')}>
                        {savedAgentTargets.map((a) => (
                          <option key={a.id} value={`agent:${savedAgentIdOfTarget(a.id)}`}>
                            {a.name || a.id}
                            {a.version ? ` (${a.version})` : ''}
                          </option>
                        ))}
                      </optgroup>
                    )}
                  </select>
                </label>
                <label style={labelStyle}>
                  {t('evalsStudio.redteamWriteTools')}
                  <input data-testid="studio-evals-redteam-tools" style={field} value={rtWriteTools} onChange={(e) => setRtWriteTools(e.target.value)} />
                </label>
              </div>
            )}
            {rtSelected.startsWith('agent:') && (
              <span data-testid="studio-evals-redteam-agent-note" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                {t('evalsStudio.redteamAgentNote')}
              </span>
            )}
            <div>
              <Button
                variant="primary"
                data-testid="studio-evals-redteam-run"
                disabled={api.busy || rtChoices.length === 0}
                onClick={runRedteamTarget}
              >
                {api.busy ? t('evalsStudio.running') : t('evalsStudio.redteamRun')}
              </Button>
            </div>
            {api.redteam && (
              <>
                <Alert variant={api.redteam.ok ? 'success' : 'danger'} title={api.redteam.ok ? t('evalsStudio.redteamOk') : t('evalsStudio.redteamFlagged')}>
                  <span data-testid="studio-evals-redteam-verdict" data-ok={String(api.redteam.ok)}>
                    {t('evalsStudio.redteamSummary')}: {api.redteam.summary.total - api.redteam.cases.filter((c) => !c.safe).length}/{api.redteam.summary.total}
                  </span>
                </Alert>
                <div style={{ fontSize: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <span data-testid="studio-evals-redteam-model">
                    {t('evalsStudio.redteamModel')}: {api.redteam.model.name} ({api.redteam.model.offline ? t('evalsStudio.redteamOffline') : t('evalsStudio.redteamLive')})
                  </span>
                  <span data-testid="studio-evals-redteam-target-version">
                    {t('evalsStudio.redteamTarget')}:{' '}
                    {api.redteam.agent_id
                      ? `${t('evalsStudio.targetSavedAgent')} ${api.redteam.agent_id} (${api.redteam.agent_version ?? ''})`
                      : `${t('evalsStudio.targetWorkflow')} ${api.redteam.workflow_id ?? ''} (${api.redteam.workflow_version ?? ''})`}
                  </span>
                  <span data-testid="studio-evals-redteam-scorers">
                    {t('evalsStudio.redteamScorers')}: {api.redteam.scorers.join(', ')}
                  </span>
                  {api.redteam.synthetic_write_tool && <span>{t('evalsStudio.redteamSyntheticTool')}</span>}
                </div>
                <table data-testid="studio-evals-redteam-results" style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr>
                      <th style={cell}>{t('evalsStudio.colCase')}</th>
                      <th style={cell}>{t('evalsStudio.redteamAttack')}</th>
                      <th style={cell}>{t('evalsStudio.colStatus')}</th>
                      <th style={cell}>{t('evalsStudio.redteamReason')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {api.redteam.cases.map((c) => (
                      <tr key={c.name} data-testid={`studio-evals-redteam-case-${c.name}`} data-safe={String(c.safe)} style={{ borderTop: '1px solid var(--border-subtle)' }}>
                        <td style={cell}>{c.name}</td>
                        <td style={cell}>{c.attack ?? '-'}</td>
                        <td style={cell}>
                          <Badge variant={c.safe ? 'success' : 'danger'}>{c.safe ? t('evalsStudio.redteamSafe') : t('evalsStudio.redteamUnsafe')}</Badge>
                        </td>
                        <td style={cell}>{c.reason || (c.status === 'paused' ? t('evalsStudio.paused') : '')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
