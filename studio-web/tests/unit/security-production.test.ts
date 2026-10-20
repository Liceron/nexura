// @vitest-environment node
import { describe, it, expect } from 'vitest';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { resolveConfig } from 'vite';

describe('Production browser environment', () => {
  it('does not expose host provider credentials through Vite client configuration', async () => {
    // Vitest/jsdom exposes host keys differently and cannot safely initialize
    // esbuild's native TextEncoder. Verify actual browser build config in Node.
    const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
    const config = await resolveConfig({ root, logLevel: 'silent' }, 'build', 'production');
    for (const key of Object.keys(config.env)) {
      expect(key).not.toMatch(/OPENAI|ANTHROPIC|MISTRAL|GEMINI|SECRET/i);
    }
  });
});
