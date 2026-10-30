// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
export type CredentialType = 'api_key' | 'bearer_token' | 'basic_auth' | 'oauth' | 'database_url' | 'custom';

export type { CreateCredentialOptions } from '@liceron/nexura-sdk';

export type CredentialProvider =
  | 'openai'
  | 'anthropic'
  | 'gemini'
  | 'sarvam'
  | 'whatsapp'
  | 'email'
  | 'postgres'
  | 'custom';

export type CredentialStatus = 'active' | 'revoked' | 'expired';

export type WorkspaceRole = 'admin' | 'editor' | 'viewer';

export interface CredentialReferenceItem {
  credential_id: string;
  workspace_id: string;
  credential_type: string;
  provider: string;
  description: string;
  environment_variable?: string | null;
  status: CredentialStatus;
  created_at: number;
  last_verified_at?: number | null;
  masked_hint?: string;
}

export interface WorkspaceMemberInfo {
  workspace_id: string;
  principal_id: string;
  role: WorkspaceRole;
  joined_at: number;
  updated_at: number;
}

export interface CurrentMembershipState {
  workspace_id: string;
  principal_id: string;
  role: WorkspaceRole;
  permissions: string[];
  profile: 'local-owner' | 'authenticated-shared-service';
}

export interface VerificationResult {
  credential_id: string;
  status: 'stored' | 'valid' | 'invalid' | 'revoked';
  checked_with_provider?: boolean;
  message: string;
  checked_at: number;
}

export const CREDENTIAL_PROVIDERS: { id: CredentialProvider; label: string; icon: string }[] = [
  { id: 'openai', label: 'OpenAI', icon: 'workflows' },
  { id: 'anthropic', label: 'Anthropic Claude', icon: 'agents' },
  { id: 'gemini', label: 'Google Gemini', icon: 'sparkles' },
  { id: 'sarvam', label: 'Sarvam AI (Indic)', icon: 'globe' },
  { id: 'whatsapp', label: 'WhatsApp Cloud API', icon: 'playground' },
  { id: 'email', label: 'SMTP / SendGrid Email', icon: 'mail' },
  { id: 'postgres', label: 'PostgreSQL Database', icon: 'database' },
  { id: 'custom', label: 'Custom Service / HTTP', icon: 'lock' },
];

export const CREDENTIAL_TYPES: { id: CredentialType; label: string }[] = [
  { id: 'api_key', label: 'API Key (Secret Token)' },
  { id: 'bearer_token', label: 'Bearer Token (JWT / OAuth)' },
  { id: 'basic_auth', label: 'Basic Auth (User:Password)' },
  { id: 'database_url', label: 'Database Connection String' },
  { id: 'oauth', label: 'OAuth Client Credentials' },
  { id: 'custom', label: 'Custom Secret Value' },
];

export const ROLE_DEFINITIONS: Record<WorkspaceRole, { label: string; description: string; permissions: string[] }> = {
  admin: {
    label: 'Admin',
    description: 'Full administrative access: run, edit workflows, manage team members, approve gates, revoke credentials.',
    permissions: ['admin', 'approve', 'edit', 'export_credentials', 'run'],
  },
  editor: {
    label: 'Editor',
    description: 'Can build, modify, and execute workflows. Cannot manage team memberships or revoke credentials.',
    permissions: ['edit', 'run'],
  },
  viewer: {
    label: 'Viewer',
    description: 'Read-only access. Cannot execute workflows, modify graphs, or add/delete credentials.',
    permissions: ['read'],
  },
};
