import { describe, it, expect } from 'vitest';
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import {
  specToGraph,
  graphToSpec,
  deleteNodeAndPruneEdges,
  copyPasteNodes,
} from '../../src/features/builder/serializer';
import { HistoryManager } from '../../src/features/builder/history';

describe('Graph Serializer & Builder Engine (F04)', () => {
  const sampleSpec: WorkflowSpec = {
    name: 'OrderSupportWorkflow',
    entry_node: 'order_lookup',
    nodes: {
      order_lookup: {
        id: 'order_lookup',
        handler: 'synthetic_order_lookup',
        module_id: 'synthetic_order_lookup',
        default_transition: 'router_step',
        presentation: { position: { x: 100, y: 150 } },
        config: { timeout_ms: 5000 },
      },
      router_step: {
        id: 'router_step',
        handler: 'synthetic_router',
        module_id: 'synthetic_router',
        transitions: {
          refund: 'approval_gate',
          support: 'ticket_creator',
        },
        default_transition: 'ticket_creator',
        presentation: { position: { x: 350, y: 150 } },
      },
      approval_gate: {
        id: 'approval_gate',
        handler: 'synthetic_fake_agent',
        module_id: 'synthetic_fake_agent',
        requires_approval: true,
        default_transition: 'ticket_creator',
        presentation: { position: { x: 600, y: 100 } },
      },
      ticket_creator: {
        id: 'ticket_creator',
        handler: 'synthetic_ticket_creator',
        module_id: 'synthetic_ticket_creator',
        presentation: { position: { x: 600, y: 250 } },
      },
    },
    presentation: {
      viewport: { x: 50, y: 50, zoom: 1.2 },
    },
  };

  it('specToGraph and graphToSpec execute faithful round-trip serialization', () => {
    const doc = specToGraph(sampleSpec);

    expect(doc.nodes).toHaveLength(4);
    expect(doc.entryNode).toBe('order_lookup');
    expect(doc.viewport).toEqual({ x: 50, y: 50, zoom: 1.2 });

    // Verify node properties
    const approvalNode = doc.nodes.find((n) => n.id === 'approval_gate');
    expect(approvalNode).toBeDefined();
    expect(approvalNode?.data.requires_approval).toBe(true);
    expect(approvalNode?.position).toEqual({ x: 600, y: 100 });

    // Verify edges: 1 default from order_lookup, 2 branch + 1 default from router_step (target same as support), 1 default from approval_gate
    expect(doc.edges.length).toBeGreaterThanOrEqual(4);
    const refundEdge = doc.edges.find((e) => e.source === 'router_step' && e.label === 'refund');
    expect(refundEdge).toBeDefined();
    expect(refundEdge?.target).toBe('approval_gate');

    // Round-trip back to spec
    const respec = graphToSpec(doc, sampleSpec);

    expect(respec.name).toBe('OrderSupportWorkflow');
    expect(respec.entry_node).toBe('order_lookup');
    expect(respec.nodes.order_lookup.default_transition).toBe('router_step');
    expect(respec.nodes.router_step.transitions).toEqual({
      refund: 'approval_gate',
      support: 'ticket_creator',
    });
    expect(respec.nodes.router_step.default_transition).toBe('ticket_creator');
    expect(respec.nodes.approval_gate.requires_approval).toBe(true);
    expect(respec.nodes.approval_gate.default_transition).toBe('ticket_creator');

    // Verify presentation coordinates separated from execution logic
    expect(respec.presentation?.viewport).toEqual({ x: 50, y: 50, zoom: 1.2 });
    expect(respec.nodes.order_lookup.presentation?.position).toEqual({ x: 100, y: 150 });
  });

  it('preserves unknown custom fields across import and export', () => {
    const specWithCustomFields: WorkflowSpec & { custom_engine_id?: string } = {
      ...sampleSpec,
      custom_engine_id: 'enterprise_cluster_99',
      nodes: {
        ...sampleSpec.nodes,
        order_lookup: {
          ...sampleSpec.nodes.order_lookup,
          legacy_annotation: 'created-by-v1-importer',
        } as any,
      },
    };

    const doc = specToGraph(specWithCustomFields as WorkflowSpec);
    expect(doc.nodes[0].data.extraFields?.legacy_annotation).toBe('created-by-v1-importer');

    const exported = graphToSpec(doc, specWithCustomFields as WorkflowSpec);
    expect((exported as any).custom_engine_id).toBe('enterprise_cluster_99');
    expect((exported.nodes.order_lookup as any).legacy_annotation).toBe('created-by-v1-importer');
  });

  it('prunes dangling transitions and cleans edges upon node deletion', () => {
    const doc = specToGraph(sampleSpec);

    // Delete router_step (which is connected to order_lookup, approval_gate, and ticket_creator)
    const result = deleteNodeAndPruneEdges('router_step', doc.nodes, doc.edges, doc.entryNode);

    expect(result.remainingNodes.find((n) => n.id === 'router_step')).toBeUndefined();
    expect(result.remainingNodes).toHaveLength(3);

    // All edges originating from or pointing to router_step must be removed
    const danglingEdges = result.remainingEdges.filter(
      (e) => e.source === 'router_step' || e.target === 'router_step'
    );
    expect(danglingEdges).toHaveLength(0);

    // Exported spec must not contain dangling references to router_step
    const cleanedDoc = {
      nodes: result.remainingNodes,
      edges: result.remainingEdges,
      entryNode: result.newEntryNode,
      viewport: doc.viewport,
    };
    const cleanedSpec = graphToSpec(cleanedDoc, sampleSpec);
    expect(cleanedSpec.nodes.order_lookup.default_transition).toBeUndefined();
    expect(cleanedSpec.nodes.router_step).toBeUndefined();
  });

  it('re-nominates a valid entry node when the entry node is deleted', () => {
    const doc = specToGraph(sampleSpec);
    const result = deleteNodeAndPruneEdges('order_lookup', doc.nodes, doc.edges, doc.entryNode);

    expect(result.newEntryNode).not.toBe('order_lookup');
    expect(result.remainingNodes.some((n) => n.id === result.newEntryNode)).toBe(true);
    expect(result.remainingNodes.find((n) => n.id === result.newEntryNode)?.data.isEntry).toBe(true);
  });

  it('copyPasteNodes remaps node IDs and internal edges without modifying original nodes', () => {
    const doc = specToGraph(sampleSpec);
    const nodeIdsToCopy = ['router_step', 'approval_gate'];

    const pasted = copyPasteNodes(nodeIdsToCopy, doc.nodes, doc.edges);

    expect(pasted.newNodes).toHaveLength(2);

    const originalIds = ['router_step', 'approval_gate'];
    const pastedIds = pasted.newNodes.map((n) => n.id);

    // Pasted nodes must have distinct new IDs
    pastedIds.forEach((id) => {
      expect(originalIds).not.toContain(id);
    });

    // Check that internal edge (router_step -> approval_gate) is remapped to new cloned IDs
    const remappedInternalEdge = pasted.newEdges.find((e) => e.label === 'refund');
    expect(remappedInternalEdge).toBeDefined();
    expect(pastedIds).toContain(remappedInternalEdge?.source);
    expect(pastedIds).toContain(remappedInternalEdge?.target);

    // Pasted positions are offset for visual distinction
    const origRouter = doc.nodes.find((n) => n.id === 'router_step')!;
    const pastedRouter = pasted.newNodes.find((n) => n.id.startsWith('router_step'))!;
    expect(pastedRouter.position.x).toBeGreaterThan(origRouter.position.x);
    expect(pastedRouter.position.y).toBeGreaterThan(origRouter.position.y);
  });

  it('HistoryManager enforces bounded undo/redo depth (capped at 30)', () => {
    const baseDoc = specToGraph(sampleSpec);
    const history = new HistoryManager(baseDoc, 30);

    expect(history.canUndo()).toBe(false);
    expect(history.canRedo()).toBe(false);

    // Push 35 state mutations
    for (let i = 1; i <= 35; i++) {
      const mutatedDoc: typeof baseDoc = {
        ...baseDoc,
        nodes: [
          ...baseDoc.nodes,
          {
            id: `node_mutation_${i}`,
            type: 'workflowNode',
            position: { x: i * 10, y: i * 10 },
            data: {
              nodeId: `node_mutation_${i}`,
              label: `node_mutation_${i}`,
              handler: 'synthetic_order_lookup',
              requires_approval: false,
              config: {},
              input_mapping: {},
              output_mapping: {},
              isEntry: false,
            },
          },
        ],
      };
      history.push(mutatedDoc);
    }

    // Depth is capped at 30
    expect(history.canUndo()).toBe(true);
    expect(history.canRedo()).toBe(false);

    // Undo step
    const prev = history.undo();
    expect(prev).toBeDefined();
    expect(history.canRedo()).toBe(true);

    // Redo step
    const redone = history.redo();
    expect(redone).toBeDefined();
    expect(history.canRedo()).toBe(false);

    // Branching: pushing new state clears redo stack
    history.undo();
    expect(history.canRedo()).toBe(true);
    history.push(baseDoc);
    expect(history.canRedo()).toBe(false);
  });
});
