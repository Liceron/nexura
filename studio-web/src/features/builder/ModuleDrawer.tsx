// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useState } from 'react';
import { useBuilderT } from './composite';
import { Card } from '../../components/Card';
import { Badge } from '../../components/Badge';
import { OperationList } from '../modules/ConnectorFields';
import type { ConnectorOperation } from '../modules/connectors';

export interface ModuleItem {
  id: string;
  name: string;
  title: string;
  description: string;
  category: string;
  requires_approval_by_default?: boolean;
  /** Connector operations with their read/write marker (modules that have operations). */
  operations?: ConnectorOperation[];
  /** Credential type a connector module needs. */
  credential_type?: string;
}

export const BUILTIN_MODULES_LIST: ModuleItem[] = [
  {
    id: 'synthetic_order_lookup',
    name: 'synthetic_order_lookup',
    title: 'Synthetic Order Lookup',
    description: 'Looks up order details from local synthetic store without external network calls.',
    category: 'data_transform',
  },
  {
    id: 'synthetic_router',
    name: 'synthetic_router',
    title: 'Synthetic Query Router',
    description: 'Evaluates order amount and category to branch into refund vs standard support.',
    category: 'router',
  },
  {
    id: 'synthetic_ticket_creator',
    name: 'synthetic_ticket_creator',
    title: 'Synthetic Ticket Creator',
    description: 'Generates support ticket and authorization code. Server-required approval gate.',
    category: 'external_action',
    requires_approval_by_default: true,
  },
  {
    id: 'synthetic_fake_agent',
    name: 'synthetic_fake_agent',
    title: 'Synthetic Agent Turn',
    description: 'Invokes local FakeModel agent turn to formulate localized support responses.',
    category: 'agent',
  },
  {
    id: 'nexura.prompt.template',
    name: 'nexura.prompt.template',
    title: 'Prompt Template Renderer',
    description: 'Safe, deterministic template interpolation preserving multilingual Indic text.',
    category: 'data_transform',
  },
];

interface ModuleDrawerProps {
  open: boolean;
  onClose: () => void;
  onAddNode: (handler: string, defaultTitle: string, requiresApproval: boolean) => void;
}

export function ModuleDrawer({ open, onClose, onAddNode }: ModuleDrawerProps) {
  const [search, setSearch] = useState('');
  const t = useBuilderT();

  if (!open) return null;

  const filtered = BUILTIN_MODULES_LIST.filter(
    (m) =>
      m.title.toLowerCase().includes(search.toLowerCase()) ||
      m.name.toLowerCase().includes(search.toLowerCase()) ||
      m.description.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div
      role="dialog"
      aria-label={t('ui.availableModulesDrawer')}
      style={{
        position: 'absolute',
        top: 60,
        left: 20,
        width: 320,
        maxHeight: 'calc(100% - 100px)',
        backgroundColor: 'var(--bg-surface-elevated)',
        borderRadius: 'var(--radius-lg)',
        border: '1px solid var(--border-subtle)',
        boxShadow: 'var(--shadow-xl)',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 50,
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--border-subtle)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <span style={{ fontWeight: 600, fontSize: 14 }}>{t('ui.availableModules')}</span>
        <button
          onClick={onClose}
          style={{ fontSize: 16, color: 'var(--text-muted)', cursor: 'pointer' }}
          aria-label={t('ui.closeModulesDrawer')}
        >
          <Icon name="close" />
        </button>
      </div>

      <div style={{ padding: 12 }}>
        <input
          placeholder={t('ui.searchModules')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label={t('ui.searchAvailableModules')}
          style={{
            width: '100%',
            padding: '6px 10px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-primary)',
            fontSize: 13,
          }}
        />
      </div>

      <div style={{ overflowY: 'auto', padding: '0 12px 12px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {filtered.map((mod) => (
          <Card
            key={mod.id}
            style={{
              padding: 10,
              cursor: 'pointer',
              transition: 'border-color 0.15s ease',
            }}
            onClick={() => onAddNode(mod.name, t(`builtinModules.${mod.id}.title`, mod.title), Boolean(mod.requires_approval_by_default))}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontWeight: 600, fontSize: 13 }}>{t(`builtinModules.${mod.id}.title`, mod.title)}</span>
              <Badge variant="neutral"><code>{mod.category}</code></Badge>
            </div>
            <p style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)', lineHeight: 1.5, marginBottom: 6 }}>
              {t(`builtinModules.${mod.id}.description`, mod.description)}
            </p>
            {mod.operations && <OperationList operations={mod.operations} />}
            {mod.credential_type && (
              <div data-testid="credential-type" style={{ fontSize: 'var(--type-meta)', marginBottom: 6 }}>
                {t('connectors.credentialType', { type: mod.credential_type })}
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <code style={{ fontSize: 'var(--type-meta)' }}>{mod.name}</code>
              <span style={{ fontSize: 'var(--type-meta)', color: 'var(--accent-primary)', fontWeight: 600 }}>{t('ui.addNode')}</span>
            </div>
          </Card>
        ))}

        {filtered.length === 0 && (
          <div style={{ padding: 20, textAlign: 'center', color: 'var(--text-secondary)', fontSize: 13 }}>
            {t('ui.noModulesFoundMatchingLdquo')}{search}{t('ui.rdquo')}
          </div>
        )}
      </div>
    </div>
  );
}
