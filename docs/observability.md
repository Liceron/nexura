# Observability with OpenTelemetry

Nexura can emit [OpenTelemetry](https://opentelemetry.io/) traces for agents, teams and
workflows. Tracing is opt-in: until you call `instrument()`, nothing is imported and nothing
is recorded (the cost is one `None` check). Span and attribute names follow the
[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).

Install the API package with the extra:

```bash
pip install "nexura-agents[otel]"
```

`instrument()` imports `opentelemetry-api` lazily and raises an `ImportError` that tells you
to install the extra if it is missing. It never sets the global `TracerProvider`: it uses the
provider you pass, or `opentelemetry.trace.get_tracer_provider()`.

The in-house tracer and OTLP JSON export in `nexura_runtime.observability` keep working
unchanged; the two can run side by side.

## Try it in memory

This example runs offline with a fake model and keeps the spans in memory. It needs
`opentelemetry-sdk`, which is a dev dependency of this repository.

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.tracing import instrument, uninstrument
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
instrument(tracer_provider=provider)


@tool
def lookup(order_id: str) -> str:
    return f"Order {order_id} shipped."


model = FakeModel(
    responses=[
        [ToolCall(id="c1", name="lookup", arguments={"order_id": "A1"})],
        "Your order shipped.",
    ]
)
agent = Agent(name="Support", instructions="Help.", model=model, tools=[lookup])
Runner().run(agent, "Where is A1?")
uninstrument()

names = sorted(span.name for span in exporter.get_finished_spans())
assert names == [
    "chat FakeModel",
    "chat FakeModel",
    "execute_tool lookup",
    "invoke_agent Support",
]
run = next(s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent"))
assert run.attributes["nexura.run.status"] == "completed"
```

`uninstrument()` stops new spans; spans already started still finish.

## Export to an OTLP collector

Configure the SDK the way you would for any Python service, then call `instrument()`.

```python notest
from nexura_agents.tracing import instrument
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "support-bot"}))
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces"))
)
instrument(tracer_provider=provider)
```

This needs `pip install opentelemetry-sdk opentelemetry-exporter-otlp`. Spans nest under
whatever span is current when you start a run, so a web framework's request span becomes the
parent of the agent run automatically. The context survives `asyncio.TaskGroup`,
`asyncio.gather` and `asyncio.to_thread`, so parallel tool calls, parallel team members and
parallel workflow branches keep the right parents.

## What is recorded

| Span | Created for | Key attributes |
| --- | --- | --- |
| `invoke_agent <agent>` | Each `Runner` run or resume (including streaming) | `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name`, `nexura.run_id`, `nexura.run.status` (`completed`, `paused`, `failed`, `cancelled`), `nexura.run.resumed`, `gen_ai.conversation.id` (the `session_id`, when the run has one), `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `nexura.usage.reasoning_tokens` |
| `chat <model>` | Each model call | `gen_ai.operation.name=chat`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons` (when the provider reports one) |
| `execute_tool <tool>` | Each tool call | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `nexura.tool.retry_reason` (model retry, rejected), `nexura.tool.pending_approval=true` for a call waiting for approval |
| `invoke_team <team>` | Each team run or resume | `gen_ai.operation.name=invoke_team`, `nexura.team.name`, `nexura.team.kind`, `nexura.team.status`, `nexura.run.resumed`, usage totals |
| `workflow <spec name>` | Each `run`, `resume`, `resume_interrupt` and `fork` | `gen_ai.operation.name=workflow`, `nexura.workflow.name`, `nexura.workflow.run_id`, `nexura.workflow.operation`, `nexura.workflow.status`, `nexura.workflow.step_count` |
| `workflow.node <node id>` | Each node step | `nexura.workflow.node.id`, `.step`, `.kind`, `.attempts`, `nexura.workflow.pause_reason` (`approval` or `interrupt`); one `nexura.workflow.retry` event per retried attempt |
| `workflow.branch <name>` / `workflow.map_item <index>` | Each parallel branch or map item | `nexura.workflow.branch`, `nexura.workflow.map_index` |

Nesting: `invoke_team` > `invoke_agent` > `chat` / `execute_tool`; `workflow` >
`workflow.node` > branch or item, a sub-workflow's own `workflow` span, or an agent run
started by the node's handler. An agent used as a tool nests under the `execute_tool` span.

Errors set the span status to `ERROR`, add an `exception` event and set `error.type`. Exception text can echo credentials or user data, so with `capture_content` off (the default) only the exception type is recorded (status description, `exception.type`, `error.type`); with `capture_content=True` a redacted, truncated message is added as `exception.message` and the status description. Run metadata attributes (`nexura.run.metadata.*`) drop secret-named keys and redact string values, like `span()` attributes. `RUN_FAILED` run events are not spans and still carry the exception text. Two things
are deliberately not errors: an approval pause (the run, tool and node spans end normally
with `paused` status or `pending_approval`), and a `ModelRetry`, which is a request to the
model rather than a failure. A resumed run is a new span with the same `nexura.run_id`.

## Your own spans and grouped traces

`tracing.span(name, **attributes)` wraps your own code in a span nested under whatever is
current: inside a tool it nests under that tool call, in your application under your own
spans. `tracing.trace(name, group_id=..., metadata=...)` puts every run started inside the
block under one parent span, so a batch job or a multi-step request shows as one trace;
`group_id` (`nexura.trace.group_id`) links separate traces, such as the turns of one
conversation. Both do nothing while tracing is off.

```python
import asyncio

from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool, tracing
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

memory = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(memory))
tracing.instrument(tracer_provider=provider)


@tool
def report(region: str) -> str:
    """Build a sales report."""
    with tracing.span("query warehouse", region=region) as step:
        step.set("rows", 120)
    return "120 rows"


model = FakeModel(
    responses=[[ToolCall(id="c1", name="report", arguments={"region": "west"})], "Done."]
)
agent = Agent(name="Reports", instructions="Use tools.", model=model, tools=[report])

with tracing.trace("nightly reports", group_id="2026-10-11", metadata={"team": "sales"}):
    asyncio.run(Runner().run_async(agent, "west region"))
tracing.uninstrument()

names = [s.name for s in memory.get_finished_spans()]
assert "query warehouse" in names and "trace nightly reports" in names
```

Attribute values must be strings, numbers or booleans. Other values and secret-named keys
(`api_key`, `password`, ...) are dropped, and strings go through the same credential
redaction as captured content. An exception inside the block marks the span as failed and
is re-raised. Custom spans record no duration metric.

## Metrics

`instrument()` also records two histograms from the GenAI metric conventions. They use the
meter from `meter_provider=` (default: the global `MeterProvider`, which does nothing
until you configure the OpenTelemetry SDK), and the global provider is never set.

| Metric | Unit | Recorded for | Attributes |
| --- | --- | --- | --- |
| `gen_ai.client.token.usage` | `{token}` | Each model call that reports usage, once per token type | `gen_ai.token.type` (`input` or `output`), `gen_ai.operation.name=chat`, `gen_ai.request.model` |
| `gen_ai.client.operation.duration` | `s` | Each agent run, model call, tool call, team run and workflow run | `gen_ai.operation.name`, plus `gen_ai.request.model`, `gen_ai.agent.name`, `gen_ai.tool.name`, `nexura.team.name` or `nexura.workflow.name` where they apply, and `error.type` when it failed (`cancelled` for a cancelled run or tool call) |

Metric attributes are low-cardinality on purpose: no run ids, call ids, tags or content.
Tokens are counted only on model calls, so the totals add up without double counting the
run and team spans. Handoff marks and tool calls waiting for approval are not timed. Pass
`instrument(metrics=False)` to record spans only.

```python
import asyncio

from nexura_agents import Agent, FakeModel, Runner, Usage
from nexura_agents.tracing import instrument, uninstrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

reader = InMemoryMetricReader()
instrument(tracer_provider=TracerProvider(), meter_provider=MeterProvider(metric_readers=[reader]))
agent = Agent(
    name="Support",
    instructions="Be brief.",
    model=FakeModel(responses=["hi"], default_usage=Usage(12, 3, 15)),
)
asyncio.run(Runner().run_async(agent, "hello"))
uninstrument()

data = reader.get_metrics_data()
names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
assert names == {"gen_ai.client.token.usage", "gen_ai.client.operation.duration"}
```

To export, configure a `MeterProvider` with an OTLP metric exporter
(`opentelemetry-exporter-otlp`) the same way as the tracer provider above.

## Privacy: `capture_content`

By default no prompts, completions, tool arguments or results, or workflow state are
recorded; spans carry only names, ids, counts, statuses and token usage. Pass
`instrument(capture_content=True)` to add them as span events:

| Event | Attribute | Content |
| --- | --- | --- |
| `gen_ai.content.prompt` | `gen_ai.prompt` | Messages sent to the model (on `chat`) |
| `gen_ai.content.completion` | `gen_ai.completion` | The model's reply (on `chat`) |
| `gen_ai.tool.call.arguments` | `gen_ai.tool.call.arguments` | Tool arguments (on `execute_tool`) |
| `gen_ai.tool.call.result` | `gen_ai.tool.call.result` | Tool result text (on `execute_tool`) |
| `nexura.workflow.input` / `nexura.workflow.output` | `nexura.workflow.state`, `nexura.workflow.update` | Workflow state at start and end, and each node's update |
| `nexura.handoff.reason` | `nexura.handoff.reason` | The model's handoff reason, or its structured `input_type` arguments (on `handoff A -> B`) |

Every value is passed through credential redaction (secret-named fields in any spelling such as `accessToken` or `Set-Cookie`, bearer and basic
credentials, `key=value` credentials, provider API keys) and truncated to 4000 characters. Redaction is
best effort and is not a personal-data detector: names, addresses and other personal data
in prompts are recorded as written. Turn content capture on only where your trace backend is
allowed to hold that data.
