// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { TemplateDetail } from './types';

export interface LaidOutNode {
  id: string;
  label: string;
  approval: boolean;
  terminal: boolean;
  x: number;
  y: number;
}

export interface LaidOutEdge {
  from: string;
  to: string;
  label: string;
  dashed: boolean;
}

export const NODE_W = 200;
export const NODE_H = 44;
const GAP_X = 40;
const GAP_Y = 56;

/** Layer nodes by their shortest distance from the entry node (breadth-first). */
export function layoutSpec(spec: TemplateDetail['spec']): {
  nodes: LaidOutNode[];
  edges: LaidOutEdge[];
  width: number;
  height: number;
} {
  const edges: LaidOutEdge[] = [];
  for (const [id, node] of Object.entries(spec.nodes)) {
    for (const [branch, target] of Object.entries(node.transitions ?? {})) {
      edges.push({ from: id, to: target, label: branch, dashed: false });
    }
    const dflt = node.default_transition;
    if (dflt && !Object.values(node.transitions ?? {}).includes(dflt)) {
      edges.push({ from: id, to: dflt, label: '', dashed: true });
    }
  }
  const depth = new Map<string, number>([[spec.entry_node, 0]]);
  const queue = [spec.entry_node];
  while (queue.length) {
    const current = queue.shift() as string;
    const node = spec.nodes[current];
    const targets = [
      ...Object.values(node?.transitions ?? {}),
      ...(node?.default_transition ? [node.default_transition] : []),
    ];
    for (const target of targets) {
      if (!depth.has(target)) {
        depth.set(target, (depth.get(current) as number) + 1);
        queue.push(target);
      }
    }
  }
  for (const id of Object.keys(spec.nodes)) {
    if (!depth.has(id)) depth.set(id, 0);
  }
  const layers = new Map<number, string[]>();
  for (const [id, d] of depth) {
    if (spec.nodes[id] === undefined) continue;
    layers.set(d, [...(layers.get(d) ?? []), id]);
  }
  const widest = Math.max(...[...layers.values()].map((l) => l.length), 1);
  const width = widest * NODE_W + (widest - 1) * GAP_X;
  const terminals = new Set(spec.terminal_nodes ?? []);
  const nodes: LaidOutNode[] = [];
  for (const [d, ids] of [...layers.entries()].sort((a, b) => a[0] - b[0])) {
    const rowWidth = ids.length * NODE_W + (ids.length - 1) * GAP_X;
    ids.forEach((id, i) => {
      nodes.push({
        id,
        label: spec.nodes[id].handler ?? id,
        approval: Boolean(spec.nodes[id].requires_approval),
        terminal: terminals.has(id),
        x: (width - rowWidth) / 2 + i * (NODE_W + GAP_X),
        y: d * (NODE_H + GAP_Y),
      });
    });
  }
  const height = (Math.max(...layers.keys(), 0) + 1) * (NODE_H + GAP_Y) - GAP_Y;
  return { nodes, edges, width, height };
}
