import { describe, it, expect } from 'vitest';
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import { specToGraph, graphToSpec } from '../../src/features/builder/serializer';
import { HistoryManager } from '../../src/features/builder/history';
import { compositeIssues, stepKind } from '../../src/features/builder/composite';
import type { GraphDocument } from '../../src/features/builder/types';

type Json = Record<string, unknown>;

/** Strip editor-only data and empty defaults so spec equality means "same runtime meaning". */
function normalize(spec: WorkflowSpec): Json {
  const nodes: Record<string, Json> = {};
  for (const [id, raw] of Object.entries(spec.nodes)) {
    const n: Json = JSON.parse(JSON.stringify(raw));
    delete n.presentation;
    const meta = (n.metadata || {}) as Json;
    delete meta.presentation;
    if (Object.keys(meta).length === 0) delete n.metadata;
    else n.metadata = meta;
    for (const k of ['config', 'input_mapping', 'output_mapping', 'transitions']) {
      if (n[k] && Object.keys(n[k] as Json).length === 0) delete n[k];
    }
    if (n.requires_approval === false) delete n.requires_approval;
    if (n.default_transition == null) delete n.default_transition;
    if (n.subgraph) n.subgraph = normalize(n.subgraph as WorkflowSpec);
    nodes[id] = n;
  }
  return { name: spec.name, entry_node: spec.entry_node, nodes, terminal_nodes: spec.terminal_nodes ?? [] };
}

const child: WorkflowSpec = {
  name: 'child',
  entry_node: 'leaf',
  nodes: { leaf: { id: 'leaf', handler: 'synthetic_router' } },
};

const spec: WorkflowSpec = {
  name: 'composite',
  entry_node: 'plain',
  nodes: {
    plain: {
      id: 'plain',
      handler: 'synthetic_order_lookup',
      module_id: 'synthetic_order_lookup',
      module_version: '1.0.0',
      requires_approval: true,
      config: { a: 1 },
      input_mapping: { order_id: 'state.id' },
      default_transition: 'fan',
      transitions: { alt: 'each' },
      retry: { max_attempts: 2 },
    },
    fan: {
      id: 'fan',
      handler: '',
      parallel: ['synthetic_router', 'synthetic_order_lookup'],
      reducers: { results: 'append' },
      max_concurrency: 4,
      default_transition: 'each',
    },
    each: {
      id: 'each',
      handler: 'synthetic_router',
      map_over: 'orders',
      map_as: 'order',
      max_concurrency: 5,
      reducers: { out: 'extend' },
      default_transition: 'sub',
    },
    sub: { id: 'sub', handler: '', subgraph: child, metadata: { subworkflow: { workflow_id: 'wf-1', name: 'child', revision: 3 } }, default_transition: 'mapsub' },
    mapsub: { id: 'mapsub', handler: '', subgraph: child, map_over: 'rows', reducers: { r: 'append' } },
  },
};

describe('composite steps: spec -> canvas -> spec', () => {
  it('round-trips every kind and existing plain steps losslessly', () => {
    const doc = specToGraph(spec);
    const back = graphToSpec(doc, spec);
    expect(normalize(back)).toEqual(normalize(spec));
  });

  it.each([
    ['plain', 'step'],
    ['fan', 'parallel'],
    ['each', 'map'],
    ['sub', 'subworkflow'],
    ['mapsub', 'subworkflow'],
  ])('classifies %s as %s', (id, kind) => {
    const node = specToGraph(spec).nodes.find((n) => n.id === id)!;
    expect(stepKind(node.data)).toBe(kind);
  });

  it('keeps each kind on its own after a second pass and keeps routes', () => {
    const once = graphToSpec(specToGraph(spec), spec);
    const twice = graphToSpec(specToGraph(once), once);
    expect(normalize(twice)).toEqual(normalize(once));
    expect(twice.nodes.fan.default_transition).toBe('each');
    expect(twice.nodes.plain.transitions).toEqual({ alt: 'each' });
  });

  it('never emits a handler or module for parallel and sub-workflow steps', () => {
    const doc = specToGraph(spec);
    const fan = doc.nodes.find((n) => n.id === 'fan')!;
    fan.data.module_id = 'synthetic_router';
    fan.data.handler = 'synthetic_router';
    const out = graphToSpec(doc, spec);
    expect(out.nodes.fan.handler).toBe('');
    expect(out.nodes.fan.module_id).toBeUndefined();
  });

  it('does not emit editor-only pending state', () => {
    const doc = specToGraph(spec);
    doc.nodes[0].data.pendingKind = 'subworkflow';
    const out = graphToSpec(doc, spec);
    expect(JSON.stringify(out)).not.toContain('pendingKind');
  });
});

describe('composite steps: inline validation mirrors the engine', () => {
  const base = { handler: 'synthetic_router' };
  it('flags a bad item key, empty list name and concurrency out of range', () => {
    expect(compositeIssues({ ...base, map_over: 'items', map_as: 'bad key' }).map((i) => i.field)).toEqual(['map_as']);
    expect(compositeIssues({ ...base, map_over: '' }).map((i) => i.field)).toContain('map_over');
    expect(compositeIssues({ ...base, map_over: 'x', max_concurrency: 0 }).map((i) => i.field)).toContain('max_concurrency');
    expect(compositeIssues({ ...base, map_over: 'x', max_concurrency: 101 }).length).toBe(1);
  });
  it('accepts valid composites and ignores plain steps', () => {
    expect(compositeIssues({ ...base, map_over: 'items', map_as: 'row', max_concurrency: 100 })).toEqual([]);
    expect(compositeIssues({ parallel: ['a', 'b'] })).toEqual([]);
    expect(compositeIssues({ handler: '' })).toEqual([]);
  });
  it('flags duplicate or empty parallel branches and a missing sub-workflow', () => {
    expect(compositeIssues({ parallel: [] }).length).toBe(1);
    expect(compositeIssues({ parallel: ['a', 'a'] }).length).toBe(1);
    expect(compositeIssues({ pendingKind: 'subworkflow' }).map((i) => i.field)).toEqual(['subgraph']);
  });
});

describe('composite steps: undo/redo', () => {
  it('restores and re-applies composite data', () => {
    const doc = specToGraph(spec);
    const snap = (d: typeof doc): GraphDocument => ({ nodes: d.nodes, edges: d.edges, entryNode: d.entryNode });
    const h = new HistoryManager<GraphDocument>(snap(doc));
    const edited = specToGraph(spec);
    const each = edited.nodes.find((n) => n.id === 'each')!;
    each.data = { ...each.data, map_over: 'changed', max_concurrency: 9 };
    h.push(snap(edited));
    const fan = edited.nodes.find((n) => n.id === 'fan')!;
    fan.data = { ...fan.data, parallel: ['synthetic_router'] };
    h.push(snap(edited));

    const u1 = h.undo()!;
    expect(u1.nodes.find((n) => n.id === 'fan')!.data.parallel).toEqual(['synthetic_router', 'synthetic_order_lookup']);
    const u2 = h.undo()!;
    expect(u2.nodes.find((n) => n.id === 'each')!.data.map_over).toBe('orders');
    const r1 = h.redo()!;
    expect(r1.nodes.find((n) => n.id === 'each')!.data.max_concurrency).toBe(9);
    expect(graphToSpec(r1, spec).nodes.each.map_over).toBe('changed');
  });
});
