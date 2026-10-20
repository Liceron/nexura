# Agent examples

Offline, runnable examples for the `nexura_agents` SDK. They use only `FakeModel`,
scripted `ToolCall`s, `httpx.ASGITransport` and `httpx.MockTransport`: no network, no keys.
Each file's docstring explains the scenario and how to swap in a real model.

Run any example with `uv run python examples/agents/<file>.py`.

| File | What it shows |
| --- | --- |
| `support_triage.py` | Triage handoffs to billing/tech; approval-gated refund, JSON-persisted pause, digest-bound resume. |
| `research_team.py` | `SequentialAgent` plus `ParallelAgent` fan-out with `combine`; optional OpenTelemetry span tree. |
| `a2a_interop.py` | Serve an agent with `A2AServer` and call it via `remote_agent_tool`, in-process. |
| `tools_mcp_openapi.py` | MCP server tools and OpenAPI tools (approval for writes) in one agent. |
| `memory_per_user.py` | `Memory` with a per-user namespace: remember, recall, isolation. |
| `structured_and_guardrails.py` | Pydantic `output_type` with PII, prompt-injection guardrails and tripwires. |
| `prompts_and_evals.py` | `PromptRegistry` versions and labels, same cases scored against both. |
