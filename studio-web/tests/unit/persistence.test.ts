import { describe, it, expect, beforeEach } from 'vitest';
import {
  deepScanForSecrets,
  getDraftStorageKey,
  saveWorkflowDraft,
  loadWorkflowDraft,
  clearWorkflowDraft,
  hasWorkflowDraft,
} from '../../src/features/workflows/persistence';
import {
  exportWorkflowToJson,
  validateAndParseWorkflowImport,
} from '../../src/features/workflows/codecs';
import type { WorkflowSpec } from '@liceron/nexura-sdk';

describe('Workflow Draft Persistence (F06)', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  const sampleSpec: WorkflowSpec = {
    name: 'Order Routing Workflow',
    entry_node: 'validate_order',
    nodes: {
      validate_order: {
        id: 'validate_order',
        module_id: 'sample.validator',
        handler: 'execute',
        default_transition: 'dispatch_order',
      },
      dispatch_order: {
        id: 'dispatch_order',
        module_id: 'sample.dispatcher',
        handler: 'execute',
      },
    },
  };

  it('generates strictly isolated draft storage keys by workspace and principal', () => {
    const key1 = getDraftStorageKey('workspace-a', 'user-1', 'wf-123');
    const key2 = getDraftStorageKey('workspace-b', 'user-1', 'wf-123');
    const key3 = getDraftStorageKey('workspace-a', 'user-2', 'wf-123');

    expect(key1).toBe('nexura_draft_workspace-a_user-1_wf-123');
    expect(key2).toBe('nexura_draft_workspace-b_user-1_wf-123');
    expect(key3).toBe('nexura_draft_workspace-a_user-2_wf-123');
    expect(key1).not.toBe(key2);
    expect(key1).not.toBe(key3);
  });

  it('saves and reloads drafts correctly within matching tenant and principal', () => {
    const res = saveWorkflowDraft('ws-prod', 'alice', 'wf-456', sampleSpec, 1);
    expect(res.saved).toBe(true);
    expect(res.secretDetected).toBe(false);

    expect(hasWorkflowDraft('ws-prod', 'alice', 'wf-456')).toBe(true);

    const loaded = loadWorkflowDraft('ws-prod', 'alice', 'wf-456');
    expect(loaded).not.toBeNull();
    expect(loaded?.workflowId).toBe('wf-456');
    expect(loaded?.workspace).toBe('ws-prod');
    expect(loaded?.principal).toBe('alice');
    expect(loaded?.serverRevision).toBe(1);
    expect(loaded?.spec.entry_node).toBe('validate_order');
    expect(Object.keys(loaded?.spec.nodes || {})).toHaveLength(2);
  });

  it('enforces strict boundary: does not leak drafts across different workspaces or principals', () => {
    saveWorkflowDraft('ws-prod', 'alice', 'wf-456', sampleSpec, 1);

    // Different workspace -> null
    expect(loadWorkflowDraft('ws-staging', 'alice', 'wf-456')).toBeNull();
    // Different principal -> null
    expect(loadWorkflowDraft('ws-prod', 'bob', 'wf-456')).toBeNull();
    // Different workflowId -> null
    expect(loadWorkflowDraft('ws-prod', 'alice', 'wf-999')).toBeNull();
  });

  it('refuses to persist draft to localStorage if specification contains raw secrets', () => {
    const specWithKey: WorkflowSpec = {
      ...sampleSpec,
      nodes: {
        ...sampleSpec.nodes,
        ai_step: {
          id: 'ai_step',
          module_id: 'llm.openai',
          handler: 'execute',
          config: {
            api_key: 'sk-proj-1234567890abcdef1234567890abcdef1234567890',
          },
        },
      },
    };

    const res = saveWorkflowDraft('ws-prod', 'alice', 'wf-secret', specWithKey, 1);
    expect(res.saved).toBe(false);
    expect(res.secretDetected).toBe(true);
    expect(res.error).toContain('raw secret');

    // Verify nothing was saved to localStorage
    expect(hasWorkflowDraft('ws-prod', 'alice', 'wf-secret')).toBe(false);
  });

  it('purges existing draft when cleared', () => {
    saveWorkflowDraft('ws-prod', 'alice', 'wf-clean', sampleSpec, 2);
    expect(hasWorkflowDraft('ws-prod', 'alice', 'wf-clean')).toBe(true);

    clearWorkflowDraft('ws-prod', 'alice', 'wf-clean');
    expect(hasWorkflowDraft('ws-prod', 'alice', 'wf-clean')).toBe(false);
    expect(loadWorkflowDraft('ws-prod', 'alice', 'wf-clean')).toBeNull();
  });

  it('scans nested objects and arrays for secret detection', () => {
    expect(deepScanForSecrets('sk-ant-api03-1234567890abcdef1234567890abcdef')).toBe(true);
    expect(deepScanForSecrets({ key: 'ghp_1234567890abcdef1234567890abcdef' })).toBe(true);
    expect(deepScanForSecrets(['safe', { token: 'gho_1234567890abcdef1234567890abcdef' }])).toBe(true);
    expect(deepScanForSecrets({ config: { model: 'gpt-4o', temp: 0.7 } })).toBe(false);
  });
});

describe('Workflow Codecs: Import / Export (F06)', () => {
  const sampleSpec: WorkflowSpec = {
    name: 'Dispatch Workflow',
    entry_node: 'start',
    nodes: {
      start: {
        id: 'start',
        module_id: 'sample.start',
        handler: 'execute',
        default_transition: 'finish',
      },
      finish: {
        id: 'finish',
        module_id: 'sample.finish',
        handler: 'execute',
      },
    },
  };

  it('exports workflow to versioned JSON envelope preserving spec and layout', () => {
    const exported = exportWorkflowToJson('wf-xyz', 'My Exported Workflow', sampleSpec);
    expect(exported.secretDetected).toBe(false);
    expect(exported.data.format).toBe('nexura-workflow-export');
    expect(exported.data.schema_version).toBe(1);
    expect(exported.data.workflow.name).toBe('My Exported Workflow');
    expect(exported.data.workflow.spec.entry_node).toBe('start');
    expect(exported.data.metadata?.node_count).toBe(2);
    expect(typeof exported.json).toBe('string');
  });

  it('detects potential raw secrets during export and sets warning flag', () => {
    const specWithSecret: WorkflowSpec = {
      ...sampleSpec,
      nodes: {
        ...sampleSpec.nodes,
        ai: {
          id: 'ai',
          module_id: 'llm.anthropic',
          handler: 'call',
          config: {
            auth: 'sk-ant-api03-99999999999999999999999999999999999999999999999',
          },
        },
      },
    };

    const exported = exportWorkflowToJson('wf-sec', 'Secret Export', specWithSecret);
    expect(exported.secretDetected).toBe(true);
  });

  it('validates and parses a valid exported JSON envelope', () => {
    const exported = exportWorkflowToJson('wf-valid', 'Valid Export', sampleSpec);
    const parsed = validateAndParseWorkflowImport(exported.json);

    expect(parsed.valid).toBe(true);
    expect(parsed.errors).toHaveLength(0);
    expect(parsed.name).toBe('Valid Export');
    expect(parsed.spec?.entry_node).toBe('start');
    expect(Object.keys(parsed.spec?.nodes || {})).toHaveLength(2);
  });

  it('rejects oversized JSON exceeding 5MB ceiling', () => {
    // Generate string exceeding 5MB
    const bigString = 'x'.repeat(5 * 1024 * 1024 + 100);
    const result = validateAndParseWorkflowImport(bigString);

    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes('5MB'))).toBe(true);
  });

  it('rejects malformed syntax JSON', () => {
    const badJson = '{"format": "nexura-workflow-export", invalid';
    const result = validateAndParseWorkflowImport(badJson);

    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes('JSON syntax error'))).toBe(true);
  });

  it('migrates raw WorkflowSpec root format with migration warning', () => {
    // Direct WorkflowSpec root (legacy format)
    const rawSpecJson = JSON.stringify(sampleSpec, null, 2);
    const result = validateAndParseWorkflowImport(rawSpecJson);

    expect(result.valid).toBe(true);
    expect(result.migrated).toBe(true);
    expect(result.warnings.some((w) => w.includes('Migrated legacy spec format'))).toBe(true);
    expect(result.spec?.entry_node).toBe('start');
  });

  it('rejects JSON missing required nodes dictionary', () => {
    const missingNodesJson = JSON.stringify({
      format: 'nexura-workflow-export',
      schema_version: '1.0.0',
      workflow: {
        name: 'Empty Workflow',
        spec: {
          entry_node: 'test',
        },
      },
    });

    const result = validateAndParseWorkflowImport(missingNodesJson);
    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes('nodes'))).toBe(true);
  });
});
