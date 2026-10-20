// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
export const ALLOWED_EXTENSIONS = ['.txt', '.md', '.markdown', '.html', '.htm', '.pdf'];

/** Request body fields for `POST /knowledge/documents/upload`. */
export interface PreparedUpload {
  filename: string;
  content: string;
  mimeType: string;
  encoding: 'text' | 'base64';
}

/** Result of validating and reading a user-selected file. */
export type PrepareResult =
  | { ok: true; upload: PreparedUpload }
  | { ok: false; reason: 'too_large' | 'unsupported' | 'empty' | 'read_failed' };

/** Human-readable size such as `5 MB` or `120 KB`. */
export function formatBytes(n: number): string {
  if (n >= 1_000_000) return `${Math.round((n / 1_000_000) * 10) / 10} MB`;
  if (n >= 1_000) return `${Math.round(n / 1_000)} KB`;
  return `${n} B`;
}

function mimeFor(name: string): string {
  const lower = name.toLowerCase();
  if (lower.endsWith('.pdf')) return 'application/pdf';
  if (lower.endsWith('.html') || lower.endsWith('.htm')) return 'text/html';
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'text/markdown';
  return 'text/plain';
}

function toBase64(bytes: Uint8Array): string {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

/** Validate type and size locally (the server enforces the same limits), then read the file. */
export async function prepareUpload(file: File, maxBytes: number): Promise<PrepareResult> {
  const lower = file.name.toLowerCase();
  if (!ALLOWED_EXTENSIONS.some((ext) => lower.endsWith(ext))) return { ok: false, reason: 'unsupported' };
  if (file.size > maxBytes) return { ok: false, reason: 'too_large' };
  if (file.size === 0) return { ok: false, reason: 'empty' };
  try {
    const mimeType = mimeFor(file.name);
    if (mimeType === 'application/pdf') {
      const bytes = new Uint8Array(await file.arrayBuffer());
      return { ok: true, upload: { filename: file.name, content: toBase64(bytes), mimeType, encoding: 'base64' } };
    }
    const content = await file.text();
    return { ok: true, upload: { filename: file.name, content, mimeType, encoding: 'text' } };
  } catch {
    return { ok: false, reason: 'read_failed' };
  }
}
