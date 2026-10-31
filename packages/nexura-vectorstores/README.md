# nexura-vectorstores

HTTP adapters for Nexura's `VectorStore` protocol: Chroma, Pinecone, Weaviate,
Milvus/Zilliz, Elasticsearch and OpenSearch. Version 0.6.0, Apache-2.0, Liceron.
This new package is source-only until a separate release; nothing was published.
Dependencies: `nexura-runtime==0.6.0`, `nexura-agents==0.6.0`, `httpx>=0.27,<1`.
No vendor SDK, automatic embedding service, Docker or hosted control plane.

Install from this checkout:

```sh
pip install ./packages/nexura-agents . ./packages/nexura-vectorstores
```

| Database | Import from `nexura_vectorstores` | URL argument/environment (default) | Credential argument/environment |
|---|---|---|---|
| Chroma 1.x REST v2 | `ChromaVectorStore` | `NEXURA_CHROMA_URL` (`http://localhost:8000`) | `api_key` / `NEXURA_CHROMA_API_KEY` (Bearer) |
| Pinecone serverless/Local | `PineconeVectorStore` | `NEXURA_PINECONE_URL` (required, index data-plane URL) | `api_key` / `NEXURA_PINECONE_API_KEY` (`Api-Key`) |
| Weaviate 1.2x | `WeaviateVectorStore` | `NEXURA_WEAVIATE_URL` (`http://localhost:8080`) | `api_key` / `NEXURA_WEAVIATE_API_KEY` (Bearer) |
| Milvus 2.4+/Zilliz | `MilvusVectorStore` | `NEXURA_MILVUS_URL` (`http://localhost:19530`) | `api_key` / `NEXURA_MILVUS_TOKEN` (Bearer) |
| Elasticsearch 8.x | `ElasticsearchVectorStore` | `NEXURA_ELASTICSEARCH_URL` (`http://localhost:9200`) | `api_key` / `NEXURA_ELASTICSEARCH_API_KEY` (encoded ApiKey) |
| OpenSearch 2.x | `OpenSearchVectorStore` | `NEXURA_OPENSEARCH_URL` (`http://localhost:9200`) | `api_key` / `NEXURA_OPENSEARCH_TOKEN` (Bearer) |

All constructors accept `url`, `api_key`, `timeout=30.0`, and either
`transport=httpx.MockTransport(...)` or `client=httpx.AsyncClient(...)`.
An injected client remains owned by the caller. `aclose()` and the async context
manager close clients created by the adapter. Redirects are disabled on owned
clients. For cluster-specific authentication (for example OpenSearch basic auth),
pass a configured client. Credential-bearing URLs are rejected; response and
transport messages are excluded from raised errors. HTTP errors expose only
status and a sanitized database error code.

Configured-server example (no model calls; do not run without a disposable server):

```python
import asyncio
from nexura_vectorstores import ChromaVectorStore
from nexura_runtime.knowledge.vector.models import VectorRecord, VectorSearchQuery


async def main():
    async with ChromaVectorStore() as store:
        await store.create_collection("demo", "documents", dimensions=2)
        await store.upsert(
            "demo",
            "documents",
            [
                VectorRecord(
                    id="one",
                    workspace_id="demo",
                    collection_id="documents",
                    generation_id="",
                    vector=[1.0, 0.0],
                    text="Local vector example",
                    metadata={"category": "guide"},
                )
            ],
        )
        hits = await store.search(
            "demo",
            VectorSearchQuery(
                workspace_id="demo",
                collection_id="documents",
                vector=[1.0, 0.0],
                filter={"category": "guide"},
                top_k=1,
            ),
        )
        print(hits[0].text)


asyncio.run(main())
```

Replace the constructor with any exported class. Chroma also accepts
`tenant="default_tenant", database="default_database"`; these must already exist.
Milvus accepts `db_name="default"`.
Pinecone requires a pre-provisioned index; set `dimensions=...` (default 1536)
and `metric="cosine" | "dot_product" | "euclidean"` to its actual configuration.
Pinecone namespaces cannot change dimension or metric. The data plane checks
dimensions but does not expose the provisioned metric.

All stores support cosine, dot product and Euclidean scoring (higher is better).
Elasticsearch dot product uses `max_inner_product`, requiring 8.11+; older
servers reject the mapping explicitly. OpenSearch uses the 2.x nmslib HNSW engine.
Stored vectors are used to recompute canonical Nexura scores, removing vendor
distance/squared-distance/positive-score differences.

Every collection has a durable manifest in its own hashed workspace/collection
space. Each generation has a separate space (Pinecone namespace).
Activation replaces the manifest once, publishing the pointer and statuses
together. Writes are serialized within an instance. **Coordinate writers across
processes externally**; there is no distributed CAS/transaction across these
HTTP APIs. Server consistency still applies, including Pinecone's eventual
visibility. Acknowledged indexing is not a global visibility barrier.

Like the current Qdrant adapter, active/building generations accept upserts;
retired/dropped generations reject them. Deletion is allowed in all generations
to propagate removal, with durable tombstones or physical deletion. Unknown
collections/generations raise on create-generation/upsert/activation and return
empty results for get/count/search/delete, following Qdrant.

Filters support `$and/$or`, equality, `$eq/$ne/$in/$nin`, numeric
`$gt/$gte/$lt/$lte`, and substring/list `$contains`. Safe positive scalar
conditions use native filters; Elasticsearch/OpenSearch also push down negative
conditions. Containment, JSON/null equality, string ordering and negative
missing-field semantics use over-fetch plus `matches_filter`. Typed, hashed
scalar projections prevent reserved-field/path injection and schema conflicts.
The canonical record JSON preserves arbitrary metadata and original timestamps.

Fully native filters on Pinecone, Milvus and Elasticsearch fetch just the requested
top_k, so those searches can address larger indexes. Chroma and Weaviate also do
this for unfiltered searches. For complete post-filter results, fallback searches
fetch at most **10,000 candidates** including tombstones; larger candidate pools
fail explicitly rather than silently under-fill results. OpenSearch's nmslib
outer filters always use this fallback. Chroma filters also use it because
upserts can retain stale scalar metadata projections. Pinecone exact counts have
the same bound. Requested top_k above the API candidate limit is also rejected.
This is a correctness-first adapter, not a large-index benchmark.
Vendor payload/metadata limits also apply (notably Pinecone's 40 KB metadata
limit and Milvus VARCHAR's 65,535 byte limit), including the manifest history.

Offline verification uses independent dict-backed `httpx.MockTransport` fakes:

```sh
.venv/bin/python -m pytest tests/vectorstores -q -p no:cacheprovider
```

The same conformance suite includes opt-in live cases. Set
`NEXURA_TEST_CHROMA_URL`, `NEXURA_TEST_WEAVIATE_URL`, `NEXURA_TEST_MILVUS_URL`,
`NEXURA_TEST_ELASTICSEARCH_URL`, `NEXURA_TEST_OPENSEARCH_URL`, or
`NEXURA_TEST_PINECONE_URL` plus `NEXURA_TEST_PINECONE_API_KEY`.
Other live credentials use `NEXURA_TEST_<DATABASE>_API_KEY` or `_TOKEN`.
Pinecone live tests use a two-dimensional index; set
`NEXURA_TEST_PINECONE_METRIC` to its metric (default cosine). Other metric cases
skip because a single index cannot support them. Use disposable servers: test
collections/namespaces have unique names and are retained for inspection.
Live servers were not contacted during this offline implementation.

API references for independent live review:
[Chroma](https://docs.trychroma.com/reference/chroma-api),
[Pinecone](https://docs.pinecone.io/reference/api/data-plane),
[Weaviate](https://docs.weaviate.io/weaviate/api/rest),
[Milvus](https://milvus.io/api-reference/restful/v2.4.x/About.md),
[Elasticsearch](https://www.elastic.co/guide/en/elasticsearch/reference/8.11/dense-vector.html),
[OpenSearch](https://docs.opensearch.org/docs/2.19/field-types/supported-field-types/knn-vector/).
These links were not fetched: the assignment prohibits network access.
