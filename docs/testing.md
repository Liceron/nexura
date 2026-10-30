# Testing your agents

`FakeModel` replaces the real model with a script, so agent logic runs offline, deterministically
and without keys. This page shows how to script replies and tool calls, assert on results, and
test approvals and guardrails. The examples use plain `assert`; in a test suite, put them in
`pytest` functions.

```python
from nexura_agents import Agent, FakeModel

model = FakeModel(responses=["Paris."])
agent = Agent(name="Geo", instructions="Answer in one word.", model=model)

result = agent.run("Capital of France?")

assert result.output == "Paris."
assert len(model.calls) == 1  # the model was called once
```

## Scripting responses

`FakeModel(responses=[...])` returns one item per model call, in order. Each item can be:

- a `str`: a plain assistant reply,
- a `list[ToolCall]`: the model asks to run those tools (several calls in one list means
  several tool calls in one turn),
- a `Message` or a `ModelResponse`, when you need full control (for example to set usage).

When the list runs out, `FakeModel` replies `"FakeModel: No more canned responses."` so a
test with too few responses fails visibly. Use `repeat_last=True` to repeat the last item
forever, or `handler=fn(messages, tools)` to compute each reply from the conversation.
`FakeModel.endless("tool_name")` always calls the same tool, which is useful for testing
`max_turns` and usage limits.

```python
from nexura_agents import Agent, FakeModel, MaxTurnsExceededError, ToolCall, tool


@tool
def ping() -> str:
    """Ping."""
    return "pong"


# Two tool calls in one turn, then the answer.
model = FakeModel(
    responses=[
        [ToolCall(id="a", name="ping", arguments={}), ToolCall(id="b", name="ping", arguments={})],
        "Both pinged.",
    ]
)
agent = Agent(name="P", instructions="", model=model, tools=[ping])
assert agent.run("go").tool_calls_count == 2

# A model that never stops calling tools hits max_turns.
loop_agent = Agent(
    name="Loop", instructions="", model=FakeModel.endless("ping"), tools=[ping], max_turns=3
)
try:
    loop_agent.run("go")
except MaxTurnsExceededError:
    pass
else:
    raise AssertionError("expected MaxTurnsExceededError")
```

## Asserting on results

A `RunResult` has `output`, `final_output`, `status`, `messages`, `events`, `usage`,
`tool_calls_count` and `turns_count`. `result.messages` is the full conversation:
the system prompt, your prompt, assistant messages (with `tool_calls`) and `tool` messages with
tool results.

```python
from nexura_agents import Agent, FakeModel, ToolCall, Usage, tool


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


model = FakeModel(
    responses=[[ToolCall(id="1", name="add", arguments={"a": 2, "b": 3})], "5"],
    default_usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
)
agent = Agent(name="Calc", instructions="Use tools.", model=model, tools=[add])
result = agent.run("2 + 3?")

roles = [m.role for m in result.messages]
assert roles == ["system", "user", "assistant", "tool", "assistant"]

tool_message = result.messages[3]
assert tool_message.content == "5" and tool_message.tool_call_id == "1"
assert result.messages[2].tool_calls[0].arguments == {"a": 2, "b": 3}

assert result.output == "5"
assert result.turns_count == 2
assert result.usage.total_tokens == 24  # 12 per model call, two calls
```

Two ways to see what the agent sent to the model:

- `model.calls` is a list with one dict per call: `messages`, `tools` (the tool schemas) and
  `timeout`. Assert on it to check prompts, history and tool schemas.
- Usage is `None` for each call unless you set `default_usage` or return a `ModelResponse`;
  Nexura does not invent token counts, and `result.usage.is_complete` becomes `False` when a
  call reports none.

## Testing approvals

A tool marked `requires_approval` pauses the run before it executes. `result.status` is
`"paused"` and `result.interruption` lists the pending calls. Resume with `Runner.resume`, passing
`approve=True` or `reject={call_id: reason}`.

```python
from dataclasses import dataclass, field

from nexura_agents import Agent, FakeModel, RunContext, Runner, ToolCall, tool


@dataclass
class Shop:
    refunds: list[str] = field(default_factory=list)


@tool(requires_approval=lambda ctx, args: args["amount"] > 100)
def refund(ctx: RunContext[Shop], order_id: str, amount: float) -> str:
    """Refund an order."""
    ctx.deps.refunds.append(f"{order_id}:{amount}")
    return "refunded"


def build(*replies) -> Agent:
    return Agent(
        name="R", instructions="", model=FakeModel(responses=list(replies)), tools=[refund]
    )


big = ToolCall(id="r1", name="refund", arguments={"order_id": "A1", "amount": 250.0})
runner = Runner()

# Approve: the tool runs only after resume.
shop = Shop()
agent = build([big], "Refunded.")
paused = runner.run(agent, "refund A1", deps=shop)
assert paused.status == "paused" and shop.refunds == []
assert [c.name for c in paused.interruption.pending] == ["refund"]
done = runner.resume(agent, paused.interruption, approve=True, deps=shop)
assert done.output == "Refunded." and shop.refunds == ["A1:250.0"]

# Reject: the tool never runs and the model is told why.
shop = Shop()
agent = build([big], "Escalated.")
paused = runner.run(agent, "refund A1", deps=shop)
done = runner.resume(agent, paused.interruption, reject={"r1": "over limit"}, deps=shop)
assert shop.refunds == []
assert "over limit" in agent.model.calls[1]["messages"][-1].content

# Small amounts skip approval entirely.
shop = Shop()
small = ToolCall(id="r2", name="refund", arguments={"order_id": "A2", "amount": 40.0})
assert runner.run(build([small], "ok"), "refund A2", deps=shop).status == "completed"
assert shop.refunds == ["A2:40.0"]
```

The paused state is a `RunInterruption` that round-trips through `to_json()` and
`RunInterruption.from_json(...)`, so you can also test "approve in another process".

## Testing guardrails

A guardrail is a function `fn(ctx, value)` that returns `GuardrailResult.passed()` or
`GuardrailResult.tripped(info)`. Input guardrails see the user's prompt text before the first
model call; output guardrails see the final output. A tripped guardrail raises
`GuardrailTripwireTriggered`, with `guardrail`, `stage` (`"input"` or `"output"`) and `info`.

```python
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailResult,
    GuardrailTripwireTriggered,
)


def no_secrets(ctx, text: str) -> GuardrailResult:
    return GuardrailResult.tripped("secret") if "password" in text else GuardrailResult.passed()


def short_answers(ctx, output: str) -> GuardrailResult:
    return GuardrailResult.tripped(len(output)) if len(output) > 20 else GuardrailResult.passed()


model = FakeModel(responses=["ok", "this reply is far too long to allow"])
agent = Agent(
    name="G",
    instructions="",
    model=model,
    input_guardrails=[no_secrets],
    output_guardrails=[short_answers],
)

# Input guardrail: tripped before the model is called.
try:
    agent.run("my password is hunter2")
except GuardrailTripwireTriggered as exc:
    assert exc.stage == "input" and exc.guardrail == "no_secrets" and exc.info == "secret"
else:
    raise AssertionError("expected a tripwire")
assert model.calls == []

# A clean prompt passes both checks.
assert agent.run("hello").output == "ok"

# Output guardrail: the reply is checked before the run completes.
try:
    agent.run("hello again")
except GuardrailTripwireTriggered as exc:
    assert exc.stage == "output" and exc.info == len("this reply is far too long to allow")
else:
    raise AssertionError("expected a tripwire")
```

## Streaming from a FakeModel

By default `FakeModel` answers in one piece. Pass `stream=True` to make it stream: text is
split into `text_delta` chunks of `chunk_size` characters, each tool call into
`tool_call_delta` chunks (id and name first, JSON arguments split across the rest), and
`provider_data` into `continuation_delta` chunks, followed by a `finish` delta with usage and
the same finish reason as without streaming. `model.calls` is recorded the same way too. As
with real providers, the runner keeps only Anthropic thinking blocks from a streamed
`provider_data`; other keys reach the events but not the stored message.

```python
from nexura_agents import Agent, FakeModel, RunEventType, ToolCall, tool
from nexura_agents.testing import collect_events


@tool
def ping(host: str) -> str:
    """Ping a host."""
    return "pong"


model = FakeModel(
    responses=[[ToolCall(id="c1", name="ping", arguments={"host": "example.org"})], "All up."],
    stream=True,
    chunk_size=4,
)
agent = Agent(name="Ops", instructions="", model=model, tools=[ping])

events = collect_events(agent, "check", stream_text=True)
args = "".join(e.data["arguments_delta"] for e in events if e.type == RunEventType.TOOL_CALL_DELTA)
assert args == '{"host": "example.org"}'
text = [e.data["text"] for e in events if e.type == RunEventType.TEXT_DELTA]
assert text == ["All ", "up."]
assert len(model.calls) == 2
```

## Blocking real model requests

`block_model_requests()` makes provider-backed models (`OpenAIChatModel`, `LiteLLMModel` and the
classes under `nexura_agents.providers`) raise `ModelRequestsBlockedError` before any request is
sent. `FakeModel` and `StreamingFakeProvider` are unaffected. It patches those classes while
active (there is no single hook in the SDK), so classes you define yourself that override
`generate` are not covered. Guards may overlap and be released in any order: requests stay
blocked until the last one is released.

```python
from nexura_agents import Agent, FakeModel
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.testing import ModelRequestsBlockedError, block_model_requests

with block_model_requests():
    assert (
        Agent(name="A", instructions="", model=FakeModel(responses=["ok"])).run("hi").output == "ok"
    )
    real = OpenAIChatModel(model="gpt-4o-mini", api_key="sk-fake")
    try:
        Agent(name="B", instructions="", model=real).run("hi")
    except ModelRequestsBlockedError as exc:
        assert "blocked" in str(exc)
    else:
        raise AssertionError("a real model request was not blocked")
```

To block for a whole suite, put this in `conftest.py` (the fixture imports `pytest` only when
you call `pytest_fixture()`):

```python notest
# conftest.py
from nexura_agents.testing import pytest_fixture

block_models = pytest_fixture()  # autouse: every test is protected

# Or, instead, block only when NEXURA_BLOCK_MODEL_REQUESTS=1 is set:
# from nexura_agents.testing import block_model_requests_from_env
# block_model_requests_from_env()
```

## Assertion helpers

`tool_calls(result)` returns `(name, arguments)` for each tool the run started.
`assert_tool_called(result, name, **arguments)` passes if some call matches the name and
contains the given arguments, and otherwise raises `AssertionError` listing the actual calls.
Arguments must match exactly, type included: `a=True` does not match a call with `a=1`.
`event_types(result)` lists the event types, and `collect_events(agent, prompt, **kwargs)`
returns the streamed events.

```python
from nexura_agents import Agent, FakeModel, ToolCall, tool
from nexura_agents.testing import assert_tool_called, event_types, tool_calls


@tool
def add(a: int, b: int) -> int:
    """Add."""
    return a + b


model = FakeModel(responses=[[ToolCall(id="1", name="add", arguments={"a": 1, "b": 2})], "3"])
result = Agent(name="Calc", instructions="", model=model, tools=[add]).run("1+2")

assert tool_calls(result) == [("add", {"a": 1, "b": 2})]
assert_tool_called(result, "add", a=1)
assert "tool_started" in event_types(result)
```

## Swapping the model of an existing agent

When your application builds its agents itself, `agent.override(...)` swaps a setting
(usually the model) for the length of a `with` block and restores it afterwards. Code
that already holds the agent, such as handoffs, teams and `as_tool()`, sees the change:

```python
from nexura_agents import Agent, FakeModel, Runner


def build_support_agent() -> Agent:  # your application code
    return Agent(name="Support", instructions="Help.", model=FakeModel(responses=["real"]))


support = build_support_agent()
with support.override(model=FakeModel(responses=["scripted reply"])):
    assert Runner().run(support, "hi").output == "scripted reply"
assert Runner().run(support, "hi").output == "real"  # restored
```

`override` changes the agent for every run in the process while the block is open, so
use it in tests only. To vary an agent in production (another region, a cheaper model),
use `agent.clone(**changes)`, which returns a new, independently validated agent; see
[Agents](agents.md#copies-with-clone).

## Async tests

For async test functions, use `await agent.run_async(...)`; `agent.run` refuses to run inside
an active event loop. Use `pytest-asyncio` or your runner's equivalent.

## Verification record

This record was transcribed on 2026-10-19 from the project's historical verification
records. It is not a new live test, an external audit or a claim of current release
readiness. The open-source edition is **single user, this machine only; for a
team or a public endpoint use the hosted or enterprise edition**.

### Owner-run Vertex check, 2026-10-08

The repository owner ran `examples/live/vertex_check.py` with their own Google
Application Default Credentials against Vertex AI, Gemini 2.5 Flash, global endpoint.
The record identifies the operator only as the owner; no personal identity is inferred.
It recorded a plain reply (`ok`, 1688 ms), one real agent tool call (answer:
`Order ORD-42 is out for delivery.`), and streamed text (`1..5`).

The initial attempt used the VM's service account and produced a misleading API-key
error. The resulting fixes made Vertex authentication errors identify the project,
credential source and Google reason codes, prefer explicit/environment projects,
and send the quota-project header for user credentials. The run also exposed Gemini
thinking-token accounting: output counts now include reasoning tokens, separately
reported as `Usage.reasoning_tokens` (also for OpenAI).

This was one live run on one model. At that date every other provider was verified with mocked
HTTP only. `examples/live/provider_check.py` exists for owner-run OpenAI, Anthropic,
Gemini API and Sarvam checks; OpenAI, Anthropic and Sarvam live checks have not run.
Gemini's later live check is recorded below. The LiteLLM example
uses the real library's mock-response mode offline; it proves no live-provider result.
Public MCP servers, live image calls and a real external MCP client app were not
verified in the recorded SDK checks.

### Latest local gate (2026-10-20)

The local release gate run on 2026-10-20 reported **Ready: True**: all 21 required checks
passed, including the Python suite (5110 passed, with disposable Postgres and Qdrant and the
real gVisor test profile), lint, types, keyless examples, external Python and TypeScript
consumers, a standalone install of each of the eight packages, service load, Studio unit tests
and build, the enforced edition boundary, and the browser suite (89 passed). A live check of
Gemini (`gemini-2.5-flash`, through the founder's API key) passed on 2026-10-20: plain reply,
tool call, streaming and a structured answer. This is one local machine; it is not a
production-readiness or security certification.

### Historical local gate and measurements

At the initial 2026-10-19 transcription, the retained readiness receipt set reported **NOT ready**:
14 required checks passed and one required browser check was skipped (`--skip-browser`).
The receipts identify the local gate process, not a named human operator, and do not
record a run date. Its environment was CPython 3.12.14, Linux 7.0.0-1014-gcp, x86_64,
`nexura-runtime` 0.4.0. These results predate this docs split and are not rerun here.
This paragraph preserves that historical snapshot; subsequent gate reruns can replace
the underlying receipts, so these figures do not describe the latest gate run.

- The Python suite recorded 4597 passed; lint, types, keyless examples, external Python
  and TypeScript consumers, service load, Studio unit/build and route assets passed.
  The standalone SDK consumer installed only the SDK's httpx/pydantic dependency tree.
- The fake-model reference scenario recorded USD 0.00060000 estimated spend and cost
  per success, with zero unknown-usage entries in that fixture. This is not a provider bill.
- Twenty paired context fixtures estimated 17390 baseline tokens versus 560 selected
  tokens. Fake fixtures establish neither answer quality nor billed savings.
- The single-machine load fixture ran 40 runs on four CPUs: 1.78 s ready time,
  82.9 MiB RSS, 177 ms p50 and 392 ms p95. It establishes no production scale.
- The edition-boundary check was report-only and therefore did not establish a passing
  release boundary. Required browser verification remained unverified.

Earlier 2026-10-08 SDK records reported 14/14 required checks and a 735-test profile
including disposable Postgres/Qdrant and gVisor, plus a 698-pass suite with vector
tests deselected because they needed database containers. Later records reported
750, then 764 Python tests and 47 browser tests. Those different historical profiles
are not interchangeable with the latest NOT-ready receipt set or the current suite.
The records attribute SDK implementation checks to Claude Code (Opus) and reviews to
Antigravity CLI; they do not name a human gate operator.

On 2026-10-09, wheel checks recorded 295 tests passing on Python 3.11 and 3.13.
An import of `nexura_agents` plus `Agent, Runner, tool` took about 135 ms on Python
3.12 with a warm cache. That is one local measurement, not a benchmark; no operator
identity is recorded for it. Provider adapters load lazily. macOS and Windows remain
untested.

Live model quality/billing, provider cache savings, competitor comparisons, production
scale and customer demand were explicitly unmeasured. No competitor frameworks were
installed/measured in that environment and no pilot sessions had taken place. Fake-model
measurements cannot establish superiority over other frameworks. No checks in this
record are security certification.

### Publication and naming

The 2026-10-08 registry lookup found `nexura` already taken on PyPI (Liceron's tracing
SDK) and npm (an unrelated React component library). The then-proposed Python names
`nexura-runtime`, `nexura-sdk`, `nexura-ai`, `nexura-agents`, `nexura-core` and npm
names `@liceron/nexura-sdk`, `@nexura/agentkit`, `@nexura/client`, `nexura-runtime`,
`nexura-sdk` appeared free. The npm organisation page was blocked, so ownership of
`@nexura` was not confirmed. The lookup record names no individual operator.
This historical check establishes no current availability or reservation. Final names
and scope ownership still need registration/confirmation before release. Nothing has
been registered or published by this work.

## Public backend catalog

The packaged `src/nexura_runtime/release/capabilities.json` is the public backend
catalog consumed by `nexura_runtime.release.get_matrix()`. B00–B32 identify historical
work packages, not 33 installed runtime modules. `GET /api/v1/modules` describes the
actual installed modules; `/api/v1/capabilities` advertises `local-owner` only in the
open-source service. Optional dependencies and operator configuration still matter.

The catalog's `status` records earlier tests, not a fresh test result or production
certification. `mock_verified` means mock transport, not live-provider validation.
Historical Postgres, Qdrant and gVisor checks require separately configured local
infrastructure to repeat. B30's old verified status does not certify subsequent sandbox
backend revisions; consult the current [security model](../SECURITY_MODEL.md) for the
implemented/unverified profiles and their limits. B24 now covers only local ownership,
scoping and the extension seams: authentication, membership writes and tenant isolation
belong to the closed edition. B31 covers the local API and server approval policy.
The closed edition maintains its additional catalog and evidence separately.

<!-- backend-catalog:start -->
| ID | Local backend area | Historical status | Profile | Test evidence |
|---|---|---|---|---|
| B00 | Typed module contracts | verified | core | [tests/backend/b00](../tests/backend/b00) |
| B01 | Versioned registry and standalone module CLI | verified | core | [tests/backend/b01](../tests/backend/b01) |
| B02 | Artifact and file storage | verified | core | [tests/backend/b02](../tests/backend/b02) |
| B03 | Credentials and outbound access policy | verified | core | [tests/backend/b03](../tests/backend/b03) |
| B04 | Storage, migrations and concurrency | verified | core | [tests/backend/b04](../tests/backend/b04) |
| B05 | Durable execution and worker lifecycle | verified | core | [tests/backend/b05](../tests/backend/b05) |
| B06 | Workflow compiler, ports and control flow | verified | core | [tests/backend/b06](../tests/backend/b06) |
| B07 | Approval policies and effect reconciliation | verified | core | [tests/backend/b07](../tests/backend/b07) |
| B08 | Sessions and long-term memory | verified | core | [tests/backend/b08](../tests/backend/b08) |
| B09 | Model protocol and streaming | verified | core | [tests/backend/b09](../tests/backend/b09) |
| B10 | OpenAI-compatible and local model adapter | mock_verified | core | [tests/backend/b10](../tests/backend/b10) |
| B11 | Native Gemini adapter | mock_verified | core | [tests/backend/b11](../tests/backend/b11) |
| B12 | Native Anthropic adapter | mock_verified | core | [tests/backend/b12](../tests/backend/b12) |
| B13 | Sarvam text and translation | mock_verified | core | [tests/backend/b13](../tests/backend/b13) |
| B14 | Speech input and output | mock_verified | core | [tests/backend/b14](../tests/backend/b14) |
| B15 | Prompts, templates and data transforms | verified | core | [tests/backend/b15](../tests/backend/b15) |
| B16 | Typed tools, HTTP and OpenAPI connectors | verified | core | [tests/backend/b16](../tests/backend/b16) |
| B17 | SQL data connector | verified | core | [tests/backend/b17](../tests/backend/b17) |
| B18 | MCP client and server interoperability | verified | core | [tests/backend/b18](../tests/backend/b18) |
| B19 | Agents, subagents and handoffs | verified | core | [tests/backend/b19](../tests/backend/b19) |
| B20 | Document ingestion and parsers | verified | knowledge | [tests/backend/b20](../tests/backend/b20) |
| B21 | Chunking and embedding generation | verified | knowledge | [tests/backend/b21](../tests/backend/b21) |
| B22 | Vector storage and indexes | verified | knowledge | [tests/backend/b22](../tests/backend/b22) |
| B23 | Retrieval, reranking and citation-backed RAG | verified | knowledge | [tests/backend/b23](../tests/backend/b23) |
| B24 | Local workspaces and ownership | verified | core | [tests/test_p28s7_auth_moved.py](../tests/test_p28s7_auth_moved.py) |
| B25 | Webhooks, schedules and durable triggers | verified | core | [tests/backend/b25](../tests/backend/b25) |
| B26 | Business channels: email and WhatsApp adapters | mock_verified | core | [tests/backend/b26](../tests/backend/b26) |
| B27 | Tracing, events, metrics and audit | verified | core | [tests/backend/b27](../tests/backend/b27) |
| B28 | Usage, cost and budget enforcement | verified | core | [tests/backend/b28](../tests/backend/b28) |
| B29 | Evaluation datasets and regression runner | verified | core | [tests/backend/b29](../tests/backend/b29) |
| B30 | Isolated code execution module | verified | core | [tests/backend/b30](../tests/backend/b30) |
| B31 | Backend API, Python/TypeScript clients and export | verified | studio | [tests/backend/b31](../tests/backend/b31) |
| B32 | Packaging, deployment and backend release gate | verified | core | [tests/backend/b32](../tests/backend/b32) |
<!-- backend-catalog:end -->

B18 is verified in-process only, against this SDK's own MCP client; no external MCP client app (Claude Desktop, Cursor, VS Code) was checked.

## See also

- [Getting started](getting-started.md): the first agent and tool.
- [Agents](agents.md): sessions, sub-agents and streaming.
- [Structured output](structured-output.md): validation retries and `ModelRetry`.
- [Human approval](approvals.md): approvals in depth.
- [Guardrails, hooks and limits](guardrails-hooks-limits.md): the full guardrail and limit APIs.

## Evidence and claims

Historical local measurements used fake models. They do not establish real-model
quality, provider bills, production scale or superiority over other frameworks.
The public [Verification record](testing.md#verification-record) preserves the
scope and limitations of those checks, including the required browser check that
was skipped in the recorded gate run. These are historical results, not current
release approval.

The one speed figure we state: `import nexura_agents` plus `Agent, Runner, tool` took
about 135 ms, measured 2026-10-09 on Python 3.12 with a warm cache. Provider adapters
load lazily. It is a single local measurement, not a benchmark.

## Publication status

Copyright holder and security contact are set (Liceron, info@liceron.in).
Before a public release the final package names must still be registered and the npm
scope's ownership confirmed. The [historical naming check](testing.md#publication-and-naming)
does not establish current availability. Nothing has been published.

## Testing your agents

`FakeModel` replays scripted turns, so the whole loop — tools, retries, approvals,
limits — runs in plain `pytest` without network or keys:

```python
import asyncio

from nexura_agents import Agent, FakeModel


async def test_greets() -> None:
    agent = Agent(name="Greeter", instructions="Be brief.", model=FakeModel(responses=["Hi!"]))
    assert (await agent.run_async("hello")).output == "Hi!"


asyncio.run(test_greets())
```
