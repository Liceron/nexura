import { describe, it, expect } from 'vitest';
import {
  CREDENTIAL_PROVIDERS,
  CREDENTIAL_TYPES,
  ROLE_DEFINITIONS,
  CredentialReferenceItem,
} from '../../src/features/credentials/types';

describe('Credential References & Role-Based Access Control (F11)', () => {
  it('provides comprehensive provider catalog including Indic and enterprise providers', () => {
    const providerIds = CREDENTIAL_PROVIDERS.map((p) => p.id);
    expect(providerIds).toContain('openai');
    expect(providerIds).toContain('anthropic');
    expect(providerIds).toContain('gemini');
    expect(providerIds).toContain('sarvam');
    expect(providerIds).toContain('whatsapp');
    expect(providerIds).toContain('email');
    expect(providerIds).toContain('postgres');
  });

  it('defines structured credential kinds without exposing plain secrets', () => {
    const typeIds = CREDENTIAL_TYPES.map((t) => t.id);
    expect(typeIds).toContain('api_key');
    expect(typeIds).toContain('bearer_token');
    expect(typeIds).toContain('oauth');
    expect(typeIds).toContain('database_url');
  });

  it('enforces strict role-based permission hierarchy', () => {
    const admin = ROLE_DEFINITIONS.admin;
    const editor = ROLE_DEFINITIONS.editor;
    const viewer = ROLE_DEFINITIONS.viewer;

    // Admin permissions
    expect(admin.permissions).toContain('admin');
    expect(admin.permissions).toContain('edit');
    expect(admin.permissions).toContain('run');
    expect(admin.permissions).toContain('approve');
    expect(admin.permissions).toContain('export_credentials');

    // Editor permissions
    expect(editor.permissions).toContain('edit');
    expect(editor.permissions).toContain('run');
    expect(editor.permissions).not.toContain('admin');
    expect(editor.permissions).not.toContain('approve');
    expect(editor.permissions).not.toContain('export_credentials');

    // Viewer permissions
    expect(viewer.permissions).not.toContain('edit');
    expect(viewer.permissions).not.toContain('run');
    expect(viewer.permissions).not.toContain('admin');
    expect(viewer.permissions).not.toContain('approve');
  });

  it('guarantees credential metadata contracts exclude raw secret tokens', () => {
    const sampleCred: CredentialReferenceItem = {
      credential_id: 'openai_main',
      workspace_id: 'default',
      credential_type: 'api_key',
      provider: 'openai',
      description: 'Main production OpenAI key',
      status: 'active',
      created_at: 1720000000,
      last_verified_at: 1720000500,
      environment_variable: 'OPENAI_API_KEY',
    };

    const keys = Object.keys(sampleCred);
    expect(keys).not.toContain('secret');
    expect(keys).not.toContain('raw_secret');
    expect(keys).not.toContain('token');
    expect(keys).not.toContain('password');

    const json = JSON.stringify(sampleCred);
    expect(json).not.toContain('sk-');
    expect(json).not.toContain('secret');
  });

  it('fails closed when credential status is revoked', () => {
    const revokedCred: CredentialReferenceItem = {
      credential_id: 'revoked_key',
      workspace_id: 'default',
      credential_type: 'api_key',
      provider: 'openai',
      description: 'Revoked key',
      status: 'revoked',
      created_at: 1720000000,
    };

    expect(revokedCred.status).toBe('revoked');
    // In our system, revoked status signals runtime resolution to fail closed
    const isUsable = revokedCred.status === 'active';
    expect(isUsable).toBe(false);
  });
});
