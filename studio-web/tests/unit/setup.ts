import '@testing-library/jest-dom/vitest';
import { configure } from '@testing-library/react';

// CI runners are slower than local machines; findBy*/waitFor wait up to 5 s instead of 1 s.
configure({ asyncUtilTimeout: 5000 });

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

if (typeof window !== 'undefined') {
  window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
}
