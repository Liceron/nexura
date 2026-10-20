// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type {
  IntegrationStatus,
  McpDiscoveredTool,
  McpTestRunResult,
  OpenAPIDiscoveredOperation,
  OpenAPITestRunResult,
} from '@liceron/nexura-sdk';

export type { IntegrationStatus };

export type IntegrationTab = 'mcp' | 'openapi';

export interface McpToolState {
  tools: McpDiscoveredTool[];
  serverName: string;
  selectedTool: McpDiscoveredTool | null;
  testArguments: Record<string, any>;
  lastResult: McpTestRunResult | null;
  isRunning: boolean;
  registeredMap: Record<string, boolean>;
}

export interface OpenAPIOperationState {
  operations: OpenAPIDiscoveredOperation[];
  prefix: string;
  selectedOperation: OpenAPIDiscoveredOperation | null;
  testInputs: Record<string, any>;
  lastResult: OpenAPITestRunResult | null;
  isRunning: boolean;
  registeredMap: Record<string, boolean>;
}
