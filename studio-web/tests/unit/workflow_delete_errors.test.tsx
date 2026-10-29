// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// Local mock transport; no service or model calls.
import type { ReactNode } from 'react';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { NexuraApiError } from '@liceron/nexura-sdk';
import { I18nProvider } from '../../src/i18n';
import { WorkflowList } from '../../src/features/workflows/WorkflowList';
import { WorkflowDetail } from '../../src/features/workflows/WorkflowDetail';

const fixture = vi.hoisted(() => {
  const workflow = { workflow_id: 'wf-delete', name: 'Delete error fixture', revision: 1, updated_at: 1, spec: { name: 'Delete error fixture', entry_node: 'start', nodes: { start: { id: 'start', handler: 'local-fixture', transitions: {} } } } };
  const workflows = { list: vi.fn(), get: vi.fn(), delete: vi.fn() };
  return { workflow, workflows, api: { manager: { client: { workflows } }, connection: { status: 'connected', workspace: 'local-mock' } } };
});
vi.mock('../../src/api/context', () => ({ useApi: () => fixture.api }));
vi.mock('../../src/features/builder/WorkflowCanvas', () => ({ WorkflowCanvas: ({ moreActions }: { moreActions: ReactNode }) => <div>{moreActions}</div> }));

beforeEach(() => {
  vi.resetAllMocks();
  fixture.workflows.list.mockResolvedValue({ items: [fixture.workflow], total: 1, has_more: false });
  fixture.workflows.get.mockResolvedValue(fixture.workflow);
  fixture.workflows.delete.mockRejectedValueOnce(new NexuraApiError(409, 'WORKFLOW_IN_USE', 'Delete refused: workflow is in use.')).mockResolvedValueOnce({ deleted: true });
});
afterEach(cleanup);

it.each(['list', 'detail'] as const)('keeps a failed %s deletion visible inside the dialog and allows retry', async view => {
  const user = userEvent.setup();
  const onBack = vi.fn();
  render(<I18nProvider>{view === 'list' ? <WorkflowList onSelectWorkflow={vi.fn()} /> : <WorkflowDetail workflowId="wf-delete" onBack={onBack} onOpenWorkflow={vi.fn()} />}</I18nProvider>);
  if (view === 'list') {
    await user.click(await screen.findByRole('button', { name: 'More' }));
    await user.click(screen.getByRole('button', { name: 'Delete' }));
  } else await user.click(await screen.findByRole('button', { name: 'Delete Workflow' }));
  const dialog = screen.getByRole('dialog');
  expect(fixture.workflows.delete).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole('button', { name: 'Delete' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Delete refused: workflow is in use.');
  expect(dialog).toBeVisible();
  expect(within(dialog).getByRole('button', { name: 'Delete' })).toBeEnabled();
  expect(onBack).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(fixture.workflows.delete).toHaveBeenCalledTimes(2);
  if (view === 'detail') expect(onBack).toHaveBeenCalledTimes(1);
});
