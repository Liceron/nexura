import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NexuraClient } from '../dist/index.js';

test('typescript client supports module discovery, capabilities, validation and live events', async () => {
  const original = globalThis.fetch;
  const calls = [];

  globalThis.fetch = async (urlStr, options) => {
    const url = new URL(urlStr);
    calls.push({ path: url.pathname, search: url.search, options });

    if (url.pathname === '/api/v1/modules') {
      return new Response(JSON.stringify({
        items: [{
          module_id: 'nexura.prompt.template',
          version: '1.0.0',
          kind: 'data_transform',
          title: 'Prompt Template Renderer',
          description: 'Safe prompt rendering',
          input_schema: { type: 'object' },
          output_schema: { type: 'object' },
          config_schema: { type: 'object' },
          named_ports: [],
          supported_routes: ['default'],
          required_credential_types: [],
          capabilities: {},
          effect_policy: 'read',
          min_sdk_version: '0.3.0',
          verification_status: 'mock_verified',
          is_available: true,
          requires_approval: false,
          digest: 'abc123hash',
        }],
        total: 1,
        modules: [],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }

    if (url.pathname === '/api/v1/modules/nexura.prompt.template') {
      return new Response(JSON.stringify({
        module_id: 'nexura.prompt.template',
        version: '1.0.0',
        kind: 'data_transform',
        title: 'Prompt Template Renderer',
        description: 'Safe prompt rendering',
        input_schema: { type: 'object' },
        output_schema: { type: 'object' },
        config_schema: { type: 'object' },
        named_ports: [],
        supported_routes: ['default'],
        required_credential_types: [],
        capabilities: {},
        effect_policy: 'read',
        min_sdk_version: '0.3.0',
        verification_status: 'mock_verified',
        is_available: true,
        requires_approval: false,
        digest: 'abc123hash',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }

    if (url.pathname === '/api/v1/capabilities') {
      return new Response(JSON.stringify({
        sdk_version: '0.4.0',
        active_profile: 'local-owner',
        supported_profiles: ['local-owner', 'authenticated-shared-service'],
        capabilities: { modules: true, validation: true, downloadable_export: true },
        supported_credential_types: ['openai_api_key'],
        supported_export_formats: ['bundle', 'archive'],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }

    if (url.pathname === '/api/v1/workflows/validate') {
      return new Response(JSON.stringify({
        valid: true,
        errors: [],
        warnings: [],
        canonical_hash: 'hash-val-123',
        node_count: 2,
        estimated_effect_policy: 'read',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }

    if (url.pathname === '/api/v1/workflows/wf-1/export/download') {
      return new Response(new Uint8Array([80, 75, 3, 4]), {
        status: 200,
        headers: { 'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="wf-1.zip"' },
      });
    }

    if (url.pathname === '/api/v1/runs/run-1/events') {
      const body = 'id: 1\nevent: RUN_STARTED\ndata: {"status":"running"}\n\nid: 2\nevent: RUN_COMPLETED\ndata: {"status":"completed"}\n\n';
      return new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      });
    }

    return new Response('Not found', { status: 404 });
  };

  try {
    const client = new NexuraClient({ baseUrl: 'http://127.0.0.1:8000', workspaceId: 'ws-builder' });

    // 1. Module listing and query
    const listRes = await client.modules.list({ kind: 'data_transform', available_only: true });
    assert.equal(listRes.total, 1);
    assert.equal(listRes.items[0].module_id, 'nexura.prompt.template');

    // 2. Single module descriptor
    const desc = await client.modules.get('nexura.prompt.template');
    assert.equal(desc.title, 'Prompt Template Renderer');

    // 3. Capabilities discovery
    const caps = await client.capabilities.get();
    assert.equal(caps.active_profile, 'local-owner');
    assert.equal(caps.capabilities.modules, true);

    // 4. Graph validation
    const valRes = await client.workflows.validate({
      name: 'Test',
      entry_node: 'n1',
      nodes: {
        n1: { id: 'n1', handler: 'nexura.prompt.template', presentation: { position: { x: 50, y: 100 } } },
      },
    });
    assert.equal(valRes.valid, true);
    assert.equal(valRes.canonical_hash, 'hash-val-123');

    // 5. Download export bundle
    const zipBytes = await client.workflows.downloadExport('wf-1');
    assert.ok(zipBytes.byteLength > 0);

    // 6. Live SSE streaming
    const events = [];
    for await (const ev of client.runs.streamEvents('run-1', { cursor: 0, live: true })) {
      events.push(ev);
    }
    assert.equal(events.length, 2);
    assert.equal(events[0].type, 'RUN_STARTED');
    assert.equal(events[1].type, 'RUN_COMPLETED');

    // Verify workspace ID header was applied
    assert.equal(calls[0].options.headers['X-Workspace-ID'], 'ws-builder');
  } finally {
    globalThis.fetch = original;
  }
});
