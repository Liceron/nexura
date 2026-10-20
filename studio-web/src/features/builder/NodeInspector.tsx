// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState, useEffect } from 'react';
import type { BuilderNode, BuilderEdge } from './types';
import { Button } from '../../components/Button';
import { Input } from '../../components/Input';
import { Badge } from '../../components/Badge';
import { useApi } from '../../api/context';
import type { ModuleDescriptor } from '@liceron/nexura-sdk';
import { SchemaForm } from '../modules/SchemaForm';
import { InputMappingForm } from '../modules/InputMappingForm';
import { CompositeInspector } from './CompositeInspector';
import { connectorOperations, mappingOperation, requiredCredentialTypes } from '../modules/connectors';
import { CredentialRefPicker, OperationSelect } from '../modules/ConnectorFields';
import { SkillsLabelSelect, SkillsPicker, acceptsSkills } from '../skills/SkillsPicker';
import { hasNoOwnHandler, isComposite, useBuilderT } from './composite';
import { AgentNodeConfig } from '../agents/AgentNodeConfig';
import { AGENT_NODE_MODULE_ID } from '../agents/agentForm';

interface NodeInspectorProps {
  selectedNode: BuilderNode | null;
  allNodes: BuilderNode[];
  allEdges: BuilderEdge[];
  entryNode: string;
  onUpdateNodeData: (nodeId: string, updates: Partial<BuilderNode['data']>) => void;
  onRenameNode: (oldId: string, newId: string) => void;
  onSetEntryNode: (nodeId: string) => void;
  onDeleteNode: (nodeId: string) => void;
  onSetDefaultTransition: (sourceNodeId: string, targetNodeId: string | null) => void;
  onSetBranchTransition: (sourceNodeId: string, branch: string, targetNodeId: string | null) => void;
  onClose: () => void;
}

export function NodeInspector({
  selectedNode,
  allNodes,
  allEdges,
  entryNode,
  onUpdateNodeData,
  onRenameNode,
  onSetEntryNode,
  onDeleteNode,
  onSetDefaultTransition,
  onSetBranchTransition,
  onClose,
}: NodeInspectorProps) {
  const { manager } = useApi();
  const t = useBuilderT();

  if (!selectedNode) {
    return (
      <div className="studio-split-secondary" style={{ width: 'min(340px, 100%)', padding: 16, color: 'var(--text-secondary)', fontSize: 13 }}>
        {t('ui.selectANodeOnTheCanvasToInspectAndConfigureProperties')}
      </div>
    );
  }

  const { id, data } = selectedNode;
  const isEntry = id === entryNode;

  const [editId, setEditId] = useState(id);
  const [activeTab, setActiveTab] = useState<'config' | 'inputs' | 'transitions' | 'properties' | 'composite'>(
    isComposite(selectedNode?.data ?? {}) ? 'composite' : 'config'
  );
  const [descriptor, setDescriptor] = useState<ModuleDescriptor | null>(null);
  const [loadingDesc, setLoadingDesc] = useState(false);
  const [versionWarning, setVersionWarning] = useState<string | null>(null);

  const [newBranchName, setNewBranchName] = useState('');
  const [newBranchTarget, setNewBranchTarget] = useState('');

  // Reset state when selected node changes
  useEffect(() => {
    setEditId(id);
    setVersionWarning(null);
    if (isComposite(selectedNode?.data ?? {})) setActiveTab('composite');
  }, [id]);

  // Keep the visible tab valid when a step changes kind (e.g. becomes a plain step again).
  const compositeNow = isComposite(data);
  const noHandlerNow = hasNoOwnHandler(data);
  useEffect(() => {
    if (!compositeNow && activeTab === 'composite') setActiveTab('config');
    else if (noHandlerNow && (activeTab === 'config' || activeTab === 'inputs')) setActiveTab('composite');
  }, [compositeNow, noHandlerNow, activeTab]);

  // Fetch descriptor for selected node's handler or module_id
  useEffect(() => {
    const moduleId = data.module_id || data.handler;
    if (!moduleId) {
      setDescriptor(null);
      return;
    }

    let isMounted = true;
    setLoadingDesc(true);
    manager.client.modules
      .get(moduleId, data.module_version)
      .then((desc) => {
        if (!isMounted) return;
        setDescriptor(desc);
      })
      .catch(() => {
        if (!isMounted) return;
        setDescriptor(null);
      })
      .finally(() => {
        if (isMounted) setLoadingDesc(false);
      });

    return () => {
      isMounted = false;
    };
  }, [data.module_id, data.handler, data.module_version, manager]);

  const credTypes = requiredCredentialTypes(descriptor);
  const connectorOps = descriptor?.kind === 'integration' ? connectorOperations(descriptor) : [];

  // Outgoing transitions
  const outgoingDefaultEdge = allEdges.find(
    (e) => e.source === id && (e.data?.isDefault || (!e.data?.branch && e.label === 'default'))
  );
  const defaultTarget = outgoingDefaultEdge?.target || '';

  const branchEdges = allEdges.filter(
    (e) => e.source === id && e.data?.branch && !e.data?.isDefault
  );

  const handleIdBlur = () => {
    const trimmed = editId.trim();
    if (trimmed && trimmed !== id) {
      onRenameNode(id, trimmed);
    }
  };

  const handleVersionChange = (newVer: string) => {
    if (newVer !== data.module_version) {
      setVersionWarning(
        t('inspector.versionChanged', { version: newVer })
      );
      onUpdateNodeData(id, { module_version: newVer });
    }
  };

  const handleAddBranch = (e: React.FormEvent) => {
    e.preventDefault();
    if (newBranchName.trim() && newBranchTarget.trim()) {
      onSetBranchTransition(id, newBranchName.trim(), newBranchTarget.trim());
      setNewBranchName('');
      setNewBranchTarget('');
    }
  };

  const availableTargets = allNodes.filter((n) => n.id !== id);

  return (
    <div
      aria-label={t('ui.nodePropertiesInspector')}
      className="studio-split-secondary"
      style={{
        width: 'min(360px, 100%)',
        backgroundColor: 'var(--bg-surface)',
        borderLeft: '1px solid var(--border-subtle)',
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflowY: 'auto',
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--border-subtle)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <h3 style={{ fontSize: 14, fontWeight: 600, margin: 0 }}>{t('ui.step') + " "}{id}</h3>
          {isEntry && <Badge variant="success">{t('ui.entry')}</Badge>}
        </div>
        <button
          onClick={onClose}
          style={{ fontSize: 18, color: 'var(--text-muted)', cursor: 'pointer', background: 'none', border: 'none' }}
          aria-label={t('ui.closeInspector')}
        >
          <Icon name="close" />
        </button>
      </div>

      {data.validationError && (
        <div
          role="alert"
          data-testid="inspector-validation-error"
          style={{
            margin: '8px 16px 0',
            padding: '6px 8px',
            fontSize: 12,
            color: 'var(--status-danger)',
            backgroundColor: 'var(--status-danger-bg)',
            borderRadius: 'var(--radius-sm)',
          }}
        >
          {data.validationError}
        </div>
      )}

      {/* Core Node Identifier & Fast Controls (Always accessible to keyboard & tests) */}
      <div style={{ padding: '12px 16px 8px 16px', borderBottom: '1px solid var(--border-subtle)' }}>
        <Input
          label={t('ui.nodeIdentifier')}
          value={editId}
          onChange={(e) => setEditId(e.target.value)}
          onBlur={handleIdBlur}
          helperText={t('inspector.identifierHelp')}
        />

        <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <label
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontSize: 12,
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            <input
              type="checkbox"
              checked={Boolean(data.requires_approval)}
              onChange={(e) => onUpdateNodeData(id, { requires_approval: e.target.checked })}
              aria-label={t('ui.requireHumanApprovalGate')}
            />
            <span>{t('ui.requireHumanApprovalGate')}</span>
          </label>
        </div>
      </div>

      {/* Inspector Tabs */}
      <div role="tablist"
        style={{
          display: 'flex',
          borderBottom: '1px solid var(--border-subtle)',
          backgroundColor: 'var(--bg-canvas)',
        }}
      >
        {isComposite(data) && (
          <button
            role="tab"
            aria-selected={activeTab === 'composite'}
            onClick={() => setActiveTab('composite')}
            style={{
              flex: 1,
              padding: '8px 4px',
              fontSize: 11,
              fontWeight: 600,
              border: 'none',
              borderBottom: activeTab === 'composite' ? '2px solid var(--brand-primary)' : '2px solid transparent',
              backgroundColor: 'transparent',
              color: activeTab === 'composite' ? 'var(--brand-primary)' : 'var(--text-secondary)',
              cursor: 'pointer',
            }}
          >
            {t('builder.tabComposite')}
          </button>
        )}
        {!hasNoOwnHandler(data) && (
        <button
          role="tab"
          aria-selected={activeTab === 'config'}
          onClick={() => setActiveTab('config')}
          style={{
            flex: 1,
            padding: '8px 4px',
            fontSize: 11,
            fontWeight: 600,
            border: 'none',
            borderBottom: activeTab === 'config' ? '2px solid var(--brand-primary)' : '2px solid transparent',
            backgroundColor: 'transparent',
            color: activeTab === 'config' ? 'var(--brand-primary)' : 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          {t('ui.config')}
        </button>
        )}
        {!hasNoOwnHandler(data) && (
        <button
          role="tab"
          aria-selected={activeTab === 'inputs'}
          onClick={() => setActiveTab('inputs')}
          style={{
            flex: 1,
            padding: '8px 4px',
            fontSize: 11,
            fontWeight: 600,
            border: 'none',
            borderBottom: activeTab === 'inputs' ? '2px solid var(--brand-primary)' : '2px solid transparent',
            backgroundColor: 'transparent',
            color: activeTab === 'inputs' ? 'var(--brand-primary)' : 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          {t('ui.inputs')}{Object.keys(data.input_mapping || {}).length})
        </button>
        )}
        <button
          role="tab"
          aria-selected={activeTab === 'transitions'}
          onClick={() => setActiveTab('transitions')}
          style={{
            flex: 1,
            padding: '8px 4px',
            fontSize: 11,
            fontWeight: 600,
            border: 'none',
            borderBottom: activeTab === 'transitions' ? '2px solid var(--brand-primary)' : '2px solid transparent',
            backgroundColor: 'transparent',
            color: activeTab === 'transitions' ? 'var(--brand-primary)' : 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          {t('ui.routes')}
        </button>
        <button
          role="tab"
          aria-selected={activeTab === 'properties'}
          onClick={() => setActiveTab('properties')}
          style={{
            flex: 1,
            padding: '8px 4px',
            fontSize: 11,
            fontWeight: 600,
            border: 'none',
            borderBottom: activeTab === 'properties' ? '2px solid var(--brand-primary)' : '2px solid transparent',
            backgroundColor: 'transparent',
            color: activeTab === 'properties' ? 'var(--brand-primary)' : 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          {t('ui.details')}
        </button>
      </div>

      {activeTab === 'composite' && isComposite(data) && (
        <CompositeInspector
          nodeId={id}
          data={data}
          onUpdate={(updates) => onUpdateNodeData(id, updates)}
        />
      )}

      {/* Tab 1: Schema-driven Configuration */}
      {activeTab === 'config' && !hasNoOwnHandler(data) && (
        <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {loadingDesc && (
            <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{t('ui.loadingModuleDescriptorSchema')}</div>
          )}

          {versionWarning && (
            <div
              style={{
                padding: '8px 10px',
                backgroundColor: 'var(--status-warning-bg)',
                color: 'var(--status-warning)',
                fontSize: 11,
                borderRadius: 'var(--radius-sm)',
              }}
            >
              {versionWarning}
            </div>
          )}

          {/* Module Availability & Metadata */}
          {descriptor && (
            <div
              style={{
                padding: '8px 10px',
                backgroundColor: 'var(--bg-canvas)',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                display: 'flex',
                flexDirection: 'column',
                gap: 4,
                fontSize: 11,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span style={{ fontWeight: 600 }}>{descriptor.title || descriptor.module_id}</span>
                <Badge variant={descriptor.is_available ? 'success' : 'danger'}>
                  {descriptor.is_available ? t('ui.available') : t('ui.missing')}
                </Badge>
              </div>
              <div style={{ color: 'var(--text-muted)' }}>
                {t('ui.policy') + " "}<code>{descriptor.effect_policy}</code> {t('ui.kind') + " "}<code>{descriptor.kind}</code>
              </div>
            </div>
          )}

          {/* Credential Reference Notice */}
          {descriptor?.required_credential_types && descriptor.required_credential_types.length > 0 && (
            <div
              style={{
                padding: '8px 10px',
                backgroundColor: 'var(--bg-canvas)',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                fontSize: 11,
              }}
            >
              <strong>{t('ui.requiredCredentialTypes')}</strong>{' '}
              {descriptor.required_credential_types.join(', ')}
              <div style={{ color: 'var(--text-secondary)', marginTop: 2 }}>
                {t('ui.boundSecurelyAtRuntimeNeverInputRawApiKeysOrSecretsInGraphFields')}
              </div>
            </div>
          )}

          {(data.module_id || data.handler) === AGENT_NODE_MODULE_ID ? (
            <AgentNodeConfig
              value={(data.config || {}) as Record<string, unknown>}
              onChange={(newCfg) => onUpdateNodeData(id, { config: newCfg })}
            />
          ) : (
          <SchemaForm
            schema={descriptor?.config_schema || { type: 'object' }}
            value={data.config || {}}
            onChange={(newCfg) => onUpdateNodeData(id, { config: newCfg })}
            title={t('ui.moduleParameters')}
            fieldOverrides={{
              ...(credTypes.length > 0 && 'credential_ref' in (descriptor?.config_schema?.properties || {})
                ? {
                    credential_ref: (v: unknown, set: (x: unknown) => void) => (
                      <CredentialRefPicker types={credTypes} value={typeof v === 'string' ? v : ''} onChange={set} />
                    ),
                  }
                : {}),
              ...(acceptsSkills(descriptor?.config_schema)
                ? {
                    skills: (v: unknown, set: (x: unknown) => void) => <SkillsPicker value={v} onChange={set} />,
                    skills_label: (v: unknown, set: (x: unknown) => void) => (
                      <SkillsLabelSelect value={v} onChange={set} />
                    ),
                  }
                : {}),
            }}
          />
          )}
        </div>
      )}

      {/* Tab 2: Input Port Mappings */}
      {activeTab === 'inputs' && !hasNoOwnHandler(data) && (
        <div style={{ padding: 16 }}>
          <InputMappingForm
            inputSchema={descriptor?.input_schema || { type: 'object' }}
            mapping={data.input_mapping || {}}
            portOverrides={
              connectorOps.length > 0
                ? {
                    operation: () => (
                      <OperationSelect
                        operations={connectorOps}
                        value={mappingOperation((data.input_mapping as Record<string, unknown> | undefined)?.operation)}
                        onChange={(op) => {
                          const next: Record<string, unknown> = { ...(data.input_mapping || {}) };
                          if (op === '') delete next.operation;
                          else next.operation = { $literal: op };
                          onUpdateNodeData(id, { input_mapping: next as Record<string, string> });
                        }}
                      />
                    ),
                  }
                : undefined
            }
            onChange={(newMap) => onUpdateNodeData(id, { input_mapping: newMap })}
          />
        </div>
      )}

      {/* Tab 3: Outgoing Transitions (Default + Named Branches) */}
      {activeTab === 'transitions' && (
        <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
          {/* Default Outgoing Transition */}
          <div>
            <label style={{ display: 'block', fontSize: 13, fontWeight: 500, marginBottom: 6 }}>
              {t('ui.defaultTransition')}
            </label>
            <select
              value={defaultTarget}
              onChange={(e) => {
                const val = e.target.value;
                onSetDefaultTransition(id, val === '' ? null : val);
              }}
              aria-label={t('ui.defaultTransitionTarget')}
              style={{
                width: '100%',
                padding: '6px 10px',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                backgroundColor: 'var(--bg-canvas)',
                color: 'var(--text-primary)',
                fontSize: 13,
              }}
            >
              <option value="">{t('ui.noneTerminal')}</option>
              {availableTargets.map((n) => (
                <option key={n.id} value={n.id}>
                  {t('ui.to')} {n.id} ({n.data.handler})
                </option>
              ))}
            </select>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
              {t('ui.unconditionalNextStepWhenHandlerReturnsWithoutCustomBranchRoute')}
            </div>
          </div>

          {/* Branch Transitions */}
          <div>
            <label style={{ display: 'block', fontSize: 13, fontWeight: 500, marginBottom: 6 }}>
              {t('ui.branchTransitions')}{branchEdges.length})
            </label>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}>
              {branchEdges.map((edge) => (
                <div
                  key={edge.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '6px 10px',
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-canvas)',
                    fontSize: 12,
                  }}
                >
                  <div>
                    <code>{edge.data?.branch}</code> <Icon name="right" /> <strong>{edge.target}</strong>
                  </div>
                  <button
                    type="button"
                    onClick={() => onSetBranchTransition(id, edge.data!.branch!, null)}
                    aria-label={t('inspector.removeBranch', { branch: edge.data?.branch ?? '' })}
                    style={{
                      color: 'var(--status-danger)',
                      cursor: 'pointer',
                      border: 'none',
                      background: 'none',
                    }}
                  >
                    <Icon name="close" />
                  </button>
                </div>
              ))}
            </div>

            <form
              onSubmit={handleAddBranch}
              style={{
                display: 'flex',
                gap: 6,
                padding: 8,
                backgroundColor: 'var(--bg-canvas)',
                borderRadius: 'var(--radius-sm)',
              }}
            >
              <input
                type="text"
                placeholder="branch_route"
                value={newBranchName}
                onChange={(e) => setNewBranchName(e.target.value)}
                aria-label={t('ui.branchRouteName')}
                style={{
                  flex: 1,
                  padding: '4px 8px',
                  borderRadius: 'var(--radius-sm)',
                  border: '1px solid var(--border-subtle)',
                  fontSize: 12,
                }}
              />
              <select
                value={newBranchTarget}
                onChange={(e) => setNewBranchTarget(e.target.value)}
                aria-label={t('ui.branchTargetNode')}
                style={{
                  flex: 1,
                  padding: '4px 8px',
                  borderRadius: 'var(--radius-sm)',
                  border: '1px solid var(--border-subtle)',
                  fontSize: 12,
                }}
              >
                <option value="">{t('ui.selectTarget')}</option>
                {availableTargets.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.id}
                  </option>
                ))}
              </select>
              <Button variant="secondary" size="sm" type="submit">
                {t('ui.add')}
              </Button>
            </form>
          </div>
        </div>
      )}

      {/* Tab 4: Step Properties & Details */}
      {activeTab === 'properties' && (
        <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
          {/* Handler / Module ID */}
          {!hasNoOwnHandler(data) && (
          <div>
            <Input
              label={t('ui.handlerModuleId')}
              value={data.handler || data.module_id || ''}
              onChange={(e) => onUpdateNodeData(id, { handler: e.target.value, module_id: e.target.value })}
              placeholder={t('ui.eGSyntheticOrderLookup')}
            />
          </div>

          )}

          {/* Module Version */}
          {!hasNoOwnHandler(data) && (
          <div>
            <label style={{ display: 'block', fontSize: 13, fontWeight: 500, marginBottom: 6 }}>
              {t('ui.moduleVersion')}
            </label>
            <select
              value={data.module_version || '1.0.0'}
              onChange={(e) => handleVersionChange(e.target.value)}
              aria-label={t('ui.moduleVersion2')}
              style={{
                width: '100%',
                padding: '6px 10px',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                backgroundColor: 'var(--bg-canvas)',
                color: 'var(--text-primary)',
                fontSize: 13,
              }}
            >
              <option value="1.0.0">{t('ui.100StandardStable')}</option>
              <option value="1.1.0">{t('ui.110MinorEnhancement')}</option>
              <option value="2.0.0">{t('ui.200MajorMigration')}</option>
            </select>
          </div>

          )}

          {/* Entry Node Action */}
          <div>
            <label style={{ display: 'block', fontSize: 13, fontWeight: 500, marginBottom: 6 }}>
              {t('ui.workflowEntry')}
            </label>
            {isEntry ? (
              <Badge variant="success">{t('ui.thisIsTheEntryNode')}</Badge>
            ) : (
              <Button variant="secondary" size="sm" onClick={() => onSetEntryNode(id)}>
                {t('ui.setAsEntryNode')}
              </Button>
            )}
          </div>
        </div>
      )}

      {/* Persistent Inspector Footer */}
      <div
        style={{
          marginTop: 'auto',
          padding: '12px 16px',
          borderTop: '1px solid var(--border-subtle)',
          backgroundColor: 'var(--bg-canvas)',
        }}
      >
        <Button variant="danger" size="sm" onClick={() => onDeleteNode(id)} style={{ width: '100%' }}>
          {t('ui.deleteNode')}
        </Button>
      </div>
    </div>
  );
}
