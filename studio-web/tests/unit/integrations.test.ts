import { describe, it, expect, vi, beforeEach } from 'vitest';
import { NexuraClient } from '@liceron/nexura-sdk';
import type {
  IntegrationStatus,
  McpDiscoverResponse,
  McpTestRunResult,
  OpenAPIDiscoverResponse,
  OpenAPITestRunResult,
  IntegrationRegisterResponse,
  WorkflowRecord,
} from '@liceron/nexura-sdk';
import { exportWorkflowToJson, validateAndParseWorkflowImport } from '../../src/features/workflows/codecs';

describe('Integrations & Runnable Export Unit Tests (F12)', () => {
  let client: NexuraClient;

  beforeEach(() => {
    vi.restoreAllMocks();
    client = new NexuraClient({
      baseUrl: 'http://localhost:8000',
      workspaceId: 'default',
    });
  });

  describe('IntegrationsClient (MCP & OpenAPI)', () => {
    it('fetches integrations status with supported fixtures', async () => {
      const mockStatus: IntegrationStatus = {
        mcp: {
          available: true,
          missing_reason: null,
          fixtures: [
            { id: 'math_fixture', name: 'Math Calculator', description: 'Local offline arithmetic' },
            { id: 'echo_fixture', name: 'Echo Server', description: 'Local text transformer' },
          ],
        },
        openapi: {
          available: true,
          missing_reason: null,
          fixtures: [
            { id: 'petstore_fixture', name: 'Petstore API', description: 'Local petstore OpenAPI 3.0' },
          ],
        },
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockStatus,
      } as Response);

      const status = await client.integrations.getStatus();
      expect(status.mcp.available).toBe(true);
      expect(status.mcp.fixtures.map((f) => f.id)).toContain('math_fixture');
      expect(status.openapi.fixtures.map((f) => f.id)).toContain('petstore_fixture');
    });

    it('discovers tools from MCP server fixture', async () => {
      const mockDiscovery: McpDiscoverResponse = {
        server_name: 'calc',
        tools: [
          {
            name: 'math_add',
            description: 'Add two numbers',
            input_schema: { type: 'object', properties: { x: { type: 'number' }, y: { type: 'number' } } },
            digest: 'digest123',
            effect_policy: 'pure',
          },
        ],
        is_fixture: true,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockDiscovery,
      } as Response);

      const res = await client.integrations.discoverMcp({
        server_name: 'calc',
        fixture_id: 'math_fixture',
      });

      expect(res.server_name).toBe('calc');
      expect(res.tools).toHaveLength(1);
      expect(res.tools[0].name).toBe('math_add');
      expect(res.is_fixture).toBe(true);
    });

    it('executes MCP tool dry-run with mock transport', async () => {
      const mockResult: McpTestRunResult = {
        status: 'success',
        output: { result: 35 },
        content: [{ type: 'text', text: '35' }],
        error: null,
        is_fixture: true,
        execution_label: 'Fixture Dry-Run (Local Mock Transport)',
        duration_ms: 12,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockResult,
      } as Response);

      const res = await client.integrations.testRunMcp({
        tool_name: 'math_add',
        arguments: { x: 10, y: 25 },
      });

      expect(res.status).toBe('success');
      expect(res.output).toEqual({ result: 35 });
      expect(res.is_fixture).toBe(true);
      expect(res.execution_label).toContain('Mock Transport');
    });

    it('discovers OpenAPI endpoints from spec/fixture', async () => {
      const mockOpenAPIDiscovery: OpenAPIDiscoverResponse = {
        prefix: 'petstore',
        operations: [
          {
            operation_id: 'getPetById',
            module_id: 'petstore_getPetById',
            path: '/pets/{petId}',
            method: 'GET',
            summary: 'Find pet by ID',
            description: 'Fetch pet record',
            input_schema: { type: 'object' },
            output_schema: { type: 'object' },
            effect_policy: 'read_only',
            is_idempotent: true,
          },
        ],
        is_fixture: true,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockOpenAPIDiscovery,
      } as Response);

      const res = await client.integrations.discoverOpenAPI({
        prefix: 'petstore',
        fixture_id: 'petstore_fixture',
      });

      expect(res.prefix).toBe('petstore');
      expect(res.operations).toHaveLength(1);
      expect(res.operations[0].operation_id).toBe('getPetById');
      expect(res.is_fixture).toBe(true);
    });

    it('executes OpenAPI operation dry-run', async () => {
      const mockResult: OpenAPITestRunResult = {
        status: 'success',
        status_code: 200,
        body: { id: 123, name: 'Fido', status: 'available' },
        headers: { 'content-type': 'application/json' },
        error: null,
        is_fixture: true,
        execution_label: 'Fixture Dry-Run (Local Mock Transport)',
        duration_ms: 15,
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockResult,
      } as Response);

      const res = await client.integrations.testRunOpenAPI({
        operation_id: 'getPetById',
        inputs: { petId: '123' },
      });

      expect(res.status).toBe('success');
      expect(res.status_code).toBe(200);
      expect(res.body).toHaveProperty('name', 'Fido');
      expect(res.is_fixture).toBe(true);
    });

    it('registers discovered tools into backend tool registry', async () => {
      const mockRegisterResult: IntegrationRegisterResponse = {
        count: 2,
        registered_modules: ['calc.math_add', 'petstore.getPetById'],
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockRegisterResult,
      } as Response);

      const res = await client.integrations.register({
        kind: 'mcp',
        server_name_or_prefix: 'calc',
        tools: ['math_add'],
      });

      expect(res.count).toBe(2);
      expect(res.registered_modules).toContain('calc.math_add');
    });
  });

  describe('Workflows Runnable Bundle Export & Import', () => {
    it('downloads export zip bundle as binary ArrayBuffer', async () => {
      const fakeZipBytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0x00]);

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        arrayBuffer: async () => fakeZipBytes.buffer,
      } as Response);

      const buffer = await client.workflows.downloadExport('wf-test-123');
      expect(buffer).toBeInstanceOf(ArrayBuffer);
      expect(buffer.byteLength).toBe(fakeZipBytes.byteLength);
    });

    it('imports archive bundle via client.workflows.import', async () => {
      const mockImportRes: WorkflowRecord = {
        workflow_id: 'wf-imported-1',
        name: 'Imported Workflow',
        revision: 1,
        entry_node: 'n1',
        node_count: 1,
        created_at: Date.now(),
        updated_at: Date.now(),
        spec: {
          name: 'Imported Workflow',
          entry_node: 'n1',
          spec_version: 1,
          nodes: { n1: { id: 'n1', handler: 'prompt' } },
        },
      };

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        json: async () => mockImportRes,
      } as Response);

      const result = await client.workflows.import({
        archive_base64: 'UEsDBAoAAAAAAA==',
        name: 'Imported Workflow',
      });

      expect(result.workflow_id).toBe('wf-imported-1');
      expect(result.name).toBe('Imported Workflow');
      expect(result.revision).toBe(1);
    });

    it('serializes portable export and detects embedded credentials', () => {
      const specWithSecret = {
        name: 'Secret Flow',
        entry_node: 'apiNode',
        spec_version: 1,
        nodes: {
          apiNode: {
            id: 'apiNode',
            handler: 'agent',
            config: {
              api_key: 'sk-secret-token-1234567890',
            },
          },
        },
      };

      const exported = exportWorkflowToJson('wf_sec', 'Secret Flow', specWithSecret as any);
      expect(exported.hasSecrets).toBe(true);
      expect(exported.secretDetected).toBe(true);
      expect(exported.data.format).toBe('nexura-workflow-export');
      expect(exported.data.workflow.name).toBe('Secret Flow');
    });

    it('validates and safely parses clean workflow import JSON', () => {
      const validPayload = JSON.stringify({
        format: 'nexura-workflow-export',
        schema_version: 1,
        spec_version: 1,
        workflow: {
          name: 'Clean Flow',
          spec: {
            name: 'Clean Flow',
            entry_node: 'start',
            spec_version: 1,
            nodes: {
              start: { id: 'start', handler: 'input' },
            },
          },
        },
      });

      const parsed = validateAndParseWorkflowImport(validPayload);
      expect(parsed.valid).toBe(true);
      expect(parsed.workflowName).toBe('Clean Flow');
      expect(parsed.hasSecrets).toBe(false);
      expect(parsed.errors).toHaveLength(0);
    });

    it('rejects invalid or oversized import JSON payloads', () => {
      const invalidJson = '{ this is not valid json }';
      const parsed = validateAndParseWorkflowImport(invalidJson);
      expect(parsed.valid).toBe(false);
      expect(parsed.errors.length).toBeGreaterThan(0);
    });
  });
});
