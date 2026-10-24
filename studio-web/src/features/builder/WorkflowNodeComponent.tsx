// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';
import type { NodeProps } from '@xyflow/react';
import type { BuilderNode } from './types';
import { Badge } from '../../components/Badge';
import { DEFAULT_MAP_AS, DEFAULT_MAX_CONCURRENCY, stepKind, useBuilderT } from './composite';

export const WorkflowNodeComponent = memo(({ id, data, selected }: NodeProps<BuilderNode>) => {
  const t = useBuilderT();
  const kind = stepKind(data);
  const subName = ((data.metadata?.subworkflow as { name?: string } | undefined)?.name) || data.subgraph?.name;
  const compositeSummary =
    kind === 'parallel'
      ? t('builder.parallelSummary', { count: (data.parallel || []).length })
      : kind === 'subworkflow'
        ? subName
          ? t('builder.subworkflowSummary', { name: subName })
          : t('builder.subworkflowUnlinked')
        : kind === 'map'
          ? t('builder.mapSummary', {
              key: data.map_over || '?',
              as: data.map_as ?? DEFAULT_MAP_AS,
              max: data.max_concurrency ?? DEFAULT_MAX_CONCURRENCY,
            })
          : '';
  const kindLabel =
    kind === 'parallel'
      ? t('builder.kindParallel')
      : kind === 'map'
        ? t('builder.kindMap')
        : kind === 'subworkflow'
          ? t('builder.kindSubworkflow')
          : '';
  const isEntry = Boolean(data.isEntry);
  const requiresApproval = Boolean(data.requires_approval);
  const hasError = Boolean(data.validationError);
  const execStatus = data.executionStatus;
  const isActiveReplay = Boolean(data.isActiveReplayNode);

  return (
    <div
      data-testid={`node-${id}`}
      tabIndex={0}
      role="group"
      aria-label={t('builder.nodeLabel', { id: data.nodeId || id }) + (kindLabel ? ` (${kindLabel})` : '')}
      data-step-kind={kind}
      style={{
        padding: 12,
        width: 224,
        minHeight: 80,
        borderRadius: 'var(--radius-md)',
        backgroundColor: 'var(--bg-surface)',
        border: '1px solid var(--border-strong)',
        outline: selected ? '2px solid var(--accent-primary)' : undefined,
        outlineOffset: 1,
        boxShadow: 'none',
        color: 'var(--text-primary)',
        fontFamily: 'var(--font-sans)',
        cursor: 'pointer',
        transition: 'border-color var(--motion-fast) var(--motion-ease)',
      }}
    >
      {/* Top Handle: Incoming Transitions */}
      <Handle
        type="target"
        position={Position.Top}
        id="target"
        style={{
          width: 10,
          height: 10,
          backgroundColor: 'var(--border-strong)',
          border: '2px solid var(--bg-surface)',
        }}
      />

      {/* Header Badges */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 6, fontSize: 12, color: 'var(--text-secondary)' }}>
        {execStatus === 'running' && <Badge variant="info"><Icon name="workflows" /> {t('ui.running')}</Badge>}
        {execStatus === 'completed' && <Badge variant="success"><Icon name="check" /> {t('ui.completed')}</Badge>}
        {execStatus === 'failed' && <Badge variant="danger"><Icon name="close" /> {t('ui.failed')}</Badge>}
        {execStatus === 'paused' && <Badge variant="warning"><Icon name="pause" /> {t('ui.approvalPending')}</Badge>}
        {kindLabel && <span>{kindLabel}</span>}
        {isEntry && <span>{t('ui.entryNode')}</span>}
        {requiresApproval && <Badge variant="warning">{t('workflowDesign.needsApproval')}</Badge>}
        {isActiveReplay && <span><Icon name="traces" /> {t('workflowDesign.replayStep')}</span>}
        {hasError && <Badge variant="danger">{t('ui.invalid')}</Badge>}
      </div>

      {/* Node ID */}
      <div style={{ fontWeight: 600, fontSize: 13, lineHeight: '20px', wordBreak: 'break-all', marginBottom: 4 }}>
        {data.nodeId || id}
      </div>

      {/* Handler Name */}
      <div style={{ fontSize: 12, color: 'var(--text-secondary)', wordBreak: 'break-all' }}>
        {kind === 'parallel' || kind === 'subworkflow' ? (
          <span data-testid="node-composite-summary">{compositeSummary}</span>
        ) : (
          <code>{data.handler || data.module_id || 'unassigned'}</code>
        )}
      </div>
      {kind === 'map' && (
        <div data-testid="node-composite-summary" style={{ fontSize: 'var(--type-meta)', color: 'var(--text-muted)' }}>
          {compositeSummary}
        </div>
      )}
      {kind === 'parallel' && (data.parallel || []).length > 0 && (
        <div style={{ fontSize: 'var(--type-meta)', color: 'var(--text-muted)', wordBreak: 'break-all' }}>
          {(data.parallel || []).join(' | ')}
        </div>
      )}

      {/* Inline Validation Error Message */}
      {hasError && (
        <div
          style={{
            marginTop: 6,
            fontSize: 'var(--type-meta)',
            color: 'var(--status-danger)',
            backgroundColor: 'var(--status-danger-bg)',
            padding: '4px 6px',
            borderRadius: 'var(--radius-sm)',
          }}
        >
          {data.validationError}
        </div>
      )}

      {/* Bottom Handle: Outgoing Default Transition */}
      <Handle
        type="source"
        position={Position.Bottom}
        id="default"
        title={t('ui.defaultTransition2')}
        style={{
          width: 10,
          height: 10,
          backgroundColor: 'var(--border-strong)',
          border: '2px solid var(--bg-surface)',
        }}
      />

      {/* Right Handle: Outgoing Branch Transition */}
      <Handle
        type="source"
        position={Position.Right}
        id="branch"
        title={t('ui.namedBranchTransition')}
        style={{
          width: 8,
          height: 8,
          backgroundColor: 'var(--border-strong)',
          border: '2px solid var(--bg-surface)',
        }}
      />
    </div>
  );
});

WorkflowNodeComponent.displayName = 'WorkflowNodeComponent';
