# Nexura Studio

The older local workflow Studio and its nexura-studio launcher.

## Install

```bash
pip install nexura-studio
```

Alpha (0.4): `pip install nexura-studio`. See [package installation](https://github.com/Liceron/nexura/blob/main/docs/packages.md).

## Minimal example

This example uses an in-process ASGI transport and a disposable database, without a listener. Runs use the real workflow engine; built-in demo data is synthetic. The React Studio/API uses `nexura-runtime[studio]` instead. Without this package, `nexura_runtime.studio.create_app()` raises `MissingDependencyError` naming `nexura-studio`.

```python
import asyncio
import tempfile
from pathlib import Path

import httpx

from nexura_studio import create_app


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(db_path=Path(tmp) / "studio.sqlite3")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"X-Nexura-Session": app.state.session_token},
        ) as client:
            modules = (await client.get("/api/v1/modules")).json()["modules"]
            print(len(modules), "modules, for example", sorted(m["name"] for m in modules)[0])
            page = await client.get("/")
            assert page.status_code == 200 and "nexura-session" in page.text


asyncio.run(main())
```

## Status and limits

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Single user, local
> use; it has not had an external security review.

Single user, this machine only. The launcher defaults to `127.0.0.1` and refuses non-loopback `--host` without `NEXURA_STUDIO_TOKEN` or the explicit insecure opt-in. State changes require the session token: it stops other web pages, not local programs that can read it. An insecure opt-in does not make a public endpoint safe.

## Documentation and licence

[Main documentation](https://github.com/Liceron/nexura/blob/main/docs/README.md) · [Full guide](https://github.com/Liceron/nexura/blob/main/docs/studio-and-service.md). MIT, Copyright (c) 2026 Liceron. See [LICENSE](https://github.com/Liceron/nexura/blob/main/packages/nexura-studio/LICENSE). Bundled Noto Sans Devanagari is SIL OFL 1.1; see its `OFL.txt`.
