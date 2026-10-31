# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline wire emulators. No adapter implementation is imported here."""

from .chroma import ChromaAPI
from .elastic import ElasticsearchAPI, OpenSearchAPI
from .milvus import MilvusAPI
from .pinecone import PineconeAPI
from .weaviate import WeaviateAPI

FAKES = {
    "chroma": ChromaAPI,
    "pinecone": PineconeAPI,
    "weaviate": WeaviateAPI,
    "milvus": MilvusAPI,
    "elasticsearch": ElasticsearchAPI,
    "opensearch": OpenSearchAPI,
}
