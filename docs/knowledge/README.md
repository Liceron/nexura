# Knowledge embeddings and retrieval evidence

The service uses a real embedding provider when the operator configures one.
Without configuration it uses `DeterministicEmbeddingProvider`, an eight-dimensional
SHA-256 test fixture. That fixture has **no semantic understanding**. Its random
vector similarities can harm the keyword ranking; it is not evidence of semantic RAG.
Studio displays this limitation in English and Hindi when `is_test_fixture` is true.

## Configuration

Set the provider and model explicitly before starting the service, for example:

```bash
export NEXURA_EMBEDDINGS=gemini:text-embedding-004
# Supply GEMINI_API_KEY or GOOGLE_API_KEY through your normal server environment.
```

Supported specs are `openai:MODEL`, `gemini:MODEL`, and `vertex:MODEL`.
OpenAI uses `OPENAI_API_KEY`; Gemini uses `GEMINI_API_KEY` or `GOOGLE_API_KEY`.
Vertex requires the optional `nexura-vertex` package and its existing Application
Default Credentials setup. It uses `GOOGLE_CLOUD_PROJECT` / `CLOUDSDK_CORE_PROJECT`
and `GOOGLE_CLOUD_LOCATION` (default `global`). A chat model configuration or an
API key alone does not select an embedding model.

Programmatic service construction accepts `create_api_app(embeddings="provider:model",
embeddings_dimensions=...)`. Explicit arguments override `NEXURA_EMBEDDINGS` and
`NEXURA_EMBEDDINGS_DIMENSIONS`. An absent/empty spec, or explicit `fixture`, selects
the local fixture. Invalid configured specs, missing keys, and unknown dimensions
fail explicitly rather than silently selecting the fixture. Construction makes no
embedding calls; actual ingestion, vector retrieval and reindexing call the provider.
Vertex credential discovery may access its normal ADC credential sources.

Known native dimensions are 1536 for OpenAI `text-embedding-3-small` and
`text-embedding-ada-002`, 3072 for `text-embedding-3-large`; 768 for Gemini
`text-embedding-004`; 3072 for Gemini/Vertex `gemini-embedding-001`; and 768 for
Vertex `text-embedding-004`, `text-embedding-005`, and `text-multilingual-embedding-002`.
Other model names require `NEXURA_EMBEDDINGS_DIMENSIONS`. This setting declares
the expected native output size: it does not request dimension reduction.
Every returned vector is validated against that size before storage.

## Collection lineage and explicit reindex

`GET /api/v1/knowledge/status?collection_id=default` returns:

- `embedder`: configured `provider`, `model`, `revision`, `dimension`, `is_test_fixture`.
- `indexed_embedder`: identity persisted with the collection, or null before indexing.
- `is_test_fixture`: whether the configured provider is the deterministic fixture.
- `requires_reindex`: true when the persisted identity differs or a populated legacy
  collection has no trustworthy lineage.

`GET /knowledge/collections` retains the existing string collection list and adds
the same default status and a `collection_status` map. `GET /knowledge/limits`
also includes the default status for Studio's notice. All routes above have the
`/api/v1` prefix. No credentials appear in these responses.

Collections are bound to provider, model, revision and dimension on their first
successful ingestion. Changing any of those refuses ingestion (including dedup),
retrieval and answer requests with HTTP 409 / `EMBEDDING_LINEAGE_MISMATCH`.
Older populated databases without lineage are deliberately treated as unknown;
the service does not assume they used the fixture. List, inspect and delete remain
available so users can review stored text while resolving a mismatch.

After configuring the desired provider, explicitly call:

```bash
curl -X POST http://127.0.0.1:8765/api/v1/knowledge/collections/default/reindex \
  -H "X-Nexura-Session: $NEXURA_SESSION" \
  -H 'Content-Type: application/json' -d '{}'
```

Use your local service's session token and URL-encode the collection id. The route
requires edit permission and scopes the operation to the caller's workspace.
Reindex embeds the stored chunks, then atomically replaces their vectors and lineage.
It preserves document ids, chunk ids, text, FTS entries, spans, metadata and citations.
Provider/dimension/database failure preserves the previous collection. A concurrent
chunk or lineage change refuses the replacement and requires a retry. On Postgres (a
service with `database_url`) the check and the replacement run in one transaction under
the collection's lock, and readers on every instance see the old vectors and lineage or
the new ones, never a mix. Reindex takes
an in-memory snapshot of the collection; large collections may need substantial RAM.
It rebuilds vectors for the existing chunks, rather than changing chunking policy.

## Reproducible offline evaluation

Dataset: [`synthetic-en-hi-v1`](../../examples/knowledge/retrieval_v1.json), 20 short
original fictional municipal-service documents (10 English, 10 Hindi), 30 English
queries and 20 Hindi queries. Each query has one gold document id. Questions include
literal terms and paraphrases; all 20 documents compete for every query. This tiny
authored dataset is a regression/diagnostic sample, not a representative benchmark.

```bash
UV_OFFLINE=1 UV_CACHE_DIR=/tmp/p35a3-uv uv run --frozen \
  python examples/knowledge/evaluate_retrieval.py > /tmp/p35a3-retrieval-v1.json
```

The default command ignores ambient embedding configuration and credentials. It
compares SQLite FTS5 BM25-only (vector weight 0, keyword weight 1) with the existing
fixture hybrid weights (0.7 vector, 0.3 keyword), reciprocal-rank fusion constant 60,
and the existing cosine floor 0.05. The production chunker yields one chunk per
document here. Search retains its existing term extraction/query normalization.
The script maps actual citation document ids back to the stable dataset ids and
deduplicates documents in rank order. Recall@k is the fraction of gold documents
among the first k results; MRR@5 is the mean reciprocal rank of the first gold
document, with zero for a miss in the top five. These measure citation-document
retrieval, not answer correctness, faithfulness or citation span accuracy.

Measured locally on 2026-10-20 with Python 3.12.14, keyless, default top-k 5:

| Mode | Queries | Recall@1 | Recall@3 | Recall@5 | MRR@5 |
| --- | --- | --- | --- | --- | --- |
| BM25-only, all | 50 | 0.7400 | 0.8200 | 0.8800 | 0.7940 |
| BM25-only, English | 30 | 0.7333 | 0.8667 | 0.8667 | 0.8000 |
| BM25-only, Hindi | 20 | 0.7500 | 0.7500 | 0.9000 | 0.7850 |
| Fixture hybrid, all | 50 | 0.1400 | 0.3400 | 0.4200 | 0.2513 |
| Fixture hybrid, English | 30 | 0.1333 | 0.3667 | 0.3667 | 0.2444 |
| Fixture hybrid, Hindi | 20 | 0.1500 | 0.3000 | 0.5000 | 0.2617 |

The Postgres index (a service with `database_url`) ranks keywords with `ts_rank_cd` per
query term, weighted by a bm25-style inverse document frequency, instead of FTS5 `bm25`.
Measured on 2026-10-27 on the same dataset and settings (all 50 queries, PostgreSQL 16):

| Keyword ranker | Recall@1 | Recall@5 | MRR@5 | Same top hit as `bm25` |
| --- | --- | --- | --- | --- |
| SQLite FTS5 `bm25` | 0.7400 | 0.8800 | 0.7940 | — |
| Postgres, IDF-weighted `ts_rank_cd` (shipped) | 0.7200 | 0.8600 | 0.7780 | 86 % |
| Postgres, plain `ts_rank_cd` of the OR query (not used) | 0.6000 | 0.8200 | 0.6983 | 76 % |

The fixture hybrid mode gives MRR@5 0.2263 on Postgres (0.2513 with `bm25`).

[Raw metrics and per-query rankings](retrieval_v1_results.json) include the dataset
SHA-256 `9121abb3cc741fdcfa17c724c18ed4a30f617feeb44735de78fc82e415cef84c`.
The fixture hybrid performs substantially worse than BM25 on this dataset. Hash
similarity is noise, not a semantic signal. Existing default weights are preserved
so this result describes the current fixture behavior; no tuning was done against
the gold answers. No real-model retrieval quality numbers have been measured.

For an operator-run real-provider comparison, configure `NEXURA_EMBEDDINGS` and the
appropriate API key, then run the same script with `--real`. That explicit flag
adds `real_hybrid` and can incur provider charges. Vertex additionally requires an
explicit `GOOGLE_APPLICATION_CREDENTIALS` path for this script, so ambient machine
credentials alone do not opt in. `--real` refuses execution whenever `CI` is set.
Normal tests use mock HTTP transports for OpenAI, Gemini and Vertex, and never
perform paid provider calls. No live-provider or production-readiness claim is made.
