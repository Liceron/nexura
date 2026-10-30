// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useRef } from 'react';

/**
 * Run a step (usually moving focus) after the next render commits. A frame callback can run
 * before React has rendered the element it wants to focus, which left focus behind on phones.
 */
export function useAfterRender() {
  const pending = useRef<(() => void) | null>(null);
  useEffect(() => {
    const step = pending.current;
    pending.current = null;
    step?.();
  });
  return useCallback((step: () => void) => {
    pending.current = step;
  }, []);
}
