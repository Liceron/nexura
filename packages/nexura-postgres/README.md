# Nexura Postgres

PostgreSQL run/session storage and a pgvector vector store for nexura-runtime.

## Install

```bash
pip install nexura-postgres
```

Alpha and unpublished: install from the [source checkout](https://github.com/Liceron/nexura) or local wheels until publication. See [package installation](../../docs/packages.md).

## Minimal example

This example constructs a repository without connecting. Call `await repository.init_db()` against a running PostgreSQL server to create the run/session/event tables. pgvector needs the `vector` extension. SQLite needs no add-on; without this package the runtime factory raises `MissingDependencyError` naming `nexura-postgres`.

```python
from nexura_runtime.storage import StorageRepository, get_storage_repository

from nexura_postgres import PostgresRepository

repository = get_storage_repository("postgresql://nexura:secret@localhost:5432/nexura")
assert isinstance(repository, PostgresRepository)
assert isinstance(repository, StorageRepository)  # same protocol as SQLiteRepository
print(type(repository).__name__, repository.dsn)
```

## Status and limits

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Tested against a local
> disposable PostgreSQL service with pgvector; never against a managed cloud database.

## Documentation and licence

[Main documentation](../../docs/README.md) · [Full guide](../../docs/knowledge.md). MIT, Copyright (c) 2026 Liceron. See [LICENSE](LICENSE).
