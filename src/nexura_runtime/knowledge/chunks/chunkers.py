# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Chunking algorithms preserving exact character offsets, overlap, and page citations."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from nexura_runtime.knowledge.chunks.errors import (
    InvalidChunkerConfigError,
    TokenLimitExceededError,
)
from nexura_runtime.knowledge.chunks.models import IndexLineage, TextChunk
from nexura_runtime.knowledge.chunks.tokenizers import (
    TokenEstimator,
    default_token_estimator,
)
from nexura_runtime.knowledge.ingest.models import Document


class BaseChunker(ABC):
    """Abstract base class for offset-aware document and text chunkers."""

    def __init__(
        self,
        chunk_size: int,
        chunk_overlap: int,
        token_estimator: TokenEstimator | None = None,
        max_chunk_tokens: int | None = None,
    ) -> None:
        if chunk_size <= 0:
            raise InvalidChunkerConfigError(f"chunk_size must be positive, got {chunk_size}")
        if chunk_overlap < 0:
            raise InvalidChunkerConfigError(
                f"chunk_overlap cannot be negative, got {chunk_overlap}"
            )
        if chunk_overlap >= chunk_size:
            raise InvalidChunkerConfigError(
                f"chunk_overlap ({chunk_overlap}) must be strictly less than "
                f"chunk_size ({chunk_size})"
            )

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.token_estimator = token_estimator or default_token_estimator
        self.max_chunk_tokens = max_chunk_tokens

    @property
    @abstractmethod
    def chunker_name(self) -> str:
        """Unique identifier for this chunker strategy."""
        ...

    @property
    @abstractmethod
    def config_dict(self) -> dict[str, Any]:
        """Dictionary of configuration parameters for lineage hashing."""
        ...

    @property
    def config_hash(self) -> str:
        """Deterministic hash of chunker parameters."""
        return IndexLineage.compute_config_hash(self.config_dict)

    @abstractmethod
    def chunk_text(self, text: str, document_id: str = "doc") -> list[TextChunk]:
        """Split text into TextChunk records with exact character offsets."""
        ...

    def chunk_document(self, document: Document) -> list[TextChunk]:
        """Split a Document into TextChunk records, aligning spans with DocumentPages."""
        chunks = self.chunk_text(document.text, document_id=document.document_id)
        if not document.pages:
            return chunks

        for chunk in chunks:
            # Map chunk character offsets against document page spans
            matching_pages = [
                page.page_number
                for page in document.pages
                if not (chunk.end_char <= page.span_start or chunk.start_char >= page.span_end)
            ]
            if matching_pages:
                chunk.page_start = min(matching_pages)
                chunk.page_end = max(matching_pages)
            else:
                # Fallback to page containing start_char
                for page in document.pages:
                    if page.span_start <= chunk.start_char <= page.span_end:
                        chunk.page_start = page.page_number
                        chunk.page_end = page.page_number
                        break

            # Carry over document metadata
            if document.metadata:
                chunk.metadata = {**document.metadata, **chunk.metadata}

        return chunks


class FixedWindowChunker(BaseChunker):
    """Chunks text into fixed-size windows with configurable overlap.

    Guarantees that slicing original text with [chunk.start_char:chunk.end_char]
    yields the exact chunk text, and consecutive chunks share exactly chunk_overlap
    characters.
    """

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        *,
        token_estimator: TokenEstimator | None = None,
        max_chunk_tokens: int | None = None,
    ) -> None:
        super().__init__(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            token_estimator=token_estimator,
            max_chunk_tokens=max_chunk_tokens,
        )

    @property
    def chunker_name(self) -> str:
        return "fixed_window"

    @property
    def config_dict(self) -> dict[str, Any]:
        return {
            "name": self.chunker_name,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "max_chunk_tokens": self.max_chunk_tokens,
        }

    def chunk_text(self, text: str, document_id: str = "doc") -> list[TextChunk]:
        if not text:
            return []

        chunks: list[TextChunk] = []
        step = self.chunk_size - self.chunk_overlap
        pos = 0
        text_len = len(text)
        chunk_idx = 0

        while pos < text_len:
            end = min(pos + self.chunk_size, text_len)
            segment = text[pos:end]
            tokens = self.token_estimator.estimate_tokens(segment)

            if self.max_chunk_tokens is not None and tokens > self.max_chunk_tokens:
                raise TokenLimitExceededError(
                    token_count=tokens,
                    max_tokens=self.max_chunk_tokens,
                    details={"chunk_index": chunk_idx, "document_id": document_id},
                )

            chunks.append(
                TextChunk.create(
                    document_id=document_id,
                    chunk_index=chunk_idx,
                    text=segment,
                    start_char=pos,
                    end_char=end,
                    token_count=tokens,
                    chunker_config_hash=self.config_hash,
                )
            )

            if end >= text_len:
                break

            pos += step
            chunk_idx += 1

        return chunks


class RecursiveCharacterChunker(BaseChunker):
    """Hierarchically splits text using prioritized separators.

    Recursively splits on paragraphs, newlines, sentence boundaries, and words
    while preserving exact character offsets and overlap across chunk boundaries.
    """

    DEFAULT_SEPARATORS: list[str] = ["\n\n", "\n", ". ", " ", ""]

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        *,
        separators: list[str] | None = None,
        token_estimator: TokenEstimator | None = None,
        max_chunk_tokens: int | None = None,
    ) -> None:
        super().__init__(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            token_estimator=token_estimator,
            max_chunk_tokens=max_chunk_tokens,
        )
        self.separators = (
            list(separators) if separators is not None else list(self.DEFAULT_SEPARATORS)
        )

    @property
    def chunker_name(self) -> str:
        return "recursive_character"

    @property
    def config_dict(self) -> dict[str, Any]:
        return {
            "name": self.chunker_name,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "separators": self.separators,
            "max_chunk_tokens": self.max_chunk_tokens,
        }

    def _split_spans(
        self,
        text: str,
        start: int,
        end: int,
        sep_index: int,
    ) -> list[tuple[int, int]]:
        """Split [start:end] text span into sub-spans using separators."""
        if end - start <= self.chunk_size:
            return [(start, end)]

        if sep_index >= len(self.separators):
            # No more separators, fallback to character slices
            spans: list[tuple[int, int]] = []
            curr = start
            while curr < end:
                nxt = min(curr + self.chunk_size, end)
                spans.append((curr, nxt))
                curr = nxt
            return spans

        sep = self.separators[sep_index]
        if sep == "":
            spans = []
            curr = start
            while curr < end:
                nxt = min(curr + self.chunk_size, end)
                spans.append((curr, nxt))
                curr = nxt
            return spans

        sub_spans: list[tuple[int, int]] = []
        curr = start
        while curr < end:
            idx = text.find(sep, curr, end)
            if idx == -1:
                sub_spans.append((curr, end))
                break
            boundary = idx + len(sep)
            sub_spans.append((curr, boundary))
            curr = boundary

        # If separator didn't split anything, try next separator
        if len(sub_spans) <= 1:
            return self._split_spans(text, start, end, sep_index + 1)

        result_spans: list[tuple[int, int]] = []
        for s_start, s_end in sub_spans:
            if s_end - s_start > self.chunk_size:
                result_spans.extend(self._split_spans(text, s_start, s_end, sep_index + 1))
            else:
                result_spans.append((s_start, s_end))

        return result_spans

    def chunk_text(self, text: str, document_id: str = "doc") -> list[TextChunk]:
        if not text:
            return []

        spans = self._split_spans(text, 0, len(text), 0)
        chunks: list[TextChunk] = []

        chunk_idx = 0
        i = 0
        while i < len(spans):
            cur_start = spans[i][0]
            cur_end = spans[i][1]
            last_idx = i

            # Accumulate spans up to chunk_size
            j = i + 1
            while j < len(spans):
                candidate_end = spans[j][1]
                if candidate_end - cur_start <= self.chunk_size:
                    cur_end = candidate_end
                    last_idx = j
                    j += 1
                else:
                    break

            segment = text[cur_start:cur_end]
            tokens = self.token_estimator.estimate_tokens(segment)

            if self.max_chunk_tokens is not None and tokens > self.max_chunk_tokens:
                raise TokenLimitExceededError(
                    token_count=tokens,
                    max_tokens=self.max_chunk_tokens,
                    details={"chunk_index": chunk_idx, "document_id": document_id},
                )

            chunks.append(
                TextChunk.create(
                    document_id=document_id,
                    chunk_index=chunk_idx,
                    text=segment,
                    start_char=cur_start,
                    end_char=cur_end,
                    token_count=tokens,
                    chunker_config_hash=self.config_hash,
                )
            )
            chunk_idx += 1

            if last_idx >= len(spans) - 1:
                break

            # Calculate overlap for next chunk
            overlap_accum = 0
            backtrack = last_idx
            while backtrack > i:
                span_len = spans[backtrack][1] - spans[backtrack][0]
                if overlap_accum + span_len <= self.chunk_overlap:
                    overlap_accum += span_len
                    backtrack -= 1
                else:
                    break

            next_i = max(i + 1, backtrack + 1)
            i = next_i

        return chunks


class MarkdownChunker(BaseChunker):
    """Markdown header-aware chunker.

    Detects header hierarchy (# through ######) and code blocks.
    Keeps section breadcrumbs in chunk metadata.
    """

    HEADER_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        *,
        token_estimator: TokenEstimator | None = None,
        max_chunk_tokens: int | None = None,
    ) -> None:
        super().__init__(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            token_estimator=token_estimator,
            max_chunk_tokens=max_chunk_tokens,
        )
        self._inner_chunker = RecursiveCharacterChunker(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            token_estimator=token_estimator,
            max_chunk_tokens=max_chunk_tokens,
        )

    @property
    def chunker_name(self) -> str:
        return "markdown_aware"

    @property
    def config_dict(self) -> dict[str, Any]:
        return {
            "name": self.chunker_name,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "max_chunk_tokens": self.max_chunk_tokens,
        }

    def chunk_text(self, text: str, document_id: str = "doc") -> list[TextChunk]:
        if not text:
            return []

        # Find all header matches with offsets
        sections: list[tuple[int, int, list[str]]] = []
        matches = list(self.HEADER_PATTERN.finditer(text))

        if not matches:
            # Fall back to recursive chunker
            return self._inner_chunker.chunk_text(text, document_id=document_id)

        header_stack: list[tuple[int, str]] = []  # (level, title)

        for idx, match in enumerate(matches):
            level = len(match.group(1))
            title = match.group(2).strip()
            sec_start = match.start()
            sec_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)

            # Update breadcrumbs stack
            while header_stack and header_stack[-1][0] >= level:
                header_stack.pop()
            header_stack.append((level, title))
            breadcrumbs = [t for _, t in header_stack]

            # If there was text before first header
            if idx == 0 and sec_start > 0:
                sections.append((0, sec_start, ["Introduction"]))

            sections.append((sec_start, sec_end, list(breadcrumbs)))

        # Chunk each section
        all_chunks: list[TextChunk] = []
        chunk_idx = 0

        for sec_start, sec_end, breadcrumbs in sections:
            sec_text = text[sec_start:sec_end]
            if len(sec_text) <= self.chunk_size:
                tokens = self.token_estimator.estimate_tokens(sec_text)
                if self.max_chunk_tokens is not None and tokens > self.max_chunk_tokens:
                    raise TokenLimitExceededError(
                        token_count=tokens,
                        max_tokens=self.max_chunk_tokens,
                    )
                all_chunks.append(
                    TextChunk.create(
                        document_id=document_id,
                        chunk_index=chunk_idx,
                        text=sec_text,
                        start_char=sec_start,
                        end_char=sec_end,
                        token_count=tokens,
                        chunker_config_hash=self.config_hash,
                        metadata={"headings": breadcrumbs},
                    )
                )
                chunk_idx += 1
            else:
                # Sub-chunk using recursive chunker
                sub_chunks = self._inner_chunker.chunk_text(
                    sec_text,
                    document_id=document_id,
                )
                for sc in sub_chunks:
                    all_chunks.append(
                        TextChunk.create(
                            document_id=document_id,
                            chunk_index=chunk_idx,
                            text=sc.text,
                            start_char=sec_start + sc.start_char,
                            end_char=sec_start + sc.end_char,
                            token_count=sc.token_count,
                            chunker_config_hash=self.config_hash,
                            metadata={"headings": breadcrumbs},
                        )
                    )
                    chunk_idx += 1

        return all_chunks
