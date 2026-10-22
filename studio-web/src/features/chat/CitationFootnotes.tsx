// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { useEffect, useState } from "react";
import { useApi } from "../../api/context";
import { useI18n } from "../../i18n";
import { setKnowledgeFocus } from "../knowledge/focus";
import type { ChatCitation } from "./citations";

const keyOf = (c: ChatCitation) => `${c.documentId}#${c.chunkIndex}`;

/** Citation keys whose chunk could not be resolved in the current workspace. */
function useUnresolved(citations: ChatCitation[]): Set<string> {
  const { manager } = useApi();
  const [bad, setBad] = useState<Set<string>>(new Set());
  useEffect(() => {
    let live = true;
    for (const c of citations) {
      manager.client
        .request<{ chunk_id?: string }>(
          "GET",
          `/knowledge/documents/${encodeURIComponent(c.documentId)}/chunks/${c.chunkIndex}`,
        )
        .then((chunk) => {
          if (
            live &&
            c.chunkId &&
            chunk?.chunk_id &&
            chunk.chunk_id !== c.chunkId
          )
            setBad((prev) => new Set(prev).add(keyOf(c)));
        })
        .catch(() => {
          if (live) setBad((prev) => new Set(prev).add(keyOf(c)));
        });
    }
    return () => {
      live = false;
    };
  }, [manager, citations.map(keyOf).join("|")]);
  return bad;
}

/** Numbered source footnotes; each links to the cited chunk, or says the source is unavailable. */
export function CitationFootnotes({
  citations,
}: {
  citations: ChatCitation[];
}) {
  const { t } = useI18n();
  const unresolved = useUnresolved(citations);
  if (citations.length === 0) return null;
  return (
    <div data-testid="citation-footnotes" style={{ fontSize: "var(--type-ui)" }}>
      <div
        style={{
          fontWeight: 600,
          color: "var(--text-secondary)",
          marginBottom: 4,
        }}
      >
        {t("citations.sources", "Sources")}
      </div>
      <ol
        style={{
          margin: 0,
          paddingLeft: 20,
          display: "flex",
          flexDirection: "column",
          gap: 2,
        }}
      >
        {citations.map((c, i) => (
          <li
            key={`${c.documentId}#${c.chunkIndex}`}
            data-testid={`citation-footnote-${i + 1}`}
          >
            {unresolved.has(keyOf(c)) ? (
              <span
                data-testid={`citation-footnote-unavailable-${i + 1}`}
                style={{ color: "var(--text-secondary)" }}
              >
                {t("citations.unavailable", "source unavailable")}
              </span>
            ) : (
              <a
                href="#/knowledge"
                data-testid={`citation-footnote-link-${i + 1}`}
                title={t("citations.openChunk", "Open source chunk")}
                onClick={() =>
                  setKnowledgeFocus({
                    documentId: c.documentId,
                    chunkIndex: c.chunkIndex,
                  })
                }
                style={{ color: "var(--accent-primary)" }}
              >
                {c.title}
              </a>
            )}{" "}
            <span style={{ color: "var(--text-muted)" }}>
              ({t("citations.chunk", "chunk")} {c.chunkIndex})
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
