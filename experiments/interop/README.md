# Interop tests against the official reference SDKs

Offline tests that run Nexura's MCP and A2A implementations against the official Python
SDKs. This is a separate uv project with its own `pyproject.toml` and `uv.lock`; the
reference SDKs are **not** dependencies of the root project or of any Nexura package.

| Environment | Packages | Used for |
|---|---|---|
| this directory | `mcp==2.3.0` (2026-07-28 plus handshake revisions), `a2a-sdk[http-server,signing]==1.2.3`, Nexura (`nexura-agents`, editable) | most tests |
| `legacy/` | `mcp==1.30.0` (handshake revisions only, newest 2025-11-25) | fallback tests; started as a subprocess |

Everything runs locally: stdio servers are subprocesses, HTTP servers are uvicorn on
127.0.0.1 with a free port. No model is called (Nexura agents use a labelled local
`FakeModel`); signing keys are generated per test session.

```bash
cd experiments/interop
uv sync --frozen
(cd legacy && uv sync --frozen)
uv run --frozen pytest -q
```

## What is covered

`tests/test_mcp_interop.py`

- `mcp` 2.3.0 client (`mode="auto"`, `"2026-07-28"`, `"legacy"`) -> Nexura `AgentMCPServer`, over stdio and HTTP (bearer token): tools, an approval-gated tool refused over MCP, a resource and a prompt.
- `mcp` 1.30.0 client (handshake only) -> Nexura server, stdio and HTTP.
- Nexura client -> `mcp` 2.3.0 server, stdio and HTTP: stateless 2026-07-28 session, cache hints, resources, prompts, and a tool whose `region` parameter carries `x-mcp-header` (the reference server rejects the call unless `Mcp-Param-Region` matches; a raw request proves that).
- Nexura client -> `mcp` 1.30.0 server: falls back to the 2025-11-25 handshake over stdio and HTTP; a client pinned to 2026-07-28 refuses it.

`tests/test_a2a_interop.py`

- `a2a-sdk` card resolver and signature verifier -> Nexura's signed card (accepted with the right key, rejected with another).
- `a2a-sdk` 1.0 client -> Nexura `A2AServer` (send, stream, `GetTask`); the SDK's v0.3 compatibility transport -> Nexura's 0.3 methods.
- Nexura `A2AClient` -> `a2a-sdk` server: verifies a card signed by `create_agent_card_signer`, refuses an untrusted key and a tampered card, sends, streams, and speaks the 0.3 wire to the SDK's compatibility endpoint.
