# Nexura Qdrant

A Qdrant vector store with workspace-scoped collections and immutable index generations.

## Install

```bash
pip install nexura-qdrant
```

Alpha (0.4): `pip install nexura-qdrant`. See [package installation](https://github.com/Liceron/nexura/blob/main/docs/packages.md).

## Minimal example

Construction is lazy; this example refuses zero dimensions before any request. A running Qdrant is required for `create_collection`, `upsert` and `search`. Workspace keys scope records and generations; active index generations are immutable. Built-in runtime vector stores work without this add-on.

```python
import asyncio

from nexura_runtime.knowledge.vector.errors import VectorDimensionMismatchError
from nexura_runtime.knowledge.vector.store import VectorStore

from nexura_qdrant import QdrantVectorStoreAdapter


async def main() -> None:
    store = QdrantVectorStoreAdapter(url="http://localhost:6333")
    assert isinstance(store, VectorStore) and store.is_available
    try:
        await store.create_collection("ws-1", "faq", dimensions=0)
    except VectorDimensionMismatchError as exc:
        print("refused before any request:", exc)
    await store.close()


asyncio.run(main())
```

## Status and limits

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Tested against a local
> disposable Qdrant service; never against Qdrant Cloud.

## Documentation and licence

[Main documentation](https://github.com/Liceron/nexura/blob/main/docs/README.md) · [Full guide](https://github.com/Liceron/nexura/blob/main/docs/knowledge.md). Apache-2.0, Copyright (c) 2026 Liceron. See [LICENSE](https://github.com/Liceron/nexura/blob/main/packages/nexura-qdrant/LICENSE).
