# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Versioned synthetic en/hi document retrieval evaluation (offline by default)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from nexura_runtime.knowledge.embeddings.config import configured_embedder, describe_embedder
from nexura_runtime.knowledge.hybrid import HybridKnowledgeIndex

DATASET = Path(__file__).with_name("retrieval_v1.json")


async def evaluate(*, real: bool = False, top_k: int = 5) -> dict[str, Any]:
    """Measure document recall@k and truncated MRR, never dispatching live by default."""
    if top_k < 1 or top_k > 50:
        raise ValueError("top_k must be between 1 and 50")
    raw = DATASET.read_bytes()
    dataset = json.loads(raw)
    modes = ["bm25_only", "fixture_hybrid"]
    if real:
        if os.environ.get("CI"):
            raise ValueError("Real embedding evaluation is disabled in CI.")
        spec = os.environ.get("NEXURA_EMBEDDINGS", "")
        provider = spec.partition(":")[0]
        keys = {
            "openai": ("OPENAI_API_KEY",),
            "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "vertex": ("GOOGLE_APPLICATION_CREDENTIALS",),
        }.get(provider, ())
        if not any(os.environ.get(k) for k in keys):
            raise ValueError("--real requires NEXURA_EMBEDDINGS and explicit provider credentials.")
        modes.append("real_hybrid")

    output: dict[str, Any] = {
        "dataset_version": dataset["version"],
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "corpus_documents": len(dataset["documents"]),
        "results": {},
    }
    for mode in modes:
        # Explicit fixture selection ignores any ambient live provider configuration.
        embedder = (
            configured_embedder() if mode == "real_hybrid" else configured_embedder("fixture")
        )
        index = HybridKnowledgeIndex(embedder=embedder)
        try:
            ids: dict[str, str] = {}
            for doc in dataset["documents"]:
                outcome = await index.ingest(
                    workspace_id="eval",
                    filename=doc["id"] + ".txt",
                    data=doc["text"].encode("utf-8"),
                )
                ids[outcome.document.document_id] = doc["id"]
            rows: list[dict[str, Any]] = []
            for query in dataset["queries"]:
                hits = await index.search(
                    workspace_id="eval",
                    query=query["query"],
                    top_k=top_k,
                    vector_weight=0.0 if mode == "bm25_only" else 0.7,
                    keyword_weight=1.0 if mode == "bm25_only" else 0.3,
                )
                ranked = list(dict.fromkeys(ids[h.citation.document_id] for h in hits))
                gold = set(query["gold_document_ids"])
                rank = next((i for i, d in enumerate(ranked, 1) if d in gold), None)
                rows.append(
                    {
                        "id": query["id"],
                        "language": query["language"],
                        "ranked_document_ids": ranked,
                        "reciprocal_rank": 1 / rank if rank else 0.0,
                        "recalls": {
                            k: len(gold.intersection(ranked[:k])) / len(gold)
                            for k in (1, 3, 5)
                            if k <= top_k
                        },
                    }
                )
            metrics = {}
            for language in ("all", "en", "hi"):
                group = [r for r in rows if language == "all" or r["language"] == language]
                metrics[language] = {
                    "queries": len(group),
                    f"mrr@{top_k}": sum(r["reciprocal_rank"] for r in group) / len(group),
                    **{
                        f"recall@{k}": sum(r["recalls"][k] for r in group) / len(group)
                        for k in (1, 3, 5)
                        if k <= top_k
                    },
                }
            output["results"][mode] = {
                "embedder": describe_embedder(embedder),
                "metrics": metrics,
                "queries": rows,
            }
        finally:
            index._conn.close()
            close = getattr(embedder.provider, "close", None)
            if close is not None:
                await close()
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="Opt in to billed provider calls")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(evaluate(real=args.real, top_k=args.top_k)), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
