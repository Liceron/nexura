# Structured output

Set `output_type` to a Pydantic model or a JSON-schema dict and the agent returns a validated
value instead of text. Where the provider has a native structured-output mode, Nexura uses it;
otherwise it adds the schema to the instructions. Either way it parses the model's final reply
and sends validation errors back to the model so it can correct itself. This page covers the
output type, native mode, the validation retry loop, and `ModelRetry` for tools.

```python
from pydantic import BaseModel

from nexura_agents import Agent, FakeModel


class Ticket(BaseModel):
    title: str
    priority: int


agent = Agent(
    name="Triage",
    instructions="Turn the message into a ticket.",
    model=FakeModel(responses=['{"title": "Router down", "priority": 1}']),
    output_type=Ticket,
)
result = agent.run("My router is down, urgent!")

assert isinstance(result.output, Ticket)
assert result.output.priority == 1
assert result.final_output == '{"title": "Router down", "priority": 1}'  # raw text
```

`result.output` is the `Ticket` object. `result.final_output` stays the raw text of the reply.
The model may wrap its JSON in a ```` ```json ```` code fence; the fence is stripped before
parsing.

## The JSON schema

`output_type` is a Pydantic `BaseModel` subclass or a JSON-schema dict (next section). Anything
else raises `SDKValidationError` when you create the agent. For a model the schema comes from
`output_type.model_json_schema()`, so field types, defaults, `Field(description=...)` and
constraints all reach the model.

```python
import json

from pydantic import BaseModel, Field

from nexura_agents import Agent, FakeModel


class Review(BaseModel):
    score: int = Field(ge=1, le=5, description="1 (bad) to 5 (great)")
    summary: str


model = FakeModel(responses=['{"score": 5, "summary": "Great."}'])
agent = Agent(name="Reviewer", instructions="Rate the text.", model=model, output_type=Review)
agent.run("Loved it")

system = model.calls[0]["messages"][0].content
assert "Rate the text." in system
assert json.dumps(Review.model_json_schema(), ensure_ascii=False) in system
```

With a model that has no native mode (`FakeModel` by default, or your own `ModelProtocol`
class) the schema is sent as text in the system prompt. See [Native mode](#native-mode) for
models that receive it natively.

## JSON-schema dict output

Pass a dict when you have no Pydantic model. `result.output` is then the parsed JSON (a `dict`,
`list`, ...), checked by a small built-in validator.

```python
from nexura_agents import Agent, FakeModel, OutputValidationError

schema = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "owner": {"type": ["string", "null"]},
    },
    "required": ["title", "priority"],
    "additionalProperties": False,
}
model = FakeModel(responses=['{"title": "", "priority": 9}', '{"title": "Router", "priority": 1}'])
agent = Agent(name="Triage", instructions="Make a ticket.", model=model, output_type=schema)
result = agent.run("Router down")

assert result.output == {"title": "Router", "priority": 1}
feedback = model.calls[1]["messages"][-1].content
assert "$.priority: 9 is greater than the maximum 5" in feedback
assert "$.title: text is shorter than minLength 1" in feedback
```

Supported keywords: `type` (a name or a list such as `["string", "null"]`), `properties`,
`required`, `additionalProperties` (bool or schema), `items`, `enum`, `const`, `description`,
`title`, `minimum`, `maximum`, `minLength`, `maxLength`, `minItems`, `maxItems`, and local
`$defs` with `$ref: "#/$defs/name"`. Any other keyword (`pattern`, `anyOf`, `format`,
`uniqueItems`, remote `$ref`, ...) raises `SDKValidationError` naming it when the agent is
created, so a constraint is never silently ignored. Error lines carry a path such as
`$.items[0].name: expected string, got integer`, and those lines are what the model sees on an
output retry.

```python
from nexura_agents import Agent, FakeModel
from nexura_agents.errors import SDKValidationError

try:
    Agent(
        name="A", instructions="", model=FakeModel(), output_type={"type": "string", "pattern": "a"}
    )
except SDKValidationError as exc:
    assert "pattern" in str(exc)
else:
    raise AssertionError("expected SDKValidationError")
```

## Native mode

A model can opt in to receive the schema through the provider's own structured-output feature
instead of the prompt. A model opts in with the class attribute `supports_response_schema = True`
and a `response_schema=` keyword on `generate()`. The runner passes the schema (a Pydantic
model's `model_json_schema()`, or your dict as is) only to models that opt in; every other
model keeps the prompt-based schema, so existing models do not change.

| Model | Native mode | Details |
| --- | --- | --- |
| `OpenAIProvider` (`"openai:..."`, `"ollama:..."`) | yes, also with tools | `response_format` of type `json_schema` |
| `OpenAIChatModel` | yes, without tools | same payload as above |
| `LiteLLMModel` | yes, without tools | `response_format` passed to `litellm.acompletion` |
| `GeminiNativeProvider`, `VertexAIProvider` | yes, without tools | `responseMimeType: application/json` plus `responseSchema` |
| `AnthropicNativeProvider` | yes, without tools | forced synthetic tool `structured_output` |
| `SarvamProvider`, `FakeModel`, your own models | no (prompt) | `FakeModel(native_schema=True)` opts in for tests |

Rules:

- **Tools and handoffs.** The schema is sent natively only if the agent offers no tools and no
  handoffs, or the model declares `response_schema_with_tools = True` (OpenAI provider,
  `FakeModel(schema_with_tools=True)`). Gemini models do not all accept function calling with a
  response schema, Anthropic can force only one tool, and `OpenAIChatModel` and LiteLLM front
  servers that differ; so with tools those keep the full schema in the prompt. A handoff target
  uses its own `output_type` and its own rule.
- **Prompt text.** With native mode active the system prompt gets one short sentence ("Respond
  with only a JSON object ... that conforms to the required output schema") and not the full
  schema. Otherwise it contains the full schema as before.
- **Validation always runs.** The runner still validates the reply and retries with feedback,
  so constraints a provider dialect cannot express are still enforced.
- **Streaming.** With `stream_text=True`, a turn that sends a schema streams when the
  model declares `streams_response_schema = True`. That covers `OpenAIChatModel`,
  `LiteLLMModel`, and the OpenAI and Gemini providers; the schema is still sent. Other
  models, including the Anthropic provider (whose structured output is a forced tool
  call), answer typed turns in one piece. See [Streaming the output](#streaming-the-output).
- **Root type.** Providers expect an object at the root. Anthropic wraps other root types
  (`{"type": "array", ...}`) in `{"value": ...}` and unwraps the answer; OpenAI may reject
  non-object roots.
- **Wrappers.** `RetryModel` and `FallbackModel` forward the opt-in (a `FallbackModel` only if
  every model opts in).

**OpenAI `strict`.** Strict mode requires every property listed in `required` and
`additionalProperties: false`. Nexura sends `strict: true` (adding `additionalProperties: false`
to every object) only when that cannot change which answers are valid: the root is an object,
each object already requires all of its properties, no object is free-form, and only keywords
strict mode documents are used. Otherwise it sends the schema unchanged with `strict: false`.
Pydantic models with defaulted fields therefore go out non-strict.

**Gemini dialect.** Gemini accepts a subset of OpenAPI schema. Nexura inlines every `$ref`
(a recursive reference becomes a plain `object`), drops `$defs`, `additionalProperties`,
`title`, `default`, `minLength`, `maxLength` and other unsupported keywords, turns
`type: [X, "null"]` or `anyOf` with null into `nullable: true`, and turns `const` into a
one-value `enum`. Dropped constraints are checked by the runner's own validation instead.

**Anthropic.** There is no JSON-schema response mode, so Nexura adds a synthetic tool
`structured_output` whose input schema is the output schema, forces it with `tool_choice`, and
returns the tool call's arguments as the reply text. This is only done when the agent has no
other tools.

```python
from pydantic import BaseModel

from nexura_agents import Agent, FakeModel


class Point(BaseModel):
    x: int
    y: int


model = FakeModel(responses=['{"x": 1, "y": 2}'], native_schema=True)
result = Agent(name="P", instructions="", model=model, output_type=Point).run("origin?")

assert model.schemas == [Point.model_json_schema()]  # received natively
assert result.output == Point(x=1, y=2)
assert "properties" not in model.calls[0]["messages"][0].content  # not in the prompt
```

## Streaming the output

With `output_type` and `stream_async(..., stream_text=True)`, the runner emits a
`PARTIAL_OUTPUT` event whenever the JSON parsed so far changes. Use it to fill a form or a
card while the answer is still arriving:

```python
import asyncio
from typing import Any

from nexura_agents import Agent, RunEventType, Runner
from nexura_agents.providers.types import ProviderCapabilities, StreamDelta
from nexura_agents.types import ModelResponse
from pydantic import BaseModel


class Ticket(BaseModel):
    title: str
    tags: list[str]


class StreamingFake:
    """Offline model that streams a JSON answer in pieces."""

    capabilities = ProviderCapabilities(can_stream=True)
    supports_response_schema = streams_response_schema = True

    async def generate(self, messages: Any, tools: Any = None, **kw: Any) -> ModelResponse:
        raise NotImplementedError

    async def stream(self, messages: Any, tools: Any = None, **kw: Any) -> Any:
        for piece in ['{"title": "Ref', 'und", "tags": ["billing"]}']:
            yield StreamDelta(kind="text_delta", text=piece)
        yield StreamDelta(kind="finish", finish_reason="stop")


async def main() -> list[Any]:
    agent = Agent(
        name="A", instructions="File a ticket.", model=StreamingFake(), output_type=Ticket
    )
    return [
        event.data["output"]
        async for event in Runner().stream_async(agent, "refund please", stream_text=True)
        if event.type == RunEventType.PARTIAL_OUTPUT
    ]


partials = asyncio.run(main())
assert partials[-1] == {"title": "Refund", "tags": ["billing"]}  # always ends complete
```

- `event.data["output"]` is a plain `dict` or `list`, the best-effort parse of the text so
  far (a string still being written is included). It is **not** validated, and fields may
  be missing. The validated object comes with `RUN_COMPLETED` (`output_value`), as before.
- An event is sent only when the parsed value changes. Text before the JSON (prose) is
  ignored, and a leading code fence is skipped.
- Parsing is throttled. The text is re-read after it has grown by about 10% (at least 32
  characters) since the last parse, and once more when the stream ends. So you get a few
  events per answer rather than one per chunk, and the work stays linear even for large
  answers. The last `PARTIAL_OUTPUT` always reflects the complete text.
- A streamed refusal (OpenAI) keeps its `Model Refusal: ...` text, as in a non-streamed
  turn.
- `TEXT_DELTA` events still carry the raw text. A turn that calls tools produces no
  partial outputs.

## Validation retries

If the reply is not valid JSON or fails validation, the runner appends a message with the
validation error and asks again. `output_retries` (default 1) sets how many such corrections are
allowed. Each correction emits a `RETRY` event.

```python
from pydantic import BaseModel

from nexura_agents import Agent, FakeModel, RunEventType


class Answer(BaseModel):
    value: int


model = FakeModel(responses=["The answer is forty-two.", '{"value": 42}'])
agent = Agent(name="A", instructions="", model=model, output_type=Answer)
result = agent.run("What is the answer?")

assert result.output == Answer(value=42)
assert [e.type for e in result.events].count(RunEventType.RETRY) == 1
feedback = model.calls[1]["messages"][-1]
assert feedback.role == "user" and "did not match" in feedback.content
```

Dict schemas use the same loop. When the retries run out, the run raises `OutputValidationError`. It carries the last bad reply
in `raw_output` and the number of tries in `attempts`.

```python
from pydantic import BaseModel

from nexura_agents import Agent, FakeModel, OutputValidationError


class Answer(BaseModel):
    value: int


agent = Agent(
    name="A",
    instructions="",
    model=FakeModel(responses=["nope", "still nope"]),
    output_type=Answer,
    output_retries=1,
)
try:
    agent.run("What is the answer?")
except OutputValidationError as exc:
    assert exc.raw_output == "still nope"
    assert exc.attempts == 2
else:
    raise AssertionError("expected OutputValidationError")
```

Set `output_retries=0` to fail on the first bad reply.

## Output validators

Schemas check the shape of an answer. `output_validators` check what it means: a price
that must be positive, an ID that must exist in your database. A validator gets the
parsed output and returns it (possibly changed), or raises `ModelRetry` to send the
model back with feedback:

```python
from nexura_agents import Agent, FakeModel, ModelRetry, Runner
from pydantic import BaseModel


class Quote(BaseModel):
    product: str
    price: float


def positive_price(quote: Quote) -> Quote:
    if quote.price <= 0:
        raise ModelRetry("price must be positive")
    return quote


model = FakeModel(responses=['{"product": "pen", "price": -1}', '{"product": "pen", "price": 2}'])
agent = Agent(
    name="Quotes",
    instructions="Quote a price.",
    model=model,
    output_type=Quote,
    output_validators=[positive_price],
)
result = Runner().run(agent, "price of a pen?")
assert result.output == Quote(product="pen", price=2)
assert "price must be positive" in model.calls[1]["messages"][-1].content
```

- A validator is `fn(output)` or `fn(ctx, output)` (it gets the `RunContext` when it has
  two required positional parameters), and can be sync or async. Validators run in order
  after the output is parsed; each one gets the previous one's result.
- `ModelRetry("reason")` sends `Your answer was not accepted: reason` back to the model and
  emits a `RETRY` event (`reason="output_validator"`). Retries share `output_retries` with
  schema failures. When they run out, the run fails with `OutputValidationError`.
- Without `output_type`, a validator that returns a different string replaces the answer,
  in `result.output` and in the stored history. With `output_type` it can return a
  changed object, and the JSON text stays as the model wrote it.
- Any other exception from a validator fails the run. When the output came from a tool
  (`tool_use_behavior`), there is no model turn to retry, so `ModelRetry` fails the run
  with `OutputValidationError`.
- Output guardrails still run afterwards, on the validated output.

## ModelRetry in tools

Tools can also ask the model to try again. Raise `ModelRetry(message)` and the message goes back
to the model as the tool result, instead of crashing the run. Use it for bad input the model can
fix, such as an unknown ID.

```python
from nexura_agents import Agent, FakeModel, ModelRetry, ToolCall, tool

ORDERS = {"A1": 19.5}


@tool
def get_order(order_id: str) -> float:
    """Return the total for an order."""
    if order_id not in ORDERS:
        raise ModelRetry(f"No order {order_id!r}. Order IDs look like A1.")
    return ORDERS[order_id]


model = FakeModel(
    responses=[
        [ToolCall(id="1", name="get_order", arguments={"order_id": "a-1"})],
        [ToolCall(id="2", name="get_order", arguments={"order_id": "A1"})],
        "The total is 19.5.",
    ]
)
agent = Agent(name="Orders", instructions="", model=model, tools=[get_order])
result = agent.run("Total for order A1?")

assert result.output == "The total is 19.5."
retry_result = model.calls[1]["messages"][-1]
assert retry_result.role == "tool" and "No order 'a-1'" in retry_result.content
```

Each tool gets one retry per run by default. A tool that raises `ModelRetry` more often than
that fails the run with a `ToolExecutionError`. Raise the limit with `@tool(max_retries=3)`.

## Typed results

For static type checkers (mypy, pyright), `Agent` and `RunResult` are generic in the output type.
`Agent(output_type=Ticket, ...)` is an `Agent[Ticket]`, and `Runner().run(agent, ...)`,
`run_async`, `resume` and `resume_async` return a `RunResult[Ticket]`, so `result.output` is a
`Ticket` without a cast. Without `output_type` it is `str`; a JSON-schema dict gives
`dict[str, Any]`. Nothing changes at run time. A bare `Agent` or `RunResult` annotation means
`Agent[Any]`, so existing code and mixed teams of agents keep type-checking.

The type describes a completed run of that agent. Two cases a type checker cannot see:

- A paused or cancelled run has no output yet: `result.output` is `None`. Check
  `result.status == "completed"` before using it.
- With handoffs, the agent that answers may have a different `output_type`. Give the entry
  agent the same `output_type` as every agent it can hand off to, or annotate it as
  `Agent[Any]`.

```python
from typing import assert_type

from pydantic import BaseModel

from nexura_agents import Agent, FakeModel, Runner


class Invoice(BaseModel):
    number: str


agent = Agent(
    name="Billing",
    instructions="Extract the invoice.",
    model=FakeModel(responses=['{"number": "INV-7"}']),
    output_type=Invoice,
)
result = Runner().run(agent, "Invoice INV-7")
assert_type(result.output, Invoice)  # checked by mypy; a no-op when the code runs
assert result.output.number == "INV-7"
```

## See also

- [Agents](agents.md): the other `Agent` options.
- [Tools](tools.md): function tools, typed dependencies and retries.
- [Testing your agents](testing.md): asserting on results and events.
- [Troubleshooting](troubleshooting.md): common errors and what they mean.

## Structured output

`output_type` accepts a Pydantic model or a JSON-schema dict (a supported subset, checked
when you create the agent). Providers that support it receive the schema natively: OpenAI
`response_format`, Gemini and Vertex `responseSchema`, LiteLLM `response_format`, and
Anthropic through a forced tool. Other models get the schema in the prompt. The runner
validates the reply in every case and sends path-level errors back to the model, so
constraints a provider ignores are still enforced. See `docs/structured-output.md`.
