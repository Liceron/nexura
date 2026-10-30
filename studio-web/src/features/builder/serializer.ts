// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type { WorkflowSpec, WorkflowNode } from '@liceron/nexura-sdk';
import type { BuilderNode, BuilderEdge, GraphDocument, BuilderNodeData } from './types';

/**
 * Transforms a server WorkflowSpec into a React Flow GraphDocument with visual positions,
 * handles, and edge transitions.
 */
export function specToGraph(spec: WorkflowSpec): GraphDocument {
  const nodes: BuilderNode[] = [];
  const edges: BuilderEdge[] = [];
  const entryNode = spec.entry_node || '';

  const rawNodes = spec.nodes || {};
  const nodeEntries = Object.entries(rawNodes);

  // Position calculation fallback if not in presentation
  nodeEntries.forEach(([nodeId, rawNode], index) => {
    const nodeObj = rawNode as WorkflowNode & Record<string, unknown>;
    const pres = (nodeObj.presentation || (nodeObj.metadata as Record<string, unknown> | undefined)?.presentation) as
      | { position?: { x: number; y: number } }
      | undefined;

    const position = pres?.position || {
      x: 100 + (index % 3) * 320,
      y: 80 + Math.floor(index / 3) * 200,
    };

    const standardKeys = new Set([
      'id',
      'handler',
      'module_id',
      'module_version',
      'requires_approval',
      'config',
      'input_mapping',
      'output_mapping',
      'transitions',
      'default_transition',
      'presentation',
      'metadata',
      'parallel',
      'map_over',
      'map_as',
      'max_concurrency',
      'reducers',
      'subgraph',
    ]);
    const extraFields: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(nodeObj)) {
      if (!standardKeys.has(k)) {
        extraFields[k] = v;
      }
    }

    const {
      handler,
      module_id,
      module_version,
      requires_approval,
      config,
      input_mapping,
      output_mapping,
      transitions,
      default_transition,
      metadata,
    } = nodeObj;
    const composite = nodeObj as unknown as Partial<BuilderNodeData>;

    const data: BuilderNodeData = {
      nodeId,
      label: nodeId,
      handler: handler || module_id || '',
      module_id: module_id ?? undefined,
      module_version: module_version ?? undefined,
      requires_approval: Boolean(requires_approval),
      config: (config as Record<string, unknown>) || {},
      input_mapping: (input_mapping as Record<string, string>) || {},
      output_mapping: (output_mapping as Record<string, string>) || {},
      isEntry: nodeId === entryNode,
      metadata: (metadata as Record<string, unknown>) || {},
      extraFields: Object.keys(extraFields).length > 0 ? extraFields : undefined,
    };
    if (Array.isArray(composite.parallel) && composite.parallel.length > 0) {
      data.parallel = [...composite.parallel];
    }
    if (composite.map_over !== undefined && composite.map_over !== null) data.map_over = composite.map_over;
    if (composite.map_as !== undefined) data.map_as = composite.map_as;
    if (composite.max_concurrency !== undefined) data.max_concurrency = composite.max_concurrency;
    if (composite.reducers && Object.keys(composite.reducers).length > 0) {
      data.reducers = { ...composite.reducers };
    }
    if (composite.subgraph) data.subgraph = JSON.parse(JSON.stringify(composite.subgraph));

    nodes.push({
      id: nodeId,
      type: 'workflowNode',
      position,
      data,
    });

    // Create edge for default_transition
    if (default_transition && typeof default_transition === 'string') {
      edges.push({
        id: `e-${nodeId}-default-${default_transition}`,
        source: nodeId,
        target: default_transition,
        sourceHandle: 'default',
        targetHandle: 'target',
        label: 'default',
        data: { isDefault: true },
        animated: false,
      });
    }

    // Create edges for named branch transitions
    if (transitions && typeof transitions === 'object') {
      Object.entries(transitions).forEach(([branch, targetNodeId]) => {
        if (targetNodeId && typeof targetNodeId === 'string') {
          edges.push({
            id: `e-${nodeId}-${branch}-${targetNodeId}`,
            source: nodeId,
            target: targetNodeId,
            sourceHandle: branch,
            targetHandle: 'target',
            label: branch,
            data: { branch },
            animated: true,
          });
        }
      });
    }
  });

  const specPres = spec.presentation as { viewport?: { x: number; y: number; zoom: number } } | undefined;

  return {
    nodes,
    edges,
    entryNode,
    viewport: specPres?.viewport,
  };
}

/**
 * Transforms a React Flow GraphDocument back into a clean, runtime-authoritative WorkflowSpec.
 * Prunes dangling references and separates presentation coordinates.
 */
export function graphToSpec(
  doc: GraphDocument,
  baseSpec?: Partial<WorkflowSpec>
): WorkflowSpec {
  const allNodeIds = new Set(doc.nodes.map((n) => n.id));
  const nodesMap: Record<string, WorkflowNode> = {};

  // Build edges map for outgoing transitions
  const outgoingDefault: Record<string, string> = {};
  const outgoingBranches: Record<string, Record<string, string>> = {};

  doc.edges.forEach((edge) => {
    // Only accept edges where source and target genuinely exist in the graph
    if (!allNodeIds.has(edge.source) || !allNodeIds.has(edge.target)) {
      return;
    }

    const branch = edge.data?.branch;
    const isDefault = edge.data?.isDefault || (!branch && edge.label === 'default');

    if (isDefault) {
      outgoingDefault[edge.source] = edge.target;
    } else if (branch && typeof branch === 'string') {
      if (!outgoingBranches[edge.source]) outgoingBranches[edge.source] = {};
      outgoingBranches[edge.source][branch] = edge.target;
    } else {
      // Unspecified transition becomes default if none set
      if (!outgoingDefault[edge.source]) {
        outgoingDefault[edge.source] = edge.target;
      }
    }
  });

  doc.nodes.forEach((node) => {
    const d = node.data;
    const nodeId = node.id;

    const position = {
      x: Math.round(node.position.x),
      y: Math.round(node.position.y),
    };

    const wfNode: WorkflowNode & Record<string, unknown> = {
      id: nodeId,
      handler: d.handler || d.module_id || '',
      default_transition: outgoingDefault[nodeId] || undefined,
      transitions: outgoingBranches[nodeId] || {},
      requires_approval: Boolean(d.requires_approval),
      config: d.config || {},
      input_mapping: d.input_mapping || {},
      output_mapping: d.output_mapping || {},
      presentation: { position },
      metadata: {
        ...(d.metadata || {}),
        presentation: { position },
      },
    };

    if (d.module_id) wfNode.module_id = d.module_id;
    if (d.module_version) wfNode.module_version = d.module_version;
    // Composite steps: a parallel or sub-workflow step has no handler of its own.
    if (d.parallel && d.parallel.length > 0) wfNode.parallel = [...d.parallel];
    if (d.subgraph) wfNode.subgraph = JSON.parse(JSON.stringify(d.subgraph));
    if (d.parallel?.length || d.subgraph) {
      wfNode.handler = '';
      delete wfNode.module_id;
      delete wfNode.module_version;
    }
    if (d.map_over !== undefined) wfNode.map_over = d.map_over;
    if (d.map_as !== undefined) wfNode.map_as = d.map_as;
    if (d.max_concurrency !== undefined) wfNode.max_concurrency = d.max_concurrency;
    if (d.reducers && Object.keys(d.reducers).length > 0) wfNode.reducers = { ...d.reducers };

    // Preserve unknown extra fields
    if (d.extraFields) {
      Object.assign(wfNode, d.extraFields);
    }

    nodesMap[nodeId] = wfNode;
  });

  // Ensure entryNode is valid
  let entryNode = doc.entryNode;
  if (!allNodeIds.has(entryNode)) {
    entryNode = doc.nodes.length > 0 ? doc.nodes[0].id : '';
  }

  const spec: WorkflowSpec = {
    ...(baseSpec as WorkflowSpec),
    name: baseSpec?.name || 'Untitled Workflow',
    entry_node: entryNode,
    nodes: nodesMap,
    spec_version: baseSpec?.spec_version ?? 1,
    terminal_nodes: baseSpec?.terminal_nodes || [],
    metadata: { ...(baseSpec?.metadata || {}) },
    presentation: {
      viewport: doc.viewport || { x: 0, y: 0, zoom: 1 },
      ...(baseSpec?.presentation || {}),
    },
  };

  return spec;
}

/**
 * Copies selected nodes, generating unique IDs and internal remapped routes
 * while leaving existing nodes untouched.
 */
export function copyPasteNodes(
  selectedNodeIds: string[],
  currentNodes: BuilderNode[],
  currentEdges: BuilderEdge[],
  offset: { x: number; y: number } = { x: 40, y: 40 }
): { newNodes: BuilderNode[]; newEdges: BuilderEdge[] } {
  if (selectedNodeIds.length === 0) {
    return { newNodes: [], newEdges: [] };
  }

  const selectedSet = new Set(selectedNodeIds);
  const idMap = new Map<string, string>();

  // 1. Generate new unique IDs for selected nodes
  selectedNodeIds.forEach((origId) => {
    const shortRand = Math.random().toString(36).substring(2, 6);
    const newId = `${origId}_copy_${shortRand}`;
    idMap.set(origId, newId);
  });

  // 2. Clone nodes with new positions and remapped data
  const newNodes: BuilderNode[] = [];
  currentNodes.forEach((node) => {
    if (selectedSet.has(node.id)) {
      const newId = idMap.get(node.id)!;
      newNodes.push({
        ...node,
        id: newId,
        position: {
          x: node.position.x + offset.x,
          y: node.position.y + offset.y,
        },
        selected: true,
        data: {
          ...node.data,
          nodeId: newId,
          label: newId,
          isEntry: false, // Pasted nodes do not hijack workflow entry
        },
      });
    }
  });

  // 3. Clone and remap only internal edges connecting selected nodes
  const newEdges: BuilderEdge[] = [];
  currentEdges.forEach((edge) => {
    if (selectedSet.has(edge.source) && selectedSet.has(edge.target)) {
      const newSource = idMap.get(edge.source)!;
      const newTarget = idMap.get(edge.target)!;
      newEdges.push({
        ...edge,
        id: `e-${newSource}-${edge.data?.branch || 'default'}-${newTarget}`,
        source: newSource,
        target: newTarget,
        selected: false,
      });
    }
  });

  return { newNodes, newEdges };
}

/**
 * Removes a node and prunes any incoming and outgoing edges to prevent dangling transitions.
 */
export function deleteNodeAndPruneEdges(
  nodeIdToDelete: string,
  currentNodes: BuilderNode[],
  currentEdges: BuilderEdge[],
  currentEntryNode: string
): {
  remainingNodes: BuilderNode[];
  remainingEdges: BuilderEdge[];
  newEntryNode: string;
} {
  const remainingNodes = currentNodes.filter((n) => n.id !== nodeIdToDelete);
  const remainingEdges = currentEdges.filter(
    (e) => e.source !== nodeIdToDelete && e.target !== nodeIdToDelete
  );

  let newEntryNode = currentEntryNode;
  if (currentEntryNode === nodeIdToDelete) {
    newEntryNode = remainingNodes.length > 0 ? remainingNodes[0].id : '';
    if (remainingNodes.length > 0) {
      remainingNodes[0] = {
        ...remainingNodes[0],
        data: {
          ...remainingNodes[0].data,
          isEntry: true,
        },
      };
    }
  }

  return { remainingNodes, remainingEdges, newEntryNode };
}
