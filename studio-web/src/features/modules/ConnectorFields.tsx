// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';
import { useApi } from '../../api/context';
import { useBuilderT } from '../builder/composite';
import {
  credentialChoices,
  operationMarkerKey,
  type ConnectorOperation,
  type CredentialChoice,
} from './connectors';

const UNKNOWN_REF = '__unknown_reference__';

const selectStyle = {
  width: '100%',
  padding: '6px 8px',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--border-subtle)',
  backgroundColor: 'var(--bg-surface)',
  color: 'var(--text-primary)',
  fontSize: 12,
} as const;

/** Read/write marker list of a connector's operations (Module Registry and module drawer). */
export function OperationList({ operations }: { operations: ConnectorOperation[] }) {
  const t = useBuilderT();
  if (operations.length === 0) return null;
  return (
    <div data-testid="operation-list" style={{ margin: '0 0 8px 0', fontSize: 'var(--type-meta)' }}>
      <div style={{ fontWeight: 600, color: 'var(--text-secondary)' }}>{t('connectors.operationsHeading')}</div>
      <ul style={{ margin: '2px 0 0 0', padding: 0, listStyle: 'none' }}>
        {operations.map((op) => (
          <li key={op.operation} data-testid={`operation-${op.operation}`}>
            <code>{op.operation}</code>{' '}
            <span
              data-testid={`operation-marker-${op.operation}`}
              style={{ color: op.write ? 'var(--status-warning)' : 'var(--text-muted)' }}
            >
              {t(`connectors.${operationMarkerKey(op.write)}`)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Select of a connector's operations, each labelled reads or writes - needs approval. */
export function OperationSelect({
  operations,
  value,
  onChange,
}: {
  operations: ConnectorOperation[];
  value: string;
  onChange: (operation: string) => void;
}) {
  const t = useBuilderT();
  const known = operations.some((o) => o.operation === value);
  const UNKNOWN = '__unknown_operation__';
  return (
    <div style={{ padding: '8px 10px', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)' }}>
      <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
        {t('connectors.operation')}
        <select
          aria-label={t('connectors.operation')}
          data-testid="operation-select"
          value={value && !known ? UNKNOWN : value}
          onChange={(e) => {
            if (e.target.value !== UNKNOWN) onChange(e.target.value);
          }}
          style={{ ...selectStyle, marginTop: 4 }}
        >
          <option value="">{t('connectors.pickOperation')}</option>
          {value && !known && (
            <option value={UNKNOWN} data-testid="operation-unknown">
              {t('connectors.unknownOperation')}
            </option>
          )}
          {operations.map((op) => (
            <option key={op.operation} value={op.operation}>
              {`${op.operation} (${t(`connectors.${operationMarkerKey(op.write)}`)})`}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

/** Picker of workspace credential references of the connector's type (id and description only). */
export function CredentialRefPicker({
  types,
  value,
  onChange,
}: {
  types: string[];
  value: string;
  onChange: (credentialId: string) => void;
}) {
  const { manager } = useApi();
  const t = useBuilderT();
  const [choices, setChoices] = useState<CredentialChoice[] | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      const all: never[] = [];
      const limit = 100;
      for (let offset = 0, page = 0; page < 1000; page++) {
        const res = (await manager.client.credentials.list({ limit, offset })) as {
          items?: unknown[];
          has_more?: boolean;
        };
        const items = res.items ?? [];
        all.push(...(items as never[]));
        if (!res.has_more || items.length === 0) break;
        offset += items.length;
      }
      return all;
    })()
      .then((all) => {
        if (alive) setChoices(credentialChoices(all, types));
      })
      .catch(() => {
        if (alive) setChoices([]);
      });
    return () => {
      alive = false;
    };
  }, [manager, types.join('|')]);

  const typeLabel = types.join(', ');
  const known = (choices ?? []).some((c) => c.id === value);
  return (
    <div style={{ marginBottom: 12 }} data-testid="credential-ref-picker">
      <label style={{ display: 'block', fontSize: 13, fontWeight: 500, marginBottom: 4 }}>
        {t('connectors.credentialRef')}
        <select
          aria-label={t('connectors.credentialRef')}
          data-testid="credential-ref-select"
          value={value && choices !== null && !known ? UNKNOWN_REF : value}
          onChange={(e) => {
            if (e.target.value !== UNKNOWN_REF) onChange(e.target.value);
          }}
          disabled={choices === null}
          style={{ ...selectStyle, marginTop: 4 }}
        >
          <option value="">{t('connectors.pickCredential')}</option>
          {value && choices !== null && !known && (
            <option value={UNKNOWN_REF} data-testid="credential-ref-unknown">
              {t('connectors.unknownReference')}
            </option>
          )}
          {(choices ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.description ? `${c.id} - ${c.description}` : c.id}
            </option>
          ))}
        </select>
      </label>
      {choices !== null && choices.length === 0 && (
        <div data-testid="credential-ref-empty" style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)', marginTop: 4 }}>
          {t('connectors.noCredentialOfType', { type: typeLabel })}{' '}
          <a href="#/credentials" data-testid="credential-ref-add-link">
            {t('connectors.addInCredentials')}
          </a>
        </div>
      )}
    </div>
  );
}
