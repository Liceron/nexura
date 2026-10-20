# Troubleshooting

Most Nexura failures raise a named exception with a message that says what to change. This page lists the common ones, how to trigger each offline so you can recognise it, and the usual fix. Agent SDK errors live in `nexura_agents.errors` and every one of them inherits from `NexuraError`, so `except NexuraError` catches them all. `nexura_runtime.errors` gives you the same classes for platform code.

## Provider authentication: `ProviderAuthError`

Raised when a provider rejects your credentials (HTTP 401 for OpenAI; 401 or 403 for Anthropic, Gemini, Sarvam and Vertex AI) or, for Vertex AI, when no usable credentials or project can be found. `RetryModel` and `FallbackModel` never retry or skip over it, on purpose, so a bad key is reported and not hidden.

The Vertex AI message names the project and where your credentials came from. The usual causes:

- **No credentials.** Run `gcloud auth application-default login`, or set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account key file.
- **`google-auth` is missing.** Install the package: `pip install nexura-vertex` (google-auth is its dependency).
- **No project.** Pass `project="..."`, set `GOOGLE_CLOUD_PROJECT`, or run `gcloud config set project`.
- **HTTP 403 for the right login.** The Vertex AI API must be enabled in that project, and the identity needs the "Vertex AI User" role.
- **Billed or authorised in the wrong project.** On a Google Cloud VM, ADC falls back to the VM's service account. Log in yourself and run `gcloud auth application-default set-quota-project YOUR_PROJECT`. `provider.credential_source` shows which identity is in use.

For the other providers, check that the key variable is set in the process that runs your code (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `GOOGLE_API_KEY`, `SARVAM_API_KEY`). See [Models and providers](providers.md).

```python
import asyncio

import httpx

from nexura_agents import Agent, Runner
from nexura_vertex import VertexAIProvider
from nexura_agents.errors import ProviderAuthError


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED"}})


provider = VertexAIProvider(
    "gemini-2.5-flash",
    project="demo-project",
    token_provider=lambda: "fake-token",
    transport=httpx.MockTransport(handler),
)
agent = Agent(name="V", instructions="x", model=provider)
try:
    asyncio.run(Runner().run_async(agent, "hi"))
except ProviderAuthError as exc:
    assert "demo-project" in str(exc)
    assert "Vertex AI User" in str(exc)
else:
    raise AssertionError("expected ProviderAuthError")
```

Import `ProviderAuthError` from `nexura_agents.errors`, which is the class the providers raise. `nexura_agents.providers` also exports a class with the same name; it is a different class and will not catch these errors.

## Other provider failures

| Exception | Meaning | What to do |
|---|---|---|
| `ProviderRateLimitError` | HTTP 429, rate limit or quota. | Wrap the model in `RetryModel`, lower your request rate, or raise the quota. |
| `ProviderRequestError` | The provider could not be reached (timeout, DNS, connection); Anthropic and Sarvam also use it for HTTP 400. | Check network and `timeout=`; `RetryModel` retries it. |
| `ProviderResponseError` | The provider returned something unparseable. | Retry; if it persists, report the provider and model. |
| `ProviderError` | Base class; also raised for other HTTP errors (such as 5xx) and when all models in a `FallbackModel` fail. `RetryModel` does not retry a plain `ProviderError` by default. | Read the message; for `FallbackModel` it lists each failure. |

## Usage limits: `UsageLimitExceededError`

A run stopped because it crossed a value in `UsageLimits`. The exception has `limit` (the name), `value` and `maximum`. Raise the limit, shorten the task, or catch the error and stop gracefully. If you set a token limit and the provider does not report tokens, the run also fails, with `(usage not reported)` in the name, unless you set `allow_unknown_usage=True`. Related run-loop limits are `MaxTurnsExceededError`, `MaxToolCallsExceededError` and `ModelTimeoutError`. See [Guardrails, hooks and limits](guardrails-hooks-limits.md) and [Budgets and usage](budgets-and-usage.md).

```python
from nexura_agents import (
    Agent,
    FakeModel,
    Runner,
    UsageLimitExceededError,
    UsageLimits,
)

agent = Agent(name="A", instructions="x", model=FakeModel(responses=["ok"]))
try:
    Runner(usage_limits=UsageLimits(request_limit=0)).run(agent, "hi")
except UsageLimitExceededError as exc:
    print(exc.limit, exc.value, exc.maximum)
    assert exc.limit == "request_limit"
else:
    raise AssertionError("expected the limit to trip")
```

## Approval errors

**Agent runs: `ApprovalDecisionError`.** `Runner.resume` refuses the call when the decisions do not match the paused run: a pending tool call has no decision, a call is in both `approve` and `reject`, the interruption belongs to a different agent, or the saved transcript no longer matches the pending calls. Nothing runs when this happens. Decide every pending call exactly once, using the same agent that paused. See [Human approval](approvals.md).

```python
from nexura_agents import Agent, ApprovalDecisionError, FakeModel, Runner, ToolCall, tool


@tool(requires_approval=True)
def pay(invoice: str) -> str:
    """Pay an invoice."""
    return f"paid {invoice}"


model = FakeModel(
    responses=[[ToolCall(id="p1", name="pay", arguments={"invoice": "INV-1"})], "Done."]
)
agent = Agent(name="Billing", instructions="Pay.", model=model, tools=[pay])
interruption = Runner().run(agent, "Pay it").interruption

try:
    Runner().resume(agent, interruption)  # no decision for p1
except ApprovalDecisionError:
    pass
else:
    raise AssertionError("expected ApprovalDecisionError")

assert Runner().resume(agent, interruption, approve=["p1"]).final_output == "Done."
```

**Workflow approvals.** `ApprovalAlreadyClaimedError` means someone else already used that `approval_id`; approvals can be used once. `WorkflowTerminalError` means the run already finished (or was rejected). Both derive from `WorkflowError`. Over the REST service a second decision returns HTTP 409 with code `NOT_PENDING_APPROVAL`. See [Workflows](workflows.md).

## Workflow spec changed: `WorkflowSpecMismatchError`

A checkpoint stores a hash of the workflow spec it was created with. Resuming it with an engine built from a different spec raises this error and does not run anything. The exception has `expected_hash` and `actual_hash`. Resume with the spec the run started with. To use a changed spec, start a new run. A different spec version raises `UnsupportedWorkflowVersionError`.

```python
import asyncio

from nexura_runtime.errors import WorkflowSpecMismatchError
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

registry = ModuleRegistry()


@registry.register_handler("step")
def step(state: dict) -> dict:
    return {"done": True}


def make_spec(name: str) -> WorkflowSpec:
    node = WorkflowNode(id="gate", handler="step", requires_approval=True)
    return WorkflowSpec(name=name, entry_node="gate", nodes={"gate": node}, terminal_nodes=["gate"])


async def main() -> None:
    store = MemoryWorkflowStore()
    paused = await WorkflowEngine(make_spec("v1"), registry, store=store).run({})
    approval_id = paused.pending_approval.approval_id

    changed = WorkflowEngine(make_spec("v2"), registry, store=store)
    try:
        await changed.resume(paused.workflow_run_id, approval_id, decision="approve")
    except WorkflowSpecMismatchError as exc:
        assert exc.expected_hash != exc.actual_hash
    else:
        raise AssertionError("expected WorkflowSpecMismatchError")

    original = WorkflowEngine(make_spec("v1"), registry, store=store)
    done = await original.resume(paused.workflow_run_id, approval_id, decision="approve")
    assert done.status == "completed"


asyncio.run(main())
```

## Blocked outbound requests: `DestinationPolicyDeniedError`

OpenAPI and HTTP-based MCP tools refuse to call loopback addresses, private ranges (`10.x`, `192.168.x`, ...), link-local and cloud-metadata addresses, schemes other than the allowed ones, and hosts that do not resolve. The message says which rule fired. The check runs before any request is sent.

Through an agent tool the error surfaces as `ToolExecutionError` with the policy error as its `__cause__`. Calling the policy directly raises `DestinationPolicyDeniedError` (from `nexura_agents.network`), which has `url` and `reason` attributes. To reach a local service on purpose, pass `allow_loopback=True` to `openapi_tools`, or name a trusted host with the lower-level `allowed_hosts` config. See [MCP and OpenAPI tools](mcp-and-openapi.md).

```python
import httpx

from nexura_agents import Agent, FakeModel, ToolCall, openapi_tools
from nexura_agents.errors import ToolExecutionError
from nexura_agents.network import DestinationPolicyDeniedError
from nexura_agents.network.policy import DestinationPolicy

try:
    DestinationPolicy().check_url("http://169.254.169.254/latest/meta-data")
except DestinationPolicyDeniedError as exc:
    assert "metadata" in exc.reason

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Local", "version": "1"},
    "servers": [{"url": "http://127.0.0.1:9000"}],
    "paths": {
        "/ping": {
            "get": {
                "operationId": "ping",
                "summary": "Ping",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}
tools = openapi_tools(SPEC, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
model = FakeModel(responses=[[ToolCall(id="1", name="ping", arguments={})], "ok"])
agent = Agent(name="Local", instructions="Ping.", model=model, tools=tools)
try:
    agent.run("ping")
except ToolExecutionError as exc:
    assert isinstance(exc.__cause__, DestinationPolicyDeniedError)
else:
    raise AssertionError("expected the request to be refused")
```

## MCP timeouts: `McpTransportError`

`McpTransportError` means the connection to an MCP server failed. A common case is a server that does not answer within `request_timeout` (60 seconds by default for a session; the HTTP transport also has its own `timeout`, default 30 seconds). The message reads "Timed out after Ns waiting for reply to '<method>'", and the same error class is raised when you send or receive on a closed transport.

Check that the server process starts and speaks MCP on the transport you chose, then raise the timeout if the server is just slow. Pass `request_timeout=` to `McpClientSession`. The example uses an in-memory transport with no server on the other end:

```python
import asyncio

from nexura_agents.mcp import McpClientSession
from nexura_agents.mcp.errors import McpTransportError
from nexura_agents.mcp.transports import McpInMemoryTransport


async def main() -> None:
    to_server: asyncio.Queue = asyncio.Queue()
    from_server: asyncio.Queue = asyncio.Queue()  # nothing ever arrives here
    session = McpClientSession(McpInMemoryTransport(from_server, to_server), request_timeout=0.05)
    try:
        await session.initialize()
    except McpTransportError as exc:
        assert "Timed out" in str(exc) and "initialize" in str(exc)
    else:
        raise AssertionError("expected a timeout")


asyncio.run(main())
```

Other MCP errors have their own classes in `nexura_agents.mcp.errors`, for example `McpCommandNotAllowedError` (a stdio command that is not on the allowlist) and `McpSecurityError`.

## Attachments: `CapabilityError`

Raised before any request when you attach something the provider cannot take, such as an audio URL sent to OpenAI, which only takes audio inline. The message names the provider. Fix it by changing the attachment (use `Attachment.from_bytes` or `from_path` instead of `from_url`), or by choosing a provider that accepts it. Import it from `nexura_agents.providers.errors`. For bad sizes, schemes or unknown file types, `Attachment` itself raises `SDKValidationError`. See [Images, documents and audio](multimodal.md).

```python
from nexura_agents import Attachment
from nexura_agents.providers.errors import CapabilityError
from nexura_agents.providers.multimodal import openai_content_parts

audio_url = Attachment.from_url("https://example.com/a.wav")
try:
    openai_content_parts("Transcribe this", [audio_url])
except CapabilityError as exc:
    assert "OpenAI cannot accept" in str(exc)
else:
    raise AssertionError("expected CapabilityError")
```

## Quick reference

| Symptom | Likely cause and fix |
|---|---|
| `EventLoopError` from `Runner.run()` or `agent.run()` | You are inside a running event loop. Use `await runner.run_async(...)`. |
| `SDKValidationError: Unknown model` | The model string needs a known `provider:` prefix. See [Models and providers](providers.md). |
| `ModuleNotFoundError: fastapi` on `nexura-runtime serve` | Install the `studio` extra. See [Service and Studio](studio-and-service.md). |
| `OutputValidationError` | The model's answer still failed your `output_type` after retries. See [Structured output](structured-output.md). |
| `ToolExecutionError` | A tool raised an error. The original exception is in `original_error`. See [Tools](tools.md). |

## See also

- [Models and providers](providers.md): credentials, fallbacks and retries.
- [Human approval](approvals.md) and [Workflows](workflows.md): approval and resume behaviour.
- [Guardrails, hooks and limits](guardrails-hooks-limits.md): usage limits and tripwires.
- [MCP and OpenAPI tools](mcp-and-openapi.md): destination policy and MCP servers.
- [Service and Studio](studio-and-service.md): running the REST API and Studio.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ModuleNotFoundError: fastapi` | Install the `studio` extra. |
| `EventLoopError` from `Runner.run()` | You are inside an event loop; use `await runner.run_async(...)`. |
| `UnsupportedWorkflowVersionError` | The spec's `spec_version` is not 1; migrate it with `nexura_runtime.release.migration.WorkflowMigrator`. |
| Validation reports `MODULE_UNAVAILABLE` | Install the extra or package named in the message (`nexura.connectors.*` modules need `pip install nexura-connectors`). |
| `MissingDependencyError: ... is provided by the 'nexura-<name>' package` | Install that package: `nexura-postgres` for `postgresql://` storage, `nexura-studio` for `nexura_runtime.studio.create_app`, `nexura-vertex` for `"vertex:..."` models. |
| Sandbox backend `wasi`, `bubblewrap` or `gvisor_oci` reports "pip install nexura-sandbox" | Install `nexura-sandbox` (`nexura-sandbox[wasi]` for WASI); Monty needs only `nexura-runtime[sandbox]`. |
| HTTP 403 `CROSS_WORKSPACE_ACCESS_DENIED` | The resource belongs to another workspace (`X-Workspace-ID`). |
| HTTP 409 on workflow save | Someone saved a newer revision; reload or save as a copy. |
| Studio shows "Unable to connect" | Start `nexura-runtime serve` or set `VITE_BACKEND_URL`. |
