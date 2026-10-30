// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, fireEvent, within } from '@testing-library/react';
import fs from 'node:fs';
import path from 'node:path';
import { Sidebar } from '../../src/app/Sidebar';
import { Icon } from '../../src/components/Icon';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { TracesView } from '../../src/features/traces/TracesView';

afterEach(() => {
  cleanup();
  localStorage.removeItem('nexura_locale');
  document.documentElement.removeAttribute('data-theme');
  vi.restoreAllMocks();
});

describe('P34 portable Studio presentation', () => {
  it.each(['en', 'hi'])('uses SVG navigation with accessible labels in %s and both themes', (language) => {
    localStorage.setItem('nexura_locale', language);
    const onSelect = vi.fn();
    render(<I18nProvider><Sidebar currentTab="workflows" onSelectTab={onSelect} pendingApprovalsCount={2} /></I18nProvider>);
    const nav = screen.getByRole('navigation', { name: language === 'hi' ? 'Studio नेविगेशन' : 'Studio navigation' });
    for (const theme of ['dark', 'light']) {
      document.documentElement.dataset.theme = theme;
      for (const button of within(nav).getAllByRole('button')) {
        expect(button).toHaveAccessibleName();
        expect(button.querySelectorAll('svg')).toHaveLength(1);
        expect(button.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
        expect(button.querySelector('svg')).toHaveAttribute('focusable', 'false');
        expect(button.textContent).not.toMatch(/\p{Extended_Pictographic}|\p{Regional_Indicator}/u);
      }
    }
    fireEvent.click(screen.getByTestId('nav-item-approvals'));
    expect(onSelect).toHaveBeenCalledWith('approvals');
    expect(screen.getByTestId('nav-item-workflows')).toHaveAttribute('aria-current', 'page');
  });

  it('uses a safe SVG fallback for legacy extension icon names', () => {
    const { container } = render(<Icon name="legacy-extension-glyph" />);
    expect(container.querySelector('svg path')?.getAttribute('d')).toBeTruthy();
    expect(container.textContent).toBe('');
  });

  const expectNexuraTitle = (file: string) => {
    const html = fs.readFileSync(path.resolve(__dirname, '../../..', file), 'utf8');
    expect(html).toContain('<title>Nexura Studio');
    expect(html).not.toContain('AgentKit');
  };

  it('keeps the document title free of the old product name in the open edition', () => {
    expectNexuraTitle('studio-web/index.html');
  });

  // The closed edition's Studio is not part of the public export, so this only runs in the
  // internal tree, where the file exists.
  it.skipIf(!fs.existsSync(path.resolve(__dirname, '../../..', 'enterprise/studio-web/index.html')))(
    'keeps the document title free of the old product name in the closed edition',
    () => {
      expectNexuraTitle('enterprise/studio-web/index.html');
    },
  );

  it('bundles the unchanged OFL font and configures Hindi fallback for prose and JSON', () => {
    const root = path.resolve(__dirname, '../../..');
    const asset = fs.readFileSync(path.join(root, 'studio-web/public/fonts/NotoSansDevanagari.ttf'));
    expect(asset.equals(fs.readFileSync(path.join(root, 'packages/nexura-studio/src/nexura_studio/static/fonts/NotoSansDevanagari.ttf')))).toBe(true);
    expect(fs.readFileSync(path.join(root, 'studio-web/public/fonts/OFL.txt'), 'utf8')).toContain('SIL OPEN FONT LICENSE Version 1.1');
    const css = fs.readFileSync(path.join(root, 'studio-web/src/styles/index.css'), 'utf8');
    expect(css).toMatch(/@font-face\s*\{[^}]*Noto Sans Devanagari[^}]*url\('\/fonts\/NotoSansDevanagari.ttf'\)/s);
    const tokens = fs.readFileSync(path.join(root, 'studio-web/src/styles/tokens.css'), 'utf8');
    expect(tokens).toMatch(/--font-sans:.*"Noto Sans Devanagari"/);
    expect(tokens).toMatch(/--font-mono:.*"Noto Sans Devanagari"/);
  });

  it('uses theme-aware styling for the run-status filter and preserves selection', () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{}', { status: 503 }));
    render(<I18nProvider><ApiProvider><TracesView /></ApiProvider></I18nProvider>);
    const filter = screen.getByRole('combobox', { name: 'Run status' });
    expect(filter).toHaveStyle({ color: 'var(--text-primary)', backgroundColor: 'var(--bg-surface-elevated)' });
    fireEvent.change(filter, { target: { value: 'pending_approval' } });
    expect(filter).toHaveValue('pending_approval');
  });
});
