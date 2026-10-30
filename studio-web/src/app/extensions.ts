// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
/**
 * Studio extension registry: nav items, hash routes and workflow page panels that are not part
 * of the core Studio. The sidebar, the hash router and the workflow page read this registry;
 * they never import an extension's feature code. The default registry already holds the in-tree
 * extensions; `bootstrap.tsx` builds a registry with them and the extensions it is given.
 */
import { createContext, useContext, type ComponentType } from 'react';
import { BUILTIN_STUDIO_EXTENSIONS } from './builtinExtensions';
import type { MembershipSource } from '../api/membership';

/** Sidebar pages that are part of the core Studio, in sidebar order. */
export const BUILTIN_NAV_TABS = [
  'workflows',
  'templates',
  'agents',
  'skills',
  'knowledge',
  'playground',
  'approvals',
  'traces',
  'evaluations',
  'triggers',
  'credentials',
  'tools',
  'onboarding',
  'system',
] as const;
export type BuiltinNavTab = (typeof BUILTIN_NAV_TABS)[number];

/** Workflow page tabs that are part of the core Studio, in tab order (the Run Console is last). */
export const BUILTIN_WORKFLOW_PANELS = ['canvas', 'nodes', 'json', 'tests', 'runs', 'run'] as const;
export type BuiltinWorkflowPanel = (typeof BUILTIN_WORKFLOW_PANELS)[number];

/** An id contributed by an extension (any string that is not a built-in id). */
export type ExtensionId = string & Record<never, never>;

export interface StudioNavItem {
  /** Page tab id. Selecting the item opens `#/<id>`, so register a route with that hash. */
  id: string;
  /** i18n key of the label. */
  labelKey: string;
  /** Local Icon name; unknown names render the standard fallback SVG. */
  icon: string;
  /** Id of the sidebar entry this one follows; omitted or unknown means "at the end". */
  after?: string;
  /** Optional presentation group; otherwise inherited from the after anchor. */
  group?: 'build' | 'operate' | 'configure' | 'footer';
}

export interface StudioRoute {
  /** Exact hash, `#/` followed by lower-case letters, digits and dashes. */
  hash: string;
  /** Page tab the hash opens. */
  tab: string;
  /** Page rendered for that tab (lazy components are fine: Studio renders pages in Suspense). */
  component: ComponentType;
}

export interface WorkflowPanelProps {
  workflowId: string;
  workflowName: string;
}

export interface StudioPanel {
  slot: 'workflow-detail';
  id: string;
  labelKey: string;
  /** `data-testid` of the tab button (default `tab-<id>`). */
  testId?: string;
  /** Id of the tab this one follows; omitted or unknown means before the Run Console. */
  after?: string;
  component: ComponentType<WorkflowPanelProps>;
}

/** Translations in the shape of `src/i18n/en.json`: nested objects whose leaves are strings. */
export type StudioMessages = Readonly<Record<string, unknown>>;

export interface StudioExtension {
  /** Unique extension id. */
  id: string;
  navItems?: readonly StudioNavItem[];
  routes?: readonly StudioRoute[];
  panels?: readonly StudioPanel[];
  /**
   * Translations for the extension's own keys, per language code (`en`, `hi`). `t(key)` looks
   * them up only when the core dictionary has no such key, so an extension cannot change a core
   * string. A key missing in the current language falls back to `en`.
   */
  messages?: Readonly<Partial<Record<string, StudioMessages>>>;
  /**
   * Workspace membership source for role checks and the Members tab (at most one extension).
   * Without one, Studio is the local owner (admin) with no Members tab, as the open-source
   * service has no member routes.
   */
  membership?: MembershipSource;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Resolve a dotted key in a nested message object; `undefined` when it is not a string. */
export function resolveMessage(messages: unknown, key: string): string | undefined {
  let current: unknown = messages;
  for (const part of key.split('.')) {
    if (!isPlainObject(current) || !(part in current)) return undefined;
    current = current[part];
  }
  return typeof current === 'string' ? current : undefined;
}

const HASH_PATTERN = /^#\/[a-z0-9][a-z0-9-]*$/;
/** Hashes the core router already answers (pages, aliases and prefixes). */
const BUILTIN_HASHES = new Set<string>([
  ...BUILTIN_NAV_TABS.map((tab) => `#/${tab}`),
  '#/chat',
  '#/integrations',
  '#/runs',
]);

export class StudioExtensionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'StudioExtensionError';
  }
}

/**
 * Merge `extra` items into `base` ids: each item goes right after the entry named by its
 * `after` (after earlier items with the same anchor); items without a known anchor go last.
 */
export function mergeAfter<T extends { id: string; after?: string }>(
  base: readonly string[],
  extra: readonly T[],
): Array<string | T> {
  const known = new Set<string>([...base, ...extra.map((item) => item.id)]);
  const out: Array<string | T> = [];
  const placed = new Set<T>();
  const emit = (entry: string | T) => {
    out.push(entry);
    const id = typeof entry === 'string' ? entry : entry.id;
    for (const item of extra) {
      if (!placed.has(item) && item.after === id) {
        placed.add(item);
        emit(item);
      }
    }
  };
  base.forEach(emit);
  for (const item of extra) {
    if (!placed.has(item) && (item.after === undefined || !known.has(item.after))) {
      placed.add(item);
      emit(item);
    }
  }
  // Anchors that only point at each other (a cycle) are never reached: keep them, at the end.
  for (const item of extra) {
    if (!placed.has(item)) {
      placed.add(item);
      emit(item);
    }
  }
  return out;
}

export class StudioExtensionRegistry {
  private readonly items: StudioExtension[] = [];

  /** Register one extension. Throws `StudioExtensionError` on any duplicate or invalid entry. */
  register(extension: StudioExtension): void {
    const fail = (why: string): never => {
      throw new StudioExtensionError(`Studio extension '${extension.id}': ${why}`);
    };
    if (!extension.id) throw new StudioExtensionError('A Studio extension needs a non-empty id.');
    if (this.items.some((e) => e.id === extension.id)) fail('the id is already registered.');

    const navIds = new Set(this.navItems().map((n) => n.id));
    for (const nav of extension.navItems ?? []) {
      if (!nav.id || !nav.labelKey) fail('a nav item needs an id and a labelKey.');
      if ((BUILTIN_NAV_TABS as readonly string[]).includes(nav.id) || navIds.has(nav.id)) {
        fail(`nav item '${nav.id}' is already defined.`);
      }
      navIds.add(nav.id);
    }

    const hashes = new Set(this.routes().map((r) => r.hash));
    for (const route of extension.routes ?? []) {
      if (!HASH_PATTERN.test(route.hash)) fail(`route '${route.hash}' is not a '#/name' hash.`);
      if (BUILTIN_HASHES.has(route.hash) || hashes.has(route.hash)) {
        fail(`route '${route.hash}' is already defined.`);
      }
      if ((BUILTIN_NAV_TABS as readonly string[]).includes(route.tab)) {
        fail(`route '${route.hash}' cannot replace the built-in page '${route.tab}'.`);
      }
      if (!route.component) fail(`route '${route.hash}' needs a component.`);
      hashes.add(route.hash);
    }

    const panelIds = new Set(this.panels('workflow-detail').map((p) => p.id));
    for (const panel of extension.panels ?? []) {
      if (panel.slot !== 'workflow-detail') fail(`panel '${panel.id}' has an unknown slot.`);
      if (!panel.id || !panel.labelKey) fail('a panel needs an id and a labelKey.');
      if ((BUILTIN_WORKFLOW_PANELS as readonly string[]).includes(panel.id) || panelIds.has(panel.id)) {
        fail(`panel '${panel.id}' is already defined.`);
      }
      if (!panel.component) fail(`panel '${panel.id}' needs a component.`);
      panelIds.add(panel.id);
    }

    if (extension.messages !== undefined) {
      if (!isPlainObject(extension.messages)) fail('messages must map language codes to objects.');
      for (const [language, messages] of Object.entries(extension.messages)) {
        if (!/^[a-z]{2,3}$/.test(language)) fail(`messages language '${language}' is not a code.`);
        if (!isPlainObject(messages)) fail(`messages for '${language}' must be an object.`);
      }
    }

    if (extension.membership !== undefined) {
      if (this.membership() !== undefined) fail('a membership source is already registered.');
      const m = extension.membership as Partial<MembershipSource> | null;
      const methods = ['getMyMembership', 'listMembers', 'addMember', 'updateMemberRole', 'removeMember'] as const;
      if (!m || methods.some((name) => typeof m[name] !== 'function')) {
        fail(`membership needs the methods ${methods.join(', ')}.`);
      }
    }

    this.items.push(extension);
  }

  /** The membership source of the extension that has one. */
  membership(): MembershipSource | undefined {
    return this.items.find((e) => e.membership !== undefined)?.membership;
  }

  /** Registered extensions, in registration order. */
  list(): readonly StudioExtension[] {
    return [...this.items];
  }

  navItems(): StudioNavItem[] {
    return this.items.flatMap((e) => e.navItems ?? []);
  }

  routes(): StudioRoute[] {
    return this.items.flatMap((e) => e.routes ?? []);
  }

  /** Tab opened by an extension route with exactly this hash. */
  routeTab(hash: string): string | undefined {
    return this.routes().find((r) => r.hash === hash)?.tab;
  }

  /** Page component of an extension tab. */
  page(tab: string): ComponentType | undefined {
    return this.routes().find((r) => r.tab === tab)?.component;
  }

  panels(slot: StudioPanel['slot']): StudioPanel[] {
    return this.items.flatMap((e) => e.panels ?? []).filter((p) => p.slot === slot);
  }

  /**
   * Translation of `key` from the extensions' messages: the first registered extension that
   * has it in `language`, else the first that has it in `en`.
   */
  message(language: string, key: string): string | undefined {
    for (const lang of language === 'en' ? ['en'] : [language, 'en']) {
      for (const extension of this.items) {
        const found = resolveMessage(extension.messages?.[lang], key);
        if (found !== undefined) return found;
      }
    }
    return undefined;
  }
}

/** A new registry holding only the in-tree extensions (none since EDITIONS_PLAN step 5). */
export function createDefaultStudioExtensions(): StudioExtensionRegistry {
  const registry = new StudioExtensionRegistry();
  for (const extension of BUILTIN_STUDIO_EXTENSIONS) registry.register(extension);
  return registry;
}

/**
 * The registry Studio reads when no `StudioExtensionsContext` provider is present, for example
 * when `App` or `Shell` is rendered without `mountStudio`. It holds the in-tree extensions (none
 * since the Organisation page moved to the closed edition), so such a Studio has the same pages
 * and tabs as `mountStudio()`.
 * `mountStudio` renders with its own registry and never changes this one.
 */
export const studioExtensions: StudioExtensionRegistry = createDefaultStudioExtensions();

export const StudioExtensionsContext = createContext<StudioExtensionRegistry>(studioExtensions);

/** The registry of the surrounding `StudioExtensionsContext` (the global one by default). */
export function useStudioExtensions(): StudioExtensionRegistry {
  return useContext(StudioExtensionsContext);
}
