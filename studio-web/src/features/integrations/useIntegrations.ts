// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useState, useEffect, useCallback } from 'react';
import { useLatestT } from '../../i18n';
import { useApi } from '../../api/context';
import type {
  IntegrationStatus,
  McpDiscoveredTool,
  McpTestRunResult,
  OpenAPIDiscoveredOperation,
  OpenAPITestRunResult,
} from '@liceron/nexura-sdk';
import { formatApiError } from '../../api/errors';

export function useIntegrations() {
  const t = useLatestT();
  const { manager, connection } = useApi();
  const client = manager.client;

  const [status, setStatus] = useState<IntegrationStatus | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // MCP State
  const [mcpServerName, setMcpServerName] = useState<string>('calc');
  const [mcpFixtureId, setMcpFixtureId] = useState<string>('math_fixture');
  const [mcpTools, setMcpTools] = useState<McpDiscoveredTool[]>([]);
  const [selectedMcpTool, setSelectedMcpTool] = useState<McpDiscoveredTool | null>(null);
  const [mcpArgsText, setMcpArgsText] = useState<string>('{\n  "x": 10,\n  "y": 25\n}');
  const [mcpRunResult, setMcpRunResult] = useState<McpTestRunResult | null>(null);
  const [mcpRunning, setMcpRunning] = useState<boolean>(false);
  const [mcpRegistered, setMcpRegistered] = useState<Record<string, boolean>>({});

  // OpenAPI State
  const [openapiPrefix, setOpenapiPrefix] = useState<string>('petstore');
  const [openapiFixtureId, setOpenapiFixtureId] = useState<string>('petstore_fixture');
  const [openapiSpecText, setOpenapiSpecText] = useState<string>('');
  const [openapiOperations, setOpenapiOperations] = useState<OpenAPIDiscoveredOperation[]>([]);
  const [selectedOpenAPIOp, setSelectedOpenAPIOp] = useState<OpenAPIDiscoveredOperation | null>(null);
  const [openapiInputsText, setOpenapiInputsText] = useState<string>('{\n  "petId": "123"\n}');
  const [openapiRunResult, setOpenapiRunResult] = useState<OpenAPITestRunResult | null>(null);
  const [openapiRunning, setOpenapiRunning] = useState<boolean>(false);
  const [openapiRegistered, setOpenapiRegistered] = useState<Record<string, boolean>>({});

  const refreshStatus = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await client.integrations.getStatus();
      setStatus(res);
    } catch (err: unknown) {
      setError(formatApiError(err, t));
    } finally {
      setLoading(false);
    }
  }, [client, t]);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus, connection.workspace]);

  // MCP Actions
  const discoverMcp = useCallback(
    async (fixtureId: string, serverName: string) => {
      try {
        setLoading(true);
        setError(null);
        setMcpRunResult(null);
        const res = await client.integrations.discoverMcp({
          fixture_id: fixtureId,
          server_name: serverName,
          transport_type: 'in_memory',
        });
        setMcpTools(res.tools);
        if (res.tools.length > 0) {
          setSelectedMcpTool(res.tools[0]);
          if (res.tools[0].name === 'math_add') {
            setMcpArgsText('{\n  "x": 15,\n  "y": 27\n}');
          } else if (res.tools[0].name === 'echo') {
            setMcpArgsText('{\n  "message": "Hello from Nexura Studio"\n}');
          }
        }
      } catch (err: unknown) {
        setError(formatApiError(err, t));
      } finally {
        setLoading(false);
      }
    },
    [client, t]
  );

  const testRunMcp = useCallback(
    async (toolName: string, argsJson: string) => {
      try {
        setMcpRunning(true);
        setError(null);
        let parsedArgs: Record<string, any> = {};
        try {
          parsedArgs = JSON.parse(argsJson);
        } catch {
          throw new Error(t('validationMessages.toolArgumentsJson'));
        }

        const res = await client.integrations.testRunMcp({
          server_name: mcpServerName,
          tool_name: toolName,
          arguments: parsedArgs,
          fixture_id: mcpFixtureId,
          transport_type: 'in_memory',
        });
        setMcpRunResult(res);
      } catch (err: unknown) {
        setError(formatApiError(err, t));
      } finally {
        setMcpRunning(false);
      }
    },
    [client, mcpServerName, mcpFixtureId, t]
  );

  const registerMcpTool = useCallback(
    async (toolName: string) => {
      try {
        setError(null);
        await client.integrations.register({
          kind: 'mcp',
          server_name_or_prefix: mcpServerName,
          fixture_id: mcpFixtureId,
          tools: [toolName],
        });
        setMcpRegistered((prev) => ({ ...prev, [toolName]: true }));
      } catch (err: unknown) {
        setError(formatApiError(err, t));
      }
    },
    [client, mcpServerName, mcpFixtureId, t]
  );

  // OpenAPI Actions
  const discoverOpenAPI = useCallback(
    async (fixtureId: string, prefix: string, specContent?: string) => {
      try {
        setLoading(true);
        setError(null);
        setOpenapiRunResult(null);
        const res = await client.integrations.discoverOpenAPI({
          fixture_id: specContent ? undefined : fixtureId,
          spec_content: specContent || undefined,
          prefix: prefix,
        });
        setOpenapiOperations(res.operations);
        if (res.operations.length > 0) {
          setSelectedOpenAPIOp(res.operations[0]);
          if (res.operations[0].operation_id === 'getPetById') {
            setOpenapiInputsText('{\n  "petId": "123"\n}');
          } else if (res.operations[0].operation_id === 'getCurrentWeather') {
            setOpenapiInputsText('{\n  "city": "Bengaluru"\n}');
          }
        }
      } catch (err: unknown) {
        setError(formatApiError(err, t));
      } finally {
        setLoading(false);
      }
    },
    [client, t]
  );

  const testRunOpenAPI = useCallback(
    async (operationId: string, inputsJson: string) => {
      try {
        setOpenapiRunning(true);
        setError(null);
        let parsedInputs: Record<string, any> = {};
        try {
          parsedInputs = JSON.parse(inputsJson);
        } catch {
          throw new Error(t('validationMessages.operationInputsJson'));
        }

        const res = await client.integrations.testRunOpenAPI({
          operation_id: operationId,
          prefix: openapiPrefix,
          fixture_id: openapiSpecText ? undefined : openapiFixtureId,
          spec_content: openapiSpecText || undefined,
          inputs: parsedInputs,
          mock_response: {
            status_code: 200,
            json: {
              status: 'success',
              id: parsedInputs.petId || '123',
              name: 'Fido the Pet',
              city: parsedInputs.city || 'Bengaluru',
              temperature_c: 24,
            },
          },
        });
        setOpenapiRunResult(res);
      } catch (err: unknown) {
        setError(formatApiError(err, t));
      } finally {
        setOpenapiRunning(false);
      }
    },
    [client, openapiPrefix, openapiFixtureId, openapiSpecText, t]
  );

  const registerOpenAPIOp = useCallback(
    async (operationId: string) => {
      try {
        setError(null);
        await client.integrations.register({
          kind: 'openapi',
          server_name_or_prefix: openapiPrefix,
          fixture_id: openapiFixtureId,
          tools: [operationId],
        });
        setOpenapiRegistered((prev) => ({ ...prev, [operationId]: true }));
      } catch (err: unknown) {
        setError(formatApiError(err, t));
      }
    },
    [client, openapiPrefix, openapiFixtureId, t]
  );

  // Auto-discover default fixtures upon first load
  useEffect(() => {
    if (status?.mcp.available && mcpTools.length === 0) {
      discoverMcp(mcpFixtureId, mcpServerName);
    }
    if (status?.openapi.available && openapiOperations.length === 0) {
      discoverOpenAPI(openapiFixtureId, openapiPrefix);
    }
  }, [status, discoverMcp, discoverOpenAPI, mcpFixtureId, mcpServerName, openapiFixtureId, openapiPrefix, mcpTools.length, openapiOperations.length]);

  return {
    status,
    loading,
    error,
    refreshStatus,

    // MCP
    mcpServerName,
    setMcpServerName,
    mcpFixtureId,
    setMcpFixtureId,
    mcpTools,
    selectedMcpTool,
    setSelectedMcpTool,
    mcpArgsText,
    setMcpArgsText,
    mcpRunResult,
    mcpRunning,
    mcpRegistered,
    discoverMcp,
    testRunMcp,
    registerMcpTool,

    // OpenAPI
    openapiPrefix,
    setOpenapiPrefix,
    openapiFixtureId,
    setOpenapiFixtureId,
    openapiSpecText,
    setOpenapiSpecText,
    openapiOperations,
    selectedOpenAPIOp,
    setSelectedOpenAPIOp,
    openapiInputsText,
    setOpenapiInputsText,
    openapiRunResult,
    openapiRunning,
    openapiRegistered,
    discoverOpenAPI,
    testRunOpenAPI,
    registerOpenAPIOp,
  };
}
