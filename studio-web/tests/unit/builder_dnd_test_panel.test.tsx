// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type { ReactNode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import type { Node, Edge, NodeChange, EdgeChange } from '@xyflow/react';
import type { RunRecord, RunEvent, WorkflowSpec } from '@liceron/nexura-sdk';
import { WorkflowCanvas, freeAddPosition } from '../../src/features/builder/WorkflowCanvas';
import { WorkflowTestPanel } from '../../src/features/builder/WorkflowTestPanel';
import { WorkflowDetail } from '../../src/features/workflows/WorkflowDetail';
import { MODULE_DRAG_TYPE } from '../../src/features/modules/ModulePalette';
import { setMembershipSource } from '../../src/api/membership';
import { workflowMessageKey } from '../../src/features/runs/runInput';
import { I18nProvider } from '../../src/i18n';
import hi from '../../src/i18n/hi.json';
import { loadWorkflowDraft } from '../../src/features/workflows/persistence';

// Local fake service only. React Flow's browser geometry is substituted; graph/history,
// palette, save validation, input parsing and the actual run stream hook remain real.
const fixture = vi.hoisted(() => {
  const descriptor = { module_id: 'local.fake', version: '1', title: 'Local fake model', description: 'Local test fixture', kind: 'model_adapter', requires_approval: true, is_available: true, config_schema: {}, input_schema: {}, output_schema: {} };
  const runs = { start: vi.fn(), get: vi.fn(), cancel: vi.fn(), streamEvents: vi.fn(), provideInput: vi.fn() };
  const workflows = { get: vi.fn(), update: vi.fn(), validate: vi.fn() };
  return { descriptor, runs, workflows, latestNodes: [] as Node[], setCenter: vi.fn(async () => true), fitBounds: vi.fn(async () => true), position: vi.fn((point: { x: number; y: number }) => ({ x: point.x / 2 - 10, y: point.y / 2 - 20 })), api: {
    connection: { workspace: 'local-test', principal: 'owner' }, manager: { client: {
      runs, workflows, modules: { list: vi.fn(), get: vi.fn() }, request: vi.fn(async () => ({ items: [] })),
    } },
  } };
});
vi.mock('../../src/api/context', () => ({ useApi: () => fixture.api }));
vi.mock('@xyflow/react', async importOriginal => {
  const react = await import('react');
  const actual = await importOriginal<typeof import('@xyflow/react')>();
  return { ...actual,
    ReactFlowProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
    ReactFlow: ({ nodes, children }: { nodes: Node[]; children: ReactNode }) => <div data-testid="flow">
      {nodes.map(node => <output key={node.id} data-testid="graph-node">{JSON.stringify(node)}</output>)}{children}
    </div>,
    useNodesState: (initial: Node[]) => {
      const [nodes, setNodes] = react.useState(initial);
      fixture.latestNodes = nodes;
      return [nodes, setNodes, (changes: NodeChange[]) => setNodes(current => actual.applyNodeChanges(changes, current))];
    },
    useEdgesState: (initial: Edge[]) => {
      const [edges, setEdges] = react.useState(initial);
      return [edges, setEdges, (changes: EdgeChange[]) => setEdges(current => actual.applyEdgeChanges(changes, current))];
    },
    useReactFlow: () => ({ screenToFlowPosition: fixture.position, setCenter: fixture.setCenter, fitBounds: fixture.fitBounds, getZoom: () => 1, fitView: vi.fn(), zoomIn: vi.fn(), zoomOut: vi.fn() }),
    Background: () => null, MiniMap: () => null,
  };
});

const spec: WorkflowSpec = { name: 'Local workflow', entry_node: 'start', nodes: { start: { id: 'start', handler: 'local.fake', input_mapping: { prompt: 'question' } } } };
const empty: WorkflowSpec = { name: 'Empty', entry_node: '', nodes: {} };
const event = (type: string, seq = 1, data = {}): RunEvent => ({ type, seq, node_id: 'start', data, timestamp: 1 });
const record = (status: RunRecord['status'] = 'completed', extra: Partial<RunRecord> = {}): RunRecord => ({
  run_id: 'run-local', workflow_id: 'wf-local', spec_hash: 'hash', status, step_count: 1,
  input_state: {}, current_state: { answer: 'Local fake answer' }, created_at: 1, updated_at: 1, events: [], ...extra,
});
let emit: ((value: RunEvent) => void) | undefined;
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); setMembershipSource(null); emit = undefined;
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query.includes('min-width'), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  fixture.api.manager.client.modules.list.mockResolvedValue({ items: [fixture.descriptor, { ...fixture.descriptor, module_id: 'internal.fake', internal: true }] });
  fixture.api.manager.client.modules.get.mockResolvedValue(fixture.descriptor);
  fixture.workflows.validate.mockResolvedValue({ valid: true, errors: [] });
  fixture.runs.start.mockResolvedValue(record());
  fixture.runs.get.mockResolvedValue(record());
  fixture.runs.cancel.mockResolvedValue(record('cancelled'));
  fixture.runs.streamEvents.mockImplementation(async function* (_id: string, { signal }: { signal: AbortSignal }) {
    while (!signal.aborted) {
      const next = await new Promise<RunEvent | null>(resolve => {
        const abort = () => resolve(null);
        signal.addEventListener('abort', abort, { once: true });
        emit = value => { signal.removeEventListener('abort', abort); resolve(value); };
      });
      if (!next) return;
      yield next;
    }
  });
});
afterEach(() => { cleanup(); setMembershipSource(null); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function canvas(props = {}) {
  return render(<I18nProvider><WorkflowCanvas workflowId="wf-local" initialSpec={empty} revision={1} onSaveSpec={vi.fn(async () => {})} {...props} /></I18nProvider>);
}
function surface() { return screen.getByTestId('flow').parentElement!; }
function dropPayload(payload: string, mime = MODULE_DRAG_TYPE) {
  const event = new MouseEvent('drop', { bubbles: true, clientX: 420, clientY: 240 });
  Object.defineProperty(event, 'dataTransfer', { value: { types: [mime], getData: () => payload } });
  fireEvent(surface(), event);
}
function drop(id = 'local.fake', mime = MODULE_DRAG_TYPE, version = '1') {
  dropPayload(JSON.stringify({ module_id: id, version }), mime);
}
function panel(props = {}) {
  return render(<I18nProvider><WorkflowTestPanel open wide active workflowId="wf-local" spec={spec} dirty={false} draftPending={false} canRun
    onSave={vi.fn(async () => true)} onClose={vi.fn()} onEvents={vi.fn()} {...props} /></I18nProvider>);
}
async function sendMessage() {
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Hello local fake' } });
  fireEvent.keyDown(screen.getByLabelText('Message'), { key: 'Enter' });
  await waitFor(() => expect(fixture.runs.start).toHaveBeenCalledTimes(1));
}

describe('module sidebar and canvas drops', () => {
  it('Round 2: dragging the second version resolves that exact descriptor', async () => {
    fixture.api.manager.client.modules.list.mockResolvedValue({ items: [fixture.descriptor, { ...fixture.descriptor, version: '2', title: 'Local fake v2', requires_approval: false }] });
    canvas();
    const title = await screen.findByText('Local fake v2');
    const transfer = { setData: vi.fn(), effectAllowed: '' };
    fireEvent.dragStart(title.closest('.studio-card')!, { dataTransfer: transfer });
    dropPayload(transfer.setData.mock.calls[0][1]);
    expect(JSON.parse(screen.getByTestId('graph-node').textContent!)).toMatchObject({
      position: { x: 200, y: 100 }, data: { module_id: 'local.fake', module_version: '2', requires_approval: false },
    });
    await act(async () => {});
  });

  it('Round 2: repeated click and keyboard adds leave earlier nodes uncovered', async () => {
    const user = userEvent.setup();
    canvas();
    const title = await screen.findByText('Local fake model');
    const add = title.closest('.studio-card')!.querySelector('button')!;
    vi.spyOn(surface(), 'getBoundingClientRect').mockReturnValue(new DOMRect(100, 50, 800, 600));
    await user.click(add);
    add.focus(); await user.keyboard('{Enter}');
    add.focus(); await user.keyboard(' ');
    const positions = screen.getAllByTestId('graph-node').map(node => JSON.parse(node.textContent!).position);
    expect(positions[0]).toEqual({ x: 240, y: 155 });
    for (let i = 0; i < positions.length; i++) {
      for (let j = i + 1; j < positions.length; j++) {
        expect(Math.abs(positions[i].x - positions[j].x) >= 224 || Math.abs(positions[i].y - positions[j].y) >= 160).toBe(true);
      }
    }
  });

  it('Round 2: ignores unknown version pairs and malformed payloads', async () => {
    canvas(); await screen.findByText('Local fake model');
    drop('local.fake', MODULE_DRAG_TYPE, 'missing');
    drop('unknown', MODULE_DRAG_TYPE, '1');
    for (const payload of ['local.fake', 'null', '[]', '"local.fake"', '{"module_id":"local.fake"}', '{"module_id":"local.fake","version":1}']) dropPayload(payload);
    expect(screen.queryByTestId('graph-node')).not.toBeInTheDocument();
    expect(fixture.position).not.toHaveBeenCalled();
  });

  it('Round 2: clicks and keyboard preserve the chosen version and drops stay exact even when occupied', async () => {
    fixture.api.manager.client.modules.list.mockResolvedValue({ items: [fixture.descriptor, { ...fixture.descriptor, title: 'Local fake v2', version: '2' }] });
    const user = userEvent.setup(); canvas();
    const title = await screen.findByText('Local fake v2');
    const add = title.closest('.studio-card')!.querySelector('button')!;
    await user.click(add); add.focus(); await user.keyboard('{Enter}');
    expect(screen.getAllByTestId('graph-node').map(node => JSON.parse(node.textContent!).data.module_version)).toEqual(['2', '2']);
    drop('local.fake', MODULE_DRAG_TYPE, '2');
    drop('local.fake', MODULE_DRAG_TYPE, '2');
    const dropped = screen.getAllByTestId('graph-node').slice(-2).map(node => JSON.parse(node.textContent!).position);
    expect(dropped).toEqual([{ x: 200, y: 100 }, { x: 200, y: 100 }]);
    await act(async () => {});
  });

  it('uses the dedicated MIME and catalog, converts drop coordinates, selects, saves approval flags, and undoes/redoes', async () => {
    const save = vi.fn(async () => {});
    canvas({ onSaveSpec: save });
    await screen.findByRole('button', { name: 'Add step: Local fake model' });
    const transfer = { setData: vi.fn(), effectAllowed: '' };
    fireEvent.dragStart(screen.getByText('Local fake model').closest('.studio-card')!, { dataTransfer: transfer });
    expect(transfer.setData).toHaveBeenCalledWith(MODULE_DRAG_TYPE, JSON.stringify({ module_id: 'local.fake', version: '1' }));
    fireEvent.dragOver(surface(), { dataTransfer: { types: [MODULE_DRAG_TYPE] } });
    expect(screen.getByText('Drop to add a step')).toBeInTheDocument();
    drop();
    expect(fixture.position).toHaveBeenCalledWith({ x: 420, y: 240 });
    const node = JSON.parse(screen.getByTestId('graph-node').textContent!);
    expect(node).toMatchObject({ position: { x: 200, y: 100 }, selected: true, data: { module_id: 'local.fake', requires_approval: true } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ nodes: expect.objectContaining({ [node.id]: expect.objectContaining({ requires_approval: true, presentation: { position: { x: 200, y: 100 } } }) }) }), 1));
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(screen.queryByTestId('graph-node')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Redo' }));
    expect(screen.getByTestId('graph-node')).toBeInTheDocument();
    await act(async () => {});
  });

  it('fills free spots on screen first, then zooms out to show every step, panning only for large graphs', async () => {
    const user = userEvent.setup(); canvas();
    const add = await screen.findByRole('button', { name: 'Add step: Local fake model' });
    vi.spyOn(surface(), 'getBoundingClientRect').mockReturnValue(new DOMRect(100, 50, 800, 600));
    // 100% zoom, no pan: the canvas shows flow x 0-800, y 0-600.
    const halved = fixture.position.getMockImplementation()!;
    onTestFinished(() => { fixture.position.mockImplementation(halved); });
    fixture.position.mockImplementation(point => ({ x: point.x - 100, y: point.y - 50 }));
    // The canvas centre is (400, 300); grid spots are 280 x 184 apart. Nine spots have their title
    // strip on screen (x 120/400/680, y 116/300/484): the first nine adds fill them in place.
    for (let i = 0; i < 9; i++) await user.click(add);
    expect(fixture.fitBounds).not.toHaveBeenCalled();
    expect(fixture.setCenter).not.toHaveBeenCalled();
    const placed = screen.getAllByTestId('graph-node').map(node => JSON.parse(node.textContent!).position);
    expect(new Set(placed.map(p => `${p.x},${p.y}`)).size).toBe(9);
    expect(placed.every(p => p.x >= 0 && p.y >= 0 && p.x <= 800 - 48 && p.y <= 600 - 48)).toBe(true);
    // The tenth has no free spot on screen: zoom out to every step (readable, so no pan).
    await user.click(add);
    const last = JSON.parse(screen.getAllByTestId('graph-node').at(-1)!.textContent!).position;
    expect(fixture.setCenter).not.toHaveBeenCalled();
    const [box, options] = fixture.fitBounds.mock.calls[0] as unknown as [{ x: number; y: number; width: number; height: number }, unknown];
    expect(options).toEqual({ padding: 0.1, duration: 200 });
    for (const p of [...placed, last]) {
      expect(p.x).toBeGreaterThanOrEqual(box.x); expect(p.y).toBeGreaterThanOrEqual(box.y);
      expect(p.x + 256).toBeLessThanOrEqual(box.x + box.width); expect(p.y + 160).toBeLessThanOrEqual(box.y + box.height);
    }
    // Drops never move the view.
    const calls = fixture.fitBounds.mock.calls.length + fixture.setCenter.mock.calls.length;
    drop();
    expect(fixture.fitBounds.mock.calls.length + fixture.setCenter.mock.calls.length).toBe(calls);
    await act(async () => {});
  });

  it('never overlaps a step when every grid spot near the centre is taken', () => {
    type Step = Parameters<typeof freeAddPosition>[1][number];
    const step = (x: number, y: number) => ({ id: `${x},${y}`, position: { x, y }, data: {} }) as unknown as Step;
    const nodes: Step[] = [];
    for (let row = -6; row <= 6; row++) for (let col = -6; col <= 6; col++) nodes.push(step(col * 280, row * 184));
    nodes.push(step(0, 7 * 184), step(0, 1288 + 200));
    const { position, reveal } = freeAddPosition({ x: 0, y: 0 }, nodes);
    expect(reveal).toBe(true);
    const overlaps = nodes.some(node =>
      position.x < node.position.x + 256 && position.x + 256 > node.position.x && position.y < node.position.y + 160 && position.y + 160 > node.position.y);
    expect(overlaps).toBe(false);
  });

  it('pans to a click-added step when showing every step would be too small to read', async () => {
    const user = userEvent.setup(); canvas();
    const add = await screen.findByRole('button', { name: 'Add step: Local fake model' });
    // A 200 x 150 canvas at 100% zoom: one spot on screen, and ten steps cannot fit at zoom 0.5.
    vi.spyOn(surface(), 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 200, 150));
    const halved = fixture.position.getMockImplementation()!;
    onTestFinished(() => { fixture.position.mockImplementation(halved); });
    fixture.position.mockImplementation(point => ({ ...point }));
    await user.click(add);
    expect(fixture.setCenter).not.toHaveBeenCalled();
    await user.click(add);
    const last = JSON.parse(screen.getAllByTestId('graph-node').at(-1)!.textContent!).position;
    expect(fixture.fitBounds).not.toHaveBeenCalled();
    expect(fixture.setCenter).toHaveBeenLastCalledWith(last.x + 128, last.y + 80, { zoom: 1, duration: 200 });
    await act(async () => {});
  });

  it('ignores text, files, links, unknown IDs and internal modules', async () => {
    canvas(); await screen.findByText('Local fake model');
    for (const mime of ['text/plain', 'Files', 'text/uri-list']) drop('local.fake', mime);
    drop('unknown'); drop('internal.fake');
    expect(screen.queryByTestId('graph-node')).not.toBeInTheDocument();
    expect(fixture.position).not.toHaveBeenCalled();
  });

  it('adds by Enter and Space at the visible canvas centre and remembers collapse', async () => {
    const user = userEvent.setup();
    const view = canvas();
    const add = await screen.findByRole('button', { name: 'Add step: Local fake model' });
    vi.spyOn(surface(), 'getBoundingClientRect').mockReturnValue(new DOMRect(100, 50, 800, 600));
    add.focus(); await user.keyboard('{Enter}');
    expect(fixture.position).toHaveBeenCalledWith({ x: 500, y: 350 });
    add.focus(); await user.keyboard(' ');
    expect(screen.getAllByTestId('graph-node')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Collapse' }));
    view.unmount(); canvas();
    expect(await screen.findByRole('button', { name: 'Modules' })).toHaveAttribute('aria-expanded', 'false');
    await act(async () => {});
  });

  it('hides additions and ignores drops for a viewer and explicit read-only canvas', async () => {
    setMembershipSource({ getMyMembership: async () => ({ role: 'viewer', permissions: [], workspace_id: 'local-test', principal_id: 'owner', profile: 'authenticated-shared-service' }) } as never);
    const view = canvas();
    await act(async () => {}); drop();
    expect(screen.queryByRole('button', { name: 'Modules' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('graph-node')).not.toBeInTheDocument();
    view.unmount(); setMembershipSource(null); canvas({ readOnly: true }); drop();
    await act(async () => {});
    expect(screen.queryByTestId('graph-node')).not.toBeInTheDocument();
  });

  it('autosaves a dropped node through the existing draft path', async () => {
    canvas(); await screen.findByText('Local fake model'); drop();
    await waitFor(() => {
      const draft = loadWorkflowDraft('local-test', 'owner', 'wf-local');
      expect(Object.values(draft?.spec.nodes ?? {})).toEqual([expect.objectContaining({ module_id: 'local.fake', requires_approval: true, presentation: { position: { x: 200, y: 100 } } })]);
    });
  });
});

describe('builder Test panel with local fake run transport', () => {
  it('Round 2: closed or inactive Test panels yield node statuses to the Run Console', async () => {
    fixture.runs.start.mockResolvedValue(record('completed', { events: [event('NODE_COMPLETED')] }));
    fixture.runs.get.mockResolvedValue(record('completed', { events: [event('NODE_COMPLETED')] }));
    const props = { workflowId: 'wf-local', initialSpec: spec, revision: 1, onSaveSpec: vi.fn(async () => {}), runNodeStatuses: new Map<'start', 'failed'>([['start', 'failed']]) };
    const view = render(<I18nProvider><WorkflowCanvas {...props} /></I18nProvider>);
    const status = () => fixture.latestNodes[0].data.executionStatus;
    await waitFor(() => expect(status()).toBe('failed'));
    fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    await sendMessage();
    await waitFor(() => expect(status()).toBe('completed'));
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(status()).toBe('failed');
    fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(status()).toBe('completed'));
    view.rerender(<I18nProvider><WorkflowCanvas {...props} active={false} /></I18nProvider>);
    await waitFor(() => expect(status()).toBe('failed'));
    view.rerender(<I18nProvider><WorkflowCanvas {...props} /></I18nProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    await waitFor(() => expect(status()).toBe('failed'));
  });

  it('requires Save and test for unsaved changes and never launches after a failed save', async () => {
    const save = vi.fn(async () => false);
    panel({ dirty: true, onSave: save });
    expect(screen.getByText('Save your changes before testing this workflow.')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save and test' }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(fixture.runs.start).not.toHaveBeenCalled();
  });

  it('maps a message, keeps Shift+Enter as newline, and sends once on Enter', async () => {
    panel();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Message'), 'first');
    await user.keyboard('{Shift>}{Enter}{/Shift}second');
    expect(screen.getByLabelText('Message')).toHaveValue('first\nsecond');
    expect(fixture.runs.start).not.toHaveBeenCalled();
    await user.keyboard('{Enter}');
    await waitFor(() => expect(fixture.runs.start).toHaveBeenCalledWith({ workflow_id: 'wf-local', initial_state: { question: 'first\nsecond' }, async_execution: true }));
    expect(await screen.findByText('answer: Local fake answer')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open run' })).toHaveAttribute('href', '#/runs/run-local');
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    expect(screen.queryByText('answer: Local fake answer')).not.toBeInTheDocument();
  });

  it('reuses JSON validation for other workflows and sends the exact object', async () => {
    panel({ spec: empty });
    const input = screen.getByLabelText('Initial State (JSON)');
    fireEvent.change(input, { target: { value: '[]' } });
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    fireEvent.change(input, { target: { value: '{"ticket":0,"enabled":false}' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(fixture.runs.start).toHaveBeenCalledWith(expect.objectContaining({ initial_state: { ticket: 0, enabled: false } })));
    expect(workflowMessageKey({ ...spec, nodes: { start: { input_mapping: { prompt: 'question', context: 'context' } } } })).toBeNull();
  });

  it('renders streamed progress and usage, highlights the node, offers approval review, and stops the run', async () => {
    const pending = record('pending_approval', { pending_approval: { approval_id: 'approval-local', node_id: 'start' } as RunRecord['pending_approval'] });
    fixture.runs.start.mockResolvedValue(record('running'));
    fixture.runs.get.mockResolvedValue(record('running'));
    const onEvents = vi.fn(); panel({ onEvents }); await sendMessage();
    await waitFor(() => expect(emit).toBeDefined());
    await act(async () => { emit!(event('NODE_STARTED')); });
    expect(screen.getByText('Node Started')).toBeInTheDocument();
    expect(onEvents).toHaveBeenLastCalledWith(expect.arrayContaining([expect.objectContaining({ type: 'NODE_STARTED' })]), 'start');
    await act(async () => { emit!(event('NODE_COMPLETED', 2, { model: 'local-fake', cost_usd: 0, usage: { input_tokens: 5 } })); });
    expect(screen.getByText(/model: local-fake/)).toBeInTheDocument();
    fixture.runs.get.mockResolvedValue(pending);
    await act(async () => { emit!(event('RUN_PENDING_APPROVAL', 3)); });
    expect(await screen.findByRole('link', { name: 'Waiting for approval · Review approval' })).toHaveAttribute('href', '#/runs/run-local');
    expect(screen.getByRole('button', { name: 'New conversation' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(fixture.runs.cancel).toHaveBeenCalledWith('run-local', undefined));
  });

  it('shows API and run failure reasons', async () => {
    fixture.runs.start.mockRejectedValueOnce(new Error('Local fake rejected input'));
    panel(); await sendMessage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Local fake rejected input');
    fixture.runs.start.mockResolvedValue(record('failed', { error: 'Local fake model unavailable' }));
    fixture.runs.get.mockResolvedValue(record('failed', { error: 'Local fake model unavailable', events: [event('NODE_FAILED', 1, { error: 'Local fake timeout' })] }));
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Local fake model unavailable')).toBeInTheDocument();
    expect(screen.getAllByText('error: Local fake timeout').length).toBeGreaterThan(0);
  });

  it('reuses the pending-input form and existing provideInput API', async () => {
    const waiting = record('waiting_for_input', { pending_input: { interrupt_id: 'input-local', node_id: 'start', value: 'Your answer?' } });
    fixture.runs.start.mockResolvedValue(record('running')); fixture.runs.get.mockResolvedValue(record('running'));
    fixture.runs.provideInput.mockResolvedValue(record());
    panel(); await sendMessage();
    await waitFor(() => expect(emit).toBeDefined());
    fixture.runs.get.mockResolvedValue(waiting);
    await act(async () => { emit!(event('RUN_WAITING_FOR_INPUT')); });
    fireEvent.change(await screen.findByLabelText('Your answer'), { target: { value: 'yes' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
    await waitFor(() => expect(fixture.runs.provideInput).toHaveBeenCalledWith('run-local', 'input-local', 'yes'));
  });

  it('saves through WorkflowDetail without unmounting the panel and starts only after save succeeds', async () => {
    const workflow = { workflow_id: 'wf-local', name: 'Local workflow', revision: 1, spec };
    fixture.workflows.get.mockResolvedValue(workflow);
    fixture.workflows.update.mockImplementation(async (_id, next) => ({ ...workflow, spec: next, revision: 2 }));
    render(<I18nProvider><WorkflowDetail workflowId="wf-local" onBack={vi.fn()} onOpenWorkflow={vi.fn()} /></I18nProvider>);
    await screen.findByText('Local fake model'); drop();
    fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'save first' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save and test' }));
    await waitFor(() => expect(fixture.workflows.update).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fixture.runs.start).toHaveBeenCalledTimes(1));
    expect(fixture.workflows.update.mock.invocationCallOrder[0]).toBeLessThan(fixture.runs.start.mock.invocationCallOrder[0]);
    expect(await screen.findByText('answer: Local fake answer')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Test' })).toBeInTheDocument();
  });

  it('uses the existing revision conflict modal and does not start a test on conflict', async () => {
    canvas({ initialSpec: spec, onSaveSpec: vi.fn(async () => { throw { statusCode: 409 }; }) });
    await screen.findByText('Local fake model'); drop();
    fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'save conflict' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save and test' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent(/conflict/i);
    expect(fixture.runs.start).not.toHaveBeenCalled();
  });

  it('does not run edits made while Save and test is awaiting the server', async () => {
    let finishSave: (() => void) | undefined;
    const save = vi.fn(() => new Promise<void>(resolve => { finishSave = resolve; }));
    canvas({ initialSpec: spec, onSaveSpec: save });
    await screen.findByText('Local fake model'); drop();
    fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'test race' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save and test' }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    await act(async () => { finishSave!(); });
    expect(screen.getByText('The canvas changed while saving. Save again before testing.')).toBeInTheDocument();
    expect(fixture.runs.start).not.toHaveBeenCalled();
  });

  it('keeps the narrow-screen palette and opens an accessible Test drawer with focus restoration', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    const user = userEvent.setup(); canvas();
    await act(async () => {});
    expect(screen.queryByRole('complementary', { name: 'Modules' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Add step' }));
    await user.click(screen.getByRole('button', { name: 'Standard' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('Local fake model');
    await user.keyboard('{Escape}');
    await user.click(screen.getByRole('button', { name: 'Test' }));
    expect(screen.getByRole('dialog', { name: 'Test' })).toHaveAttribute('aria-modal', 'true');
    await user.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: 'Test' })).toHaveFocus();
  });

  it('blocks duplicate starts and keeps the first run tracked', async () => {
    let finish: ((run: RunRecord) => void) | undefined;
    fixture.runs.start.mockImplementation(() => new Promise<RunRecord>(resolve => { finish = resolve; }));
    panel();
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'once' } });
    fireEvent.keyDown(screen.getByLabelText('Message'), { key: 'Enter' });
    fireEvent.keyDown(screen.getByLabelText('Message'), { key: 'Enter' });
    expect(fixture.runs.start).toHaveBeenCalledTimes(1);
    await act(async () => { finish!(record()); });
    expect(screen.getByRole('link', { name: 'Open run' })).toBeInTheDocument();
  });

  it('translates the Test panel and composer in Hindi', async () => {
    localStorage.setItem('nexura_locale', 'hi');
    panel();
    expect(screen.getByRole('region', { name: hi.builderTest.title })).toBeInTheDocument();
    expect(screen.getByText(hi.builderTest.costNote)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(hi.builderTest.message), { target: { value: 'नमस्ते' } });
    fireEvent.click(screen.getByRole('button', { name: hi.builderTest.send }));
    await waitFor(() => expect(fixture.runs.start).toHaveBeenCalledWith(expect.objectContaining({ initial_state: { question: 'नमस्ते' } })));
    expect(screen.getByRole('link', { name: hi.builderTest.openRun })).toBeInTheDocument();
  });
});
