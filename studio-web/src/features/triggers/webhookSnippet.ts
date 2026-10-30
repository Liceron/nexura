// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
// B39: delivery URL and signing snippets for a Studio-registered webhook.
//
// Mirrors the server's v2 scheme (src/nexura_runtime/service/triggers/signatures.py):
// HMAC-SHA256, hex encoded, over `v2.<integer unix timestamp>.<event id>.` followed by the
// canonical JSON body, i.e. Python's json.dumps(body, sort_keys=True, separators=(",", ":"))
// with its default ensure_ascii escaping. The snippets read the secret from an environment
// variable, so they never contain it and can be shown again after the secret is dismissed.
// On a shared service (authenticated-shared-service profile) /deliver also needs an API key or
// bearer token like every /api/v1 route; the snippets send `Authorization: Bearer` from the
// NEXURA_API_KEY environment variable when it is set, and never embed it either.
//
// No imports: tests/test_b39_webhook_snippet.py loads this file with Node's type stripping and
// runs the generated snippets against the real deliver route.

export const WEBHOOK_SIGNATURE_HEADER = 'X-Webhook-Signature';
export const WEBHOOK_TIMESTAMP_HEADER = 'X-Webhook-Timestamp';
export const WEBHOOK_EVENT_ID_HEADER = 'X-Webhook-Event-ID';
/** Environment variable the snippets read the signing secret from. */
export const WEBHOOK_SECRET_ENV = 'NEXURA_WEBHOOK_SECRET';
/** Environment variable the snippets read the service API key from (shared service only). */
export const WEBHOOK_API_KEY_ENV = 'NEXURA_API_KEY';
/** Event ids the server accepts (no dots, so the signed string stays unambiguous). */
export const WEBHOOK_EVENT_ID_PATTERN = /^[A-Za-z0-9_:-]{1,200}$/;

export const SAMPLE_WEBHOOK_PAYLOAD: Record<string, unknown> = {
  event: 'ping',
  message: 'hello from a webhook',
};

/** `POST` target for signed deliveries; `endpoint` is the service origin (see deriveEndpoint). */
export function webhookDeliveryUrl(endpoint: string, endpointId: string): string {
  return `${endpoint.replace(/\/+$/, '')}/api/v1/triggers/webhooks/${encodeURIComponent(endpointId)}/deliver`;
}

// Python's ensure_ascii escapes everything outside printable ASCII, so DEL (U+007F) as well.
// Astral characters are already two UTF-16 code units here, each escaped like Python does.
function escapeNonAscii(text: string): string {
  return text.replace(/[\u007f-\uffff]/g, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

// Python sorts str keys by code point; JavaScript's `<` compares UTF-16 code units, which
// orders astral characters (surrogate pairs) before U+E000-U+FFFF.
function compareCodePoints(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i += 1) {
    const d = (x[i].codePointAt(0) ?? 0) - (y[i].codePointAt(0) ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

/**
 * Canonical JSON as the server computes it before checking the signature. Use it for JSON
 * objects of strings, integers, booleans, null, arrays and nested objects; exotic floats
 * (for example 1e-7) are formatted differently by JavaScript and Python.
 */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    const text = JSON.stringify(value);
    if (text === undefined) throw new Error('canonicalJson: value is not JSON serialisable');
    return escapeNonAscii(text);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item === undefined ? null : item)).join(',')}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => compareCodePoints(a, b));
  return `{${entries.map(([k, v]) => `${escapeNonAscii(JSON.stringify(k))}:${canonicalJson(v)}`).join(',')}}`;
}

/** The exact string that is HMAC-signed for one delivery. */
export function webhookSigningString(timestamp: number, eventId: string, canonicalBody: string): string {
  if (!WEBHOOK_EVENT_ID_PATTERN.test(eventId)) {
    throw new Error('event id must be 1-200 characters of [A-Za-z0-9_:-]');
  }
  return `v2.${Math.trunc(timestamp)}.${eventId}.${canonicalBody}`;
}

function shellQuote(text: string): string {
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

function pythonLiteral(value: unknown, indent: string): string {
  if (value === null || value === undefined) return 'None';
  if (value === true) return 'True';
  if (value === false) return 'False';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'None';
  if (typeof value === 'string') return JSON.stringify(value); // a JSON string is a valid Python str
  const inner = `${indent}    `;
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    return `[\n${value.map((v) => `${inner}${pythonLiteral(v, inner)},`).join('\n')}\n${indent}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== undefined);
  if (entries.length === 0) return '{}';
  return `{\n${entries.map(([k, v]) => `${inner}${JSON.stringify(k)}: ${pythonLiteral(v, inner)},`).join('\n')}\n${indent}}`;
}

export interface WebhookSnippetOptions {
  deliveryUrl: string;
  payload?: Record<string, unknown>;
}

/** POSIX shell + openssl + curl. BODY is already canonical, so it is signed and sent as is. */
export function buildCurlSnippet({ deliveryUrl, payload = SAMPLE_WEBHOOK_PAYLOAD }: WebhookSnippetOptions): string {
  return [
    `# Signature v2: hex HMAC-SHA256 of "v2.<unix seconds>.<event id>.<canonical JSON body>"`,
    `SECRET="\${${WEBHOOK_SECRET_ENV}:?set ${WEBHOOK_SECRET_ENV} to the webhook signing secret}"`,
    `# Shared service: also export ${WEBHOOK_API_KEY_ENV} (an API key); it is sent as a bearer token.`,
    'TS=$(date +%s)',
    'EID="evt-$TS-$$"',
    `BODY=${shellQuote(canonicalJson(payload))}   # canonical JSON: sorted keys, no spaces`,
    `SIG=$(printf 'v2.%s.%s.%s' "$TS" "$EID" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')`,
    '',
    `curl -s -X POST ${shellQuote(deliveryUrl)} \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H "${WEBHOOK_TIMESTAMP_HEADER}: $TS" \\`,
    `  -H "${WEBHOOK_EVENT_ID_HEADER}: $EID" \\`,
    `  -H "${WEBHOOK_SIGNATURE_HEADER}: $SIG" \\`,
    `  \${${WEBHOOK_API_KEY_ENV}:+-H "Authorization: Bearer $${WEBHOOK_API_KEY_ENV}"} \\`,
    '  -d "$BODY"',
  ].join('\n');
}

/** Python 3 standard library only. */
export function buildPythonSnippet({ deliveryUrl, payload = SAMPLE_WEBHOOK_PAYLOAD }: WebhookSnippetOptions): string {
  return [
    'import hashlib',
    'import hmac',
    'import json',
    'import os',
    'import time',
    'import urllib.request',
    'import uuid',
    '',
    `URL = ${JSON.stringify(deliveryUrl)}`,
    `secret = os.environ[${JSON.stringify(WEBHOOK_SECRET_ENV)}]`,
    `api_key = os.environ.get(${JSON.stringify(WEBHOOK_API_KEY_ENV)})  # needed on a shared service`,
    `payload = ${pythonLiteral(payload, '')}`,
    '',
    '# Canonical JSON body: sorted keys, no spaces. Sign and send these exact bytes.',
    'body = json.dumps(payload, sort_keys=True, separators=(",", ":"))',
    'ts = str(int(time.time()))',
    'event_id = "evt-" + uuid.uuid4().hex  # 1-200 chars of [A-Za-z0-9_:-]',
    'signed = f"v2.{ts}.{event_id}.{body}".encode("utf-8")',
    'signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()',
    '',
    'headers = {',
    '    "Content-Type": "application/json",',
    `    "${WEBHOOK_TIMESTAMP_HEADER}": ts,`,
    `    "${WEBHOOK_EVENT_ID_HEADER}": event_id,`,
    `    "${WEBHOOK_SIGNATURE_HEADER}": signature,`,
    '}',
    'if api_key:',
    '    headers["Authorization"] = "Bearer " + api_key',
    '',
    'request = urllib.request.Request(URL, data=body.encode("utf-8"), method="POST", headers=headers)',
    'with urllib.request.urlopen(request) as response:',
    '    print(response.status, response.read().decode("utf-8"))',
  ].join('\n');
}
