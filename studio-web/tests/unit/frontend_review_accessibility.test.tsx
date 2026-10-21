import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Modal } from '../../src/components/Modal';
import { I18nProvider, useI18n } from '../../src/i18n';
import { Header } from '../../src/app/Header';
import { ErrorBoundary } from '../../src/app/ErrorBoundary';
import { Shell } from '../../src/app/Shell';
import { ExactAction } from '../../src/features/approvals/ExactAction';
import { ApprovalInbox } from '../../src/features/approvals/ApprovalInbox';
import type { PendingApprovalItem } from '../../src/features/approvals/types';

const state = vi.hoisted(() => ({ crash: false, approvals: [] as PendingApprovalItem[] }));
vi.mock('../../src/api/context', () => ({
  useApi: () => ({
    connection: { status: 'connected', workspace: 'default', baseUrl: '/api', profile: 'local-owner' },
    changeWorkspace: vi.fn(), changeBaseUrl: vi.fn(), changeAuthToken: vi.fn(), refreshConnection: vi.fn(),
  }),
}));
vi.mock('../../src/features/approvals/usePendingApprovals', () => ({
  APPROVALS_BADGE_POLL_MS: 10000,
  usePendingApprovals: () => ({ count: state.approvals.length, approvals: state.approvals, loading: false }),
}));
vi.mock('../../src/features/approvals/WaitingForInputList', () => ({ WaitingForInputList: () => null }));
vi.mock('../../src/features/workflows/WorkflowList', () => ({
  WorkflowList: () => {
    if (state.crash) throw new Error('private render details');
    return <p>Workflow page recovered</p>;
  },
}));
vi.mock('../../src/features/knowledge/KnowledgeView', () => ({
  KnowledgeView: () => <p>Knowledge page available</p>,
}));
vi.mock('../../src/features/workflows/WorkflowDetail', () => ({
  WorkflowDetail: ({ workflowId, runId }: { workflowId: string; runId?: string }) => {
    if (state.crash) throw new Error('Workflow render failed');
    return <p>Workflow {workflowId} {runId}</p>;
  },
}));
vi.mock('../../src/features/runs/RunPage', () => ({
  RunPage: ({ runId }: { runId: string }) => {
    if (state.crash) throw new Error('Run render failed');
    return <p>Run {runId}</p>;
  },
}));
vi.mock('../../src/features/traces/TracesView', () => ({
  TracesView: ({ runId }: { runId: string }) => {
    if (state.crash) throw new Error('Trace render failed');
    return <p>Trace {runId}</p>;
  },
}));

beforeEach(() => {
  localStorage.clear();
  window.location.hash = '#/workflows';
  state.crash = false;
  state.approvals = [];
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

describe('Modal keyboard focus', () => {
  beforeEach(() => {
    // jsdom has no layout. Supply rectangles only for controls with visible ancestors.
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function (this: HTMLElement) {
      for (let element: HTMLElement | null = this; element; element = element.parentElement) {
        if (element.hidden || getComputedStyle(element).display === 'none') return [] as unknown as DOMRectList;
      }
      return [new DOMRect(0, 0, 100, 20)] as unknown as DOMRectList;
    });
  });

  function Example() {
    const [open, setOpen] = useState(false);
    return <>
      <button onClick={() => setOpen(true)}>Open modal</button>
      <Modal open={open} onClose={() => setOpen(false)} title="Focus example">
        <input aria-label="Name" />
        <button disabled>Unavailable</button>
        <button hidden>Hidden</button>
        <button>Save</button>
        <div style={{ display: 'none' }}><button>Hidden descendant</button></div>
      </Modal>
    </>;
  }

  it('enters the dialog, traps Tab both ways, and restores focus after Escape', async () => {
    const user = userEvent.setup();
    render(<Example />);
    const opener = screen.getByRole('button', { name: 'Open modal' });
    await user.click(opener);
    const close = screen.getByRole('button', { name: 'Close dialog' });
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    await user.tab({ shift: true });
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole('button', { name: 'Save' })).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it.each(['data-autofocus', 'autoFocus'])('prioritizes %s and restores the opener after closing', async (attribute) => {
    const user = userEvent.setup();
    const opener = document.createElement('button');
    document.body.append(opener);
    opener.focus();
    const { unmount } = render(<Modal open onClose={() => {}} title="Autofocus">
      <input aria-label="First field" />
      <input aria-label="Preferred field" {...(attribute === 'autoFocus' ? { autoFocus: true } : { 'data-autofocus': true })} />
    </Modal>);
    expect(screen.getByRole('textbox', { name: 'Preferred field' })).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole('textbox', { name: 'First field' })).toHaveFocus();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it('keeps focus in place on rerender and restores it on close or unmount', async () => {
    const user = userEvent.setup();
    const opener = document.createElement('button');
    document.body.append(opener);
    opener.focus();
    const { rerender, unmount } = render(<Modal open onClose={() => {}} title="First"><input aria-label="Edit" /></Modal>);
    const input = screen.getByRole('textbox');
    await user.click(input);
    rerender(<Modal open onClose={() => {}} title="Updated"><input aria-label="Edit" /></Modal>);
    expect(input).toHaveFocus();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it('gives simultaneous dialogs distinct title ids and handles only the top dialog', () => {
    const firstClose = vi.fn();
    const secondClose = vi.fn();
    render(<>
      <Modal open onClose={firstClose} title="First">First body</Modal>
      <Modal open onClose={secondClose} title="Second">Second body</Modal>
    </>);
    const first = screen.getByRole('dialog', { name: 'First' });
    const second = screen.getByRole('dialog', { name: 'Second' });
    expect(first.getAttribute('aria-labelledby')).not.toBe(second.getAttribute('aria-labelledby'));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(firstClose).not.toHaveBeenCalled();
    expect(secondClose).toHaveBeenCalledOnce();
  });
});

describe('Localized shell controls and recovery', () => {
  it.each([
    ['Error', 'Ordinary render failure', false],
    ['ChunkLoadError', 'Chunk is missing', true],
    ['TypeError', 'Failed to fetch dynamically imported module: /assets/page.js', true],
    ['Error', 'Loading chunk 12 failed', true],
  ])('offers reload for page errors and prioritizes chunk failures (%s: %s)', (name, message, reloadFirst) => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const Broken = () => { throw Object.assign(new Error(message), { name }); };
    render(<I18nProvider><ErrorBoundary onGoToWorkflows={() => {}}><Broken /></ErrorBoundary></I18nProvider>);
    const actions = within(screen.getByRole('alert')).getAllByRole('button');
    expect(actions.map((button) => button.textContent)).toEqual(reloadFirst ?
      ['Reload Studio', 'Go to Workflows'] : ['Go to Workflows', 'Reload Studio']);
    expect(actions[0]).toHaveStyle({ backgroundColor: 'var(--button-primary)' });
    expect(actions[1]).toHaveStyle({ backgroundColor: 'var(--bg-surface-elevated)' });
  });

  it('translates both page recovery actions in Hindi', () => {
    localStorage.setItem('nexura_locale', 'hi');
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const Broken = () => { throw new Error('Loading chunk 12 failed'); };
    render(<I18nProvider><ErrorBoundary onGoToWorkflows={() => {}}><Broken /></ErrorBoundary></I18nProvider>);
    expect(within(screen.getByRole('alert')).getAllByRole('button').map((button) => button.textContent))
      .toEqual(['स्टूडियो दोबारा लोड करें', 'वर्कफ़्लो पर जाएँ']);
  });

  it.each([
    ['#/workflows/first', '#/workflows/second', 'Workflow second'],
    ['#/workflows/first', '#/workflows', 'Workflow page recovered'],
    ['#/workflows/first/runs/run-one', '#/workflows/first/runs/run-two', 'Workflow first run-two'],
    ['#/runs/run-one', '#/runs/run-two', 'Run run-two'],
    ['#/traces/run-one', '#/traces/run-two', 'Trace run-two'],
  ])('clears a failed page when the route changes inside its tab (%s)', async (start, next, text) => {
    state.crash = true;
    window.location.hash = start;
    vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<I18nProvider><Shell /></I18nProvider>);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    state.crash = false;
    act(() => {
      window.location.hash = next;
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([
    [undefined, 'Amount: 5000'],
    ['INR', 'Amount: 5000 INR'],
    ['', 'Amount: 5000'],
  ])('shows an approval amount using only recorded currency (%s)', (currency, text) => {
    state.approvals = [{
      approvalId: 'approval', runId: 'run', nodeId: 'node', workflowId: 'workflow',
      currentState: { amount: 5000, currency }, createdAt: 0,
      runRecord: {} as PendingApprovalItem['runRecord'],
    }];
    render(<I18nProvider><ApprovalInbox /></I18nProvider>);
    expect(screen.getByText(text, { exact: true })).toBeInTheDocument();
    expect(screen.queryByText(/\$5000/)).not.toBeInTheDocument();
  });

  it('announces active language, workspace, connection and theme, and updates html lang', async () => {
    const user = userEvent.setup();
    render(<I18nProvider><Header /></I18nProvider>);
    expect(document.documentElement.lang).toBe('en');
    expect(screen.getByRole('button', { name: 'Active workspace selector: default' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Service connection status: Connected' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Toggle Theme: dark theme active; switch to light' }));
    expect(screen.getByRole('button', { name: 'Toggle Theme: light theme active; switch to dark' })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe('light');
    await user.click(screen.getByRole('button', { name: /^Toggle Language: English/ }));
    expect(document.documentElement.lang).toBe('hi');
    expect(screen.getByRole('button', { name: 'वर्तमान भाषा हिन्दी है; अंग्रेज़ी (EN) में बदलें' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'हल्की थीम सक्रिय है; गहरी थीम में बदलें' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'सक्रिय कार्यक्षेत्र चुनें: default' })).toBeInTheDocument();
  });

  it.each(['en', 'hi'])('localizes the root error fallback in %s without exposing the exception', (language) => {
    localStorage.setItem('nexura_locale', language);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const Broken = () => { throw new Error('private render details'); };
    render(<I18nProvider><ErrorBoundary><Broken /></ErrorBoundary></I18nProvider>);
    expect(screen.getByRole('alert')).not.toHaveTextContent('private render details');
    expect(screen.getByRole('button', { name: language === 'en' ? 'Reload Studio' : 'स्टूडियो दोबारा लोड करें' })).toBeInTheDocument();
  });

  it('keeps navigation and the header usable after a page crash and resets on navigation', async () => {
    state.crash = true;
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const user = userEvent.setup();
    render(<I18nProvider><Shell /></I18nProvider>);
    expect(await screen.findByRole('alert')).toHaveTextContent('This page could not be displayed.');
    expect(screen.getByRole('banner')).toBeInTheDocument();
    await user.click(screen.getByTestId('nav-item-knowledge'));
    expect(await screen.findByText('Knowledge page available')).toBeInTheDocument();
    state.crash = false;
    await user.click(screen.getByTestId('nav-item-workflows'));
    expect(await screen.findByText('Workflow page recovered')).toBeInTheDocument();
  });

  it('recovers from a workflows page crash using the fallback action', async () => {
    state.crash = true;
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const user = userEvent.setup();
    render(<I18nProvider><Shell /></I18nProvider>);
    const action = await screen.findByRole('button', { name: 'Go to Workflows' });
    state.crash = false;
    await user.click(action);
    expect(await screen.findByText('Workflow page recovered')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([['en', 'en-IN'], ['hi', 'hi-IN']])('formats recorded approval expiry with %s', (language, locale) => {
    localStorage.setItem('nexura_locale', language);
    const expiry = 2_000_000_000;
    const format = vi.spyOn(Date.prototype, 'toLocaleString');
    function LocaleCheck() {
      const { locale: selected } = useI18n();
      return <span data-testid="locale">{selected}</span>;
    }
    render(<I18nProvider>
      <LocaleCheck />
      <ExactAction request={{ expires_at: expiry }} />
    </I18nProvider>);
    expect(screen.getByTestId('locale')).toHaveTextContent(locale);
    expect(format).toHaveBeenCalledWith(locale, { timeZoneName: 'short' });
    expect(within(screen.getByTestId('exact-action')).getByText(new Date(expiry * 1000).toLocaleString(locale, { timeZoneName: 'short' }))).toBeInTheDocument();
  });
});
