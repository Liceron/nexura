// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// P28 step 4: the Publish feature moved to the closed edition; extensions bring their own
// translations through the registry's `messages`.
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { I18nProvider, useI18n } from '../../src/i18n';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';
import { BUILTIN_STUDIO_EXTENSIONS } from '../../src/app/builtinExtensions';
import {
  StudioExtensionError,
  StudioExtensionRegistry,
  StudioExtensionsContext,
  resolveMessage,
  type StudioExtension,
} from '../../src/app/extensions';

const STUDIO = resolve(__dirname, '../..');

const reports: StudioExtension = {
  id: 'test.reports',
  messages: {
    en: { reports: { title: 'Reports', onlyEnglish: 'English only' }, nav: { workflows: 'Hijacked' } },
    hi: { reports: { title: 'रिपोर्ट' } },
  },
};

function Probe({ keys }: { keys: string[] }) {
  const { t } = useI18n();
  return <div data-testid="probe">{keys.map((k) => t(k)).join('|')}</div>;
}

describe('P28 step 4: Publish is not in the open-source Studio', () => {
  it('has no in-tree Publish extension, feature, widget or widget build', () => {
    expect(BUILTIN_STUDIO_EXTENSIONS.map((e) => e.id)).not.toContain('nexura.publish');
    for (const path of ['src/features/publish', 'widget', 'vite.widget.config.ts']) {
      expect(existsSync(resolve(STUDIO, path)), path).toBe(false);
    }
    const pkg = JSON.parse(readFileSync(resolve(STUDIO, 'package.json'), 'utf-8'));
    expect(pkg.scripts['build:widget']).toBeUndefined();
  });

  it('ships no publish.* strings', () => {
    expect('publish' in en).toBe(false);
    expect('publish' in hi).toBe(false);
  });
});

describe('Studio extension messages', () => {
  it('resolves dotted keys, falls back to English and never overrides a core key', () => {
    const registry = new StudioExtensionRegistry();
    registry.register(reports);
    expect(registry.message('en', 'reports.title')).toBe('Reports');
    expect(registry.message('hi', 'reports.title')).toBe('रिपोर्ट');
    expect(registry.message('hi', 'reports.onlyEnglish')).toBe('English only');
    expect(registry.message('en', 'reports.missing')).toBeUndefined();
    expect(resolveMessage({ a: { b: 'x' } }, 'a')).toBeUndefined();

    render(
      <StudioExtensionsContext.Provider value={registry}>
        <I18nProvider>
          <Probe keys={['reports.title', 'nav.workflows', 'reports.missing']} />
        </I18nProvider>
      </StudioExtensionsContext.Provider>,
    );
    expect(screen.getByTestId('probe').textContent).toBe(
      `Reports|${en.nav.workflows}|reports.missing`,
    );
  });

  it('refuses malformed messages', () => {
    const registry = new StudioExtensionRegistry();
    expect(() =>
      registry.register({ id: 'bad.lang', messages: { English: { a: 'b' } } }),
    ).toThrow(StudioExtensionError);
    expect(() =>
      registry.register({ id: 'bad.value', messages: { en: 'nope' as unknown as Record<string, unknown> } }),
    ).toThrow(StudioExtensionError);
    expect(registry.list()).toEqual([]);
  });
});
