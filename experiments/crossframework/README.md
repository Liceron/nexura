# Cross-framework governance experiment

An independent, offline project using official `mcp==2.3.0` with the local Nexura
agents, guardrails and runtime packages. Its fake model, fake reviewer and fake
refund tool make no provider/payment calls. See the
[walkthrough](../../docs/crossframework-governance.md) for the policy, review API,
wire contract and limits.

From this directory, with Python 3.12:

```bash
uv sync --frozen
uv run --frozen python -m crossframework.demo
uv run --frozen pytest -q
```

The pytest configuration cuts off ancestor conftest files. The root suite does not
import or require the official MCP SDK. CI tests this experiment in its own environment.
