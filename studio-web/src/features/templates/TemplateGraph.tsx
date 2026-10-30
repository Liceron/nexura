// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { layoutSpec, NODE_H, NODE_W } from './graphLayout';
import type { TemplateDetail } from './types';

/** Read-only drawing of a template's graph: no dragging, no editing. */
export function TemplateGraph({ spec, label }: { spec: TemplateDetail['spec']; label: string }) {
  const { nodes, edges, width, height } = layoutSpec(spec);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const pad = 16;
  return (
    <svg
      role="img"
      aria-label={label}
      data-testid="template-graph"
      viewBox={`${-pad} ${-pad} ${width + pad * 2} ${height + pad * 2 + 8}`}
      style={{ width: '100%', maxHeight: 420, background: 'var(--bg-surface)' }}
    >
      <defs>
        <marker id="tpl-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--text-secondary)" />
        </marker>
      </defs>
      {edges.map((e, i) => {
        const a = byId.get(e.from);
        const b = byId.get(e.to);
        if (!a || !b) return null;
        const x1 = a.x + NODE_W / 2;
        const y1 = a.y + NODE_H;
        const x2 = b.x + NODE_W / 2;
        const y2 = b.y;
        return (
          <g key={`${e.from}-${e.to}-${i}`}>
            <line
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
              stroke="var(--text-secondary)"
              strokeWidth={1.5}
              strokeDasharray={e.dashed ? '4 3' : undefined}
              markerEnd="url(#tpl-arrow)"
            />
            {e.label && (
              <text
                x={(x1 + x2) / 2 + 4}
                y={(y1 + y2) / 2}
                fontSize={11}
                fill="var(--text-secondary)"
              >
                {e.label}
              </text>
            )}
          </g>
        );
      })}
      {nodes.map((n) => (
        <g key={n.id} data-testid={`template-node-${n.id}`}>
          <rect
            x={n.x}
            y={n.y}
            width={NODE_W}
            height={NODE_H}
            rx={8}
            fill="var(--bg-surface-elevated)"
            stroke={n.approval ? 'var(--status-warning)' : 'var(--border-subtle)'}
            strokeWidth={n.terminal ? 3 : 1.5}
          />
          <text x={n.x + NODE_W / 2} y={n.y + 19} fontSize={12} fontWeight={600} textAnchor="middle" fill="var(--text-primary)">
            {n.id}
          </text>
          <text x={n.x + NODE_W / 2} y={n.y + 34} fontSize={10} textAnchor="middle" fill="var(--text-secondary)">
            {n.approval ? `${n.label} (approval)` : n.label}
          </text>
        </g>
      ))}
    </svg>
  );
}
