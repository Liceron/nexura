// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
// Local mock API only; no model or service calls.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../src/i18n';
import { parseHash } from '../../src/app/routes';
import { CreateWorkflowModal } from '../../src/features/workflows/CreateWorkflowModal';
import { TemplatesView } from '../../src/features/templates/TemplatesView';
import type { TemplateSummary } from '../../src/features/templates/types';

const fixture = vi.hoisted(() => ({
  manager: { client: { request: vi.fn() } },
  connection: { status: 'connected', workspace: 'local-mock' },
}));
vi.mock('../../src/api/context', () => ({ useApi: () => fixture }));

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubEnv('VITE_LANGUAGE_CHOICE', 'off');
  localStorage.clear();
  window.history.replaceState(null, '', '#/workflows');
});
afterEach(() => { cleanup(); vi.unstubAllEnvs(); window.history.replaceState(null, '', '#/'); });

it.each(['off', 'on'])('shows the bilingual example only when language choice is on (%s)', mode => {
  vi.stubEnv('VITE_LANGUAGE_CHOICE', mode);
  render(<I18nProvider><CreateWorkflowModal open onClose={vi.fn()} onCreated={vi.fn()} /></I18nProvider>);
  const greeting = screen.queryByRole('radio', { name: /English and Hindi greeting/ });
  if (mode === 'on') expect(greeting).toBeInTheDocument();
  else expect(greeting).not.toBeInTheDocument();
  expect(screen.getByRole('radio', { name: /Blank Workflow/ })).toBeChecked();
  for (const id of ['order-triage-support', 'order-router']) {
    const example = screen.getAllByRole('radio').find(input => (input as HTMLInputElement).value === id)!;
    expect(example).not.toBeChecked();
    fireEvent.click(example);
    expect(example).toBeChecked();
  }
  expect(screen.getByTestId('create-workflow-submit-btn')).toBeInTheDocument();
});

it('a disabled bilingual initial selection falls back to blank', () => {
  render(<I18nProvider><CreateWorkflowModal open initialTemplateId="bilingual-greeting" onClose={vi.fn()} onCreated={vi.fn()} /></I18nProvider>);
  expect(screen.getByRole('radio', { name: /Blank Workflow/ })).toBeChecked();
  expect(screen.queryByRole('radio', { name: /English and Hindi greeting/ })).not.toBeInTheDocument();
});

it('Browse all templates closes creation and navigates to the Templates tab', async () => {
  const onClose = vi.fn();
  render(<I18nProvider><CreateWorkflowModal open onClose={onClose} onCreated={vi.fn()} /></I18nProvider>);
  const link = screen.getByRole('link', { name: 'Browse all templates' });
  expect(link).toHaveAttribute('data-testid', 'create-browse-gallery');
  await userEvent.setup().click(link);
  await waitFor(() => expect(window.location.hash).toBe('#/templates'));
  expect(parseHash(window.location.hash).tab).toBe('templates');
  expect(onClose).toHaveBeenCalledOnce();
  expect(fixture.manager.client.request).not.toHaveBeenCalled();
});

const categories = ['agents', 'knowledge', 'automation', 'data', 'governance', 'support'];
const ids = ['research-team', 'rag-qa-citations', 'scheduled-report', 'structured-extraction', 'approval-workflow', 'support-desk-refund-approval'];
const items: TemplateSummary[] = ids.map((id, index) => ({
  id, category: categories[index], title: `Example ${index}`, description: `Outcome ${index}`,
  template_version: 1, difficulty: 'beginner', tags: [], required_modules: [], required_credentials: [],
  node_count: 1, has_approval: index >= 4, has_trigger: index === 2,
}));

it('labels and orders all six catalog categories with agents before governance and support', async () => {
  fixture.manager.client.request.mockResolvedValue({ items: [...items].reverse(), categories: [...categories].reverse(), tags: [], total: 6 });
  render(<I18nProvider><TemplatesView /></I18nProvider>);
  const grid = screen.getByTestId('template-grid');
  await waitFor(() => expect(within(grid).getAllByRole('listitem')).toHaveLength(6));
  const cards = within(grid).getAllByRole('listitem');
  expect(cards.map(card => card.dataset.testid)).toEqual(ids.map(id => `template-card-${id}`));
  const labels = ['Agents', 'Knowledge', 'Automation', 'Data', 'Governance', 'Support'];
  labels.forEach((label, index) => expect(within(cards[index]).getByText(`${label} · Beginner`)).toBeInTheDocument());
  expect(within(screen.getByRole('combobox', { name: 'Category' })).getAllByRole('option').map(option => option.textContent)).toEqual(['All', ...labels]);
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'Outcome 2' } });
  expect(within(grid).getAllByRole('listitem')).toHaveLength(1);
  expect(within(grid).getByTestId('template-card-scheduled-report')).toBeInTheDocument();
});

it('category labels preserve the catalog values used for filtering', async () => {
  fixture.manager.client.request.mockImplementation(async (_method, _path, options) => ({
    items: options.params.category ? items.filter(item => item.category === options.params.category) : items,
    categories, tags: [], total: 6,
  }));
  render(<I18nProvider><TemplatesView /></I18nProvider>);
  await screen.findByTestId('template-card-research-team');
  fireEvent.change(screen.getByRole('combobox', { name: 'Category' }), { target: { value: 'knowledge' } });
  await waitFor(() => expect(within(screen.getByTestId('template-grid')).getAllByRole('listitem')).toHaveLength(1));
  expect(screen.getByTestId('template-card-rag-qa-citations')).toBeInTheDocument();
  expect(fixture.manager.client.request).toHaveBeenLastCalledWith('GET', '/templates', { params: { category: 'knowledge', tag: undefined } });
});
