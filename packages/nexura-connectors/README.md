# Nexura Connectors

Fifteen approval-gated SaaS, database and mail connectors for Nexura workflows.

## Install

```bash
pip install nexura-connectors
```

Alpha (0.4): `pip install nexura-connectors`. See [package installation](https://github.com/Liceron/nexura/blob/main/docs/packages.md).

## Minimal example

The runtime registers all 15 modules when this package is installed; otherwise they report `MODULE_UNAVAILABLE`. Write operations need human approval; an `approved: true` input grants nothing. Credentials are references, not literal tokens; destination validation blocks private/metadata addresses and pins DNS.

```python
from nexura_runtime.modules import default_registry

connectors = [
    m for m in default_registry.list_modules() if m["module_id"].startswith("nexura.connectors.")
]
assert len(connectors) == 15 and all(m["is_available"] for m in connectors)
print(sorted(m["module_id"] for m in connectors))
```

## Status and limits

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Every connector is
> tested offline only, against mock HTTP transports and in-memory database and mail fakes; none
> has been checked against the live service. The full guide is `docs/connectors.md` in the
> repository.

## Documentation and licence

[Main documentation](https://github.com/Liceron/nexura/blob/main/docs/README.md) · [Full guide](https://github.com/Liceron/nexura/blob/main/docs/connectors.md). MIT, Copyright (c) 2026 Liceron. See [LICENSE](https://github.com/Liceron/nexura/blob/main/packages/nexura-connectors/LICENSE).
