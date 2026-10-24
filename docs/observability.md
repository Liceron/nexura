# Observability with OpenTelemetry

Nexura can emit [OpenTelemetry](https://opentelemetry.io/) traces for agents, teams and
workflows. Tracing is opt-in: until you call `instrument()`, nothing is imported and nothing
is recorded (the cost is one `None` check). Span and attribute names follow the
[OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai),
pinned to one version (see [Semantic convention version](#semantic-convention-version)).

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

## Semantic convention version

Nexura implements the OpenTelemetry GenAI semantic conventions, which are still in
**Development** status and now live in their own repository. The version implemented is
pinned to commit
[`8538971bd11dba723fdd33c804b5a32121827ff7`](https://github.com/open-telemetry/semantic-conventions-genai/tree/8538971bd11dba723fdd33c804b5a32121827ff7)
of `open-telemetry/semantic-conventions-genai` (2026-10-21, schema
`https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev`, built on semantic-conventions
v1.44.0). The tracer is created with that schema URL, and
`nexura_agents.tracing.SEMCONV_GENAI_COMMIT` holds the commit. Because the conventions can
still change, a later Nexura release may follow a newer commit; renamed attributes keep a
deprecated alias for one release, as below.

## What is recorded

| Span (kind) | Created for | Key attributes |
| --- | --- | --- |
| `invoke_agent <agent>` (`INTERNAL`) | Each `Runner` run or resume (including streaming) | `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name`, `nexura.run_id`, `nexura.run.status` (`completed`, `paused`, `failed`, `cancelled`), `nexura.run.resumed`, `gen_ai.conversation.id` (the `session_id`, when the run has one), `gen_ai.prompt.name` / `gen_ai.prompt.version` (registry prompts), usage totals (`gen_ai.usage.*`) |
| `chat <model>` (`CLIENT`) | Each model call | `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.id` and `gen_ai.response.model` (when the provider reports them), `gen_ai.response.finish_reasons` (the conventions' values: `stop`, `length`, `content_filter`, `tool_call`, and `error` when the call failed; Nexura's own `tool_calls` is also kept in `nexura.response.finish_reasons`), `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens`, `gen_ai.usage.reasoning.output_tokens` (cache and reasoning only when reported and non-zero) |
| `execute_tool <tool>` (`INTERNAL`) | Each tool call | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `nexura.tool.retry_reason` (model retry, rejected), `nexura.tool.pending_approval=true` for a call waiting for approval |
| `invoke_team <team>` (`INTERNAL`) | Each team run or resume | `gen_ai.operation.name=invoke_team` (a custom value), `nexura.team.name`, `nexura.team.kind`, `nexura.team.status`, `nexura.run.resumed`, usage totals |
| `invoke_workflow <spec name>` (`INTERNAL`) | Each `run`, `resume`, `resume_interrupt` and `fork` | `gen_ai.operation.name=invoke_workflow`, `gen_ai.workflow.name`, `nexura.workflow.name`, `nexura.workflow.run_id`, `nexura.workflow.operation`, `nexura.workflow.status`, `nexura.workflow.step_count` |
| `workflow.node <node id>` | Each node step | `nexura.workflow.node.id`, `.step`, `.kind`, `.attempts`, `nexura.workflow.pause_reason` (`approval` or `interrupt`); one `nexura.workflow.retry` event per retried attempt |
| `workflow.branch <name>` / `workflow.map_item <index>` | Each parallel branch or map item | `nexura.workflow.branch`, `nexura.workflow.map_index` |

Span names follow the conventions' `{gen_ai.operation.name} {name}` format.
`gen_ai.provider.name` uses the conventions' well-known values: `anthropic`, `gcp.gemini`
(the Gemini API), `gcp.vertex_ai` (`nexura-vertex`), the provider prefix of a LiteLLM model
string (`bedrock/...` is `aws.bedrock`, no prefix is `openai`), and the custom values
`sarvam` and `fake` (the test models). The OpenAI chat and Responses adapters and
`OpenAIChatModel` are named by their base URL: `api.openai.com` is `openai`,
`*.openai.azure.com` is `azure.ai.openai`, and the hosted OpenAI-compatible APIs of Groq,
DeepSeek, Mistral, xAI, Perplexity, Moonshot and Gemini get their own values. Any other
endpoint (a local Ollama or vLLM server, a proxy) gets no value. A custom model can declare
its own with a `gen_ai_provider_name` string attribute. When the provider is unknown the
attribute is left out rather than guessed, although the conventions make it Required on
inference spans and metrics: the attribute is a discriminator for provider-specific
telemetry, and a made-up value would be wrong for every consumer that relies on it. Retry,
rate-limit and fallback wrappers report the provider of the (first) model they wrap. Response ids and models come from non-streamed responses of the
OpenAI chat and Responses, Anthropic, Gemini (`responseId`, `modelVersion`), Sarvam and
LiteLLM adapters; they are absent for streamed calls.

Why teams keep `invoke_team`: the conventions list crew-style orchestration
(`Crew.kickoff()`) as an `invoke_workflow` example, so a team could be reported as a
workflow. Changing it renames every team span and its metrics, so it is kept as a custom
operation value in this release; `gen_ai.workflow.name` is not set on team spans.

Nesting: `invoke_team` > `invoke_agent` > `chat` / `execute_tool`; `invoke_workflow` >
`workflow.node` > branch or item, a sub-workflow's own `invoke_workflow` span, or an agent
run started by the node's handler. An agent used as a tool nests under the `execute_tool`
span.

Errors set the span status to `ERROR`, add an `exception` event and set `error.type` (the
exception class name, for example `RuntimeError` on a `chat` span whose provider call failed
and `ProviderError` on its run span). Exception text can echo credentials or user data, so with `capture_content` off (the default) only the exception type is recorded (status description, `exception.type`, `error.type`); with `capture_content=True` a redacted, truncated message is added as `exception.message` and the status description. Run metadata attributes (`nexura.run.metadata.*`) drop secret-named keys and redact string values, like `span()` attributes. `RUN_FAILED` run events are not spans and still carry the exception text. Two things
are deliberately not errors: an approval pause (the run, tool and node spans end normally
with `paused` status or `pending_approval`), and a `ModelRetry`, which is a request to the
model rather than a failure. A resumed run is a new span with the same `nexura.run_id`.

## Approvals, budgets, durable effects and guardrails (`nexura.*` events)

Four things the GenAI conventions do not cover are recorded as span events in the
`nexura.*` namespace. They carry ids, counts, classes and statuses only: never arguments,
results, reasons, actors or other content, whatever `capture_content` says.

| Event | Recorded on | Attributes |
| --- | --- | --- |
| `nexura.approval.requested` | The `invoke_agent` span of a run that pauses for approval (also a reapproval and an `effect_unknown` pause after a crash); the `workflow.node` span of a gated node; the current span for a node's `request_approval()` | `nexura.approval.id` (interruption or approval id), `nexura.approval.kind` (`tool_call` or `workflow_node`), `nexura.approval.reason` (`approval`, `effect_unknown`, `node_request`), `nexura.approval.pending_count` and `nexura.approval.tools` (tool names), `nexura.workflow.node.id` |
| `nexura.approval.decided` | The resumed `invoke_agent` span, once the approval is consumed: right after the decision checks, or, with a journal-capable checkpoint store, after the resumed run has recorded its claim in the journal and consumed the approval. A wrong digest, an expired or already spent approval records nothing, and each approval records it once: when a resume stopped after its journal claim but before consuming the approval, `recover()` consumes it and records the event on the recovered run's span; when the stopped resume had already consumed it, recovery adds none. The `invoke_workflow` span of a workflow resume | `nexura.approval.id`, `nexura.approval.kind`, `nexura.approval.decision` (`approve`, `reject`, `mixed` or `answer`), `nexura.approval.approved_count`, `.rejected_count`, `.answered_count`, `.resolved_count` (agents), `nexura.approval.inputs_edited` (workflows) |
| `nexura.budget.reserve` | The current span (the `chat` span for non-streamed model calls) | `nexura.budget.model`, `nexura.budget.method` (`generate`, `stream`, `embed`), `nexura.budget.reservation_id`, `nexura.budget.estimated_tokens` |
| `nexura.budget.settle` | Same | `nexura.budget.model`, `nexura.budget.reservation_id`, `nexura.budget.outcome` (`settled`, or `released` for a failed or never-dispatched call), `nexura.budget.input_tokens`, `nexura.budget.output_tokens` |
| `nexura.budget.refuse` | Same | `nexura.budget.model`, `nexura.budget.method`, `nexura.budget.estimated_tokens`, `nexura.budget.limit_type` (when the budget reports one), `error.type` (for example `BudgetExceededError`) |
| `nexura.effect.intent` | The `invoke_agent` span of a durable run (journal-capable checkpoint store); the current workflow span for workflows | `nexura.effect.id`, `nexura.effect.step_key`, `nexura.effect.class` (`non_idempotent`, `idempotent`, `reconcilable`, `pure`), `nexura.effect.status=pending`, `nexura.run_id`, `nexura.approval.id` (when the step runs under an approval) |
| `nexura.effect.outcome` | Same | Same, with `nexura.effect.status=confirmed` |
| `nexura.effect.unknown` | Same | Same, with `nexura.effect.status=indeterminate` (a timed-out call, a crash, a failed run with dispatched steps) |
| `nexura.effect.resolved` | Same | Same, with `nexura.effect.resolution` when a resolution is committed through the journal |
| `nexura.guardrail.decision` | The `invoke_agent` span for an agent input or output guardrail; the `execute_tool` span for a tool guardrail. Recorded for every outcome other than a pass, together with a `GUARDRAIL_DECISION` run event holding the same fields (see [Guardrail decision events](guardrails-hooks-limits.md#guardrail-decision-events)) | `nexura.guardrail.stage` (`input`, `output`, `tool_input`, `tool_output`), `nexura.guardrail.check` (the guardrail's name), `nexura.guardrail.action` (`fail`, `scrub`, `reject`), `nexura.guardrail.latency_ms`, `nexura.guardrail.source` (`live`), `gen_ai.tool.name` and `gen_ai.tool.call.id` on tool stages, `error.type` for a guardrail that crashed or timed out; from the guardrail's `info`, only when they are short tokens or plain values: `nexura.guardrail.categories`, `.score`, `.backend`, `.cached`, `.rule`, `.decision` |

The budget events are recorded once per budget that reserves. Nested run budgets of one
hierarchy are combined into the deepest scope, which records one event; independent
allowances (for example a run budget and a separate per-model allowance) each record their
own. When no span is open at that moment (for
example a streamed model call, whose admission happens while the stream is read), the event
is exported as a zero-length span with the same name and attributes, so it is not lost. These
helpers never raise and do nothing while tracing is off or OpenTelemetry is not installed.

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

`instrument()` also records the metrics of the GenAI conventions. They use the meter from
`meter_provider=` (default: the global `MeterProvider`, which does nothing until you
configure the OpenTelemetry SDK), and the global provider is never set.

| Metric | Type, unit | Recorded for | Attributes |
| --- | --- | --- | --- |
| `gen_ai.client.inference.duration` | Histogram, `s` | Each model call | `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `error.type` when it failed |
| `gen_ai.invoke_agent.duration` | Histogram, `s` | Each agent run | `gen_ai.agent.name`, `error.type` |
| `gen_ai.execute_tool.duration` | Histogram, `s` | Each tool call | `gen_ai.tool.name`, `error.type` |
| `gen_ai.invoke_workflow.duration` | Histogram, `s` | Each workflow run | `gen_ai.workflow.name`, `error.type` |
| `gen_ai.client.inference.usage.input_tokens`, `.output_tokens`, `.cache_read.input_tokens`, `.cache_write.input_tokens`, `.reasoning.output_tokens` | Counter, `{token}` | Each model call that reports the count | The inference attributes above and `gen_ai.token.modality=unknown` (providers do not report a per-modality split to Nexura) |
| `gen_ai.client.inference.operation.input_tokens`, `.output_tokens` | Histogram, `{token}` | Each model call that reports usage | The inference attributes above |
| `gen_ai.client.token.usage` (deprecated) | Histogram, `{token}` | As before 0.4.0: each model call that reports usage, once per `gen_ai.token.type` | `gen_ai.token.type`, `gen_ai.operation.name=chat`, `gen_ai.request.model` |
| `gen_ai.client.operation.duration` (deprecated for model calls) | Histogram, `s` | As before 0.4.0: each agent run, model call, tool call, team run and workflow run | `gen_ai.operation.name` with the pre-0.4.0 values (`workflow` for workflows, not `invoke_workflow`), plus `gen_ai.request.model`, `gen_ai.agent.name`, `gen_ai.tool.name`, `nexura.team.name` or `nexura.workflow.name`, and `error.type` (`cancelled` for a cancelled run or tool call) |

Bucket boundaries are the ones the conventions recommend for each histogram. Metric
attributes are low-cardinality on purpose: no run ids, call or response ids, tags or
content. Tokens are counted only on model calls, so the totals add up without double
counting the run and team spans. Handoff marks and tool calls waiting for approval are not
timed. Team runs are timed only in `gen_ai.client.operation.duration` (there is no
convention metric for a custom operation). A run or workflow that pauses for approval and is
resumed records one duration point per segment (each span), not one end-to-end value; a
workflow resume that only records a rejection records no `gen_ai.invoke_workflow.duration`
point. A model call that fails before a response records
no token counts. Pass `instrument(metrics=False)` to record spans only.

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
assert {
    "gen_ai.client.inference.duration",
    "gen_ai.invoke_agent.duration",
    "gen_ai.client.inference.usage.input_tokens",
    "gen_ai.client.inference.usage.output_tokens",
} <= names
```

To export, configure a `MeterProvider` with an OTLP metric exporter
(`opentelemetry-exporter-otlp`) the same way as the tracer provider above.

## Privacy: `capture_content`

With content capture off, model/adapter-supplied tool call ids in `gen_ai.tool.call.id`
(on tool spans and guardrail events) and the call part of `nexura.effect.step_key` are
exported as `call-` plus the first 16 hexadecimal characters of HMAC-SHA256 of the raw
call id, keyed by the run id. The same call id within one run has the same pseudonym
across spans and effect events, including retries; different runs have different
pseudonyms. Turn numbers and retry suffixes in effect keys are preserved. With
`capture_content=True`, the original ids and step-key representations are exported.
Only telemetry attributes change: journal step keys, effect ids, payload digests,
idempotency and recovery identities retain their original values. The pinned
`semconv-genai@8538971` defines `gen_ai.tool.call.id` as a string identifier, so a
pseudonym is compliant. These pseudonyms provide correlation within a run; they are
not a claim of anonymization (the run id is also exported).
A trace reader can recompute candidate HMACs using the exported run id to confirm
short or guessable call ids: this protects against verbatim export and direct
cross-run pseudonym correlation, not anonymization. Application `RunEvents` retain
raw ids, as do logs that record those events; this OTel projection does not redact
application logs.

By default no instructions, prompts, completions, tool arguments or results, or workflow
state are recorded; spans carry only names, ids, counts, statuses and token usage. Pass
`instrument(capture_content=True)` to add them. Content is recorded as span attributes in
the structured format of the GenAI conventions' JSON schemas, serialized to a JSON string
(OpenTelemetry Python span attributes cannot hold structured values yet):

| Attribute | On | Content |
| --- | --- | --- |
| `gen_ai.system_instructions` | `chat` | The agent's instructions (the leading system messages), as `[{"type": "text", "content": ...}]` |
| `gen_ai.input.messages` | `chat` | The chat history sent to the model: `[{"role", "parts": [...]}]` with `text`, `tool_call` (`id`, `name`, `arguments`), `tool_call_response` (`id`, `response`), `uri` and `blob` parts. Inline attachment bytes are never copied (`blob` content is a size placeholder). |
| `gen_ai.output.messages` | `chat` | The model's reply, one message. Its finish reason is in `gen_ai.response.finish_reasons` (always recorded, also with capture off), not in the message: the pinned schema deprecates `finish_reason` on output messages |
| `gen_ai.tool.call.arguments` | `execute_tool` | Tool arguments (JSON object) |
| `gen_ai.tool.call.result` | `execute_tool` | The tool result (parsed as JSON when it is JSON text, otherwise the text) |

Other content is recorded as span events:

| Event | Attribute | Content |
| --- | --- | --- |
| `gen_ai.content.prompt` (deprecated) | `gen_ai.prompt` | Messages sent to the model (on `chat`) |
| `gen_ai.content.completion` (deprecated) | `gen_ai.completion` | The model's reply (on `chat`) |
| `gen_ai.tool.call.arguments` (deprecated) | `gen_ai.tool.call.arguments` | Tool arguments (on `execute_tool`) |
| `gen_ai.tool.call.result` (deprecated) | `gen_ai.tool.call.result` | Tool result text (on `execute_tool`) |
| `nexura.workflow.input` / `nexura.workflow.output` | `nexura.workflow.state`, `nexura.workflow.update` | Workflow state at start and end, and each node's update |
| `nexura.handoff.reason` | `nexura.handoff.reason` | The model's handoff reason, or its structured `input_type` arguments (on `handoff A -> B`) |

Every value is passed through credential redaction (secret-named fields in any spelling such as `accessToken` or `Set-Cookie`, bearer and basic
credentials, `key=value` credentials, provider API keys). Event values are truncated to 4000
characters; in the structured attributes each text value is truncated to 4000 characters
(the JSON stays valid). `gen_ai.input.messages` holds at most the newest 100 messages of
the history, and the oldest of those are dropped when the value would pass 64,000
characters, so the current turn is always kept. Inside one value, lists and objects keep
their first 100 items and nesting stops at 12 levels (`"[DEPTH LIMIT]"`). Attachment URLs
lose their query string, user name and fragment (`https://host/path?...`, presigned URLs
carry their signature there); only `http`, `https` and `gs` URLs are kept, any other scheme
(such as a `data:` URL, which holds the bytes themselves) is replaced by a placeholder; and
inline attachment bytes are replaced by a size marker,
in the attributes and in the deprecated events. A tool result is parsed as JSON only when it
is at most 64,000 characters long; otherwise, or if it cannot be parsed, it is recorded as
text. Content that cannot be converted at all is left out: recording content never changes
or fails a run. Redaction is
best effort and is not a personal-data detector: names, addresses and other personal data
in prompts are recorded as written. Turn content capture on only where your trace backend is
allowed to hold that data.

## Deprecated aliases

These names changed when Nexura moved to the pinned conventions in 0.4.0. The old names are
still emitted next to the new ones for one release and are **removed in 0.5.0**
(`nexura_agents.tracing.ALIASES_REMOVED_IN`). Move dashboards and queries to the new names.

| Old (deprecated) | New | Where |
| --- | --- | --- |
| `gen_ai.system` | `gen_ai.provider.name` | `chat` spans (same value) |
| `nexura.usage.reasoning_tokens` | `gen_ai.usage.reasoning.output_tokens` | `chat`, run and team spans |
| `gen_ai.operation.name=workflow`, span `workflow <name>` | `gen_ai.operation.name=invoke_workflow`, span `invoke_workflow <name>`; the old value stays in `nexura.operation.name=workflow` | Workflow spans (the span name itself changed in 0.4.0) |
| Events `gen_ai.content.prompt` / `gen_ai.content.completion` | Attributes `gen_ai.input.messages` (with `gen_ai.system_instructions`) / `gen_ai.output.messages` | `chat` spans, with `capture_content=True` |
| Events `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` (not names the conventions define) | Attributes `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` | `execute_tool` spans, with `capture_content=True` |
| `gen_ai.response.finish_reasons=["tool_calls"]` | `["tool_call"]` (the conventions' value); the raw value stays in `nexura.response.finish_reasons` | `chat` spans |
| Metric `gen_ai.client.token.usage` (`gen_ai.token.type`) | `gen_ai.client.inference.usage.input_tokens` / `.output_tokens` counters and the `gen_ai.client.inference.operation.*_tokens` histograms | Model calls |
| Metric `gen_ai.client.operation.duration` for model calls, agent runs, tool calls and workflows | `gen_ai.client.inference.duration`, `gen_ai.invoke_agent.duration`, `gen_ai.execute_tool.duration`, `gen_ai.invoke_workflow.duration` | Recorded exactly as before 0.4.0, including `gen_ai.operation.name=workflow` for workflows. From 0.5.0 it is no longer recorded for model calls (the conventions reserve it for non-inference operations) and workflows use `invoke_workflow` |

The Nexura V2 trace exporter forwards `gen_ai.provider.name` and still reads `gen_ai.system`
from spans recorded by older instrumentation.
