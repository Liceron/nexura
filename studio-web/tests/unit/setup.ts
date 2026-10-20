import '@testing-library/jest-dom/vitest';

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

if (typeof window !== 'undefined') {
  window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
}
