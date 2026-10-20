// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../components/Icon';
import { useEffect, useState } from 'react';
import { ConnectionIndicator } from './ConnectionIndicator';
import { WorkspaceSelector } from './WorkspaceSelector';
import { useI18n } from '../i18n';

export function Header() {
  const { language, setLanguage, t } = useI18n();
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    try {
      const saved = localStorage.getItem('nexura_theme');
      if (saved === 'dark' || saved === 'light') return saved;
    } catch {
      // ignore
    }
    return 'dark';
  });

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  const toggleTheme = () => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark';
      try {
        localStorage.setItem('nexura_theme', next);
      } catch {
        // ignore
      }
      return next;
    });
  };

  return (
    <header
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        minWidth: 0,
        gap: 12,
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '12px 24px',
        backgroundColor: 'var(--bg-surface)',
        borderBottom: '1px solid var(--border-subtle)',
        position: 'sticky',
        top: 0,
        zIndex: 100,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: 'var(--radius-sm)',
            backgroundColor: 'var(--accent-primary)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#ffffff',
            fontWeight: 700,
            fontSize: 15,
          }}
        >
          N
        </div>
        <div className="studio-header-title">
          <h1 style={{ fontSize: 15, fontWeight: 600, letterSpacing: '-0.01em' }}>
            {t('app.title')}
          </h1>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', minWidth: 0 }}>
        <WorkspaceSelector />
        <ConnectionIndicator />

        {/* Language switcher */}
        <button
          onClick={() => setLanguage(language === 'en' ? 'hi' : 'en')}
          title={t(`header.language.${language}`)}
          aria-label={t(`header.language.${language}`)}
          data-testid="toggle-language-btn"
          style={{
            padding: '4px 8px',
            fontSize: 12,
            fontWeight: 600,
            borderRadius: 'var(--radius-md)',
            backgroundColor: 'var(--bg-surface-elevated)',
            border: '1px solid var(--border-subtle)',
            color: 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          {language === 'en' ? 'हिन्दी' : 'EN'}
        </button>

        {/* Theme toggle */}
        <button
          onClick={toggleTheme}
          title={theme === 'dark' ? t('app.themeLight') : t('app.themeDark')}
          aria-label={t(`header.theme.${theme}`)}
          data-testid="toggle-theme-btn"
          style={{
            padding: '4px 8px',
            fontSize: 14,
            borderRadius: 'var(--radius-md)',
            backgroundColor: 'var(--bg-surface-elevated)',
            border: '1px solid var(--border-subtle)',
            color: 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          {theme === 'dark' ? <Icon name="sun" /> : <Icon name="moon" />}
        </button>
      </div>
    </header>
  );
}
