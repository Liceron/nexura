// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/** A run of snippet text, either plain or highlighted. */
export interface HighlightSegment {
  text: string;
  mark: boolean;
}

/** Unit of highlight offsets: Unicode code points (Python `str` indexes) or UTF-16 code units. */
export type HighlightUnit = 'codepoint' | 'utf16';

/** Convert a code-point index of `text` to a UTF-16 index (JS string index). */
function cpToUtf16(text: string, cp: number): number {
  let units = 0;
  let seen = 0;
  for (const ch of text) {
    if (seen >= cp) break;
    units += ch.length;
    seen += 1;
  }
  return units;
}

/**
 * Split `text` into plain/highlighted segments from `[start, end)` character spans.
 * `unit` says how spans are measured (default code points, as Python reports them;
 * use 'utf16' for the API's `highlights_utf16`). Spans never split surrogate pairs.
 * Spans are clamped, sorted and merged; invalid spans are dropped. The result is plain
 * strings only, so callers render segments as React text nodes (never as HTML).
 */
export function buildHighlightSegments(
  text: string,
  spans: ReadonlyArray<readonly [number, number]> | undefined | null,
  unit: HighlightUnit = 'codepoint',
): HighlightSegment[] {
  if (!text) return [];
  const conv = (n: number) => (unit === 'codepoint' ? cpToUtf16(text, Math.max(0, Math.floor(n))) : Math.floor(n));
  const isLow = (i: number) => {
    const c = text.charCodeAt(i);
    return c >= 0xdc00 && c <= 0xdfff && i > 0 && text.charCodeAt(i - 1) >= 0xd800 && text.charCodeAt(i - 1) <= 0xdbff;
  };
  const snap = (i: number) => (i > 0 && i < text.length && isLow(i) ? i - 1 : i);
  const valid = (spans ?? [])
    .filter((s) => Array.isArray(s) && Number.isFinite(s[0]) && Number.isFinite(s[1]))
    .map(([a, b]) => [snap(Math.max(0, conv(a))), snap(Math.min(text.length, conv(b)))] as [number, number])
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0]);
  const merged: [number, number][] = [];
  for (const [a, b] of valid) {
    const last = merged[merged.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else merged.push([a, b]);
  }
  const out: HighlightSegment[] = [];
  let cursor = 0;
  for (const [a, b] of merged) {
    if (a > cursor) out.push({ text: text.slice(cursor, a), mark: false });
    out.push({ text: text.slice(a, b), mark: true });
    cursor = b;
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor), mark: false });
  return out;
}

/** Return `uri` only when it is an http(s) URL, otherwise null (blocks javascript: links). */
export function safeHref(uri: string | null | undefined): string | null {
  if (!uri) return null;
  try {
    const u = new URL(uri);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  } catch {
    return null;
  }
}
