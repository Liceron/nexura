# Packages and installation

The package inventory, source/wheel installation commands and backend requirements for the MIT edition. All packages are alpha and unpublished; bare package names below describe the intended install interface. Until publication, build local wheels as described in the [front page](../README.md#install). See the [documentation index](README.md) for the subject guides.

## Package overview

A typed Python SDK for building operational agents and approval-gated workflows,
with a local REST service, a TypeScript API client and a browser Studio.

> **Status: alpha, not published.** Nothing here is on PyPI or npm yet. Install from
> a source checkout or a locally built wheel. See [Publication status](testing.md#publication-and-naming).

> **Just want to build agents?** Start with **[Nexura Agents](../packages/nexura-agents/README.md)**
> (`pip install nexura-agents`): the standalone agent SDK, two dependencies, offline-testable.
> This repository's `nexura-runtime` is the full platform built on top of it.

> **Guides:** [docs/](README.md) has 35 guides (agents, teams, tools, MCP and OpenAPI,
> A2A, memory, prompts, structured output, approvals, workflows, providers, tracing and
> more) plus an index and a generated [API reference](api-reference.md). Every
> offline example on those pages is run by the test suite; blocks needing a real model or
> running service are marked and skipped. Publishing (the embeddable chat widget)
> and authenticated multi-user self-hosting are part of the closed edition, not of this
> open-source edition; a single-user local Docker setup is in `deployment/`.

## What is in this repository

| Path | Package | What it is |
|---|---|---|
| `packages/nexura-agents` | `nexura-agents` (Python, import `nexura_agents`) | The standalone agent SDK: agents, teams and handoffs, tools, MCP and OpenAPI, A2A, structured output, memory, prompt registry, tracing, runner, approvals, usage limits, budgets, providers, sessions. Core dependencies are httpx and pydantic; optional extras `litellm` and `otel` |
| `src/nexura_runtime` | `nexura-runtime` (Python, import `nexura_runtime`) | The platform on top: durable workflows, RAG, MCP/OpenAPI/SQL connectors, evaluations, observability. Re-exports `nexura_agents` under its old import paths |
| `packages/nexura-connectors` | `nexura-connectors` (Python, import `nexura_connectors`) | The 15 SaaS connectors (Slack, GitHub, Google Drive and Sheets, S3, Postgres, MySQL, email and more) as `nexura.connectors.*` workflow modules. Depends on `nexura-runtime`, which registers them when the package is installed and otherwise reports them unavailable ([docs/connectors.md](connectors.md)) |
| `packages/nexura-postgres` | `nexura-postgres` (Python, import `nexura_postgres`) | PostgreSQL run and session storage (`PostgresRepository`) and the pgvector store. `get_storage_repository("postgresql://...")` uses it when installed and otherwise names `pip install nexura-postgres` |
| `packages/nexura-qdrant` | `nexura-qdrant` (Python, import `nexura_qdrant`) | The Qdrant vector store (`QdrantVectorStoreAdapter`) for the runtime's `VectorStore` protocol |
| `packages/nexura-sandbox` | `nexura-sandbox` (Python, import `nexura_sandbox`) | The WASI, bubblewrap and gVisor code-execution backends. The runtime keeps the backend selection, `SandboxConfig` and the default Monty backend, and reports these backends unavailable ("pip install nexura-sandbox") without it |
| `packages/nexura-vertex` | `nexura-vertex` (Python, import `nexura_vertex`) | The Google Vertex AI provider (`VertexAIProvider`, `"vertex:..."` model strings). Depends on `nexura-agents` only |
| `src/nexura_runtime/service` | same package, `[studio]` extra | Local REST API (`/api/v1/...`) that Studio web and the TS client call, plus a trigger worker that runs scheduled and webhook workflows |
| `clients/typescript` | `@liceron/nexura-sdk` | TypeScript client for the REST API (no runtime of its own) |
| `studio-web` | `nexura-studio-web` (private, not a package) | React Studio UI served by Vite; talks to the REST API |
| `packages/nexura-studio` | `nexura-studio` (Python, import `nexura_studio`) | The older built-in Studio: the `nexura-studio` command and its static web page. Its store, built-in modules and traces stay in `nexura_runtime.studio`, shared with the REST service |

The Python package is the only runtime. Studio web and the TS client are optional
front ends to the same API, so a workflow behaves the same whichever you use
(`examples/oss/o07.py` checks this).

## Supported versions

Only what is tested in this repository. The first Python row is the platform package
`nexura-runtime`; the SDK `nexura-agents` supports a wider range:

| Component | Supported | Tested on |
|---|---|---|
| Python | 3.12 only (`>=3.12,<3.13`) | CPython 3.12.14 |
| Python for `nexura-agents` alone (the SDK) | 3.11, 3.12 and 3.13 (`>=3.11,<3.14`) | CI matrix defined for 3.11, 3.12, 3.13; no remote CI has run yet. Locally, 295 SDK tests passed on 3.11 and 3.13 from a built wheel on 2026-10-09, excluding 4 platform-importing test files |
| Node.js (TS client, Studio web) | 24.x | Node 24.20.0, npm 11.19.0 |
| OS | Linux | Linux x86_64 (kernel 7.0) |
| Storage | SQLite (default); Postgres (`nexura-postgres`) and Qdrant (`nexura-qdrant`) need running services | SQLite |

macOS and Windows are untested.

Live validation: Google Vertex AI (Gemini 2.5 Flash) passed a live check run by the
repository owner on 2026-10-08: a plain reply, a tool call and streaming (details in
[Verification record](testing.md#verification-record)). At that time every other provider was verified
with mocked HTTP only. Gemini API also passed a live check on 2026-10-20; see the [latest local gate](testing.md#latest-local-gate-2026-10-20). `examples/live/provider_check.py` is a script for the owner to run
against OpenAI, Anthropic, Gemini or Sarvam with their own key; the OpenAI, Anthropic and
Sarvam live checks have not run. Gemini's 2026-10-20 result is recorded separately above.
`examples/live/litellm_offline_check.py` is not a live check: it runs the real LiteLLM
library in its mock-response mode, with no network.

## Install from source

```bash
git clone https://github.com/Liceron/nexura && cd nexura
uv sync                      # core SDK plus dev tools
uv sync --extra studio       # add the REST service (FastAPI, uvicorn)
```

Or build and install a wheel into any Python 3.12 environment:

```bash
uv build
pip install "dist/nexura_runtime-0.4.0-py3-none-any.whl[studio]"
```

The repository builds one package per feature group (`uv build --all-packages` writes every
wheel to `dist/`; nothing is on PyPI yet, so install those wheels with
`pip install --find-links dist <name>`). Each package depends on `nexura-runtime` (or, for
Vertex AI, on `nexura-agents` only), and the runtime works without any of them:

```bash
pip install nexura-runtime               # platform: workflows, agents, RAG, CLI, SQLite storage
pip install "nexura-runtime[studio]"     # + the REST service (nexura-runtime serve)
pip install nexura-studio                # the nexura-studio launcher and its web page
pip install nexura-connectors            # the 15 SaaS connectors
pip install nexura-postgres              # PostgreSQL storage and the pgvector store
pip install nexura-qdrant                # the Qdrant vector store
pip install nexura-sandbox               # bubblewrap and gVisor sandbox backends
pip install "nexura-sandbox[wasi]"       # + the WASI (WebAssembly) sandbox backend
pip install nexura-vertex                # Google Vertex AI models ("vertex:...")
pip install nexura-agents                # the agent SDK alone (Python 3.11 to 3.13)
```

Core dependencies are only `httpx`, `pydantic`, `packaging` and `cryptography`. Optional extras:

| Extra | Adds | Needed for |
|---|---|---|
| `studio` | fastapi, uvicorn | The REST service: `nexura-runtime serve`, `worker`, `create_api_app` (the `nexura-studio` launcher is its own package) |
| `knowledge` | pypdf | PDF ingestion |
| `secrets` | (nothing; `cryptography` is core since 0.4.0) | Kept so old install commands work |
| `sandbox` | pydantic-monty | The Monty code-execution sandbox backend (`NEXURA_SANDBOX_BACKENDS=monty`) |

The `postgres`, `vector`, `vertex` and `sandbox-wasi` extras of earlier versions became the
`nexura-postgres`, `nexura-qdrant`, `nexura-vertex` and `nexura-sandbox[wasi]` packages above.
`nexura-agents` has two more optional extras: `nexura-agents[litellm]` (LiteLLM, for
`LiteLLMModel` and `"litellm:..."` model strings) and `nexura-agents[otel]`
(`opentelemetry-api`, for `nexura_agents.tracing.instrument()`).

A module that declares an extra which is not installed is listed but cannot run;
validation reports `MODULE_UNAVAILABLE` with the missing package.

## Development

See [CONTRIBUTING.md](../CONTRIBUTING.md) for setup and test profiles. The short version:

```bash
uv run pytest
uv run ruff check src tests examples
uv run mypy src packages/*/src
```

## License

MIT, copyright (c) 2026 Liceron. See [LICENSE](../LICENSE). Third-party notices are in
[THIRD_PARTY.md](../THIRD_PARTY.md).
