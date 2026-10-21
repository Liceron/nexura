// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { selectStyle } from '../../components/Input';
import { useEffect, useMemo, useState, useRef } from 'react';
import { useApi } from '../../api/context';
import { Button } from '../../components/Button';
import { RunStatus } from '../runs/RunStatus';
import { JsonText } from '../approvals/ExactAction';
import { Spinner } from '../../components/Spinner';
import { useI18n } from '../../i18n';
import { runHref } from '../../app/routes';
import {
  filterSpans,
  formatJson,
  formatMs,
  orderSpans,
  type RunTrace,
  type SpanStatus,
  type TraceSpan,
} from './types';

const STATUS_COLOR: Record<SpanStatus, string> = {
  ok: 'var(--status-success)',
  error: 'var(--status-danger)',
  running: 'var(--status-info)',
  unknown: 'var(--text-secondary)',
};

function tokensLabel(span: TraceSpan, unknown: string): string {
  if (span.total_tokens === null) return unknown;
  return `${span.input_tokens ?? '?'} in / ${span.output_tokens ?? '?'} out`;
}

function costLabel(span: TraceSpan, unknown: string): string {
  return span.cost_usd === null ? unknown : `$${span.cost_usd}`;
}

export function RunTraceView({
  runId,
  revision = 0,
  showOpenRun = false,
}: {
  runId: string;
  revision?: number;
  /** Link to the run page (`#/runs/{id}`); off inside the Run Console, which is that page. */
  showOpenRun?: boolean;
}) {
  const { manager, connection } = useApi();
  const { t } = useI18n();
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [includeContent, setIncludeContent] = useState(false);
  const [spanFilter, setSpanFilter] = useState<'all' | 'error'>('all');
  const [expanded, setExpanded] = useState<string | null>(null);

  const detailRef = useRef<HTMLElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  const selectSpan = (id: string | null) => {
    setExpanded(id);
    const url = new URL(window.location.href);
    if (id) { url.searchParams.set('traceRun', runId); url.searchParams.set('traceSpan', id); }
    else { url.searchParams.delete('traceRun'); url.searchParams.delete('traceSpan'); }
    window.history.replaceState(window.history.state, '', url);
  };
  useEffect(() => {
    if (expanded) detailRef.current?.focus();
    else opener.current?.focus();
  }, [expanded, trace?.run_id]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setExpanded(params.get('traceRun') === runId ? params.get('traceSpan') : null);
    setIncludeContent(false);
    setTrace(null);
  }, [runId, connection.workspace, connection.baseUrl, connection.principal]);

  useEffect(() => {
    if (connection.status !== 'connected') return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    manager.client
      .request<RunTrace>('GET', `/runs/${encodeURIComponent(runId)}/trace`, {
        params: { include_content: includeContent },
        signal: controller.signal,
      })
      .then((data) => {
        if (!controller.signal.aborted) setTrace(data);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(t('traces.loadError'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [manager, connection.status, connection.workspace, connection.baseUrl, connection.principal, runId, includeContent, revision, t]);

  const rows = useMemo(() => {
    if (!trace) return [];
    return orderSpans(filterSpans(trace.spans, spanFilter));
  }, [trace, spanFilter]);

  const root = trace?.spans.find((s) => s.kind === 'run');
  const total = useMemo(() => {
    if (!trace) return 1;
    const ends = trace.spans.map((s) => (s.start_ms ?? 0) + (s.duration_ms ?? 0));
    return Math.max(trace.duration_ms ?? 0, ...ends, 1);
  }, [trace]);
  const errorCount = trace?.spans.filter((s) => s.status === 'error').length ?? 0;
  const unknown = t('traces.unknown');

  const selected = rows.find(row => row.span.span_id === expanded)?.span;
  return <section aria-label={t('traces.regionLabel')} style={{ display: 'grid', gap: 16, minWidth: 0 }}>
    <div className="inspection-actions">
      <label>{t('traces.spanFilter')} <select style={selectStyle} aria-label={t('traces.spanFilter')} value={spanFilter}
        onChange={e => { setSpanFilter(e.target.value as 'all' | 'error'); selectSpan(null); }}>
        <option value="all">{t('traces.spansAll')}</option><option value="error">{t('traces.spansErrors')}</option>
      </select></label>
      {showOpenRun && <a href={runHref(runId)} data-testid="trace-open-run">{t('runs.open')}</a>}
      <label><input type="checkbox" checked={includeContent} onChange={e => setIncludeContent(e.target.checked)} /> {t('traces.showContent')}</label>
    </div>
    <p className="inspection-meta">{t('traces.contentHint')}</p>
    {loading && !trace && <p role="status"><Spinner size={14} /> {t('common.loading')}</p>}
    {error && <p role="alert">{error}</p>}
    {trace && root && <>
      <div aria-label={t('traces.summaryLabel')} className="inspection-meta">
        <RunStatus status={trace.status} />
        <span><strong>{t('traces.duration')}:</strong> {formatMs(root.duration_ms)}</span>
        <span><strong>{t('traces.tokens')}:</strong> {tokensLabel(root, unknown)}</span>
        <span><strong>{t('traces.cost')}:</strong> {costLabel(root, unknown)}</span>
        <span><strong>{t('traces.spans')}:</strong> {trace.span_count}</span>
        <span><strong>{t('traces.errors')}:</strong> {errorCount}</span>
      </div>
      {trace.truncated && <p role="note">{t('traces.truncated')}</p>}
      {rows.length === 0 && <p>{t('traces.noSpans')}</p>}
      <div className={'trace-panes' + (selected ? ' trace-panes--selected' : '')}>
        <ul aria-label={t('traces.waterfall')} data-testid="trace-waterfall" className="trace-waterfall" style={{ listStyle: 'none', minWidth: 0 }}>
          {rows.map(({ span, depth }) => {
            const left = Math.min(((span.start_ms ?? 0) / total) * 100, 99);
            const width = Math.min(Math.max(((span.duration_ms ?? 0) / total) * 100, .8), 100 - left);
            return <li key={span.span_id} data-testid="trace-span" data-kind={span.kind} data-status={span.status}
              style={{ borderBottom: '1px solid var(--border-subtle)', background: expanded === span.span_id ? 'var(--bg-muted)' : undefined }}>
              <button type="button" className="trace-row" aria-expanded={expanded === span.span_id}
                ref={element => { if (expanded === span.span_id && element) opener.current = element; }}
                aria-controls={expanded === span.span_id ? 'selected-trace-span' : undefined}
                onClick={e => { opener.current = e.currentTarget; selectSpan(expanded === span.span_id ? null : span.span_id); }}>
                <span style={{ paddingLeft: Math.min(depth, 4) * 12, overflowWrap: 'anywhere', minWidth: 0 }}>
                  <span className="inspection-meta">{span.kind} · {t('traces.status')}: {span.status}</span>{span.name}
                </span>
                <span role="img" aria-label={t('traces.startsAt') + ' ' + formatMs(span.start_ms) + ', ' + t('traces.lasts') + ' ' + formatMs(span.duration_ms)}
                  style={{ position: 'relative', height: 8, background: 'var(--bg-muted)' }}>
                  <span style={{ position: 'absolute', left: left + '%', width: width + '%', top: 0, bottom: 0, background: STATUS_COLOR[span.status] }} />
                </span>
                <span className="inspection-meta">{formatMs(span.duration_ms)}{span.total_tokens !== null && ' · ' + span.total_tokens + ' tok'}</span>
              </button>
            </li>;
          })}
        </ul>
        {selected ? <section ref={detailRef} tabIndex={-1} className="trace-detail" id="selected-trace-span" role="region"
          aria-label={t('traces.detailsFor') + ' ' + selected.name}>
          <Button variant="ghost" className="trace-back" onClick={() => selectSpan(null)}>{t('reviewDesign.backEvents')}</Button>
          <h3 style={{ overflowWrap: 'anywhere' }}>{selected.name}</h3>
          <div className="inspection-meta">{t('traces.status')}: {selected.status} · {t('traces.tokens')}: {tokensLabel(selected, unknown)} · {t('traces.cost')}: {costLabel(selected, unknown)} {selected.model_id || ''}</div>
          {selected.error != null && <div role="alert" className="inspection-error"><strong>{t('traces.error')}:</strong> {formatJson(selected.error)}</div>}
          {includeContent ? <>
            <JsonText label={t('traces.input')} text={selected.input == null ? unknown : formatJson(selected.input)} />
            <JsonText label={t('traces.output')} text={selected.output == null ? unknown : formatJson(selected.output)} />
          </> : <p>{t('traces.enableContent')}</p>}
        </section> : <p className="inspection-meta">{t('reviewDesign.selectEvent')}</p>}
      </div>
    </>}
  </section>;
}
