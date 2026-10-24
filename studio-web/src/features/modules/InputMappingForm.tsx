// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useT } from '../../i18n';
import { useState, type ReactNode } from 'react';
import type { JsonSchema } from './types';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';

interface InputMappingFormProps {
  inputSchema: JsonSchema;
  mapping: Record<string, string>;
  onChange: (mapping: Record<string, string>) => void;
  /** Replaces the default row of a named port (e.g. the connector operation select). */
  portOverrides?: Record<string, (value: string, onChange: (v: string) => void) => ReactNode>;
}

export function InputMappingForm({ inputSchema, mapping, onChange, portOverrides }: InputMappingFormProps) {
  const t = useT();
  const [customKey, setCustomKey] = useState('');
  const [customVal, setCustomVal] = useState('');

  const properties = inputSchema.properties || {};
  const requiredSet = new Set(inputSchema.required || []);
  const knownKeys = Object.keys(properties);

  const handleUpdate = (port: string, stateKey: string) => {
    const next = { ...mapping };
    if (stateKey.trim() === '') {
      delete next[port];
    } else {
      next[port] = stateKey.trim();
    }
    onChange(next);
  };

  const handleAddCustom = (e: React.FormEvent) => {
    e.preventDefault();
    if (!customKey.trim() || !customVal.trim()) return;
    onChange({ ...mapping, [customKey.trim()]: customVal.trim() });
    setCustomKey('');
    setCustomVal('');
  };

  const handleRemove = (port: string) => {
    const next = { ...mapping };
    delete next[port];
    onChange(next);
  };

  const customKeys = Object.keys(mapping).filter((k) => !knownKeys.includes(k));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{t('ui.inputMappings')}</span>
        <Badge variant="neutral">{Object.keys(mapping).length} {t('ui.mapped')}</Badge>
      </div>

      <p style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)', margin: 0 }}>
        {t('ui.bindModuleInputPortsToWorkflowStateKeysInputsWithoutExplicitMappingsReadFrom')}
      </p>

      {/* Known Schema Input Ports */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {knownKeys.map((port) => {
          const prop = properties[port];
          const isReq = requiredSet.has(port);
          const currentBinding = mapping[port] || '';
          const override = portOverrides?.[port];
          if (override) {
            return <div key={port}>{override(currentBinding, (v) => handleUpdate(port, v))}</div>;
          }

          return (
            <div
              key={port}
              style={{
                padding: '8px 10px',
                border: '1px solid var(--border-subtle)',
                borderRadius: 'var(--radius-sm)',
                backgroundColor: 'var(--bg-canvas)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <code style={{ fontSize: 12, fontWeight: 600 }}>{port}</code>
                  {isReq && <span style={{ color: 'var(--status-danger)', fontSize: 'var(--type-meta)' }}>{t('ui.required')}</span>}
                </div>
                <Badge variant="neutral">{prop.type || t('ui.any')}</Badge>
              </div>

              {prop.description && (
                <div style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)', marginBottom: 6 }}>
                  {prop.description}
                </div>
              )}

              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 'var(--type-meta)', color: 'var(--text-muted)' }}>{t('ui.mapsFrom')}</span>
                <input
                  type="text"
                  placeholder={`state.${port}`}
                  value={currentBinding}
                  onChange={(e) => handleUpdate(port, e.target.value)}
                  aria-label={t('ui.mappingFor').replace('{port}', port)}
                  style={{
                    flex: 1,
                    padding: '4px 8px',
                    borderRadius: 'var(--radius-sm)',
                    border: '1px solid var(--border-subtle)',
                    backgroundColor: 'var(--bg-surface)',
                    color: 'var(--text-primary)',
                    fontSize: 12,
                    fontFamily: 'var(--font-mono)',
                  }}
                />
              </div>
            </div>
          );
        })}

        {knownKeys.length === 0 && (
          <div style={{ fontSize: 12, color: 'var(--text-secondary)', fontStyle: 'italic' }}>
            {t('ui.noDeclaredInputPortsInModuleSchema')}
          </div>
        )}
      </div>

      {/* Custom Mappings */}
      {customKeys.length > 0 && (
        <div style={{ marginTop: 6 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
            {t('ui.additionalPortMappings')}
          </span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 }}>
            {customKeys.map((k) => (
              <div
                key={k}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '4px 8px',
                  backgroundColor: 'var(--bg-canvas)',
                  borderRadius: 'var(--radius-sm)',
                }}
              >
                <div style={{ fontSize: 12 }}>
                  <code>{k}</code> <Icon name="right" /> <code>{mapping[k]}</code>
                </div>
                <button
                  type="button"
                  onClick={() => handleRemove(k)}
                  aria-label={t('ui.removeMapping').replace('{key}', k)}
                  style={{
                    color: 'var(--status-danger)',
                    cursor: 'pointer',
                    background: 'none',
                    border: 'none',
                    padding: '2px 4px',
                  }}
                >
                  <Icon name="close" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Add Custom Port Mapping */}
      <form
        onSubmit={handleAddCustom}
        style={{
          display: 'flex',
          gap: 6,
          marginTop: 6,
          paddingTop: 8,
          borderTop: '1px solid var(--border-subtle)',
        }}
      >
        <input
          type="text"
          placeholder="port_name"
          value={customKey}
          onChange={(e) => setCustomKey(e.target.value)}
          aria-label={t('ui.customPortName')}
          style={{
            flex: 1,
            padding: '4px 8px',
            fontSize: 12,
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-primary)',
          }}
        />
        <input
          type="text"
          placeholder="state_key"
          value={customVal}
          onChange={(e) => setCustomVal(e.target.value)}
          aria-label={t('ui.customStateKey')}
          style={{
            flex: 1,
            padding: '4px 8px',
            fontSize: 12,
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-primary)',
          }}
        />
        <Button variant="secondary" size="sm" type="submit">
          {t('ui.map')}
        </Button>
      </form>
    </div>
  );
}
