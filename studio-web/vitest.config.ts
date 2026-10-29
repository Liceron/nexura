import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/unit/setup.ts'],
    globals: true,
    include: ['tests/unit/**/*.{test,spec}.{ts,tsx}'],
    // Studio ships English-only; the switched-off Hindi support keeps its tests.
    env: { VITE_LANGUAGE_CHOICE: 'on' },
  },
});
