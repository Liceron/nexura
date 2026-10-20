// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useT } from '../../i18n';
import { useState } from 'react';
import type { JsonSchemaProperty } from './types';
import { isPotentialSecret } from './types';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';

/** Warning shown under a field whose value looks like a raw secret. */
export function SecurityNotice() {
  const t = useT();
  return (
    <div
      style={{
        marginTop: 4,
        padding: '6px 10px',
        backgroundColor: 'var(--status-danger-bg)',
        color: 'var(--status-danger)',
        fontSize: 11,
        borderRadius: 'var(--radius-sm)',
        lineHeight: 1.3,
      }}
    >
      <Icon name="warning" /> <strong>{t('ui.securityNotice')}</strong> {t('ui.thisFieldLooksLikeARawSecretOrApiKeyDoNotEmbedCredentialsInWorkflowSpecifi')}
    </div>
  );
}

interface SchemaFieldProps {
  name: string;
  schema: JsonSchemaProperty;
  value: unknown;
  onChange: (value: unknown) => void;
  isRequired?: boolean;
  level?: number;
}

export function SchemaField({
  name,
  schema,
  value,
  onChange,
  isRequired = false,
  level = 0,
}: SchemaFieldProps) {
  const t = useT();
  const [collapsed, setCollapsed] = useState(false);

  const title = schema.title || name;
  const description = schema.description;
  const isSecret = isPotentialSecret(value);

  // 1. Enum dropdown
  if (schema.enum && Array.isArray(schema.enum)) {
    return (
      <div style={{ marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
          <label style={{ fontSize: 13, fontWeight: 500 }}>
            {title} {isRequired && <span style={{ color: 'var(--status-danger)' }}>*</span>}
          </label>
          {isRequired && <Badge variant="warning">{t('ui.required2')}</Badge>}
        </div>
        <select
          value={value !== undefined ? String(value) : (schema.default !== undefined ? String(schema.default) : '')}
          onChange={(e) => {
            const val = e.target.value;
            // Type-cast if enum has numbers
            const matched = schema.enum?.find((item) => String(item) === val);
            onChange(matched !== undefined ? matched : val);
          }}
          aria-label={title}
          style={{
            width: '100%',
            padding: '6px 10px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-primary)',
            fontSize: 13,
          }}
        >
          <option value="">{t('ui.selectAnOption')}</option>
          {schema.enum.map((opt, i) => (
            <option key={i} value={String(opt)}>
              {String(opt)}
            </option>
          ))}
        </select>
        {description && (
          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 }}>{description}</div>
        )}
      </div>
    );
  }

  // 2. Boolean checkbox
  if (schema.type === 'boolean') {
    const checked = value !== undefined ? Boolean(value) : Boolean(schema.default);
    return (
      <div style={{ marginBottom: 12 }}>
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 13,
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => onChange(e.target.checked)}
            aria-label={title}
          />
          <span>{title}</span>
          {isRequired && <span style={{ color: 'var(--status-danger)' }}>*</span>}
        </label>
        {description && (
          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4, marginLeft: 24 }}>
            {description}
          </div>
        )}
      </div>
    );
  }

  // 3. Number / Integer
  if (schema.type === 'number' || schema.type === 'integer') {
    const isInt = schema.type === 'integer';
    return (
      <div style={{ marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
          <label style={{ fontSize: 13, fontWeight: 500 }}>
            {title} {isRequired && <span style={{ color: 'var(--status-danger)' }}>*</span>}
          </label>
          {isRequired && <Badge variant="warning">{t('ui.required2')}</Badge>}
        </div>
        <input
          type="number"
          step={isInt ? 1 : 'any'}
          min={schema.minimum}
          max={schema.maximum}
          value={value !== undefined ? Number(value) : (schema.default !== undefined ? Number(schema.default) : '')}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === '') {
              onChange(undefined);
            } else {
              onChange(isInt ? parseInt(raw, 10) : parseFloat(raw));
            }
          }}
          aria-label={title}
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
        {description && (
          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 }}>{description}</div>
        )}
      </div>
    );
  }

  // 4. Array
  if (schema.type === 'array') {
    const list = Array.isArray(value) ? value : [];
    const itemSchema = schema.items || { type: 'string' };

    const handleAddItem = () => {
      const defaultItem = itemSchema.default !== undefined ? itemSchema.default : '';
      onChange([...list, defaultItem]);
    };

    const handleRemoveItem = (index: number) => {
      onChange(list.filter((_, i) => i !== index));
    };

    const handleUpdateItem = (index: number, itemVal: unknown) => {
      const next = [...list];
      next[index] = itemVal;
      onChange(next);
    };

    return (
      <div
        style={{
          marginBottom: 14,
          padding: 10,
          border: '1px solid var(--border-subtle)',
          borderRadius: 'var(--radius-sm)',
          backgroundColor: 'var(--bg-canvas)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>
            {title} ({list.length}) {isRequired && <span style={{ color: 'var(--status-danger)' }}>*</span>}
          </span>
          <Button variant="secondary" size="sm" onClick={handleAddItem} aria-label={t('ui.addItemTo').replace('{name}', name)}>
            {t('ui.addItem')}
          </Button>
        </div>
        {description && (
          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 8 }}>{description}</div>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {list.map((item, idx) => (
            <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                type="text"
                value={String(item)}
                onChange={(e) => handleUpdateItem(idx, e.target.value)}
                aria-label={t('ui.itemAt').replace('{name}', name).replace('{index}', String(idx + 1))}
                style={{
                  flex: 1,
                  padding: '4px 8px',
                  borderRadius: 'var(--radius-sm)',
                  border: '1px solid var(--border-subtle)',
                  backgroundColor: 'var(--bg-surface)',
                  color: 'var(--text-primary)',
                  fontSize: 12,
                }}
              />
              <button
                type="button"
                onClick={() => handleRemoveItem(idx)}
                aria-label={t('ui.removeItemAt').replace('{name}', name).replace('{index}', String(idx + 1))}
                style={{
                  color: 'var(--status-danger)',
                  cursor: 'pointer',
                  padding: '2px 6px',
                  fontSize: 14,
                  border: 'none',
                  background: 'none',
                }}
              >
                <Icon name="close" />
              </button>
            </div>
          ))}
          {list.length === 0 && (
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic' }}>
              {t('ui.noItemsInList')}
            </div>
          )}
        </div>
      </div>
    );
  }

  // 5. Nested Object
  if (schema.type === 'object') {
    const objVal = (value && typeof value === 'object' && !Array.isArray(value)) ? (value as Record<string, unknown>) : {};
    const childProps = schema.properties || {};
    const requiredKeys = new Set(schema.required || []);

    const handlePropChange = (pKey: string, pVal: unknown) => {
      const next = { ...objVal, [pKey]: pVal };
      if (pVal === undefined || pVal === '') {
        delete next[pKey];
      }
      onChange(next);
    };

    return (
      <div
        style={{
          marginBottom: 14,
          padding: 10,
          border: '1px solid var(--border-subtle)',
          borderRadius: 'var(--radius-sm)',
          backgroundColor: level > 0 ? 'var(--bg-canvas)' : 'var(--bg-surface)',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            cursor: 'pointer',
            userSelect: 'none',
            marginBottom: collapsed ? 0 : 8,
          }}
          onClick={() => setCollapsed(!collapsed)}
        >
          <span style={{ fontSize: 13, fontWeight: 600 }}>
            {collapsed ? <Icon name="play" /> : <Icon name="down" />} {title} {isRequired && <span style={{ color: 'var(--status-danger)' }}>*</span>}
          </span>
          <Badge variant="neutral">{Object.keys(objVal).length} {t('ui.set')}</Badge>
        </div>

        {description && !collapsed && (
          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 8 }}>{description}</div>
        )}

        {!collapsed && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, paddingLeft: 6 }}>
            {Object.entries(childProps).map(([childKey, childSchema]) => (
              <SchemaField
                key={childKey}
                name={childKey}
                schema={childSchema}
                value={objVal[childKey]}
                onChange={(newChildVal) => handlePropChange(childKey, newChildVal)}
                isRequired={requiredKeys.has(childKey)}
                level={level + 1}
              />
            ))}
            {Object.keys(childProps).length === 0 && (
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                {t('ui.freeformObjectUseAdvancedJsonModeToEditCustomKeys')}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // 6. Default: String / Freeform
  const strVal = value !== undefined && value !== null ? String(value) : '';
  const isMultiline =
    name.includes('template') ||
    name.includes('instruction') ||
    name.includes('prompt') ||
    name.includes('code') ||
    name.includes('notes');

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
        <label style={{ fontSize: 13, fontWeight: 500 }}>
          {title} {isRequired && <span style={{ color: 'var(--status-danger)' }}>*</span>}
        </label>
        {isRequired && <Badge variant="warning">{t('ui.required2')}</Badge>}
      </div>

      {isMultiline ? (
        <textarea
          rows={3}
          value={strVal}
          onChange={(e) => onChange(e.target.value)}
          aria-label={title}
          style={{
            width: '100%',
            padding: '6px 10px',
            borderRadius: 'var(--radius-sm)',
            border: `1px solid ${isSecret ? 'var(--status-danger)' : 'var(--border-subtle)'}`,
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-primary)',
            fontSize: 12,
            fontFamily: 'var(--font-mono)',
          }}
        />
      ) : (
        <input
          type="text"
          value={strVal}
          onChange={(e) => onChange(e.target.value)}
          aria-label={title}
          placeholder={schema.default !== undefined ? String(schema.default) : ''}
          style={{
            width: '100%',
            padding: '6px 10px',
            borderRadius: 'var(--radius-sm)',
            border: `1px solid ${isSecret ? 'var(--status-danger)' : 'var(--border-subtle)'}`,
            backgroundColor: 'var(--bg-canvas)',
            color: 'var(--text-primary)',
            fontSize: 13,
          }}
        />
      )}

      {/* Secret leak detector warning */}
      {isSecret && <SecurityNotice />}

      {description && !isSecret && (
        <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 }}>{description}</div>
      )}
    </div>
  );
}
