// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { Icon } from '../components/Icon';
import { useI18n } from '../i18n';
import { mergeAfter, useStudioExtensions, type BuiltinNavTab, type ExtensionId } from './extensions';

const BUILTIN_TABS: ReadonlyArray<{ id: BuiltinNavTab; labelKey: string; icon: string }> = [
  { id: 'workflows', labelKey: 'nav.workflows', icon: 'workflows' },
  { id: 'templates', labelKey: 'nav.templates', icon: 'templates' },
  { id: 'approvals', labelKey: 'nav.approvals', icon: 'approvals' },
  { id: 'playground', labelKey: 'nav.playground', icon: 'playground' },
  { id: 'knowledge', labelKey: 'nav.knowledge', icon: 'knowledge' },
  { id: 'skills', labelKey: 'nav.skills', icon: 'skills' },
  { id: 'agents', labelKey: 'nav.agents', icon: 'agents' },
  { id: 'credentials', labelKey: 'nav.credentials', icon: 'credentials' },
  { id: 'tools', labelKey: 'nav.tools', icon: 'tools' },
  { id: 'evaluations', labelKey: 'nav.evaluations', icon: 'evaluations' },
  { id: 'triggers', labelKey: 'nav.triggers', icon: 'triggers' },
  { id: 'traces', labelKey: 'nav.traces', icon: 'traces' },
  { id: 'onboarding', labelKey: 'nav.onboarding', icon: 'onboarding' },
  { id: 'system', labelKey: 'nav.system', icon: 'system' },
];

/** A Studio page: a built-in one or one registered through `extensions.ts`. */
export type NavTab = BuiltinNavTab | ExtensionId;

interface SidebarProps {
  currentTab: NavTab;
  onSelectTab: (tab: NavTab) => void;
  pendingApprovalsCount?: number;
}

export function Sidebar({ currentTab, onSelectTab, pendingApprovalsCount = 0 }: SidebarProps) {
  const { t } = useI18n();

  const extensions = useStudioExtensions();
  // Extension nav items are placed after the entry their `after` names.
  const builtinById = new Map(BUILTIN_TABS.map((tab) => [tab.id as string, tab]));
  const tabList: Array<{ id: NavTab; labelKey: string; icon: string }> = mergeAfter(
    BUILTIN_TABS.map((tab) => tab.id),
    extensions.navItems(),
  ).map((entry) => (typeof entry === 'string' ? builtinById.get(entry)! : entry));

  return (
    <nav
      aria-label={t('ui.studioNavigation')}
      className="studio-sidebar"
    >
      {tabList.map((tab) => {
        const isActive = currentTab === tab.id;
        return (
          <button
            key={tab.id}
            data-testid={`nav-item-${tab.id}`}
            aria-label={t(tab.labelKey)}
            aria-current={isActive ? 'page' : undefined}
            onClick={() => onSelectTab(tab.id)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '8px 12px',
              borderRadius: 'var(--radius-md)',
              backgroundColor: isActive ? 'var(--accent-subtle)' : 'transparent',
              color: isActive ? 'var(--accent-primary)' : 'var(--text-secondary)',
              fontWeight: isActive ? 600 : 500,
              fontSize: 13,
              textAlign: 'left',
              cursor: 'pointer',
              transition: 'background-color 0.15s ease',
            }}
          >
            <Icon name={tab.icon} />
            <span className="studio-sidebar-label" style={{ flex: 1 }}>{t(tab.labelKey)}</span>
            {tab.id === 'approvals' && pendingApprovalsCount > 0 && (
              <span
                className="studio-sidebar-badge"
                style={{
                  backgroundColor: 'var(--status-warning)',
                  color: '#000',
                  fontSize: 11,
                  fontWeight: 700,
                  padding: '1px 6px',
                  borderRadius: 10,
                }}
              >
                {pendingApprovalsCount}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
