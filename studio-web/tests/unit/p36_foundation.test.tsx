// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useState } from 'react';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Shell } from '../../src/app/Shell';
import { Sidebar } from '../../src/app/Sidebar';
import { StudioExtensionsContext, StudioExtensionRegistry } from '../../src/app/extensions';
import { Input } from '../../src/components/Input';
import { Modal } from '../../src/components/Modal';
import { PageHeader, PageLayout } from '../../src/components/PageHeader';
import { I18nProvider } from '../../src/i18n';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';

const service = vi.hoisted(() => ({
  connection: { status: 'connected', workspace: 'default', baseUrl: '/api', profile: 'local-owner', latencyMs: 77 },
  changeWorkspace: vi.fn(), changeBaseUrl: vi.fn(), changeAuthToken: vi.fn(), refreshConnection: vi.fn(),
}));
vi.mock('../../src/api/context', () => ({ useApi: () => service }));
vi.mock('../../src/features/approvals/usePendingApprovals', () => ({
  APPROVALS_BADGE_POLL_MS: 10000,
  usePendingApprovals: () => ({ count: 2, approvals: [], loading: false }),
}));
vi.mock('../../src/features/workflows/WorkflowList', () => ({
  WorkflowList: () => <PageLayout><PageHeader title="Workflows" /></PageLayout>,
}));

let compact = false;
const mediaListeners = new Set<() => void>();
beforeEach(() => {
  compact = false;
  mediaListeners.clear();
  localStorage.clear();
  window.location.hash = '#/workflows';
  service.connection.status = 'connected';
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    get matches() { return compact; },
    addEventListener: (_event: string, callback: () => void) => mediaListeners.add(callback),
    removeEventListener: (_event: string, callback: () => void) => mediaListeners.delete(callback),
  })));
  // jsdom has no geometry; hidden/inert ancestors still exclude a control.
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
    for (let node: HTMLElement | null = this; node; node = node.parentElement) {
      if (node.hidden || node.hasAttribute('inert') || getComputedStyle(node).display === 'none') return [] as unknown as DOMRectList;
    }
    return [new DOMRect(0, 0, 100, 32)] as unknown as DOMRectList;
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

describe('Round 1 shell', () => {
  it('groups every core destination, retains current-page state, and keeps diagnostics out of the visible status', () => {
    render(<I18nProvider><Shell /></I18nProvider>);
    expect(screen.getByRole('link', { name: 'Nexura Studio' })).toHaveAttribute('href', '#/workflows');
    expect(screen.getByRole('heading', { level: 1, name: 'Workflows' })).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Studio navigation' });
    expect(within(nav).getAllByRole('button')).toHaveLength(14);
    expect(within(within(nav).getByRole('group', { name: 'Build' })).getAllByRole('button').map(b => b.textContent))
      .toEqual(['Workflows', 'Templates', 'Agents', 'Skills', 'Knowledge', 'Playground']);
    expect(within(nav).getByRole('button', { name: 'Workflows' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByLabelText('2 pending approvals')).toHaveTextContent('2');
    const connection = screen.getByRole('button', { name: 'Service connection status: Connected' });
    expect(connection).toHaveTextContent(/^Connected$/);
    expect(connection).toHaveAccessibleDescription(/local-owner.*\/api.*77 ms/);
  });

  it('inherits extension groups through after anchors, retains relative ordering and allows explicit groups', () => {
    const registry = new StudioExtensionRegistry();
    registry.register({ id: 'test', navItems: [
      { id: 'organisation', labelKey: 'test.organisation', icon: 'organisation', after: 'credentials' },
      { id: 'audit', labelKey: 'test.audit', icon: 'system', after: 'organisation' },
      { id: 'reports', labelKey: 'test.reports', icon: 'traces', after: 'skills', group: 'operate' },
    ], messages: { en: { test: { organisation: 'Organisation', audit: 'Audit', reports: 'Reports' } } } });
    render(<StudioExtensionsContext.Provider value={registry}><I18nProvider>
      <Sidebar currentTab="organisation" onSelectTab={() => {}} />
    </I18nProvider></StudioExtensionsContext.Provider>);
    expect(within(screen.getByRole('group', { name: 'Configure' })).getAllByRole('button').map(b => b.textContent))
      .toEqual(['Credentials', 'Organisation', 'Audit', 'Tools']);
    expect(within(screen.getByRole('group', { name: 'Operate' })).getByRole('button', { name: 'Reports' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Organisation' })).toHaveAttribute('aria-current', 'page');
  });

  it('opens a labelled phone drawer, traps focus both ways, restores focus and closes on selection', async () => {
    compact = true;
    const user = userEvent.setup();
    const { container } = render(<I18nProvider><Shell /></I18nProvider>);
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    const opener = screen.getByRole('button', { name: 'Open navigation' });
    await user.click(opener);
    const dialog = screen.getByRole('dialog', { name: 'Studio navigation' });
    expect(container).toHaveAttribute('inert');
    expect(opener).toHaveAttribute('aria-expanded', 'true');
    const close = within(dialog).getByRole('button', { name: 'Close dialog' });
    const first = within(dialog).getByRole('button', { name: 'Workflows' });
    const last = within(dialog).getByRole('button', { name: 'System' });
    expect(first).toHaveFocus();
    await user.tab({ shift: true });
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(last).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(opener).toHaveFocus();
    expect(container).not.toHaveAttribute('inert');
    await user.click(opener);
    await user.click(screen.getByRole('button', { name: 'Workflows' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(window.location.hash).toBe('#/workflows');
  });

  it('keeps phone settings, nested connection editing and preference persistence accessible', async () => {
    compact = true;
    const user = userEvent.setup();
    render(<I18nProvider><Shell /></I18nProvider>);
    const opener = screen.getByRole('button', { name: 'Studio settings' });
    await user.click(opener);
    const settings = screen.getByRole('dialog', { name: 'Studio settings' });
    const connection = within(settings).getByRole('button', { name: 'Service connection status: Connected' });
    await user.click(connection);
    expect(settings).toHaveAttribute('inert');
    const connectionDialog = screen.getByRole('dialog', { name: en.connection.settings });
    expect(within(connectionDialog).getByText('77 ms')).toBeInTheDocument();
    const token = within(connectionDialog).getByLabelText(en.ui.serviceAuthTokenOptional);
    await user.type(token, 'local-fake-token');
    await user.keyboard('{Escape}');
    expect(settings).not.toHaveAttribute('inert');
    expect(connection).toHaveFocus();
    await user.click(connection);
    expect(screen.getByLabelText(en.ui.serviceAuthTokenOptional)).toHaveValue('');
    await user.keyboard('{Escape}');
    await user.click(screen.getByTestId('toggle-theme-btn'));
    expect(localStorage.getItem('nexura_theme')).toBe('light');
    await user.click(screen.getByTestId('toggle-language-btn'));
    expect(document.documentElement.lang).toBe('hi');
    expect(localStorage.getItem('nexura_locale')).toBe('hi');
    expect(screen.getByRole('dialog', { name: 'स्टूडियो सेटिंग्स' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(opener).toHaveFocus();
  });

  it('shows connection failure on the phone and releases modal state on desktop resize', async () => {
    compact = true;
    service.connection.status = 'offline';
    const user = userEvent.setup();
    const { container } = render(<I18nProvider><Shell /></I18nProvider>);
    expect(within(screen.getByRole('status')).getByRole('button', { name: en.connection.offline })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Open navigation' }));
    act(() => { compact = false; mediaListeners.forEach(update => update()); });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(container).not.toHaveAttribute('inert');
    expect(screen.getAllByRole('navigation')).toHaveLength(1);
    expect(screen.getAllByTestId('nav-item-workflows')).toHaveLength(1);
  });
});

describe('Round 1 shared primitives', () => {
  it('keeps unique input ids stable across translated labels and associates help/errors', () => {
    const { rerender } = render(<><span id="context">Shared help</span><Input label="Name" helperText="Required name" aria-describedby="context" /><Input label="Name" /></>);
    const fields = screen.getAllByRole('textbox', { name: 'Name' });
    expect(fields[0].id).not.toBe(fields[1].id);
    const id = fields[0].id;
    expect(fields[0]).toHaveAccessibleDescription('Shared help Required name');
    rerender(<><span id="context">Shared help</span><Input label="नाम" error="नाम भरें" aria-describedby="context" /><Input label="नाम" /></>);
    expect(screen.getAllByRole('textbox', { name: 'नाम' })[0]).toHaveAttribute('id', id);
    expect(screen.getAllByRole('textbox')[0]).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getAllByRole('textbox')[0]).toHaveAccessibleDescription('Shared help नाम भरें');
  });

  it('keeps modal footer outside the body scroller and restores pre-existing inert state', async () => {
    const user = userEvent.setup();
    const unrelated = document.createElement('div');
    unrelated.setAttribute('inert', '');
    document.body.append(unrelated);
    function Example() {
      const [open, setOpen] = useState(false);
      return <><button onClick={() => setOpen(true)}>Open</button>
        <Modal open={open} title="Review" onClose={() => setOpen(false)} footer={<button>Save</button>}>
          <p>Long content</p>
        </Modal>
      </>;
    }
    render(<Example />);
    await user.click(screen.getByRole('button', { name: 'Open' }));
    expect(screen.getByRole('button', { name: 'Save' }).closest('.studio-modal-body')).toBeNull();
    expect(document.body.style.overflow).toBe('hidden');
    await user.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: 'Open' })).toHaveFocus();
    expect(document.body.style.overflow).not.toBe('hidden');
    expect(unrelated).toHaveAttribute('inert');
    unrelated.remove();
  });

  it('keeps English/Hindi leaf keys complete, non-empty and interpolation-compatible', () => {
    const flatten = (value: Record<string, unknown>, prefix = ''): Record<string, string> =>
      Object.fromEntries(Object.entries(value).flatMap(([key, entry]) => {
        const name = prefix ? `${prefix}.${key}` : key;
        return typeof entry === 'string' ? [[name, entry]] : Object.entries(flatten(entry as Record<string, unknown>, name));
      }));
    const english = flatten(en), hindi = flatten(hi);
    expect(Object.keys(english).length).toBeGreaterThanOrEqual(1669);
    expect(Object.keys(hindi).sort()).toEqual(Object.keys(english).sort());
    const placeholders = (text: string) => [...text.matchAll(/\{([a-zA-Z_]\w*)\}/g)].map(match => match[1]).sort();
    for (const [key, value] of Object.entries(english)) {
      expect(hindi[key].trim(), key).not.toBe('');
      expect(placeholders(hindi[key]), key).toEqual(placeholders(value));
    }
  });
});
