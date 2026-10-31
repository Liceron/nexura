# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Optional HTTP vector stores implementing nexura-runtime's VectorStore protocol."""

from .chroma import ChromaVectorStore
from .elastic import ElasticsearchVectorStore, OpenSearchVectorStore
from .milvus import MilvusVectorStore
from .pinecone import PineconeVectorStore
from .weaviate import WeaviateVectorStore

__version__ = "0.7.1"

__all__ = [
    "ChromaVectorStore",
    "PineconeVectorStore",
    "WeaviateVectorStore",
    "MilvusVectorStore",
    "ElasticsearchVectorStore",
    "OpenSearchVectorStore",
]
