// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { ModuleDescriptor } from '@liceron/nexura-sdk';

/** One operation of a connector module and whether it writes (needs human approval). */
export interface ConnectorOperation {
  operation: string;
  write: boolean;
}

/** A credential reference offered by the picker: id and description only, never a secret. */
export interface CredentialChoice {
  id: string;
  description: string;
}

interface CredentialLike {
  credential_id?: unknown;
  credential_type?: unknown;
  description?: unknown;
  status?: unknown;
}

type DescriptorLike = Pick<ModuleDescriptor, 'input_schema' | 'display'> &
  Partial<Pick<ModuleDescriptor, 'required_credential_types'>> & { write_operations?: string[] };

/** Operations declared by a connector descriptor (display.examples, else input enum). */
export function connectorOperations(desc: DescriptorLike | null | undefined): ConnectorOperation[] {
  if (!desc) return [];
  const seen = new Set<string>();
  const out: ConnectorOperation[] = [];
  for (const ex of desc.display?.examples ?? []) {
    if (ex && typeof ex === 'object' && typeof ex.operation === 'string' && !seen.has(ex.operation)) {
      seen.add(ex.operation);
      out.push({ operation: ex.operation, write: ex.write === true });
    }
  }
  if (out.length > 0) return out;
  const names = desc.input_schema?.properties?.operation?.enum;
  if (!Array.isArray(names)) return [];
  const writes = new Set(desc.write_operations ?? []);
  for (const n of names) {
    if (typeof n === 'string' && !seen.has(n)) {
      seen.add(n);
      out.push({ operation: n, write: writes.has(n) });
    }
  }
  return out;
}

/** i18n key (under `connectors.`) of the marker for an operation. */
export function operationMarkerKey(write: boolean): 'writesNeedApproval' | 'reads' {
  return write ? 'writesNeedApproval' : 'reads';
}

/** Credential types a module needs (empty when it needs none). */
export function requiredCredentialTypes(desc: Pick<ModuleDescriptor, 'required_credential_types'> | null | undefined): string[] {
  const types = desc?.required_credential_types;
  return Array.isArray(types) ? types.filter((t): t is string => typeof t === 'string' && t !== '') : [];
}

/** Workspace credential references whose type is one of `types`; id and description only. */
export function credentialChoices(creds: CredentialLike[], types: string[]): CredentialChoice[] {
  const wanted = new Set(types);
  const out: CredentialChoice[] = [];
  for (const c of creds) {
    if (typeof c.credential_id !== 'string' || typeof c.credential_type !== 'string') continue;
    if (!wanted.has(c.credential_type) || c.status === 'revoked') continue;
    out.push({ id: c.credential_id, description: typeof c.description === 'string' ? c.description : '' });
  }
  return out;
}

/** Operation held by an input_mapping value: a plain string or the `{"$literal": op}` form. */
export function mappingOperation(raw: unknown): string {
  if (typeof raw === 'string') return raw;
  if (raw && typeof raw === 'object' && typeof (raw as { $literal?: unknown }).$literal === 'string') {
    return (raw as { $literal: string }).$literal;
  }
  return '';
}

/** Distinct credential types required by the registered modules, sorted. */
export function moduleCredentialTypes(mods: Array<Pick<ModuleDescriptor, 'required_credential_types'>>): string[] {
  const out = new Set<string>();
  for (const m of mods) for (const t of requiredCredentialTypes(m)) out.add(t);
  return Array.from(out).sort();
}
