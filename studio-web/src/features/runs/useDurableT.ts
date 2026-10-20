// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useT } from '../../i18n';

type Translate = (key: string, fallback?: string) => string;

/** Translate durable panels, including standalone consumers without a provider. */
export function useDurableT(): Translate {
  return useT();
}
