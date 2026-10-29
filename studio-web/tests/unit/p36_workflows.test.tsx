// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useState, type ReactNode } from 'react';
import { cleanup, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActionDisclosure } from '../../src/components/ActionDisclosure';
import { WorkflowTabs } from '../../src/components/WorkflowTabs';
import { Modal } from '../../src/components/Modal';
import { WorkflowDetail } from '../../src/features/workflows/WorkflowDetail';
import { I18nProvider } from '../../src/i18n';
import type { Node, NodeChange, Edge, EdgeChange } from '@xyflow/react';

// Only local fake APIs and browser graph primitives. The editor and inspector are real.
const fixture = vi.hoisted(() => {
  const spec = { name: 'Round 2', entry_node: 'start', nodes: {
    start: { id: 'start', handler: 'synthetic_order_lookup', transitions: {} },
    done: { id: 'done', handler: 'synthetic_order_lookup', transitions: {} },
  } };
  const workflow = { workflow_id: 'wf-round2', name: 'Round 2', revision: 1, spec, updated_at: 1, spec_hash: 'hash' };
  const workflows = {
    get: vi.fn(async () => workflow),
    validate: vi.fn(async () => ({ valid: true, errors: [] })),
    update: vi.fn(async (_id: string, next: typeof spec, revision: number) => ({ ...workflow, spec: next, revision: revision + 1 })),
  };
  return { spec, workflow, workflows, api: { connection: { workspace: 'default', principal: 'local-owner', status: 'connected' }, manager: { client: {
    workflows,
    modules: { get: vi.fn(async () => ({ module_id: 'synthetic_order_lookup', version: '1.0.0', config_schema: { type: 'object', properties: {} }, input_schema: { type: 'object', properties: {} }, output_schema: { type: 'object', properties: {} } })), list: vi.fn(async () => ({ items: [] })) },
    request: vi.fn(async () => ({ datasets: [], items: [] })),
  } } } };
});
vi.mock('../../src/api/context', () => ({ useApi: () => fixture.api }));
vi.mock('@xyflow/react', async (importOriginal) => {
  const react = await import('react');
  const actual = await importOriginal<typeof import('@xyflow/react')>();
  return {
    ReactFlowProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
    ReactFlow: ({ children, onNodesChange }: { children: ReactNode; onNodesChange: (changes: NodeChange[]) => void }) => <div data-testid="mock-graph">{children}
      <button onClick={() => onNodesChange([{ type: 'position', id: 'start', position: { x: 400, y: 200 }, dragging: false }])}>Simulate node move</button>
      <button onClick={() => onNodesChange([{ type: 'select', id: 'start', selected: true }])}>Simulate selection</button>
    </div>,
    useNodesState: (initial: Node[]) => {
      const [nodes, setNodes] = react.useState(initial);
      return [nodes, setNodes, (changes: NodeChange[]) => setNodes(current => actual.applyNodeChanges(changes, current))];
    },
    useEdgesState: (initial: Edge[]) => {
      const [edges, setEdges] = react.useState(initial);
      return [edges, setEdges, (changes: EdgeChange[]) => setEdges(current => actual.applyEdgeChanges(changes, current))];
    },
    applyNodeChanges: actual.applyNodeChanges, applyEdgeChanges: actual.applyEdgeChanges,
    useReactFlow: () => ({ fitView: vi.fn(), zoomIn: vi.fn(), zoomOut: vi.fn() }),
    Background: () => null, MiniMap: () => <div data-testid="mock-minimap" />,
    Handle: () => null, Position: { Top: 'top', Bottom: 'bottom', Right: 'right' }, addEdge: vi.fn(),
  };
});

let phone = false;
beforeEach(() => {
  phone = false;
  localStorage.clear();
  vi.clearAllMocks();
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query.includes('max-width') ? phone : !phone, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
    for (let node: HTMLElement | null = this; node; node = node.parentElement) {
      if (node.hidden || node.hasAttribute('inert')) return [] as unknown as DOMRectList;
    }
    return [new DOMRect(0, 0, 100, 32)] as unknown as DOMRectList;
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });
const show = (children: ReactNode) => render(<I18nProvider>{children}</I18nProvider>);
const detail = () => show(<WorkflowDetail workflowId="wf-round2" onBack={vi.fn()} onOpenWorkflow={vi.fn()} />);

describe('Round 2 working surfaces', () => {
  it('saves moved coordinates while selection leaves a clean workflow unchanged', async () => {
    const user = userEvent.setup();
    detail();
    await screen.findByRole('heading', { name: 'Round 2' });
    await user.click(screen.getByRole('button', { name: 'Simulate selection' }));
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Simulate node move' }));
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fixture.workflows.update).toHaveBeenCalledWith('wf-round2', expect.objectContaining({
      nodes: expect.objectContaining({ start: expect.objectContaining({ presentation: { position: { x: 400, y: 200 } } }) }),
    }), 1));
  });

  it('opens actions by keyboard and restores focus after Escape and a child dialog', async () => {
    function Example() {
      const [open, setOpen] = useState(false);
      return <><ActionDisclosure label="More"><button onClick={() => setOpen(true)}>Duplicate</button></ActionDisclosure>
        <Modal open={open} onClose={() => setOpen(false)} title="Duplicate"><button onClick={() => setOpen(false)}>Cancel</button></Modal></>;
    }
    const user = userEvent.setup();
    show(<Example />);
    const trigger = screen.getByRole('button', { name: 'More' });
    trigger.focus();
    await user.keyboard('{Enter}');
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: 'Duplicate' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole('group')).not.toBeInTheDocument();
    await user.keyboard('{Enter}{Enter}');
    expect(screen.getByRole('dialog', { name: 'Duplicate' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(trigger).toHaveFocus();
  });

  it('roves tabs with arrows/Home/End and activates only on Enter', async () => {
    function Example() {
      const [active, setActive] = useState('graph');
      return <><WorkflowTabs tabs={[{ id: 'graph', label: 'Graph' }, { id: 'nodes', label: 'Nodes' }, { id: 'json', label: 'Specification' }]} active={active} onChange={setActive} label="Views" panelId="test-panel" />
        <div role="tabpanel" id="test-panel">{active}</div></>;
    }
    const user = userEvent.setup();
    show(<Example />);
    screen.getByRole('tab', { name: 'Graph' }).focus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Nodes' })).toHaveFocus();
    expect(screen.getByRole('tab', { name: 'Graph' })).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{End}{Enter}');
    expect(screen.getByRole('tabpanel')).toHaveTextContent('json');
    expect(screen.getAllByRole('tab').filter(tab => tab.tabIndex === 0)).toHaveLength(1);
    await user.keyboard('{Home}{Enter}');
    expect(screen.getByRole('tabpanel')).toHaveTextContent('graph');
  });

  it('keeps one primary, live unsaved-run warning and undo history when changing panels', async () => {
    const user = userEvent.setup();
    detail();
    await screen.findByRole('heading', { name: 'Round 2' });
    const run = screen.getByRole('button', { name: 'Run' });
    expect(run).toHaveClass('studio-button--primary');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(document.querySelectorAll('.studio-button--primary')).toHaveLength(1);
    await user.click(screen.getByRole('tab', { name: /^Nodes/ }));
    await user.click(screen.getByRole('button', { name: 'Edit start' }));
    const inspector = screen.getByLabelText('Node properties inspector');
    await user.click(within(inspector).getByRole('tab', { name: 'Routes' }));
    await user.selectOptions(within(inspector).getByLabelText('Default transition target'), 'done');
    expect(screen.getByRole('button', { name: 'Save' })).toHaveClass('studio-button--primary');
    expect(run).toHaveClass('studio-button--secondary');
    expect(document.querySelectorAll('.studio-button--primary')).toHaveLength(1);
    localStorage.clear(); // The warning must not wait for the one-second draft debounce.
    await user.click(run);
    expect(within(screen.getByRole('dialog')).getByRole('alert')).toHaveTextContent(/uncommitted local edits/i);
    await user.keyboard('{Escape}');
    await user.click(screen.getByRole('tab', { name: 'Specification' }));
    await user.click(screen.getByRole('tab', { name: /^Nodes/ }));
    expect(screen.getByTestId('accessible-node-start')).toHaveTextContent('default → done');
    await user.click(screen.getByRole('button', { name: 'Undo' }));
    expect(screen.getByTestId('accessible-node-start')).not.toHaveTextContent('default → done');
    await user.click(screen.getByRole('button', { name: 'Redo' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fixture.workflows.update).toHaveBeenCalledWith('wf-round2', expect.objectContaining({ nodes: expect.objectContaining({ start: expect.objectContaining({ default_transition: 'done' }) }) }), 1));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled());
    expect(screen.getByRole('button', { name: 'Run' })).toHaveClass('studio-button--primary');
    expect(screen.getByLabelText('Workflow revision')).toHaveTextContent('Revision: 2');
    await user.click(screen.getByRole('tab', { name: 'Tests' }));
    expect(screen.getByRole('button', { name: 'Run' })).toHaveClass('studio-button--secondary');
    expect(screen.getByRole('button', { name: 'Save' })).toHaveClass('studio-button--secondary');
  });

  it('starts phones in editable Nodes, traps drawer focus and returns to the selected row', async () => {
    phone = true;
    const user = userEvent.setup();
    detail();
    expect(await screen.findByRole('tab', { name: /^Nodes/ })).toHaveAttribute('aria-selected', 'true');
    const edit = screen.getByRole('button', { name: 'Edit start' });
    await user.click(edit);
    const dialog = screen.getByRole('dialog', { name: 'Edit start' });
    await user.click(within(dialog).getByRole('tab', { name: 'Routes' }));
    await user.selectOptions(within(dialog).getByLabelText('Default transition target'), 'done');
    const close = within(dialog).getByRole('button', { name: 'Close dialog' });
    close.focus();
    await user.tab({ shift: true });
    expect(within(dialog).getByRole('button', { name: 'Delete Node' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(edit).toHaveFocus();
    expect(screen.getByTestId('accessible-node-start')).toHaveTextContent('default → done');
    await user.click(screen.getByRole('tab', { name: 'Graph' }));
    expect(screen.getByRole('tab', { name: 'Graph' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByTestId('mock-minimap')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Minimap' }));
    expect(screen.getByTestId('mock-minimap')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: /^Nodes/ }));
    await user.click(screen.getByRole('button', { name: 'Edit done' }));
    const confirmDelete = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true);
    await user.click(within(screen.getByRole('dialog', { name: 'Edit done' })).getByRole('button', { name: 'Delete Node' }));
    expect(screen.getByTestId('accessible-node-done')).toBeInTheDocument();
    await user.click(within(screen.getByRole('dialog', { name: 'Edit done' })).getByRole('button', { name: 'Delete Node' }));
    expect(confirmDelete).toHaveBeenCalledWith('Delete node “done” and its connections?');
    expect(screen.queryByTestId('accessible-node-done')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add step' })).toHaveFocus();
  });
});
