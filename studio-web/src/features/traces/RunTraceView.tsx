// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { selectStyle } from '../../components/Input';
import { useEffect, useMemo, useState } from 'react';
import { useApi } from '../../api/context';
import { Badge } from '../../components/Badge';
import type { BadgeVariant } from '../../components/Badge';
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
const STATUS_BADGE: Record<SpanStatus, BadgeVariant> = {
  ok: 'success',
  error: 'danger',
  running: 'info',
  unknown: 'neutral',
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

  useEffect(() => {
    setExpanded(null);
    setIncludeContent(false);
  }, [runId]);

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
  }, [manager, connection.status, runId, includeContent, revision, t]);

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

  return (
    <section aria-label={t('traces.regionLabel')} style={{ display: 'grid', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 14, alignItems: 'center', fontSize: 13 }}>
        <label>
          {t('traces.spanFilter')}{' '}
          <select
            style={selectStyle}
            aria-label={t('traces.spanFilter')}
            value={spanFilter}
            onChange={(e) => setSpanFilter(e.target.value as 'all' | 'error')}
          >
            <option value="all">{t('traces.spansAll')}</option>
            <option value="error">{t('traces.spansErrors')}</option>
          </select>
        </label>
        {showOpenRun && (
          <a href={runHref(runId)} data-testid="trace-open-run">
            {t('runs.open')}
          </a>
        )}
        <label>
          <input
            type="checkbox"
            checked={includeContent}
            onChange={(e) => setIncludeContent(e.target.checked)}
          />{' '}
          {t('traces.showContent')}
        </label>
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: 0 }}>{t('traces.contentHint')}</p>
      {loading && !trace && (
        <p role="status">
          <Spinner size={14} /> {t('common.loading')}
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      {trace && root && (
        <>
          <div
            aria-label={t('traces.summaryLabel')}
            style={{ display: 'flex', flexWrap: 'wrap', gap: 16, fontSize: 13 }}
          >
            <span>
              <strong>{t('traces.duration')}:</strong> {formatMs(root.duration_ms)}
            </span>
            <span>
              <strong>{t('traces.tokens')}:</strong> {tokensLabel(root, unknown)}
            </span>
            <span>
              <strong>{t('traces.cost')}:</strong> {costLabel(root, unknown)}
            </span>
            <span>
              <strong>{t('traces.spans')}:</strong> {trace.span_count}
            </span>
            <span>
              <strong>{t('traces.errors')}:</strong> {errorCount}
            </span>
          </div>
          {trace.truncated && <p role="note">{t('traces.truncated')}</p>}
          {rows.length === 0 && <p>{t('traces.noSpans')}</p>}
          <ul
            aria-label={t('traces.waterfall')}
            data-testid="trace-waterfall"
            style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 4 }}
          >
            {rows.map(({ span, depth }) => {
              const isOpen = expanded === span.span_id;
              const left = ((span.start_ms ?? 0) / total) * 100;
              const width = Math.max(((span.duration_ms ?? 0) / total) * 100, 0.8);
              const panelId = `span-detail-${span.span_id}`;
              return (
                <li
                  key={span.span_id}
                  data-testid="trace-span"
                  data-kind={span.kind}
                  data-status={span.status}
                  style={{
                    border: `1px solid ${span.status === 'error' ? 'var(--status-danger)' : 'var(--border-subtle)'}`,
                    borderRadius: 'var(--radius-sm)',
                    backgroundColor: span.status === 'error' ? 'var(--status-danger-bg)' : 'transparent',
                  }}
                >
                  <button
                    type="button"
                    aria-expanded={isOpen}
                    aria-controls={panelId}
                    onClick={() => setExpanded(isOpen ? null : span.span_id)}
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'minmax(180px, 34%) 1fr auto',
                      gap: 10,
                      alignItems: 'center',
                      width: '100%',
                      padding: '6px 8px',
                      textAlign: 'left',
                      cursor: 'pointer',
                      background: 'transparent',
                      border: 0,
                      color: 'inherit',
                      fontSize: 12,
                    }}
                  >
                    <span style={{ paddingLeft: depth * 16, display: 'flex', gap: 6, alignItems: 'center' }}>
                      <Badge variant={STATUS_BADGE[span.status]}>{span.kind}</Badge>
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {span.name}
                      </span>
                      <span className="sr-only" style={{ position: 'absolute', left: -9999 }}>
                        {' '}
                        {t('traces.status')}: {span.status}
                      </span>
                    </span>
                    <span
                      role="img"
                      aria-label={
                        t('traces.startsAt') + ' ' + formatMs(span.start_ms) + ', ' + t('traces.lasts') + ' ' + formatMs(span.duration_ms)
                      }
                      style={{
                        position: 'relative',
                        height: 10,
                        backgroundColor: 'var(--bg-muted)',
                        borderRadius: 4,
                      }}
                    >
                      <span
                        style={{
                          position: 'absolute',
                          left: `${Math.min(left, 99)}%`,
                          width: `${Math.min(width, 100 - Math.min(left, 99))}%`,
                          top: 0,
                          bottom: 0,
                          borderRadius: 4,
                          backgroundColor: STATUS_COLOR[span.status],
                        }}
                      />
                    </span>
                    <span style={{ whiteSpace: 'nowrap', color: 'var(--text-secondary)' }}>
                      {formatMs(span.duration_ms)}
                      {span.total_tokens !== null && ` · ${span.total_tokens} tok`}
                    </span>
                  </button>
                  {isOpen && (
                    <div
                      id={panelId}
                      role="region"
                      aria-label={t('traces.detailsFor') + ' ' + span.name}
                      style={{ padding: '4px 10px 10px', display: 'grid', gap: 6, fontSize: 12 }}
                    >
                      <div style={{ color: 'var(--text-secondary)' }}>
                        {t('traces.status')}: {span.status} · {t('traces.tokens')}: {tokensLabel(span, unknown)} ·{' '}
                        {t('traces.cost')}: {costLabel(span, unknown)}
                        {span.model_id ? ` · ${span.model_id}` : ''}
                      </div>
                      {span.error != null && (
                        <div role="alert">
                          <strong>{t('traces.error')}:</strong> {formatJson(span.error)}
                        </div>
                      )}
                      {includeContent ? (
                        <>
                          <h4 style={{ margin: 0 }}>{t('traces.input')}</h4>
                          <pre style={preStyle}>{span.input == null ? unknown : formatJson(span.input)}</pre>
                          <h4 style={{ margin: 0 }}>{t('traces.output')}</h4>
                          <pre style={preStyle}>{span.output == null ? unknown : formatJson(span.output)}</pre>
                        </>
                      ) : (
                        <p style={{ margin: 0 }}>{t('traces.enableContent')}</p>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}

const preStyle = {
  margin: 0,
  padding: 10,
  borderRadius: 'var(--radius-sm)',
  backgroundColor: 'var(--bg-canvas)',
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  overflow: 'auto',
  maxHeight: 220,
} as const;
