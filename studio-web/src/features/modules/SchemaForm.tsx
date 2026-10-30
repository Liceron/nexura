// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { Icon } from '../../components/Icon';
import { useT } from '../../i18n';
import { useState, useEffect, type ReactNode } from 'react';
import type { JsonSchema } from './types';
import { isPotentialSecret } from './types';
import { SchemaField, SecurityNotice } from './SchemaField';
import { Button } from '../../components/Button';
import { Badge } from '../../components/Badge';

interface SchemaFormProps {
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (val: Record<string, unknown>) => void;
  title?: string;
  /** Replaces the default control of a named field (e.g. the connector credential picker). */
  fieldOverrides?: Record<string, (value: unknown, onChange: (v: unknown) => void) => ReactNode>;
}

export function SchemaForm({ schema, value, onChange, title, fieldOverrides }: SchemaFormProps) {
  const t = useT();
  const [isAdvanced, setIsAdvanced] = useState(false);
  const [jsonText, setJsonText] = useState(() => JSON.stringify(value || {}, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);

  // Keep jsonText in sync with value when switching tabs or external updates
  useEffect(() => {
    setJsonText(JSON.stringify(value || {}, null, 2));
    setJsonError(null);
  }, [value, isAdvanced]);

  const properties = schema.properties || {};
  const requiredSet = new Set(schema.required || []);
  const propKeys = Object.keys(properties);

  // Separate known schema keys from preserved unknown extra keys
  const extraKeys = Object.keys(value || {}).filter((k) => !propKeys.includes(k));

  const handleFieldChange = (propKey: string, propVal: unknown) => {
    const next = { ...(value || {}) };
    if (propVal === undefined || propVal === '') {
      delete next[propKey];
    } else {
      next[propKey] = propVal;
    }
    onChange(next);
  };

  const handleJsonBlur = () => {
    try {
      const parsed = JSON.parse(jsonText);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        setJsonError(t('schema.objectError'));
        return;
      }
      setJsonError(null);
      onChange(parsed);
    } catch (err: unknown) {
      setJsonError(err instanceof Error ? err.message : t('schema.invalidJson'));
    }
  };

  const handleApplyDefaults = () => {
    const defaults: Record<string, unknown> = { ...(value || {}) };
    for (const [k, p] of Object.entries(properties)) {
      if (p.default !== undefined && defaults[k] === undefined) {
        defaults[k] = p.default;
      }
    }
    onChange(defaults);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Header with Mode Toggle */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {title && <span style={{ fontSize: 13, fontWeight: 600 }}>{title}</span>}
          {propKeys.length > 0 && <Badge variant="neutral">{propKeys.length} {t('ui.fields')}</Badge>}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {propKeys.length > 0 && !isAdvanced && (
            <Button variant="secondary" size="sm" onClick={handleApplyDefaults}>
              {t('ui.applyDefaults')}
            </Button>
          )}
          <Button
            variant={isAdvanced ? 'primary' : 'secondary'}
            size="sm"
            onClick={() => setIsAdvanced(!isAdvanced)}
            aria-label={t('ui.toggleAdvancedJsonEditor')}
          >
            {isAdvanced ? t('ui.visualForm') : t('ui.advancedJson')}
          </Button>
        </div>
      </div>

      {/* Advanced JSON Mode */}
      {isAdvanced ? (
        <div>
          <textarea
            value={jsonText}
            onChange={(e) => {
              setJsonText(e.target.value);
              try {
                JSON.parse(e.target.value);
                setJsonError(null);
              } catch (err: unknown) {
                setJsonError(err instanceof Error ? err.message : t('schema.invalidJson'));
              }
            }}
            onBlur={handleJsonBlur}
            aria-label={t('ui.advancedJsonConfiguration')}
            rows={8}
            style={{
              width: '100%',
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              padding: 10,
              borderRadius: 'var(--radius-sm)',
              border: `1px solid ${jsonError ? 'var(--status-danger)' : 'var(--border-subtle)'}`,
              backgroundColor: 'var(--bg-canvas)',
              color: 'var(--text-primary)',
            }}
          />
          {jsonError && (
            <div style={{ color: 'var(--status-danger)', fontSize: 'var(--type-meta)', marginTop: 4 }}>
              {t('ui.syntaxError') + " "}{jsonError}
            </div>
          )}
          {(() => {
            let hasSecret = false;
            try {
              const parsed = JSON.parse(jsonText);
              if (parsed && typeof parsed === 'object') {
                hasSecret = Object.values(parsed).some((v) => isPotentialSecret(v));
              }
            } catch {
              // ignore
            }
            return hasSecret ? (
              <div
                style={{
                  marginTop: 6,
                  padding: '6px 10px',
                  backgroundColor: 'var(--status-danger-bg)',
                  color: 'var(--status-danger)',
                  fontSize: 'var(--type-meta)',
                  borderRadius: 'var(--radius-sm)',
                  lineHeight: 1.5,
                }}
              >
                <Icon name="warning" /> <strong>{t('ui.securityNotice')}</strong> {t('ui.thisFieldLooksLikeARawSecretOrApiKeyDoNotEmbedCredentialsInWorkflowSpecifi')}
              </div>
            ) : null;
          })()}
        </div>
      ) : (
        /* Visual Form Mode */
        <div>
          {propKeys.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              {propKeys.map((key) => fieldOverrides?.[key] ? (
                <div key={key}>
                  {fieldOverrides[key]((value || {})[key], (newVal) => handleFieldChange(key, newVal))}
                  {isPotentialSecret((value || {})[key]) && <SecurityNotice />}
                </div>
              ) : (
                <SchemaField
                  key={key}
                  name={key}
                  schema={properties[key]}
                  value={(value || {})[key]}
                  onChange={(newVal) => handleFieldChange(key, newVal)}
                  isRequired={requiredSet.has(key)}
                />
              ))}
            </div>
          ) : (
            <div style={{ padding: '12px 0', fontSize: 12, color: 'var(--text-secondary)' }}>
              {t('ui.noStructuredConfigurationSchemaSpecifiedForThisModuleSwitchToAdvancedJsonToCo')}
            </div>
          )}

          {/* Preserved Unknown / Extra Fields Notice */}
          {extraKeys.length > 0 && (
            <div
              style={{
                marginTop: 8,
                padding: '8px 12px',
                backgroundColor: 'var(--bg-canvas)',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                fontSize: 'var(--type-meta)',
              }}
            >
              <div style={{ fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 4 }}>
                {t('ui.preservedCustomFields')}{extraKeys.length}):
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {extraKeys.map((k) => {
                  const val = (value || {})[k];
                  const isSecret = isPotentialSecret(val);
                  return (
                    <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <Badge variant={isSecret ? 'danger' : 'neutral'}>
                          {k}: {String(val)}
                        </Badge>
                      </div>
                      {isSecret && (
                        <div
                          style={{
                            padding: '6px 10px',
                            backgroundColor: 'var(--status-danger-bg)',
                            color: 'var(--status-danger)',
                            fontSize: 'var(--type-meta)',
                            borderRadius: 'var(--radius-sm)',
                            lineHeight: 1.5,
                          }}
                        >
                          <Icon name="warning" /> <strong>{t('ui.securityNotice')}</strong> {t('ui.thisFieldLooksLikeARawSecretOrApiKeyDoNotEmbedCredentialsInWorkflowSpecifi')}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
