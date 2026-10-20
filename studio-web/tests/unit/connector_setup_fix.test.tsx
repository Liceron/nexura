import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const state: { list: (a: { offset?: number }) => unknown; calls: Array<{ offset?: number }> } = {
  list: () => ({ items: [] }),
  calls: [],
};
const api = {
  manager: {
    client: {
      credentials: {
        list: async (a: { offset?: number }) => {
          state.calls.push(a);
          return state.list(a);
        },
      },
    },
  },
};
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

import { CredentialRefPicker, OperationSelect } from '../../src/features/modules/ConnectorFields';
import { SchemaForm } from '../../src/features/modules/SchemaForm';
import { mappingOperation, moduleCredentialTypes } from '../../src/features/modules/connectors';

const cred = (id: string, type: string) => ({ credential_id: id, credential_type: type, description: '', status: 'active' });

describe('P14 connector setup fixes', () => {
  it('pages through every credential until has_more is false', async () => {
    state.calls = [];
    state.list = (a) =>
      (a.offset ?? 0) === 0
        ? { items: Array.from({ length: 100 }, (_, i) => cred('g' + i, 'github_token')), has_more: true }
        : (a.offset ?? 0) === 100
          ? { items: Array.from({ length: 100 }, (_, i) => cred('h' + i, 'github_token')), has_more: true }
          : { items: [cred('late', 'slack_bot_token')], has_more: false };
    render(<CredentialRefPicker types={['slack_bot_token']} value="" onChange={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('credential-ref-select')).toHaveProperty('disabled', false));
    expect(state.calls.map((c) => c.offset)).toEqual([0, 100, 200]);
    expect(screen.getByRole('option', { name: 'late' })).toBeTruthy();
    expect(screen.queryByTestId('credential-ref-empty')).toBeNull();
  });

  it('shows the empty state only when no page has a match', async () => {
    state.list = () => ({ items: [cred('a', 'github_token')], has_more: false });
    render(<CredentialRefPicker types={['slack_bot_token']} value="" onChange={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('credential-ref-empty')).toBeTruthy());
  });

  it('an unknown reference is hidden and cannot be re-selected as a value', async () => {
    state.list = () => ({ items: [cred('slack_a', 'slack_bot_token')], has_more: false });
    const onChange = vi.fn();
    const { container } = render(
      <CredentialRefPicker types={['slack_bot_token']} value="xoxb-1234567890-abcdefghij" onChange={onChange} />,
    );
    await waitFor(() => expect(screen.getByTestId('credential-ref-unknown')).toBeTruthy());
    expect(screen.getByTestId('credential-ref-unknown').textContent).toBe('unknown reference (hidden)');
    expect(container.innerHTML).not.toContain('xoxb-');
    fireEvent.change(screen.getByTestId('credential-ref-select'), { target: { value: '__unknown_reference__' } });
    expect(onChange).not.toHaveBeenCalled();
  });

  it('an unknown operation is marked as a write needing approval', () => {
    const ops = [{ operation: 'list_channels', write: false }];
    render(<OperationSelect operations={ops} value="drop_table" onChange={() => {}} />);
    expect(screen.getByTestId('operation-unknown').textContent).toBe(
      'unknown operation, treated as a write (writes - needs approval)',
    );
  });

  it('reads the operation from a string or a $literal mapping', () => {
    expect(mappingOperation('post_message')).toBe('post_message');
    expect(mappingOperation({ $literal: 'post_message' })).toBe('post_message');
    expect(mappingOperation({ $literal: 5 })).toBe('');
    expect(mappingOperation(undefined)).toBe('');
  });

  it('collects required credential types of modules, deduplicated', () => {
    expect(
      moduleCredentialTypes([
        { required_credential_types: ['slack_bot_token', 'x'] },
        { required_credential_types: ['slack_bot_token'] },
        {},
      ] as never),
    ).toEqual(['slack_bot_token', 'x']);
  });

  it('a secret-looking credential_ref still shows the security notice with the picker override', async () => {
    state.list = () => ({ items: [], has_more: false });
    const { container } = render(
      <SchemaForm
        schema={{ type: 'object', properties: { credential_ref: { type: 'string' } } } as never}
        value={{ credential_ref: 'xoxb-1234567890-abcdefghij' }}
        onChange={() => {}}
        fieldOverrides={{
          credential_ref: (v, set) => (
            <CredentialRefPicker types={['slack_bot_token']} value={String(v)} onChange={set as never} />
          ),
        }}
      />,
    );
    expect(container.textContent).toMatch(/Security Notice/);
    await waitFor(() => expect(screen.getByTestId('credential-ref-unknown')).toBeTruthy());
    expect(container.innerHTML).not.toContain('xoxb-');
  });
});
