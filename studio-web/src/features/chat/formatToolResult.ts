// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/** Readable tool output that omits absent fields and preserves zero/false values. */
export function formatToolResult(value: unknown): string {
  if (typeof value === 'string') {
    try { return formatValue(JSON.parse(value)); } catch { return value; }
  }
  return formatValue(value);
}

/** Strings inside a decoded result are data, even when they look like JSON. */
function formatValue(value: unknown): string {
  if (value == null) return '';
  if (Array.isArray(value)) return value.filter(item => item != null).map(item => formatValue(item)).filter(Boolean).join('\n');
  if (typeof value === 'object') return Object.entries(value).filter(([, item]) => item != null).map(([key, item]) => `${key}: ${formatValue(item)}`).join('\n');
  return String(value);
}
