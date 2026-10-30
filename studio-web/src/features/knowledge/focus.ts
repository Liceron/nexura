// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
/** Session-storage key used to hand a cited chunk from chat footnotes to the Knowledge page. */
export const KNOWLEDGE_FOCUS_KEY = 'nexura_knowledge_focus';

/** A document chunk the Knowledge page should open. */
export interface KnowledgeFocus {
  documentId: string;
  chunkIndex: number;
}

/** Remember which chunk to show, then navigate to `#/knowledge` (done by the caller). */
export function setKnowledgeFocus(focus: KnowledgeFocus): void {
  try {
    sessionStorage.setItem(KNOWLEDGE_FOCUS_KEY, JSON.stringify(focus));
  } catch {
    // sessionStorage unavailable: the link still opens the Knowledge page.
  }
}

/** Read and clear the pending focus, validating its shape. */
export function takeKnowledgeFocus(): KnowledgeFocus | null {
  try {
    const raw = sessionStorage.getItem(KNOWLEDGE_FOCUS_KEY);
    if (!raw) return null;
    sessionStorage.removeItem(KNOWLEDGE_FOCUS_KEY);
    const v = JSON.parse(raw) as Partial<KnowledgeFocus>;
    if (typeof v.documentId === 'string' && Number.isInteger(v.chunkIndex)) {
      return { documentId: v.documentId, chunkIndex: v.chunkIndex as number };
    }
  } catch {
    // malformed value: ignore
  }
  return null;
}
