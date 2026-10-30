// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, renderHook, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { I18nProvider, languageChoiceEnabled, useI18n } from '../../src/i18n';
import { Header } from '../../src/app/Header';

const service = vi.hoisted(() => ({
  connection: { status: 'connected', workspace: 'default', baseUrl: '/api', profile: 'local-owner', latencyMs: 77 },
  changeWorkspace: vi.fn(), changeBaseUrl: vi.fn(), changeAuthToken: vi.fn(), refreshConnection: vi.fn(),
}));
vi.mock('../../src/api/context', () => ({ useApi: () => service }));

const wrapper = ({ children }: { children: ReactNode }) => <I18nProvider>{children}</I18nProvider>;

describe('English-only Studio (language choice switched off)', () => {
  afterEach(() => { vi.unstubAllEnvs(); localStorage.clear(); });

  it('is off unless the build turns it on', () => {
    vi.stubEnv('VITE_LANGUAGE_CHOICE', '');
    expect(languageChoiceEnabled()).toBe(false);
    vi.stubEnv('VITE_LANGUAGE_CHOICE', 'on');
    expect(languageChoiceEnabled()).toBe(true);
  });

  it('ignores a saved Hindi choice and refuses to switch to Hindi', () => {
    vi.stubEnv('VITE_LANGUAGE_CHOICE', '');
    localStorage.setItem('nexura_locale', 'hi');
    const { result } = renderHook(() => useI18n(), { wrapper });
    expect(result.current.language).toBe('en');
    act(() => result.current.setLanguage('hi'));
    expect(result.current.language).toBe('en');
    expect(document.documentElement.lang).toBe('en');
  });

  it('shows no language toggle in the header', () => {
    vi.stubEnv('VITE_LANGUAGE_CHOICE', '');
    render(<Header />, { wrapper });
    expect(screen.getByTestId('toggle-theme-btn')).toBeInTheDocument();
    expect(screen.queryByTestId('toggle-language-btn')).toBeNull();
  });

  it('keeps the toggle when a build turns language choice on', () => {
    vi.stubEnv('VITE_LANGUAGE_CHOICE', 'on');
    render(<Header />, { wrapper });
    expect(screen.getByTestId('toggle-language-btn')).toBeInTheDocument();
  });
});
