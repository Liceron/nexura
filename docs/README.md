# Nexura documentation

Guides for building AI agents and durable workflows with Nexura. Every example on these
pages runs offline with `FakeModel` and is executed by the test suite
(`tests/docs/test_docs_examples.py`). Blocks that need a real model or a running service
are marked as such.

Two packages:

- **`nexura-agents`** (`import nexura_agents`): the agent SDK. It needs only `httpx`
  and `pydantic`.
- **`nexura-runtime`** (`import nexura_runtime`): the platform. It adds durable
  workflows, the REST service, Studio, RAG, evaluations and budgets.

The MIT open-source edition is **single user, this machine only; for a team or a
public endpoint use the hosted or enterprise edition**. Local workspaces organise
one owner's projects. They do not add accounts or tenant isolation.

## Start here

- [Packages and installation](packages.md): the nine Python packages, TypeScript client,
  source/wheel installs, supported versions and optional backend requirements.
- [Getting started](getting-started.md): install, first agent, first tool, in five minutes.
- [Quickstart in 10 minutes](quickstart-10-minutes.md): from install to an approval-gated
  workflow with a budget, resumed after a restart, all offline.
- [Project templates](project-templates.md): `nexura-runtime init` (agent, workflow and RAG
  templates with offline tests), `dev` (local service and Studio with the project registered)
  and `deploy --target docker` (writes a single-user Dockerfile).
- [Agents](agents.md): instructions (sync or async), sessions and history trimming,
  handoffs with structured input, agents as tools, streaming, cancelling runs, `clone`,
  diagrams.
- [Testing your agents](testing.md): `FakeModel`, scripted tool calls, assertions,
  `agent.override(model=...)`.

## Build agents

- [Tools](tools.md): function tools, typed dependencies, retries, timeouts, caching,
  hiding tools per run (`enabled`, `prepare_tools`), ending a run with a tool result.
- [MCP and OpenAPI tools](mcp-and-openapi.md): use any MCP server or REST API as tools,
  with timeouts, guardrails and `enabled` for every tool.
- [Connectors](connectors.md): 15 integrations (Slack, email, Drive, Sheets, Postgres,
  MySQL, S3, GitHub, Notion, Jira, Airtable, Discord, Teams, webhooks) as modules; writes
  need approval.
- [Versioned prompts](prompts.md): prompts outside code with versions, labels and promotion.
- [Skills](skills.md): reusable, versioned instruction bundles an agent loads on demand;
  text only, never code.
- [Harness](harness.md): opt-in SKILL.md directories, context compaction, durable task
  lists and scoped filesystems shared with sub-agents.
- [Structured output](structured-output.md): typed results validated with Pydantic,
  output validators, streaming partial output.
- [Images, documents and audio](multimodal.md): attachments for each provider.
- [Teams](teams.md): sequential, parallel and loop agents with approvals that pass through.
- [Long-term memory](memory.md): facts agents remember across sessions and recall
  automatically, one namespace per user.
- [Evaluating agents and teams](evals.md): cases, scorers, LLM judge, reports and CI
  regression checks, all offline.
- [Human approval](approvals.md): pause before risky tools and resume later.
- [Durable runs](durable-runs.md): checkpoint runs and recover them after a crash.
- [Durable execution contract](durable-execution.md): Contract v1 types and target
  recovery rules (being implemented; current behavior is linked from the guide).
- [Reproducing failures](reproducing-failures.md): portable recordings and offline
  replay with model and tool fixtures. Uses best-effort redaction of known secret
  shapes; review a bundle before sharing it.
- [Guardrails, hooks and limits](guardrails-hooks-limits.md): input, output and tool
  guardrails, lifecycle hooks, usage caps.
- [Guardrails package](guardrails-package.md): `nexura-guardrails`, an LLM judge that
  fails closed, fast pre-filters, tiered and parallel checks, a category taxonomy.
- [Guardrail policy](guardrail-policy.md): one declarative policy (Python, JSON or YAML) for
  every run: rules by stage, tool, origin, effect class and arguments, allowlist mode, approval
  gates, journaled model verdicts, audit, and the service's `--guardrail-policy`. Starts with
  why guardrail checks are not a security boundary.
- [Serving agents over MCP](serving-over-mcp.md): make your agents usable from Claude
  Desktop, Cursor and other MCP clients.
- [Agent2Agent (A2A)](a2a.md): serve agents to other frameworks and call remote A2A agents
  as tools.
- [Importing Langflow flows](langflow-import.md): run flows saved by Nexura V2 on the SDK, with an
  import report, tweaks, sessions and the export back to Langflow JSON.

## Operate locally

- [Models and providers](providers.md): OpenAI, Anthropic, Gemini, Vertex AI, Sarvam,
  LiteLLM, fallbacks, model settings (temperature, `tool_choice`, reasoning effort,
  extended thinking).
- [Budgets and usage](budgets-and-usage.md): token and cost accounting, budgets, limits.
- [Workflows](workflows.md): durable graphs with approvals, retries, `interrupt()`,
  history and fork, sub-workflows, parallel steps and Mermaid diagrams.
- [Observability](observability.md): OpenTelemetry traces for agents and workflows, OTLP
  export, opt-in content capture.
- [Starter templates](templates.md): tested, offline-runnable workflows (support desk, RAG,
  research team, approvals, reports, extraction) you can browse and instantiate from Studio.
- [Service and Studio](studio-and-service.md): REST API, TypeScript client and the visual
  Studio.
- [Streaming to frontends](streaming-to-frontends.md): native SSE, AG-UI and AI SDK
  UI message streams, content negotiation and approval boundaries.
- [Run Nexura on a server (single operator)](remote-single-operator.md): one operator token
  on every request, a TLS reverse proxy (Caddy, nginx) and Studio sign-in; multi-user is in
  the enterprise edition.
- [Agents in Studio](agents-in-studio.md): saved workspace agents (declarative specs, no
  code), the `nexura.agents.agent` node, approvals for write tools, chat, evals and red-team.
- [Credentials and local secrets](local-secrets.md): env-var credential references and the
  local envelope-encrypted secrets file (`NEXURA_MASTER_KEY`). Organisations, invites and
  master-key rotation are in the closed edition.
- [Extending the service and Studio](extensions.md): auth providers, service extensions and
  opt-in `nexura.extensions` entry points, credential backends, membership stores, and the
  Studio extension registry.
- [Production gVisor profile](sandbox-production.md): the hardened gvisor_production code sandbox, the operator checklist (root, cgroups, pinned runsc, rootfs) and the host preflight `python -m nexura_sandbox.preflight`.
- [Troubleshooting](troubleshooting.md): common errors and what they mean.

## Reference

- [Historical framework comparisons](comparisons.md): the original SDK comparison and
  its dated, unverified claims and maturity caveats.
- [Verification record and backend catalog](testing.md#verification-record): historical
  checks, their limitations and the public capability inventory.

- [API reference](api-reference.md): every public name with its signature and
  docstring, generated by `scripts/gen_api_reference.py`.
- [Knowledge](knowledge.md): ingest files, hybrid keyword + vector search, and citations.
- [Migrating from OpenAI Agent Builder and Evals](migrating-from-openai.md): concept map from the
  Agents SDK, a worked translation, and importing OpenAI Evals JSONL as Studio datasets (before
  the 2026-10-31 read-only and 2026-11-30 shutdown dates).
- [Migrating from LangGraph](migrating-from-langgraph.md): `StateGraph`, checkpointers,
  `interrupt()`, history and fork mapped to workflows; agents and approvals mapped to the SDK.
- [Migrating from Pydantic AI](migrating-from-pydantic-ai.md): agents, tools, deferred tool
  approvals, testing and durable execution mapped to the SDK.
- [Migrating from CrewAI](migrating-from-crewai.md): crews mapped to agents and teams, flows
  mapped to workflows, human feedback mapped to approvals and `interrupt()`.
