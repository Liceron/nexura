# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Context packing, token budgeting, and verifiable citation mapping."""

from __future__ import annotations

from collections.abc import Sequence

from nexura_runtime.knowledge.chunks.tokenizers import (
    TokenEstimator,
    default_token_estimator,
)
from nexura_runtime.knowledge.retrieval.errors import ContextBudgetExceededError
from nexura_runtime.knowledge.retrieval.models import (
    Citation,
    PackedContext,
    RetrievedItem,
)


class ContextPacker:
    """Packs retrieved items into a token-bounded prompt context with verifiable citations.

    Guarantees:
    - Deleted or permission-restricted documents never enter prompts.
    - Context token budget is strictly enforced without silent overflow.
    - Preserves exact document ID, page citations, and chunk character spans.
    - Accurately signals insufficient evidence when retrieval is empty.
    """

    def __init__(
        self,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self.token_estimator = token_estimator or default_token_estimator

    def pack(
        self,
        items: Sequence[RetrievedItem],
        *,
        max_context_tokens: int = 2000,
        allowed_document_ids: Sequence[str] | None = None,
        min_score_threshold: float = 0.0,
    ) -> PackedContext:
        allowed_set = set(allowed_document_ids) if allowed_document_ids is not None else None

        # 1. Permission and threshold filtering
        eligible_items: list[RetrievedItem] = []
        for it in items:
            # Check deletion flag in metadata
            if it.metadata.get("is_deleted", False):
                continue

            # Check document permissions
            if allowed_set is not None and it.document_id not in allowed_set:
                continue

            # Check score threshold
            if it.score < min_score_threshold:
                continue

            eligible_items.append(it)

        # 2. Handle empty evidence case
        if not eligible_items:
            return PackedContext(
                items=[],
                citations=[],
                citation_map={},
                formatted_context="No relevant source documents were retrieved.",
                total_tokens=0,
                has_sufficient_evidence=False,
                truncated=False,
            )

        # 3. Pack items within token budget
        packed_items: list[RetrievedItem] = []
        citations: list[Citation] = []
        context_blocks: list[str] = []
        current_tokens = 0
        truncated = False

        header = "=== Reference Sources & Evidence ===\n"
        current_tokens += self.token_estimator.estimate_tokens(header)

        for idx, it in enumerate(eligible_items):
            cit_id = f"[{idx + 1}]"
            origin_name = it.origin or it.document_id

            if it.page_start == it.page_end:
                page_str = f"Page {it.page_start}"
            else:
                page_str = f"Pages {it.page_start}..{it.page_end}"

            block = f"{cit_id} Source: {origin_name} ({page_str})\nContent: {it.text}\n"
            block_tokens = self.token_estimator.estimate_tokens(block)

            if current_tokens + block_tokens > max_context_tokens:
                if not packed_items:
                    # Not even the first item fits within the budget
                    raise ContextBudgetExceededError(
                        required_tokens=current_tokens + block_tokens,
                        max_context_tokens=max_context_tokens,
                    )
                truncated = True
                break

            current_tokens += block_tokens
            context_blocks.append(block)
            packed_items.append(it)

            citation = Citation(
                citation_id=cit_id,
                document_id=it.document_id,
                origin=origin_name,
                chunk_id=it.chunk_id,
                page_start=it.page_start,
                page_end=it.page_end,
                start_char=it.start_char,
                end_char=it.end_char,
                snippet=it.text[:200],
                score=it.score,
                metadata=it.metadata,
            )
            citations.append(citation)

        formatted_text = header + "\n" + "\n".join(context_blocks)
        cit_map = {c.citation_id: c for c in citations}

        return PackedContext(
            items=packed_items,
            citations=citations,
            citation_map=cit_map,
            formatted_context=formatted_text,
            total_tokens=current_tokens,
            has_sufficient_evidence=True,
            truncated=truncated,
        )
