// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useLocale } from '../../i18n';
import { Icon } from '../../components/Icon';
import { useState, useCallback, useRef, useEffect, useMemo, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ActionDisclosure } from '../../components/ActionDisclosure';
import { Modal } from '../../components/Modal';
import {
  ReactFlow,
  Background,
  useReactFlow,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
  Connection,
  Edge,
  Node,
  ReactFlowProvider,
} from '@xyflow/react';
import type { BuilderNode, BuilderEdge, GraphDocument, BuilderNodeData } from './types';
import { WorkflowNodeComponent } from './WorkflowNodeComponent';
import { NodeInspector } from './NodeInspector';
import { ModulePalette } from '../modules/ModulePalette';
import { HistoryManager } from './history';
import {
  graphToSpec,
  specToGraph,
  copyPasteNodes,
  deleteNodeAndPruneEdges,
} from './serializer';
import { Button } from '../../components/Button';
import { useApi } from '../../api/context';
import type { WorkflowSpec, WorkflowRecord, ValidationIssue } from '@liceron/nexura-sdk';
import {
  saveWorkflowDraft,
  loadWorkflowDraft,
  clearWorkflowDraft,
  WorkflowDraft,
} from '../workflows/persistence';
import { useBuilderT } from './composite';
import type { StepKind } from './composite';
import { ConflictModal } from '../workflows/ConflictModal';
import { ImportExportModal } from '../workflows/ImportExportModal';

import type { NodeExecutionStatus } from '../runs/types';
import { formatApiError } from '../../api/errors';

interface WorkflowCanvasProps {
  initialSpec: WorkflowSpec;
  actionHost?: HTMLElement | null;
  feedbackHost?: HTMLElement | null;
  active?: boolean;
  primaryAllowed?: boolean;
  viewMode?: 'graph' | 'nodes';
  moreActions?: ReactNode;
  onDirtyChange?: (dirty: boolean) => void;
  onImportedPython?: (record: WorkflowRecord) => void;
  workflowId: string;
  workflowName?: string;
  revision: number;
  runNodeStatuses?: Map<string, NodeExecutionStatus>;
  activeReplayNodeId?: string | null;
  onSaveSpec: (spec: WorkflowSpec, expectedRevision: number) => Promise<void>;
  onRunWorkflow?: () => void;
  onReloadWorkflow?: () => void;
  onSaveAsCopy?: (copyName: string, spec: WorkflowSpec) => Promise<void>;
}

const nodeTypes = {
  workflowNode: WorkflowNodeComponent,
};

export function WorkflowCanvasInner({
  initialSpec,
  actionHost,
  feedbackHost,
  active = true,
  primaryAllowed = true,
  viewMode = 'graph',
  moreActions,
  onDirtyChange,
  onImportedPython,
  workflowId,
  workflowName,
  revision,
  runNodeStatuses,
  activeReplayNodeId,
  onSaveSpec,
  onRunWorkflow,
  onReloadWorkflow,
  onSaveAsCopy,
}: WorkflowCanvasProps) {
  const { manager, connection } = useApi();
  const locale = useLocale();
  const t = useBuilderT();
  const editorRoot = useRef<HTMLDivElement>(null);
  const flow = useReactFlow<BuilderNode, BuilderEdge>();
  const [showMinimap, setShowMinimap] = useState(false);
  const [interactive, setInteractive] = useState(true);
  const [wide, setWide] = useState(() => typeof matchMedia === 'function' && matchMedia('(min-width: 1200px)').matches);
  useEffect(() => {
    if (typeof matchMedia !== 'function') return;
    const query = matchMedia('(min-width: 1200px)');
    const update = () => setWide(query.matches);
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);

  // Initialize graph from spec
  const initialDoc = useMemo(() => specToGraph(initialSpec), [initialSpec]);

  const [nodes, setNodes, onNodesChange] = useNodesState<BuilderNode>(initialDoc.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<BuilderEdge>(initialDoc.edges);
  const [entryNode, setEntryNode] = useState<string>(initialDoc.entryNode);

  // Synchronize execution status and replay highlights onto node data
  useEffect(() => {
    setNodes((currentNodes) =>
      currentNodes.map((n) => {
        const status = runNodeStatuses?.get(n.id) || 'idle';
        const isActiveReplay = n.id === activeReplayNodeId;
        if (n.data.executionStatus === status && n.data.isActiveReplayNode === isActiveReplay) {
          return n;
        }
        return {
          ...n,
          data: {
            ...n.data,
            executionStatus: status,
            isActiveReplayNode: isActiveReplay,
          },
        };
      })
    );
  }, [runNodeStatuses, activeReplayNodeId, setNodes]);

  // Selection
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  // Drawers & Modals
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [conflictModalOpen, setConflictModalOpen] = useState(false);
  const [importExportOpen, setImportExportOpen] = useState(false);
  const [importExportMode, setImportExportMode] = useState<'export' | 'import'>('export');

  // Save / Validate / Dirty states
  const [isSaving, setIsSaving] = useState(false);
  const [isValidating, setIsValidating] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [validationIssues, setValidationIssues] = useState<ValidationIssue[]>([]);
  const [validationSuccess, setValidationSuccess] = useState(false);
  const [isDirty, setIsDirty] = useState(false);
  const [saveStatus, setSaveStatus] = useState<'clean' | 'dirty' | 'saving' | 'conflict'>('clean');

  useEffect(() => { onDirtyChange?.(isDirty); }, [isDirty, onDirtyChange]);

  // Draft recovery notice
  const [draftNotice, setDraftNotice] = useState<WorkflowDraft | null>(null);

  // Check for local draft on mount
  useEffect(() => {
    const draft = loadWorkflowDraft(connection.workspace, connection.principal, workflowId);
    if (draft && draft.savedAt && draft.spec) {
      const draftDoc = specToGraph(draft.spec);
      const isDifferent =
        draftDoc.nodes.length !== initialDoc.nodes.length ||
        draft.spec.entry_node !== initialSpec.entry_node ||
        JSON.stringify(draft.spec.nodes) !== JSON.stringify(initialSpec.nodes);
      if (isDifferent) {
        setDraftNotice(draft);
      }
    }
  }, [connection.workspace, connection.principal, workflowId, initialDoc, initialSpec]);

  const handleRestoreDraft = () => {
    if (!draftNotice) return;
    const restoredDoc = specToGraph(draftNotice.spec);
    setNodes(restoredDoc.nodes);
    setEdges(restoredDoc.edges);
    setEntryNode(restoredDoc.entryNode);
    pushHistory(restoredDoc.nodes, restoredDoc.edges, restoredDoc.entryNode);
    setIsDirty(true);
    setSaveStatus('dirty');
    setDraftNotice(null);
  };

  const handleDiscardDraft = () => {
    clearWorkflowDraft(connection.workspace, connection.principal, workflowId);
    setDraftNotice(null);
  };

  // Debounced draft autosave to localStorage (1000ms) with secret protection
  useEffect(() => {
    if (!isDirty) return;
    const timer = setTimeout(() => {
      const currentSpec = graphToSpec({ nodes, edges, entryNode }, initialSpec);
      saveWorkflowDraft(
        connection.workspace,
        connection.principal,
        workflowId,
        currentSpec,
        revision
      );
    }, 1000);
    return () => clearTimeout(timer);
  }, [
    nodes,
    edges,
    entryNode,
    isDirty,
    initialSpec,
    connection.workspace,
    connection.principal,
    workflowId,
    revision,
  ]);

  // History Manager (Undo / Redo)
  const historyRef = useRef<HistoryManager<GraphDocument>>(
    new HistoryManager({
      nodes: initialDoc.nodes,
      edges: initialDoc.edges,
      entryNode: initialDoc.entryNode,
    })
  );

  // Clipboard
  const clipboardRef = useRef<string[]>([]);

  // Push snapshot to history
  const pushHistory = useCallback(
    (newNodes: BuilderNode[], newEdges: BuilderEdge[], newEntry: string) => {
      historyRef.current.push({
        nodes: newNodes,
        edges: newEdges,
        entryNode: newEntry,
      });
      setIsDirty(true);
      setValidationSuccess(false);
    },
    []
  );

  const handleUndo = useCallback(() => {
    const prev = historyRef.current.undo();
    if (prev) {
      setNodes(prev.nodes);
      setEdges(prev.edges);
      setEntryNode(prev.entryNode);
      setIsDirty(true);
    }
  }, [setNodes, setEdges]);

  const handleRedo = useCallback(() => {
    const next = historyRef.current.redo();
    if (next) {
      setNodes(next.nodes);
      setEdges(next.edges);
      setEntryNode(next.entryNode);
      setIsDirty(true);
    }
  }, [setNodes, setEdges]);

  // Connect edges
  const onConnect = useCallback(
    (params: Connection | Edge) => {
      const source = params.source!;
      const target = params.target!;
      const sourceHandle = params.sourceHandle;

      const isDefault = sourceHandle === 'default' || !sourceHandle;
      const branch = isDefault ? undefined : String(sourceHandle);

      const newEdge: BuilderEdge = {
        id: `e-${source}-${branch || 'default'}-${target}`,
        source,
        target,
        sourceHandle: sourceHandle || undefined,
        targetHandle: params.targetHandle || 'target',
        label: branch || 'default',
        data: {
          isDefault,
          branch,
        },
        animated: Boolean(branch),
      };

      setEdges((eds) => {
        // If connecting default transition, remove existing default edge from this source
        const filtered = isDefault
          ? eds.filter((e) => !(e.source === source && (e.data?.isDefault || !e.data?.branch)))
          : eds;
        const updated = addEdge(newEdge, filtered) as BuilderEdge[];
        pushHistory(nodes, updated, entryNode);
        return updated;
      });
    },
    [nodes, entryNode, pushHistory, setEdges]
  );

  // Node selection
  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    setSelectedNodeId(node.id);
    setInspectorOpen(true);
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedNodeId(null);
  }, []);

  // Add node from module palette
  const handleSelectModule = useCallback(
    (mod: { module_id: string; version: string; title: string; requires_approval: boolean }) => {
      const shortId = Math.random().toString(36).substring(2, 6);
      const safePrefix = mod.module_id.split('.').pop() || 'step';
      const newNodeId = `${safePrefix}_${shortId}`;

      const newNode: BuilderNode = {
        id: newNodeId,
        type: 'workflowNode',
        position: {
          x: 200 + (nodes.length % 4) * 80,
          y: 150 + (nodes.length % 4) * 80,
        },
        data: {
          nodeId: newNodeId,
          label: mod.title || newNodeId,
          handler: mod.module_id,
          module_id: mod.module_id,
          module_version: mod.version,
          requires_approval: mod.requires_approval,
          config: {},
          input_mapping: {},
          output_mapping: {},
          isEntry: nodes.length === 0, // First node becomes entry
        },
      };

      const updatedNodes = [...nodes, newNode];
      const newEntry = nodes.length === 0 ? newNodeId : entryNode;
      setNodes(updatedNodes);
      if (nodes.length === 0) setEntryNode(newEntry);
      pushHistory(updatedNodes, edges, newEntry);
      setSelectedNodeId(newNodeId);
      setInspectorOpen(true);
      setDrawerOpen(false);
    },
    [nodes, edges, entryNode, pushHistory, setNodes]
  );

  // Add a composite step (parallel / map / sub-workflow). The spec fields start empty and
  // are filled in from the inspector; validation reports anything still missing.
  const handleAddComposite = useCallback(
    (kind: Exclude<StepKind, 'step'>) => {
      const shortId = Math.random().toString(36).substring(2, 6);
      const newNodeId = `${kind}_${shortId}`;
      const base: BuilderNodeData = {
        nodeId: newNodeId,
        label: newNodeId,
        handler: '',
        requires_approval: false,
        config: {},
        input_mapping: {},
        output_mapping: {},
        isEntry: nodes.length === 0,
      };
      const data: BuilderNodeData =
        kind === 'parallel'
          ? { ...base, parallel: [] }
          : kind === 'map'
            ? { ...base, map_over: '' }
            : { ...base, pendingKind: 'subworkflow' };
      const newNode: BuilderNode = {
        id: newNodeId,
        type: 'workflowNode',
        position: { x: 200 + (nodes.length % 4) * 80, y: 150 + (nodes.length % 4) * 80 },
        data,
      };
      const updatedNodes = [...nodes, newNode];
      const newEntry = nodes.length === 0 ? newNodeId : entryNode;
      setNodes(updatedNodes);
      if (nodes.length === 0) setEntryNode(newEntry);
      pushHistory(updatedNodes, edges, newEntry);
      setSelectedNodeId(newNodeId);
      setInspectorOpen(true);
    },
    [nodes, edges, entryNode, pushHistory, setNodes]
  );

  // Delete node
  const handleDeleteNode = useCallback(
    (nodeIdToDelete: string) => {
      const { remainingNodes, remainingEdges, newEntryNode } = deleteNodeAndPruneEdges(
        nodeIdToDelete,
        nodes,
        edges,
        entryNode
      );
      setNodes(remainingNodes);
      setEdges(remainingEdges);
      setEntryNode(newEntryNode);
      if (selectedNodeId === nodeIdToDelete) setSelectedNodeId(null);
      pushHistory(remainingNodes, remainingEdges, newEntryNode);
    },
    [nodes, edges, entryNode, selectedNodeId, pushHistory, setNodes, setEdges]
  );

  // Copy & Paste
  const handleCopy = useCallback(() => {
    const selected = nodes.filter((n) => n.selected).map((n) => n.id);
    if (selected.length > 0) {
      clipboardRef.current = selected;
    } else if (selectedNodeId) {
      clipboardRef.current = [selectedNodeId];
    }
  }, [nodes, selectedNodeId]);

  const handlePaste = useCallback(() => {
    if (clipboardRef.current.length === 0) return;
    const { newNodes, newEdges } = copyPasteNodes(clipboardRef.current, nodes, edges);
    if (newNodes.length > 0) {
      const updatedNodes = [...nodes.map((n) => ({ ...n, selected: false })), ...newNodes];
      const updatedEdges = [...edges, ...newEdges];
      setNodes(updatedNodes);
      setEdges(updatedEdges);
      pushHistory(updatedNodes, updatedEdges, entryNode);
    }
  }, [nodes, edges, entryNode, pushHistory, setNodes, setEdges]);

  // Inspector updates
  const handleUpdateNodeData = useCallback(
    (nodeId: string, updates: Partial<BuilderNodeData>) => {
      setNodes((nds) => {
        const updated = nds.map((n) =>
          n.id === nodeId ? { ...n, data: { ...n.data, validationError: undefined, ...updates } } : n
        );
        pushHistory(updated, edges, entryNode);
        return updated;
      });
    },
    [edges, entryNode, pushHistory, setNodes]
  );

  const handleRenameNode = useCallback(
    (oldId: string, newId: string) => {
      if (nodes.some((n) => n.id === newId)) return; // ID must be unique
      setNodes((nds) =>
        nds.map((n) =>
          n.id === oldId
            ? { ...n, id: newId, data: { ...n.data, nodeId: newId, label: newId } }
            : n
        )
      );
      setEdges((eds) =>
        eds.map((e) => ({
          ...e,
          id: `e-${e.source === oldId ? newId : e.source}-${e.data?.branch || 'default'}-${
            e.target === oldId ? newId : e.target
          }`,
          source: e.source === oldId ? newId : e.source,
          target: e.target === oldId ? newId : e.target,
        }))
      );
      const newEntry = entryNode === oldId ? newId : entryNode;
      setEntryNode(newEntry);
      if (selectedNodeId === oldId) setSelectedNodeId(newId);
      pushHistory(nodes, edges, newEntry);
    },
    [nodes, edges, entryNode, selectedNodeId, pushHistory, setNodes, setEdges]
  );

  const handleSetEntryNode = useCallback(
    (newEntryId: string) => {
      setEntryNode(newEntryId);
      setNodes((nds) => {
        const updated = nds.map((n) => ({
          ...n,
          data: {
            ...n.data,
            isEntry: n.id === newEntryId,
          },
        }));
        pushHistory(updated, edges, newEntryId);
        return updated;
      });
    },
    [edges, pushHistory, setNodes]
  );

  const handleSetDefaultTransition = useCallback(
    (sourceNodeId: string, targetNodeId: string | null) => {
      setEdges((eds) => {
        // Remove existing default transition from this source
        const withoutOld = eds.filter(
          (e) => !(e.source === sourceNodeId && (e.data?.isDefault || !e.data?.branch))
        );
        if (!targetNodeId) {
          pushHistory(nodes, withoutOld, entryNode);
          return withoutOld;
        }
        const newEdge: BuilderEdge = {
          id: `e-${sourceNodeId}-default-${targetNodeId}`,
          source: sourceNodeId,
          target: targetNodeId,
          sourceHandle: 'default',
          targetHandle: 'target',
          label: 'default',
          data: { isDefault: true },
        };
        const updated = [...withoutOld, newEdge];
        pushHistory(nodes, updated, entryNode);
        return updated;
      });
    },
    [nodes, entryNode, pushHistory, setEdges]
  );

  const handleSetBranchTransition = useCallback(
    (sourceNodeId: string, branch: string, targetNodeId: string | null) => {
      setEdges((eds) => {
        // Remove existing transition for this specific branch
        const withoutOld = eds.filter(
          (e) => !(e.source === sourceNodeId && e.data?.branch === branch)
        );
        if (!targetNodeId) {
          pushHistory(nodes, withoutOld, entryNode);
          return withoutOld;
        }
        const newEdge: BuilderEdge = {
          id: `e-${sourceNodeId}-${branch}-${targetNodeId}`,
          source: sourceNodeId,
          target: targetNodeId,
          sourceHandle: branch,
          targetHandle: 'target',
          label: branch,
          data: { branch },
          animated: true,
        };
        const updated = [...withoutOld, newEdge];
        pushHistory(nodes, updated, entryNode);
        return updated;
      });
    },
    [nodes, entryNode, pushHistory, setEdges]
  );

  // Authoritative Validation
  const runValidation = async (): Promise<boolean> => {
    setIsValidating(true);
    setValidationIssues([]);
    setValidationSuccess(false);
    let valid = false;
    try {
      const spec = graphToSpec({ nodes, edges, entryNode }, initialSpec);
      const res = await manager.client.workflows.validate(spec);
      if (res.valid) {
        setValidationSuccess(true);
        valid = true;
        // Clear errors on all nodes
        setNodes((nds) =>
          nds.map((n) => ({ ...n, data: { ...n.data, validationError: undefined } }))
        );
      } else {
        setValidationIssues(res.errors);
        // Annotate nodes with validation errors
        const errorByNode = new Map<string, string>();
        res.errors.forEach((e) => {
          if (e.node_id) errorByNode.set(e.node_id, `[${e.code}] ${e.message}`);
        });
        setNodes((nds) =>
          nds.map((n) => ({
            ...n,
            data: {
              ...n.data,
              validationError: errorByNode.get(n.id),
            },
          }))
        );
      }
    } catch (err: unknown) {
      setSaveError(formatApiError(err, t));
    } finally {
      setIsValidating(false);
    }
    return valid;
  };

  const handleValidate = async () => {
    await runValidation();
  };

  // Authoritative Save
  const handleSave = async () => {
    if (!isDirty || draftNotice || isSaving || isValidating) return;
    setSaveError(null);
    // Never send a spec the backend would reject: validate first and show errors inline.
    if (!(await runValidation())) {
      setSaveError(t('builder.validateFirst'));
      return;
    }
    setIsSaving(true);
    try {
      const spec = graphToSpec({ nodes, edges, entryNode }, initialSpec);
      await onSaveSpec(spec, revision);
      clearWorkflowDraft(connection.workspace, connection.principal, workflowId);
      setIsDirty(false);
      setSaveStatus('clean');
    } catch (err: unknown) {
      const isConflict =
        (err as any)?.statusCode === 409 ||
        (err as any)?.code === 'REVISION_CONFLICT' ||
        String(err).includes('409') ||
        String(err).toLowerCase().includes('conflict');
      if (isConflict) {
        setSaveStatus('conflict');
        setConflictModalOpen(true);
      } else {
        setSaveError(formatApiError(err, t));
      }
    } finally {
      setIsSaving(false);
    }
  };

  const handleApplyImportedSpec = (importedSpec: WorkflowSpec) => {
    const doc = specToGraph(importedSpec);
    setNodes(doc.nodes);
    setEdges(doc.edges);
    setEntryNode(doc.entryNode);
    pushHistory(doc.nodes, doc.edges, doc.entryNode);
    setIsDirty(true);
    setSaveStatus('dirty');
  };

  const handleSaveAsCopyConfirm = async (copyName: string) => {
    if (!onSaveAsCopy) return;
    const spec = graphToSpec({ nodes, edges, entryNode }, initialSpec);
    spec.name = copyName.trim();
    await onSaveAsCopy(copyName, spec);
    clearWorkflowDraft(connection.workspace, connection.principal, workflowId);
  };

  // Keyboard accessibility & hotkeys
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!active || document.querySelector('[role="dialog"]')) return;
      // Don't intercept when user is typing in form inputs or textareas
      const target = e.target as HTMLElement;
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT') {
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        handleSave();
      } else if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
        e.preventDefault();
        if (e.shiftKey) handleRedo();
        else handleUndo();
      } else if ((e.ctrlKey || e.metaKey) && e.key === 'y') {
        e.preventDefault();
        handleRedo();
      } else if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
        e.preventDefault();
        handleCopy();
      } else if ((e.ctrlKey || e.metaKey) && e.key === 'v') {
        e.preventDefault();
        handlePaste();
      } else if (e.key === 'Delete' || e.key === 'Backspace') {
        if (selectedNodeId) {
          e.preventDefault();
          handleDeleteNode(selectedNodeId);
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [active, selectedNodeId, handleUndo, handleRedo, handleCopy, handlePaste, handleDeleteNode, handleSave]);

  const selectedNode = nodes.find((n) => n.id === selectedNodeId) || null;

  const inspector = <NodeInspector
    selectedNode={selectedNode} allNodes={nodes} allEdges={edges} entryNode={entryNode}
    onUpdateNodeData={handleUpdateNodeData} onRenameNode={handleRenameNode}
    onSetEntryNode={handleSetEntryNode} onDeleteNode={handleDeleteNode}
    onSetDefaultTransition={handleSetDefaultTransition} onSetBranchTransition={handleSetBranchTransition}
    onClose={() => setInspectorOpen(false)} />;
  const feedback = <>
      {/* Validation status banners */}
      {validationSuccess && (
        <div role="status"
          style={{
            padding: '8px 16px',
            backgroundColor: 'var(--status-success-bg)',
            color: 'var(--status-success)',
            fontSize: 12,
            fontWeight: 500,
            borderBottom: '1px solid var(--border-subtle)',
          }}
        >
          <Icon name="check" /> {t('ui.authoritativeValidationPassedGraphStructureRoutesAndMappingsAreFullyValid')}
        </div>
      )}

      {validationIssues.length > 0 && (
        <div
          role="alert"
          data-testid="validation-issues"
          style={{
            padding: '8px 16px',
            backgroundColor: 'var(--status-danger-bg)',
            color: 'var(--status-danger)',
            fontSize: 12,
            borderBottom: '1px solid var(--border-subtle)',
          }}
        >
          <strong>{t('builder.issuesTitle')} ({validationIssues.length}):</strong>
          <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
            {validationIssues.map((v, i) => (
              <li key={i}>
                [{v.code}] {v.message} {v.node_id ? `(Node: ${v.node_id})` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}

      {saveError && (
        <div role="alert"
          style={{
            padding: '8px 16px',
            backgroundColor: 'var(--status-danger-bg)',
            color: 'var(--status-danger)',
            fontSize: 12,
            borderBottom: '1px solid var(--border-subtle)',
          }}
        >
          <strong>{t('ui.saveError')}</strong> {saveError}
        </div>
      )}

  </>;
  const actions = <>
    <span role="status" aria-label={t('ui.saveStatusBadge')} className="workflow-save-status">
      {saveStatus === 'conflict' ? t('ui.revisionConflict') : isSaving ? t('ui.saving') : isDirty ? t('ui.unsavedChanges') : t('workflowDesign.saved')}
    </span>
    <Button variant="ghost" size="sm" disabled={isSaving} loading={isValidating} onClick={handleValidate}>{t('ui.validate')}</Button>
    <Button variant={isDirty && primaryAllowed ? 'primary' : 'secondary'} size="sm" disabled={!isDirty || !!draftNotice || isValidating} loading={isSaving} onClick={handleSave} title={t('ui.saveCtrlS')}>{t('ui.save')}</Button>
    {onRunWorkflow && <Button variant={!isDirty && primaryAllowed && !draftNotice ? 'primary' : 'secondary'} size="sm" disabled={isSaving || isValidating} onClick={onRunWorkflow}>{t('ui.run')}</Button>}
    <ActionDisclosure label={t('workflowDesign.more')}>
      {moreActions}
      <Button variant="ghost" onClick={() => { setImportExportMode('export'); setImportExportOpen(true); }}>{t('workflowDesign.importExport')}</Button>
    </ActionDisclosure>
  </>;

  return (
    <div ref={editorRoot} style={{ display: 'flex', flexDirection: 'column', height: '100%', position: 'relative' }}>
      {/* Draft recovery banner */}
      {draftNotice && (
        <div
          role="alert"
          aria-label={t('ui.localDraftRecoveryBanner')}
          style={{
            padding: '8px 16px',
            backgroundColor: 'var(--status-warning-bg)',
            color: 'var(--text-primary)',
            borderBottom: '1px solid var(--status-warning)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            fontSize: 12,
            zIndex: 10,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span><Icon name="warning" /></span>
            <span>
              <strong>{t('ui.unsavedLocalDraft')}</strong> {t('ui.recoveredUnsavedLocalChangesFrom')}{' '}
              {new Date(draftNotice.savedAt).toLocaleTimeString(locale)}.
            </span>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button variant="secondary" size="sm" onClick={handleDiscardDraft}>
              {t('ui.discardDraft')}
            </Button>
            <Button variant="primary" size="sm" onClick={handleRestoreDraft}>
              {t('ui.restoreDraft')}
            </Button>
          </div>
        </div>
      )}

      {actionHost ? createPortal(actions, actionHost) : <div className="workflow-actions">{actions}</div>}
      <div className="workflow-canvas-toolbar">
        <ActionDisclosure label={t('ui.addStep')}>
          <Button variant="ghost" onClick={() => setDrawerOpen(true)}>{t('workflowDesign.standard')}</Button>
          <Button variant="ghost" onClick={() => handleAddComposite('parallel')} title={t('builder.addParallelHint')}>{t('builder.addParallel')}</Button>
          <Button variant="ghost" onClick={() => handleAddComposite('map')} title={t('builder.addMapHint')}>{t('builder.addMap')}</Button>
          <Button variant="ghost" onClick={() => handleAddComposite('subworkflow')} title={t('builder.addSubworkflowHint')}>{t('builder.addSubworkflow')}</Button>
        </ActionDisclosure>
        <Button variant="ghost" size="sm" onClick={handleUndo} disabled={!historyRef.current.canUndo()} title={t('ui.undoCtrlZ')}><Icon name="undo" />{t('ui.undo')}</Button>
        <Button variant="ghost" size="sm" onClick={handleRedo} disabled={!historyRef.current.canRedo()} title={t('ui.redoCtrlY')}><Icon name="redo" />{t('ui.redo')}</Button>
        <ActionDisclosure label={t('workflowDesign.edit')}>
          <Button variant="ghost" onClick={handleCopy} disabled={!selectedNodeId}>{t('ui.copy')}</Button>
          <Button variant="ghost" onClick={handlePaste}>{t('ui.paste')}</Button>
          {selectedNodeId && <Button variant="ghost" className="workflow-danger-action" onClick={() => handleDeleteNode(selectedNodeId)}>{t('ui.delete')}</Button>}
        </ActionDisclosure>
        {viewMode === 'graph' && <div className="workflow-viewport-controls">
          <Button variant="ghost" size="sm" onClick={() => flow.fitView({ padding: '40px' })}>{t('flowLabels.fitView')}</Button>
          <Button variant="ghost" size="sm" aria-label={t('flowLabels.zoomOut')} onClick={() => flow.zoomOut()}>−</Button>
          <Button variant="ghost" size="sm" aria-label={t('flowLabels.zoomIn')} onClick={() => flow.zoomIn()}>+</Button>
          <Button variant="ghost" size="sm" aria-pressed={showMinimap} onClick={() => setShowMinimap(!showMinimap)}>{t('workflowDesign.minimap')}</Button>
          <Button variant="ghost" size="sm" aria-label={t('flowLabels.interactive')} aria-pressed={!interactive} onClick={() => setInteractive(!interactive)}><Icon name="lock" /></Button>
        </div>}
      </div>

      {feedbackHost ? createPortal(feedback, feedbackHost) : feedback}

      {/* Main Canvas Area */}
      <div className="workflow-editor-panes" style={{ display: 'flex', flex: 1, position: 'relative', overflow: 'hidden' }}>
        <div className="workflow-editor-main" onKeyDownCapture={(event) => {
          if (event.key !== 'Enter') return;
          const target = event.target as HTMLElement;
          const id = target.closest('[data-testid^="node-"]')?.getAttribute('data-testid')?.slice(5)
            || target.closest('.react-flow__node')?.getAttribute('data-id');
          if (id && nodes.some((node) => node.id === id)) { event.preventDefault(); setSelectedNodeId(id); setInspectorOpen(true); }
        }} style={{ flex: 1, height: '100%', position: 'relative' }}>
          {viewMode === 'nodes' ? <div role="region" aria-label={t('ui.accessibleNodesList')} data-testid="accessible-nodes-list" className="workflow-node-list">
            {nodes.map((node) => <div key={node.id} data-testid={`accessible-node-${node.id}`}>
              <Button variant="ghost" aria-label={t('workflowDesign.editNode', { name: node.id })} onClick={() => { setSelectedNodeId(node.id); setInspectorOpen(true); }}>{node.id}</Button>
              <code>{node.data.handler || node.data.module_id}</code>
              {node.id === entryNode && <span>{t('ui.entryNode')}</span>}
              {node.data.requires_approval && <span>{t('workflowDesign.needsApproval')}</span>}
              {edges.filter((edge) => edge.source === node.id).map((edge) => <span key={edge.id}>{String(edge.label || 'default')} → {edge.target}</span>)}
            </div>)}
          </div> : active ? <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            nodesDraggable={interactive}
            nodesConnectable={interactive}
            elementsSelectable={interactive}
            onNodesChange={(changes) => {
              onNodesChange(changes);
              // Measurement and selection are presentation state. Layout edits are saved.
              const changed = changes.some((change) => change.type === 'position' && change.position || change.type === 'remove');
              if (changed) setIsDirty(true);
              if (changes.some((change) => change.type === 'position' && !change.dragging || change.type === 'remove')) {
                pushHistory(applyNodeChanges(changes, nodes), edges, entryNode);
              }
            }}
            onEdgesChange={(changes) => {
              onEdgesChange(changes);
              if (changes.some((change) => change.type === 'remove')) {
                pushHistory(nodes, applyEdgeChanges(changes, edges), entryNode);
              }
            }}
            onConnect={onConnect}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            fitView
            fitViewOptions={{ padding: '40px' }}
            defaultEdgeOptions={{ style: { stroke: 'var(--border-strong)', strokeWidth: 1.5 }, labelStyle: { fill: 'var(--text-primary)', fontSize: 12 }, labelBgStyle: { fill: 'var(--bg-canvas)' } }}
            aria-label={t('builder.canvasLabel', { id: workflowId })}
            ariaLabelConfig={{
              'node.a11yDescription.default': t('flowLabels.nodeDescription'),
              'node.a11yDescription.keyboardDisabled': t('flowLabels.nodeDescriptionKeyboardDisabled'),
              'node.a11yDescription.ariaLiveMessage': ({ direction, x, y }) => t('flowLabels.moved', {
                direction: t(`flowLabels.direction.${direction}`, direction), x, y,
              }),
              'edge.a11yDescription.default': t('flowLabels.edgeDescription'),
              'controls.ariaLabel': t('flowLabels.controls'),
              'controls.zoomIn.ariaLabel': t('flowLabels.zoomIn'),
              'controls.zoomOut.ariaLabel': t('flowLabels.zoomOut'),
              'controls.fitView.ariaLabel': t('flowLabels.fitView'),
              'controls.interactive.ariaLabel': t('flowLabels.interactive'),
              'minimap.ariaLabel': t('flowLabels.minimap'),
              'handle.ariaLabel': t('flowLabels.handle'),
            }}
          >
            <Background color="var(--border-subtle)" gap={24} size={1} />
            {showMinimap && <MiniMap
              nodeColor={() => 'var(--border-strong)'}
              style={{
                backgroundColor: 'var(--bg-surface)',
                border: '1px solid var(--border-subtle)',
                borderRadius: 'var(--radius-sm)',
              }}
            />}
          </ReactFlow> : null}

          {/* Module Palette Popover */}
          <ModulePalette
            open={drawerOpen}
            onClose={() => setDrawerOpen(false)}
            onSelectModule={handleSelectModule}
          />
        </div>

        {wide && inspectorOpen && inspector}
      </div>
      {!wide && <Modal open={active && inspectorOpen && !!selectedNode} onClose={() => setInspectorOpen(false)} title={t('workflowDesign.editNode', { name: selectedNode?.id || '' })} variant="drawer" maxWidth={320}
        returnFocusTo={() => editorRoot.current?.querySelector<HTMLButtonElement>('.workflow-canvas-toolbar .studio-disclosure-trigger') ?? null}>
        {inspector}
      </Modal>}

      {/* CAS Revision Conflict Recovery Modal */}
      <ConflictModal
        open={conflictModalOpen}
        workflowName={workflowName || 'Workflow'}
        expectedRevision={revision}
        localSpec={graphToSpec({ nodes, edges, entryNode }, initialSpec)}
        onClose={() => setConflictModalOpen(false)}
        onReloadServer={() => {
          clearWorkflowDraft(connection.workspace, connection.principal, workflowId);
          if (onReloadWorkflow) onReloadWorkflow();
        }}
        onSaveAsCopy={handleSaveAsCopyConfirm}
      />

      {/* Versioned JSON Import & Export Modal */}
      <ImportExportModal
        open={importExportOpen}
        initialMode={importExportMode}
        workflowId={workflowId}
        workflowName={workflowName || 'Workflow'}
        currentSpec={graphToSpec({ nodes, edges, entryNode }, initialSpec)}
        onClose={() => setImportExportOpen(false)}
        onImportedPython={(record) => {
          setImportExportOpen(false);
          if (onImportedPython) onImportedPython(record);
          else window.location.hash = `#/workflows/${record.workflow_id}`;
        }}
        onApplyImportedSpec={handleApplyImportedSpec}
        onCreateFromImport={async (name, spec) => {
          if (onSaveAsCopy) {
            await onSaveAsCopy(name, spec);
          } else {
            const created = await manager.client.workflows.create(spec, { name });
            clearWorkflowDraft(connection.workspace, connection.principal, workflowId);
            window.location.hash = `#/workflows/${created.workflow_id}`;
          }
        }}
      />
    </div>
  );
}

export function WorkflowCanvas(props: WorkflowCanvasProps) {
  return (
    <ReactFlowProvider>
      <WorkflowCanvasInner {...props} />
    </ReactFlowProvider>
  );
}
