// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react';

/** Shared with the stylesheet. Render one navigation/control tree at a time. */
export function useCompactLayout() {
  const [compact, setCompact] = useState(() =>
    typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px)').matches);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(max-width: 767px)');
    const update = () => setCompact(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return compact;
}
