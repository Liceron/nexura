// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../components/Icon';
import { useEffect, useState } from 'react';
import { ConnectionIndicator } from './ConnectionIndicator';
import { WorkspaceSelector } from './WorkspaceSelector';
import { useI18n } from '../i18n';
import { Modal } from '../components/Modal';
import { useCompactLayout } from './useCompactLayout';
import { useApi } from '../api/context';

export function Header({ onOpenNavigation, navigationOpen = false }: {
  onOpenNavigation?: () => void;
  navigationOpen?: boolean;
}) {
  const { language, setLanguage, t } = useI18n();
  const { connection } = useApi();
  const compact = useCompactLayout();
  const [settingsOpen, setSettingsOpen] = useState(false);
  useEffect(() => { if (!compact) setSettingsOpen(false); }, [compact]);
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

  const preferences = <>
        <button
          onClick={() => setLanguage(language === 'en' ? 'hi' : 'en')}
          title={t(`header.language.${language}`)}
          aria-label={t(`header.language.${language}`)}
          data-testid="toggle-language-btn"
          type="button"
          className="studio-utility-button"
        >
          {language === 'en' ? 'हिन्दी' : 'EN'}
        </button>

        <button
          onClick={toggleTheme}
          title={theme === 'dark' ? t('app.themeLight') : t('app.themeDark')}
          aria-label={t(`header.theme.${theme}`)}
          data-testid="toggle-theme-btn"
          type="button"
          className="studio-icon-button"
        >
          {theme === 'dark' ? <Icon name="sun" /> : <Icon name="moon" />}
        </button>
  </>;

  return (
    <header className="studio-header">
      {compact ? (
        <button className="studio-icon-button" type="button" onClick={onOpenNavigation}
          aria-label={t('shell.openNavigation')} aria-expanded={navigationOpen} aria-haspopup="dialog">
          <Icon name="menu" />
        </button>
      ) : (
        <a className="studio-brand" href="#/workflows" aria-label={t('app.title')}>
          <span className="studio-brand-mark" aria-hidden="true">N</span>
          <span>{t('app.title')}</span>
        </a>
      )}
      <div className="studio-header-controls">
        <WorkspaceSelector />
        {compact ? (
          <button className="studio-icon-button" type="button" aria-label={t('shell.settings')}
            aria-haspopup="dialog" aria-expanded={settingsOpen} onClick={() => setSettingsOpen(true)}>
            <Icon name="system" />
          </button>
        ) : <><ConnectionIndicator />{preferences}</>}
      </div>
      {compact && (connection.status === 'offline' || connection.status === 'unauthorized') && (
        <div className="studio-connection-failure" role="status">
          <button className="studio-utility-button" type="button" onClick={() => setSettingsOpen(true)}
            style={{ color: 'var(--status-danger)' }}>
            <Icon name="warning" />{t(`connection.${connection.status}`)}
          </button>
        </div>
      )}
      <Modal open={compact && settingsOpen} onClose={() => setSettingsOpen(false)} title={t('shell.settings')}>
        <div className="studio-settings-controls"><ConnectionIndicator />{preferences}</div>
      </Modal>
    </header>
  );
}
