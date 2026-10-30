// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { Icon } from '../components/Icon';
import { useI18n } from '../i18n';
import { mergeAfter, useStudioExtensions, type BuiltinNavTab, type ExtensionId, type StudioNavItem } from './extensions';

type NavGroup = NonNullable<StudioNavItem['group']>;
const GROUPS: ReadonlyArray<{ id: NavGroup; tabs: readonly BuiltinNavTab[] }> = [
  { id: 'build', tabs: ['workflows', 'templates', 'agents', 'skills', 'knowledge', 'playground'] },
  { id: 'operate', tabs: ['approvals', 'traces', 'evaluations', 'triggers'] },
  { id: 'configure', tabs: ['credentials', 'tools'] },
  { id: 'footer', tabs: ['onboarding', 'system'] },
];
// Keep message keys explicit for translation extraction and the documentation audit.
const LABEL_KEYS: Record<BuiltinNavTab, string> = {
  workflows: 'nav.workflows', templates: 'nav.templates', agents: 'nav.agents',
  skills: 'nav.skills', knowledge: 'nav.knowledge', playground: 'nav.playground',
  approvals: 'nav.approvals', traces: 'nav.traces', evaluations: 'nav.evaluations',
  triggers: 'nav.triggers', credentials: 'nav.credentials', tools: 'nav.tools',
  onboarding: 'nav.onboarding', system: 'nav.system',
};
const BUILTIN_TABS = GROUPS.flatMap(({ id: group, tabs }) =>
  tabs.map((id) => ({ id, labelKey: LABEL_KEYS[id], icon: id, group })));

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
  const extra = extensions.navItems();
  const byId = new Map<string, StudioNavItem>([...BUILTIN_TABS, ...extra].map((tab) => [tab.id, tab]));
  const groupFor = (item: StudioNavItem, seen = new Set<string>()): NavGroup => {
    if (item.group) return item.group;
    if (!item.after || seen.has(item.id)) return 'configure';
    seen.add(item.id);
    const anchor = byId.get(item.after);
    return anchor ? groupFor(anchor, seen) : 'configure';
  };
  const tabList = mergeAfter(
    BUILTIN_TABS.map((tab) => tab.id),
    extra,
  ).map((entry) => {
    const item = typeof entry === 'string' ? builtinById.get(entry)! : entry;
    return { ...item, group: groupFor(item) };
  });

  return (
    <nav
      aria-label={t('ui.studioNavigation')}
      className="studio-sidebar"
    >
      {GROUPS.map(({ id: group }) => (
        <div key={group} className={`studio-nav-group studio-nav-group--${group}`}
          role="group" aria-label={t(`shell.groups.${group}`)}>
          {group !== 'footer' && <div className="studio-nav-group-label" aria-hidden="true">{t(`shell.groups.${group}`)}</div>}
      {tabList.filter((tab) => tab.group === group).map((tab) => {
        const isActive = currentTab === tab.id;
        return (
          <button
            key={tab.id}
            data-testid={`nav-item-${tab.id}`}
            aria-label={t(tab.labelKey)}
            aria-current={isActive ? 'page' : undefined}
            onClick={() => onSelectTab(tab.id)}
            type="button"
            className="studio-nav-item"
          >
            <Icon name={tab.icon} />
            <span className="studio-sidebar-label">{t(tab.labelKey)}</span>
            {tab.id === 'approvals' && pendingApprovalsCount > 0 && (
              <span
                className="studio-sidebar-badge"
                aria-label={t('shell.pendingApprovals').replace('{count}', String(pendingApprovalsCount))}
              >
                {pendingApprovalsCount}
              </span>
            )}
          </button>
        );
      })}
        </div>
      ))}
    </nav>
  );
}
