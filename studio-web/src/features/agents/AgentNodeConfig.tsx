// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useI18n } from '../../i18n';
import { useAgentList } from './useAgents';

const fieldStyle = {
  width: '100%',
  padding: '6px 8px',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--border-subtle)',
  backgroundColor: 'var(--bg-canvas)',
  color: 'var(--text-primary)',
  fontSize: 12,
  boxSizing: 'border-box',
} as const;

const labelStyle = { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, fontWeight: 600 } as const;

export interface AgentNodeConfigValue {
  agent_id?: unknown;
  input_key?: unknown;
  output_key?: unknown;
  [key: string]: unknown;
}

/**
 * Config of a `nexura.agents.agent` node: a picker of the workspace's saved agents plus the
 * state keys it reads and writes. An id that is no longer a saved agent is shown as missing.
 */
export function AgentNodeConfig({
  value,
  onChange,
}: {
  value: AgentNodeConfigValue;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const { t } = useI18n();
  const { agents, error } = useAgentList();
  const agentId = typeof value.agent_id === 'string' ? value.agent_id : '';
  const inputKey = typeof value.input_key === 'string' ? value.input_key : '';
  const outputKey = typeof value.output_key === 'string' ? value.output_key : '';
  const missing = agentId !== '' && agents !== null && !agents.some((a) => a.agent_id === agentId);
  const chosen = agents?.find((a) => a.agent_id === agentId);

  const update = (key: string, v: string) => {
    const next: Record<string, unknown> = { ...value };
    if (v === '') delete next[key];
    else next[key] = v;
    onChange(next);
  };

  return (
    <div data-testid="agent-node-config" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <label style={labelStyle}>
        {t('agents.pickerLabel')}
        <select
          data-testid="agent-node-picker"
          aria-label={t('agents.pickerLabel')}
          value={agentId}
          onChange={(e) => update('agent_id', e.target.value)}
          style={fieldStyle}
        >
          <option value="">{t('agents.pickerNone')}</option>
          {(agents ?? []).map((a) => (
            <option key={a.agent_id} value={a.agent_id}>
              {a.name} ({t('agents.revision').replace('{revision}', String(a.revision))})
            </option>
          ))}
          {missing && (
            <option value={agentId}>
              {agentId} ({t('agents.pickerMissing')})
            </option>
          )}
        </select>
      </label>
      {agents === null && !error && (
        <span style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>{t('common.loading')}</span>
      )}
      {error && (
        <span role="alert" style={{ fontSize: 'var(--type-meta)', color: 'var(--status-danger)' }}>
          {t('agents.pickerError')} {error}
        </span>
      )}
      {agents !== null && agents.length === 0 && !missing && (
        <span data-testid="agent-node-empty" style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>
          {t('agents.pickerEmpty')}
        </span>
      )}
      {missing && (
        <span role="alert" data-testid="agent-node-missing" style={{ fontSize: 'var(--type-meta)', color: 'var(--status-warning)' }}>
          {t('agents.pickerMissingHelp').replace('{id}', agentId)}
        </span>
      )}
      {!agentId && (
        <span style={{ fontSize: 'var(--type-meta)', color: 'var(--status-danger)' }}>{t('agents.pickerRequired')}</span>
      )}
      {chosen && (
        <span data-testid="agent-node-summary" style={{ fontSize: 'var(--type-meta)', color: 'var(--text-secondary)' }}>
          {chosen.model} · {t('agents.toolCount').replace('{count}', String(chosen.tool_count))}
          {(chosen.approval_tools?.length ?? 0) > 0 ? ` · ${t('agents.needsApproval')}` : ''}
        </span>
      )}
      <label style={labelStyle}>
        {t('agents.inputKey')}
        <input
          data-testid="agent-node-input-key"
          value={inputKey}
          placeholder={t('ui.input')}
          maxLength={200}
          onChange={(e) => update('input_key', e.target.value)}
          style={fieldStyle}
        />
        <span style={{ fontSize: 'var(--type-meta)', fontWeight: 400, color: 'var(--text-secondary)' }}>{t('agents.inputKeyHelp')}</span>
      </label>
      <label style={labelStyle}>
        {t('agents.outputKey')}
        <input
          data-testid="agent-node-output-key"
          value={outputKey}
          placeholder={t('ui.output')}
          maxLength={200}
          onChange={(e) => update('output_key', e.target.value)}
          style={fieldStyle}
        />
        <span style={{ fontSize: 'var(--type-meta)', fontWeight: 400, color: 'var(--text-secondary)' }}>{t('agents.outputKeyHelp')}</span>
      </label>
    </div>
  );
}
