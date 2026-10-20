// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { WorkflowSpec } from '@liceron/nexura-sdk';
import { englishT, type Translate } from '../../i18n';
import { deepScanForSecrets } from './persistence';
import { formatApiError } from '../../api/errors';

export const CURRENT_EXPORT_SCHEMA_VERSION = 1;
export const MAX_IMPORT_SIZE_BYTES = 5 * 1024 * 1024; // 5MB

export interface VersionedWorkflowExport {
  format?: string;
  schema_version: number;
  spec_version: number;
  generator: string;
  exported_at: string;
  metadata?: {
    node_count: number;
    has_approval_gates: boolean;
  };
  workflow: {
    id?: string;
    name: string;
    description?: string;
    spec: WorkflowSpec;
  };
}

export interface ImportValidationResult {
  valid: boolean;
  spec?: WorkflowSpec;
  workflowName?: string;
  name?: string;
  errors: string[];
  warnings: string[];
  hasSecrets: boolean;
  migrated: boolean;
}

/**
 * Serializes a workflow and its specification into versioned, portable JSON.
 * Preserves presentation coordinates and viewport without altering executable semantics.
 */
export function exportWorkflowToJson(
  workflowId: string,
  name: string,
  spec: WorkflowSpec,
  description?: string
): {
  json: string;
  hasSecrets: boolean;
  secretDetected: boolean;
  data: VersionedWorkflowExport;
} {
  const hasSecrets = deepScanForSecrets(spec);

  const payload: VersionedWorkflowExport = {
    format: 'nexura-workflow-export',
    schema_version: CURRENT_EXPORT_SCHEMA_VERSION,
    spec_version: spec.spec_version || 1,
    generator: '@nexura/studio-web',
    exported_at: new Date().toISOString(),
    metadata: {
      node_count: Object.keys(spec.nodes || {}).length,
      has_approval_gates: Object.values(spec.nodes || {}).some((n) => Boolean(n.requires_approval)),
    },
    workflow: {
      id: workflowId,
      name: name || 'Untitled Workflow',
      description: description || '',
      spec,
    },
  };

  return {
    json: JSON.stringify(payload, null, 2),
    data: payload,
    hasSecrets,
    secretDetected: hasSecrets,
  };
}

/**
 * Validates, migrates, and parses an imported workflow JSON payload.
 * Enforces:
 * 1. File size limit to protect against denial-of-service / browser crash.
 * 2. Strict JSON syntax validation.
 * 3. Schema validation on essential graph structure.
 * 4. Graceful migration from legacy specs without dropping custom fields.
 * 5. Secret detection warning.
 */
export function validateAndParseWorkflowImport(
  rawText: string,
  maxSizeBytes: number = MAX_IMPORT_SIZE_BYTES,
  t: Translate = englishT,
): ImportValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];

  // 1. Size guard
  if (!rawText || rawText.trim().length === 0) {
    return {
      valid: false,
      errors: [t('validationMessages.importEmpty')],
      warnings: [],
      hasSecrets: false,
      migrated: false,
    };
  }

  const byteLength = new Blob([rawText]).size;
  if (byteLength > maxSizeBytes) {
    return {
      valid: false,
      errors: [
        t('validationMessages.importSize').replace('{v1}', String(Math.round(
          maxSizeBytes / 1024 / 1024
        ))).replace('{v2}', String((byteLength / 1024 / 1024).toFixed(2))),
      ],
      warnings: [],
      hasSecrets: false,
      migrated: false,
    };
  }

  // 2. Syntax parse
  let parsed: any;
  try {
    parsed = JSON.parse(rawText);
  } catch (err: unknown) {
    return {
      valid: false,
      errors: [t('validationMessages.jsonSyntax').replace('{v1}', String(formatApiError(err, t)))],
      warnings: [],
      hasSecrets: false,
      migrated: false,
    };
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return {
      valid: false,
      errors: [t('validationMessages.importObject')],
      warnings: [],
      hasSecrets: false,
      migrated: false,
    };
  }

  // 3. Extract spec and metadata
  let rawSpec: any;
  let workflowName = 'Imported Workflow';
  let migrated = false;

  if (parsed.schema_version !== undefined && parsed.workflow?.spec) {
    // Standard versioned studio export format
    rawSpec = parsed.workflow.spec;
    if (parsed.workflow.name) {
      workflowName = String(parsed.workflow.name);
    }
    if (parsed.schema_version > CURRENT_EXPORT_SCHEMA_VERSION) {
      warnings.push(
        t('validationMessages.importNewerVersion').replace('{v1}', String(parsed.schema_version)).replace('{v2}', String(CURRENT_EXPORT_SCHEMA_VERSION))
      );
    }
  } else if (parsed.spec && typeof parsed.spec === 'object') {
    // Raw API envelope format: { spec: { ... }, name: "..." }
    rawSpec = parsed.spec;
    if (parsed.name) workflowName = String(parsed.name);
    migrated = true;
    warnings.push(t('validationMessages.importLegacyMigrated'));
  } else if (parsed.nodes && typeof parsed.nodes === 'object') {
    // Direct WorkflowSpec root format: { nodes: { ... }, entry_node: "..." }
    rawSpec = parsed;
    if (parsed.name) workflowName = String(parsed.name);
    migrated = true;
    warnings.push(t('validationMessages.importLegacyMigrated'));
  } else {
    return {
      valid: false,
      errors: [
        t('validationMessages.importFormat'),
      ],
      warnings: [],
      hasSecrets: false,
      migrated: false,
    };
  }

  // 4. Validate graph nodes and structure
  if (!rawSpec.nodes || typeof rawSpec.nodes !== 'object' || Array.isArray(rawSpec.nodes)) {
    errors.push(t('validationMessages.importNodes'));
  }

  const nodeCount = Object.keys(rawSpec.nodes || {}).length;
  if (nodeCount === 0) {
    warnings.push(t('validationMessages.importEmptyGraph'));
  }

  // Migration: Normalize node definitions and ensure spec_version
  const migratedNodes: Record<string, any> = {};
  for (const [nodeId, rawNode] of Object.entries(rawSpec.nodes || {})) {
    if (typeof rawNode !== 'object' || rawNode === null || Array.isArray(rawNode)) {
      errors.push(t('validationMessages.importMalformedNode').replace('{v1}', String(nodeId)));
      continue;
    }
    const nodeObj = rawNode as Record<string, any>;

    // Ensure required minimum node attributes
    const handler = nodeObj.handler || nodeObj.module_id || nodeObj.type;
    if (!handler) {
      errors.push(t('validationMessages.importMissingHandler').replace('{v1}', String(nodeId)));
    }

    // Preserve all existing custom fields while ensuring standard contract
    migratedNodes[nodeId] = {
      ...nodeObj,
      id: nodeId,
      handler: String(handler || 'unknown'),
      module_id: nodeObj.module_id || handler,
      module_version: nodeObj.module_version || '1.0.0',
      config: nodeObj.config && typeof nodeObj.config === 'object' ? nodeObj.config : {},
      input_mapping:
        nodeObj.input_mapping && typeof nodeObj.input_mapping === 'object'
          ? nodeObj.input_mapping
          : {},
      output_mapping:
        nodeObj.output_mapping && typeof nodeObj.output_mapping === 'object'
          ? nodeObj.output_mapping
          : {},
      transitions:
        nodeObj.transitions && typeof nodeObj.transitions === 'object'
          ? nodeObj.transitions
          : {},
      default_transition:
        nodeObj.default_transition !== undefined ? nodeObj.default_transition : null,
      requires_approval: Boolean(nodeObj.requires_approval),
      // Preserve presentation layout if present
      presentation: nodeObj.presentation || undefined,
    };
  }

  // Validate entry node
  let entryNode = rawSpec.entry_node;
  const nodeKeys = Object.keys(migratedNodes);
  if (!entryNode && nodeKeys.length > 0) {
    entryNode = nodeKeys[0];
    migrated = true;
    warnings.push(t('validationMessages.importMissingEntry').replace('{v1}', String(entryNode)));
  } else if (entryNode && !migratedNodes[entryNode] && nodeKeys.length > 0) {
    const fallback = nodeKeys[0];
    warnings.push(
      t('validationMessages.importInvalidEntry').replace('{v1}', String(entryNode)).replace('{v2}', String(fallback))
    );
    entryNode = fallback;
    migrated = true;
  }

  if (rawSpec.spec_version === undefined) {
    migrated = true;
    warnings.push(t('validationMessages.importLegacyVersion'));
  }

  const finalSpec: WorkflowSpec = {
    ...rawSpec,
    spec_version: rawSpec.spec_version || 1,
    entry_node: entryNode || '',
    nodes: migratedNodes,
    presentation: rawSpec.presentation || undefined,
  };

  // 5. Secret check
  const hasSecrets = deepScanForSecrets(finalSpec);
  if (hasSecrets) {
    warnings.push(
      t('validationMessages.importSecrets')
    );
  }

  return {
    valid: errors.length === 0,
    spec: errors.length === 0 ? finalSpec : undefined,
    workflowName,
    name: workflowName,
    errors,
    warnings,
    hasSecrets,
    migrated,
  };
}
