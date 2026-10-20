// B39 review: canonicalJson matches Python's json.dumps for DEL and astral keys, and the
// snippets send the shared-service API key from the environment without embedding it.
import { describe, it, expect } from 'vitest';
import {
  WEBHOOK_API_KEY_ENV,
  buildCurlSnippet,
  buildPythonSnippet,
  canonicalJson,
} from '../../src/features/triggers/webhookSnippet';

describe('B39 review: canonicalJson', () => {
  it('escapes DEL (U+007F) like Python ensure_ascii', () => {
    expect(canonicalJson({ a: 'x\u007fy' })).toBe('{"a":"x\\u007fy"}');
  });

  it('sorts keys by code point like Python, not by UTF-16 code unit', () => {
    // Python: sorted(["｡", "\U0001f600"]) keeps U+FF61 first.
    expect(canonicalJson({ '\u{1F600}': 2, '｡': 1 })).toBe('{"\\uff61":1,"\\ud83d\\ude00":2}');
    expect(canonicalJson({ b: 1, a: 2, ab: 3 })).toBe('{"a":2,"ab":3,"b":1}');
  });
});

describe('B39 review: shared-service API key', () => {
  const deliveryUrl = 'http://127.0.0.1:8765/api/v1/triggers/webhooks/orders/deliver';

  it('curl sends a bearer header only when the key variable is set', () => {
    const curl = buildCurlSnippet({ deliveryUrl });
    expect(curl).toContain(`\${${WEBHOOK_API_KEY_ENV}:+-H "Authorization: Bearer $${WEBHOOK_API_KEY_ENV}"}`);
    expect(curl).not.toMatch(/nxk_/);
  });

  it('Python reads the key from the environment and adds Authorization when present', () => {
    const py = buildPythonSnippet({ deliveryUrl });
    expect(py).toContain(`api_key = os.environ.get("${WEBHOOK_API_KEY_ENV}")`);
    expect(py).toContain('headers["Authorization"] = "Bearer " + api_key');
    expect(py).not.toMatch(/nxk_/);
  });
});
