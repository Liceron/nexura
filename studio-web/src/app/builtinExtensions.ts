// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
/**
 * Features that ship in this package but plug in through the extension registry, so that the
 * sidebar, router and workflow page do not import them. There are none at the moment: the
 * Publish tab (EDITIONS_PLAN step 4) and the Organisation page (step 5) moved to the closed
 * edition, whose Studio entry registers them through `mountStudio(root, [...])`.
 */
import type { StudioExtension } from './extensions';

/** In-tree extensions, in registration order. */
export const BUILTIN_STUDIO_EXTENSIONS: readonly StudioExtension[] = [];
