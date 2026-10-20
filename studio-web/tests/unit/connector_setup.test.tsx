import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  connectorOperations,
  credentialChoices,
  operationMarkerKey,
  requiredCredentialTypes,
} from '../../src/features/modules/connectors';
import { OperationList, OperationSelect } from '../../src/features/modules/ConnectorFields';

const slack = {
  input_schema: { type: 'object', properties: { operation: { type: 'string', enum: ['list_channels', 'post_message'] } } },
  display: {
    examples: [
      { operation: 'post_message', write: true },
      { operation: 'list_channels', write: false },
      { operation: 'post_message', write: true },
      { nonsense: 1 },
    ],
  },
  required_credential_types: ['slack_bot_token'],
  write_operations: ['post_message'],
};

describe('connector setup helpers (P14)', () => {
  it('lists operations with write flags from display.examples, de-duplicated', () => {
    expect(connectorOperations(slack as never)).toEqual([
      { operation: 'post_message', write: true },
      { operation: 'list_channels', write: false },
    ]);
  });

  it('falls back to the input enum and write_operations', () => {
    const ops = connectorOperations({ ...slack, display: { examples: [] } } as never);
    expect(ops).toEqual([
      { operation: 'list_channels', write: false },
      { operation: 'post_message', write: true },
    ]);
    expect(connectorOperations(null)).toEqual([]);
    expect(connectorOperations({ input_schema: {}, display: null } as never)).toEqual([]);
  });

  it('marks writes as needing approval and reads as reads', () => {
    expect(operationMarkerKey(true)).toBe('writesNeedApproval');
    expect(operationMarkerKey(false)).toBe('reads');
  });

  it('reads required credential types defensively', () => {
    expect(requiredCredentialTypes(slack as never)).toEqual(['slack_bot_token']);
    expect(requiredCredentialTypes(undefined)).toEqual([]);
    expect(requiredCredentialTypes({ required_credential_types: [1, '', 'x'] } as never)).toEqual(['x']);
  });

  it('filters credentials by type, drops revoked, exposes only id and description', () => {
    const creds = [
      { credential_id: 'slack_a', credential_type: 'slack_bot_token', description: 'bot', status: 'active', masked_hint: 'xox...', secret: 'xoxb-leak' },
      { credential_id: 'gh', credential_type: 'github_token', description: 'other', status: 'active' },
      { credential_id: 'slack_old', credential_type: 'slack_bot_token', description: '', status: 'revoked' },
      { credential_type: 'slack_bot_token' },
    ];
    const out = credentialChoices(creds as never, ['slack_bot_token']);
    expect(out).toEqual([{ id: 'slack_a', description: 'bot' }]);
    expect(JSON.stringify(out)).not.toContain('xoxb-leak');
    expect(credentialChoices(creds as never, [])).toEqual([]);
  });

  it('renders operation markers and the operation select', () => {
    const ops = connectorOperations(slack as never);
    render(
      <div>
        <OperationList operations={ops} />
        <OperationSelect operations={ops} value="" onChange={() => {}} />
      </div>,
    );
    expect(screen.getByTestId('operation-marker-post_message').textContent).toBe('writes - needs approval');
    expect(screen.getByTestId('operation-marker-list_channels').textContent).toBe('reads');
    expect(screen.getByRole('option', { name: 'post_message (writes - needs approval)' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'list_channels (reads)' })).toBeTruthy();
  });
});
