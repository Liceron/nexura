// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useLocale } from '../../i18n';
import { useT } from '../../i18n';
import { useState, useMemo } from 'react';
import type { RunEvent } from '@liceron/nexura-sdk';
import { JsonText } from '../approvals/ExactAction';
import { Button } from '../../components/Button';
import { formatEventTitle } from './events';

interface RunTimelineProps {
  events: RunEvent[];
  activeNodeId?: string | null;
  onSelectNode?: (nodeId: string) => void;
}

export function RunTimeline({ events, onSelectNode }: RunTimelineProps) {
  const t = useT();
  const locale = useLocale();
  const [filterType, setFilterType] = useState<string>('all');
  const [searchTerm, setSearchTerm] = useState<string>('');
  const [expandedSeq, setExpandedSeq] = useState<number | null>(null);

  const filteredEvents = useMemo(() => {
    return events.filter((ev) => {
      // Type filter
      if (filterType === 'nodes') {
        if (!ev.type.startsWith('NODE_')) return false;
      } else if (filterType === 'approvals') {
        if (!ev.type.includes('APPROVAL')) return false;
      } else if (filterType === 'errors') {
        if (!ev.type.includes('FAILED') && !ev.type.includes('CANCELLED')) return false;
      }

      // Search term
      if (searchTerm.trim()) {
        const q = searchTerm.toLowerCase();
        const matchesType = ev.type.toLowerCase().includes(q);
        const matchesNode = ev.node_id?.toLowerCase().includes(q);
        const matchesData = JSON.stringify(ev.data).toLowerCase().includes(q);
        if (!matchesType && !matchesNode && !matchesData) {
          return false;
        }
      }

      return true;
    });
  }, [events, filterType, searchTerm]);

  const toggleExpand = (seq: number) => {
    setExpandedSeq((prev) => (prev === seq ? null : seq));
  };

  const getBadgeVariant = (type: string) => {
    if (type.includes('COMPLETED') || type === 'APPROVAL_GRANTED') return 'success';
    if (type.includes('FAILED') || type === 'APPROVAL_REJECTED') return 'danger';
    if (type.includes('PENDING') || type === 'RUN_INTERRUPTED') return 'warning';
    if (type.includes('STARTED')) return 'info';
    return 'neutral';
  };

  return <div className="run-timeline">
    <div className="inspection-actions">
      {[
        ['all', t('ui.all') + events.length + ')'], ['nodes', t('ui.nodeSteps')],
        ['approvals', t('ui.approvals')], ['errors', t('ui.failuresHalts')],
      ].map(([id, label]) => <Button key={id} variant="ghost" aria-pressed={filterType === id}
        onClick={() => setFilterType(id)}>{label}</Button>)}
      <input placeholder={t('ui.searchTimeline')} value={searchTerm} onChange={e => setSearchTerm(e.target.value)} aria-label={t('ui.searchEventTimeline')} />
    </div>
    {filteredEvents.length === 0 ? <p className="inspection-empty">{events.length === 0 ? t('ui.noEventsRecordedYetRunIsInitializing') : t('ui.noEventsMatchingSearchFilter')}</p> :
      <ol style={{ listStyle: 'none' }}>{filteredEvents.map(ev => {
        const nodeId = ev.node_id || (ev.data && ev.data.node_id as string);
        const isExpanded = expandedSeq === ev.seq;
        const title = t(`eventTitles.${ev.type}`, formatEventTitle(ev.type));
        return <li key={ev.seq} data-testid={`event-item-${ev.seq}`} style={{ borderBottom: '1px solid var(--border-subtle)', padding: '8px 0' }}>
          <div className="inspection-actions">
            <button type="button" aria-expanded={isExpanded} aria-controls={isExpanded ? 'timeline-event-' + ev.seq : undefined}
              onClick={() => toggleExpand(ev.seq)} style={{ flex: 1, textAlign: 'left', padding: '8px 0', minWidth: 0 }}>
              <span className={`run-status run-status--${getBadgeVariant(ev.type)}`}>{title}</span>
              <span className="inspection-meta">#{ev.seq} · {new Date(ev.timestamp * 1000).toLocaleTimeString(locale)}</span>
            </button>
            {nodeId && (onSelectNode ? <Button variant="ghost" onClick={() => onSelectNode(nodeId)}><code>{nodeId}</code></Button> : <code>{nodeId}</code>)}
          </div>
          {ev.data && ev.type === 'NODE_COMPLETED' && ev.data.branch && <p className="inspection-meta">{t('ui.branchedTransition')} {String(ev.data.branch)}</p>}
          {ev.data && ev.type === 'NODE_FAILED' && ev.data.error && <p className="inspection-error">{t('ui.error2')} {String(ev.data.error)}</p>}
          {isExpanded && ev.data && <div id={'timeline-event-' + ev.seq}><JsonText label={title} text={JSON.stringify(ev.data, null, 2)} /></div>}
        </li>;
      })}</ol>}
  </div>;
}
