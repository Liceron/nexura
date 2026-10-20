// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
export interface JsonSchemaProperty {
  type?: 'string' | 'number' | 'integer' | 'boolean' | 'object' | 'array' | string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: (string | number | boolean)[];
  properties?: Record<string, JsonSchemaProperty>;
  items?: JsonSchemaProperty;
  required?: string[];
  minimum?: number;
  maximum?: number;
  format?: string;
  additionalProperties?: boolean;
}

export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
  additionalProperties?: boolean;
}

export type ModuleCategory =
  | 'all'
  | 'data'
  | 'models'
  | 'knowledge'
  | 'channels'
  | 'connectors'
  | 'tools'
  | 'services'
  | 'synthetic';

export const MODULE_CATEGORIES: { id: ModuleCategory; label: string }[] = [
  { id: 'all', label: 'All Modules' },
  { id: 'data', label: 'Data & Transform' },
  { id: 'models', label: 'Models & AI' },
  { id: 'knowledge', label: 'Knowledge & RAG' },
  { id: 'channels', label: 'Channels & Comms' },
  { id: 'connectors', label: 'Connectors' },
  { id: 'tools', label: 'Tools & APIs' },
  { id: 'services', label: 'Governance & Services' },
  { id: 'synthetic', label: 'Local Mock / Legacy' },
];

/** The descriptor fields the palette categorises on (a subset of the SDK ModuleDescriptor). */
export interface PaletteModule {
  module_id: string;
  kind: string;
  internal?: boolean;
  display?: { group?: string | null } | null;
}

/**
 * Channels are integration modules with ids `nexura.channel.*`; no module declares a
 * "channel" kind.
 */
export function isChannelModule(mod: PaletteModule): boolean {
  return mod.module_id.startsWith('nexura.channel.');
}

/** The connectors pack: integration modules in the "connectors" display group (`nexura.connectors.*`). */
export function isConnectorModule(mod: PaletteModule): boolean {
  if (mod.kind !== 'integration') return false;
  return mod.display?.group === 'connectors' || mod.module_id.startsWith('nexura.connectors.');
}

/**
 * Whether the builder palette offers this module as a new node. Service-internal modules
 * (descriptor `internal: true`, set by the backend manifest) stay registered so saved
 * workflows keep running, but are not offered.
 */
export function isOfferedInPalette(mod: PaletteModule): boolean {
  return mod.internal !== true;
}

/**
 * Category membership, keyed on the module kinds the service reports (agent, approval_manager,
 * data_transform, governance, integration, knowledge, legacy_handler, model_adapter, service, tool).
 */
export function moduleMatchesCategory(mod: PaletteModule, category: ModuleCategory): boolean {
  switch (category) {
    case 'all':
      return true;
    case 'data':
      return mod.kind === 'data_transform';
    case 'models':
      return mod.kind === 'model_adapter' || mod.kind === 'agent';
    case 'knowledge':
      return mod.kind === 'knowledge';
    case 'channels':
      return isChannelModule(mod);
    case 'connectors':
      return isConnectorModule(mod);
    case 'tools':
      // Tools plus the remaining integrations (HTTP, SQL, speech, MCP tools).
      return (
        mod.kind === 'tool' ||
        (mod.kind === 'integration' && !isChannelModule(mod) && !isConnectorModule(mod))
      );
    case 'services':
      return mod.kind === 'governance' || mod.kind === 'service' || mod.kind === 'approval_manager';
    case 'synthetic':
      return mod.kind === 'legacy_handler' || mod.module_id.startsWith('synthetic_');
    default:
      return false;
  }
}

/**
 * Detects whether a string value resembles an API key or bearer secret.
 * Used to prevent accidental leakage into plain-text graph configs.
 */
export function isPotentialSecret(value: unknown): boolean {
  if (typeof value !== 'string') return false;
  const trimmed = value.trim();
  if (trimmed.length < 8) return false;

  // Patterns for OpenAI, Anthropic, Bearer tokens, GitHub, Slack tokens, AWS
  const secretPatterns = [
    /^sk-[a-zA-Z0-9_-]{20,}$/,
    /^Bearer\s+[a-zA-Z0-9._-]{20,}$/i,
    /^gh[pousr]_[a-zA-Z0-9]{20,}$/,
    /^xox[baprs]-[a-zA-Z0-9-]{10,}$/,
    /^AKIA[0-9A-Z]{16}$/,
    /^AIzaSy[a-zA-Z0-9_-]{33}$/,
  ];

  return secretPatterns.some((pattern) => pattern.test(trimmed));
}

/**
 * Formats a concise 1-2 line summary of a node's configuration for visual canvas nodes.
 */
export function formatConfigSummary(config: Record<string, unknown> | undefined): string | null {
  if (!config || Object.keys(config).length === 0) return null;

  const parts: string[] = [];
  for (const [key, val] of Object.entries(config)) {
    if (val === undefined || val === null || val === '') continue;
    if (typeof val === 'object') {
      const keys = Object.keys(val as Record<string, unknown>);
      if (keys.length > 0) parts.push(`${key}: {${keys.length}}`);
    } else {
      const strVal = String(val);
      const truncated = strVal.length > 15 ? `${strVal.substring(0, 15)}...` : strVal;
      parts.push(`${key}: ${truncated}`);
    }
    if (parts.length >= 2) break;
  }

  return parts.length > 0 ? parts.join(', ') : null;
}
