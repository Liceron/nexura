import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

vi.mock('../../src/api/context', () => ({
  useApi: () => ({
    manager: {
      client: {
        modules: { list: async () => ({ items: [{ module_id: 'alpha', is_available: true }, { module_id: 'beta', is_available: true }] }) },
        workflows: { list: async () => ({ items: [] }), get: async () => ({}) },
      },
    },
  }),
}));

import { CompositeInspector } from '../../src/features/builder/CompositeInspector';

const base = { nodeId: 'n', label: 'n', handler: '', requires_approval: false, config: {}, input_mapping: {}, output_mapping: {}, isEntry: false };

describe('composite inspector review: saved values stay visible', () => {
  it('parallel branch not in available-module list stays visible and removable', async () => {
    render(<CompositeInspector nodeId="n" data={{ ...base, parallel: ['legacy_handler'] } as any} onUpdate={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText(/alpha/)).toBeTruthy());
    expect(screen.queryByLabelText(/legacy_handler/)).not.toBeNull();
  });
  it('map handler not in module list is still shown in the select', async () => {
    render(<CompositeInspector nodeId="n" data={{ ...base, handler: 'legacy_handler', map_over: 'items' } as any} onUpdate={() => {}} />);
    await waitFor(() => expect(screen.getByText('alpha')).toBeTruthy());
    const sel = document.getElementById('n-map-handler') as HTMLSelectElement;
    expect(sel.value).toBe('legacy_handler');
  });
});
