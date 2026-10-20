// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useLocale } from '../../i18n';
import { useT } from '../../i18n';
import { useState, useMemo } from 'react';
import type { RunEvent } from '@liceron/nexura-sdk';
import { Badge } from '../../components/Badge';
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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Controls & Filter Bar */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
        <div style={{ display: 'flex', gap: 6 }}>
          <button
            type="button"
            onClick={() => setFilterType('all')}
            style={{
              padding: '4px 10px',
              borderRadius: 'var(--radius-sm)',
              fontSize: 12,
              fontWeight: 500,
              backgroundColor: filterType === 'all' ? 'var(--button-primary)' : 'var(--bg-canvas)',
              color: filterType === 'all' ? '#fff' : 'var(--text-secondary)',
              border: '1px solid var(--border-subtle)',
              cursor: 'pointer',
            }}
          >
            {t('ui.all')}{events.length})
          </button>
          <button
            type="button"
            onClick={() => setFilterType('nodes')}
            style={{
              padding: '4px 10px',
              borderRadius: 'var(--radius-sm)',
              fontSize: 12,
              fontWeight: 500,
              backgroundColor: filterType === 'nodes' ? 'var(--button-primary)' : 'var(--bg-canvas)',
              color: filterType === 'nodes' ? '#fff' : 'var(--text-secondary)',
              border: '1px solid var(--border-subtle)',
              cursor: 'pointer',
            }}
          >
            {t('ui.nodeSteps')}
          </button>
          <button
            type="button"
            onClick={() => setFilterType('approvals')}
            style={{
              padding: '4px 10px',
              borderRadius: 'var(--radius-sm)',
              fontSize: 12,
              fontWeight: 500,
              backgroundColor: filterType === 'approvals' ? 'var(--button-primary)' : 'var(--bg-canvas)',
              color: filterType === 'approvals' ? '#fff' : 'var(--text-secondary)',
              border: '1px solid var(--border-subtle)',
              cursor: 'pointer',
            }}
          >
            {t('ui.approvals')}
          </button>
          <button
            type="button"
            onClick={() => setFilterType('errors')}
            style={{
              padding: '4px 10px',
              borderRadius: 'var(--radius-sm)',
              fontSize: 12,
              fontWeight: 500,
              backgroundColor: filterType === 'errors' ? 'var(--button-primary)' : 'var(--bg-canvas)',
              color: filterType === 'errors' ? '#fff' : 'var(--text-secondary)',
              border: '1px solid var(--border-subtle)',
              cursor: 'pointer',
            }}
          >
            {t('ui.failuresHalts')}
          </button>
        </div>

        <input
          type="text"
          placeholder={t('ui.searchTimeline')}
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          aria-label={t('ui.searchEventTimeline')}
          style={{
            fontSize: 12,
            padding: '5px 10px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-primary)',
            width: 200,
          }}
        />
      </div>

      {/* Events List */}
      {filteredEvents.length === 0 ? (
        <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-secondary)', fontSize: 13 }}>
          {events.length === 0 ? t('ui.noEventsRecordedYetRunIsInitializing') : t('ui.noEventsMatchingSearchFilter')}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 420, overflowY: 'auto' }}>
          {filteredEvents.map((ev) => {
            const isExpanded = expandedSeq === ev.seq;
            const timeStr = new Date(ev.timestamp * 1000).toLocaleTimeString(locale);
            const nodeId = ev.node_id || (ev.data && (ev.data.node_id as string));

            return (
              <div
                key={ev.seq}
                data-testid={`event-item-${ev.seq}`}
                style={{
                  padding: '8px 12px',
                  borderRadius: 'var(--radius-sm)',
                  backgroundColor: 'var(--bg-surface)',
                  border: '1px solid var(--border-subtle)',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 6,
                  cursor: 'pointer',
                }}
                onClick={() => toggleExpand(ev.seq)}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <code style={{ fontSize: 11, color: 'var(--text-muted)' }}>#{ev.seq}</code>
                    <Badge variant={getBadgeVariant(ev.type)}>{t(`eventTitles.${ev.type}`, formatEventTitle(ev.type))}</Badge>
                    {nodeId && (
                      <span
                        style={{
                          fontSize: 12,
                          fontWeight: 600,
                          color: 'var(--brand-primary)',
                        }}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (onSelectNode) onSelectNode(nodeId);
                        }}
                      >
                        <code>{nodeId}</code>
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{timeStr}</div>
                </div>

                {/* Brief summary line */}
                {ev.data && ev.type === 'NODE_COMPLETED' && ev.data.branch && (
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                    {t('ui.branchedTransition') + " "}<code>{String(ev.data.branch)}</code>
                  </div>
                )}
                {ev.data && ev.type === 'NODE_FAILED' && ev.data.error && (
                  <div style={{ fontSize: 11, color: 'var(--status-danger)' }}>
                    {t('ui.error2') + " "}{String(ev.data.error)}
                  </div>
                )}

                {/* Expanded data payload view */}
                {isExpanded && ev.data && (
                  <pre
                    style={{
                      margin: '6px 0 0 0',
                      padding: 8,
                      backgroundColor: 'var(--bg-canvas)',
                      borderRadius: 'var(--radius-sm)',
                      fontSize: 11,
                      fontFamily: 'var(--font-mono)',
                      overflowX: 'auto',
                    }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    {JSON.stringify(ev.data, null, 2)}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
