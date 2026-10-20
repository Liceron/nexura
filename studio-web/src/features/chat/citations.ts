// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
/** A knowledge citation found in tool output, shown as a chat footnote. */
export interface ChatCitation {
  documentId: string;
  chunkIndex: number;
  title: string;
  sourceUri: string | null;
  chunkId: string | null;
}

function fromObject(obj: unknown, out: ChatCitation[], depth: number): void {
  if (depth > 4 || obj === null || obj === undefined) return;
  if (typeof obj === 'string') {
    const t = obj.trim();
    if (t.startsWith('{') && t.includes('"citations"')) {
      try {
        fromObject(JSON.parse(t), out, depth + 1);
      } catch {
        // not JSON: no citations
      }
    }
    return;
  }
  if (typeof obj !== 'object') return;
  const rec = obj as Record<string, unknown>;
  const list = rec.citations;
  if (Array.isArray(list)) {
    for (const c of list) {
      if (!c || typeof c !== 'object') continue;
      const r = c as Record<string, unknown>;
      if (typeof r.document_id === 'string' && Number.isInteger(r.chunk_index)) {
        const title = typeof r.title === 'string' ? r.title : typeof r.origin === 'string' ? r.origin : r.document_id;
        out.push({
          documentId: r.document_id,
          chunkIndex: r.chunk_index as number,
          title,
          sourceUri: typeof r.source_uri === 'string' ? r.source_uri : null,
          chunkId: typeof r.chunk_id === 'string' ? r.chunk_id : null,
        });
      }
    }
  }
  if ('output' in rec) fromObject(rec.output, out, depth + 1);
}

/** Collect de-duplicated citations from tool outputs (objects or JSON strings with `citations`). */
export function extractCitations(outputs: ReadonlyArray<unknown>): ChatCitation[] {
  const found: ChatCitation[] = [];
  for (const o of outputs) fromObject(o, found, 0);
  const seen = new Set<string>();
  return found.filter((c) => {
    const key = `${c.documentId}#${c.chunkIndex}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
