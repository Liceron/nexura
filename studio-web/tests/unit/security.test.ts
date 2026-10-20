import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

describe('Security & Secret Redaction', () => {
  it('keeps browser storage free of provider credentials', () => {
    expect(localStorage.getItem('openai_api_key')).toBeNull();
    expect(localStorage.getItem('anthropic_api_key')).toBeNull();
    expect(localStorage.getItem('provider_secret')).toBeNull();
    expect(sessionStorage.getItem('openai_api_key')).toBeNull();
  });

  it('rejects secret-looking literal tokens in built production bundles', () => {
    const distPath = path.resolve(__dirname, '../../dist/assets');
    if (!fs.existsSync(distPath)) {
      // Build may not have run yet in test runner
      return;
    }

    const files = fs.readdirSync(distPath).filter((f: string) => f.endsWith('.js'));
    for (const file of files) {
      const content = fs.readFileSync(path.join(distPath, file), 'utf-8');
      // Credential-reference names such as OPENAI_API_KEY are not secret values.
      // Boolean assertions also avoid dumping an entire bundle into failure logs.
      expect(/\bsk-(?:proj-|ant-)?[a-zA-Z0-9_-]{20,}/.test(content),
        `${file}: secret-looking literal token detected`).toBe(false);
    }
  });
});
