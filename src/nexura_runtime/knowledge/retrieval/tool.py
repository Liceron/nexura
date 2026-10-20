# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Retrieval-as-tool helper integrating RagPipeline into AgentKit tools."""

from __future__ import annotations

from nexura_agents.tools import Tool

from nexura_runtime.knowledge.retrieval.models import RagQuery
from nexura_runtime.knowledge.retrieval.pipeline import RagPipeline


def create_retrieval_tool(
    pipeline: RagPipeline,
    collection_id: str = "default",
    workspace_id: str = "default",
    name: str = "search_knowledge_base",
    description: str | None = None,
    *,
    top_k: int = 5,
    max_context_tokens: int = 1500,
) -> Tool:
    """Create a typed agent Tool that queries the RAG pipeline and returns formatted citations."""
    desc = description or (
        "Search the verified organizational knowledge base for relevant facts, policies, "
        "and specifications. Returns source snippets with citation references."
    )

    async def search_knowledge_base(query: str) -> str:
        rag_query = RagQuery(
            query=query,
            collection_id=collection_id,
            workspace_id=workspace_id,
            top_k=top_k,
            max_context_tokens=max_context_tokens,
        )
        packed = await pipeline.retrieve(rag_query)
        if not packed.has_sufficient_evidence:
            return "No relevant sources found in the knowledge base."
        return packed.formatted_context

    return Tool(name=name, fn=search_knowledge_base, description=desc)
