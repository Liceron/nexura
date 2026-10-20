# Historical framework comparisons

The following comparison is retained from the agent SDK README. It is a historical reading of other projects' documentation, not a current capability survey or measured benchmark. No network checks were made for this README restructuring.

## How it compares

The original rows were checked against each project's public docs in October 2026. The
rows from "Handoffs" down to "Hosted tools" were added on 2026-10-09 from our reading of
those docs and were not re-checked against the live pages, so re-verify before relying on
them. "not verified" means we could not confirm it either way. Those projects are far
more mature — weigh that too.

| | Nexura Agents | Pydantic AI | OpenAI Agents SDK | Google ADK |
|---|---|---|---|---|
| Offline test model | `FakeModel`, `"test"` | `TestModel` | not verified | not verified |
| Resumable per-tool approval | Yes, persisted as JSON | Yes (deferred tools) | Yes (human in the loop) | not verified |
| Approval bound to exact arguments | Yes (SHA-256 digest) | No ([issue #6968](https://github.com/pydantic/pydantic-ai/issues/6968)) | not verified | not verified |
| Cost reserved before each model call | Yes (provider models) | not verified | not verified | not verified |
| Vertex AI with your gcloud login | Yes (`nexura-vertex` package) | Yes | not verified | Yes (Google-first) |
| Languages | Python | Python | Python, TypeScript | Python, TypeScript, Go, Java |
| Handoffs between agents | Yes (`Agent(handoffs=...)`) | not verified | Yes | not verified |
| Sequential, parallel and loop agent teams | Yes | not verified | not verified | Yes (workflow agents) |
| Structured output from a JSON-schema dict | Yes, native for four providers | not verified | not verified | not verified |
| MCP client and server | Yes | not verified | MCP client yes; server not verified | not verified |
| A2A (Agent2Agent) server and client | Yes (JSON-RPC; no push, no OAuth) | not verified | not verified | Yes |
| Tracing | Opt-in OpenTelemetry spans; no UI | not verified | Built-in tracing | not verified |
| 100+ providers through LiteLLM | Yes (optional extra; checked offline with LiteLLM's mock mode only) | not verified | not verified | not verified |
| Hosted tools (web search, code execution, file search) | **No** | not verified | **Yes** (OpenAI-hosted) | not verified |
| Maturity | **Alpha, unpublished** | Mature | Mature | Mature |

Where others are clearly ahead, plainly:

- **LangGraph and LangGraph Platform** give you explicit graphs, durable execution and a
  hosted deployment and debugging product. Nexura Agents is an agent loop; durable
  workflows live in the separate `nexura-runtime` platform, whose local service has not
  had an external security review. If you need hosted long-running orchestration, use
  LangGraph.
- **OpenAI's hosted tools** (web search, code interpreter, file search) run on OpenAI's
  side with the OpenAI Agents SDK. Nexura Agents has no equivalent; you bring your own
  tools, MCP servers or OpenAPI specs.
- **Ecosystem, community, integrations and documentation depth** are far larger in all
  four projects above, and none of ours has real-world users yet.
