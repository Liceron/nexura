// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/** Studio entry point that takes extensions: `mountStudio(element, extensions)`. */
import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from '../App';
import { setMembershipSource } from '../api/membership';
import { BUILTIN_STUDIO_EXTENSIONS } from './builtinExtensions';
import {
  StudioExtensionError,
  StudioExtensionRegistry,
  StudioExtensionsContext,
  type StudioExtension,
} from './extensions';

/** Registries that `registerStudioExtensions` has already filled. */
const bootstrapped = new WeakSet<StudioExtensionRegistry>();

/**
 * Register the in-tree extensions, then `extensions`, in order, into `registry` (a new one by
 * default), and use the membership source of the extension that has one. Throws on any duplicate, and when `registry` was already bootstrapped (this used to
 * follow from the in-tree Organisation extension being registered twice; since that page moved
 * to the closed edition in EDITIONS_PLAN step 5 there are no in-tree extensions, so it is checked
 * directly).
 */
export function registerStudioExtensions(
  extensions: readonly StudioExtension[] = [],
  registry: StudioExtensionRegistry = new StudioExtensionRegistry(),
): StudioExtensionRegistry {
  if (bootstrapped.has(registry)) {
    throw new StudioExtensionError('This extension registry was already bootstrapped; use a new one.');
  }
  bootstrapped.add(registry);
  for (const extension of [...BUILTIN_STUDIO_EXTENSIONS, ...extensions]) {
    registry.register(extension);
  }
  // Workspace membership comes from the extension that provides it, else there is none (the
  // open-source local owner).
  setMembershipSource(registry.membership() ?? null);
  return registry;
}

/**
 * Register the in-tree extensions and `extensions` into `registry` (a new one by default) and
 * render Studio into `element` with that registry.
 */
export function mountStudio(
  element: HTMLElement,
  extensions: readonly StudioExtension[] = [],
  registry: StudioExtensionRegistry = new StudioExtensionRegistry(),
): ReactDOM.Root {
  registerStudioExtensions(extensions, registry);
  const root = ReactDOM.createRoot(element);
  root.render(
    <React.StrictMode>
      <StudioExtensionsContext.Provider value={registry}>
        <App />
      </StudioExtensionsContext.Provider>
    </React.StrictMode>,
  );
  return root;
}
