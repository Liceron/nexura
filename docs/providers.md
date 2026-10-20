# Models and providers

An agent talks to a model through a provider. You can pass a model object, or a string of the form `"provider:model"` and let Nexura build the provider for you. This page covers the providers that ship with `nexura-agents` (OpenAI, Anthropic, Gemini, Sarvam) and Vertex AI (the separate `nexura-vertex` package), how credentials are found, how reasoning tokens show up in usage, and how to retry and fail over between models. Everything except the live-call examples runs offline.

## Start offline

`FakeModel` needs no key and makes no network call. The string `"test"` builds a `FakeModel` that answers every turn with the same fixed text.

```python
from nexura_agents import Agent, FakeModel

agent = Agent(name="Demo", instructions="Be brief.", model=FakeModel(responses=["Hello!"]))
assert agent.run("Hi").final_output == "Hello!"

# "test" is a shortcut for an offline model with a fixed reply.
quick = Agent(name="Quick", instructions="Be brief.", model="test")
assert quick.run("Hi").final_output == "Hello from the offline test model."
```

See [Testing your agents](testing.md) for scripted tool calls.

## Model strings

`Agent(model="provider:model")` calls `infer_model`. These prefixes are recognised:

| Prefix | Provider class | Credentials |
|---|---|---|
| `openai` | `OpenAIProvider` | `OPENAI_API_KEY` |
| `anthropic` | `AnthropicNativeProvider` (exported as `AnthropicProvider`) | `ANTHROPIC_API_KEY` |
| `gemini` or `google` | `GeminiNativeProvider` (exported as `GeminiProvider`) | `GEMINI_API_KEY`, or `GOOGLE_API_KEY` |
| `vertex` | `VertexAIProvider` (`pip install nexura-vertex`) | Google Application Default Credentials |
| `sarvam` | `SarvamProvider` | `SARVAM_API_KEY` |
| `ollama` | `OpenAIProvider` pointed at `http://localhost:11434/v1` | none |
| `litellm` | `LiteLLMModel` (optional extra, see below) | whatever LiteLLM needs for that provider |
| `test` | `FakeModel` | none |

Anything else raises `SDKValidationError` that lists the valid prefixes. Building a provider does not contact the network, so you can check the wiring offline by passing a placeholder key:

```python
import os

from nexura_agents import Agent, infer_model
from nexura_agents.errors import SDKValidationError

os.environ["OPENAI_API_KEY"] = "placeholder-not-a-real-key"
model = infer_model("openai:gpt-4o-mini")
assert type(model).__name__ == "OpenAIProvider"
assert model.model == "gpt-4o-mini"

agent = Agent(name="A", instructions="x", model="openai:gpt-4o-mini")
assert type(agent.model).__name__ == "OpenAIProvider"

try:
    infer_model("llama")  # no "provider:" prefix
except SDKValidationError as exc:
    assert "Unknown model" in str(exc)
else:
    raise AssertionError("expected SDKValidationError")
```

A string gives you the default settings of each provider. To change a timeout, a base URL or a key, build the provider yourself. All HTTP providers take `api_key`, `base_url`, `timeout` (seconds, default 30) and `transport` (an `httpx` transport, which is how the tests here stay offline).

## Live calls

These need real credentials and are billed to your account. The automated docs tests skip them.

```python notest
from nexura_agents import Agent

agent = Agent(name="Support", instructions="Be brief.", model="openai:gpt-4o-mini")
print(agent.run("Summarize our refund policy in one line.").final_output)
```

Set the key in your environment first (`export OPENAI_API_KEY=...`) or pass `api_key=` to the provider class. Do not put keys in source files.

## Vertex AI

`VertexAIProvider` serves Gemini models from Google Cloud. It signs requests with OAuth access tokens, not API keys. By default it uses Application Default Credentials (ADC) through `google-auth`, which the `nexura-vertex` package installs:

```bash
pip install nexura-vertex
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT
```

```python notest
from nexura_agents import Agent
from nexura_vertex import VertexAIProvider

model = VertexAIProvider("gemini-2.5-flash", project="YOUR_PROJECT", location="global")
print(model.project, model.credential_source, model.quota_project)
agent = Agent(name="Support", instructions="Be brief.", model=model)
print(agent.run("Say hello.").final_output)
```

How the provider picks its settings:

- **Project.** The `project=` argument first, then `GOOGLE_CLOUD_PROJECT`, then `CLOUDSDK_CORE_PROJECT`, then the project of your credentials. On a Google Cloud VM the credentials' project is the VM's project, which may not be the one you want to bill. If no project is found, it raises `ProviderAuthError`.
- **Location.** `location="global"` (the default) or a region such as `"asia-south1"`. A region changes the host to `asia-south1-aiplatform.googleapis.com`.
- **Quota project.** If your credentials carry a quota project (set with `set-quota-project`), it is sent as the `x-goog-user-project` header so that project is billed. It is available as `provider.quota_project`.
- **Where credentials came from.** `provider.credential_source` is a plain-text description (for example your user login or a VM service account), which helps when billing lands in the wrong project.
- **Your own tokens.** Pass `token_provider=` (a sync or async callable that returns an access token) to skip ADC. This is also how the example below runs offline.

Missing credentials, a missing `google-auth` install, a missing project, and HTTP 401 or 403 from Google all raise `ProviderAuthError`. See [Troubleshooting](troubleshooting.md).

The example below uses a fake token and `httpx.MockTransport`, so nothing leaves your machine. The mock also reports `thoughtsTokenCount`, which becomes `usage.reasoning_tokens`.

```python
import asyncio

import httpx

from nexura_agents import Agent, Runner
from nexura_vertex import VertexAIProvider

seen: list[httpx.Request] = []


def handler(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "Hi from Vertex"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 25,
                "totalTokenCount": 37,
            },
        },
    )


provider = VertexAIProvider(
    "gemini-2.5-flash",
    project="demo-project",
    location="asia-south1",
    token_provider=lambda: "fake-token",
    transport=httpx.MockTransport(handler),
)
agent = Agent(name="V", instructions="Be brief.", model=provider)
result = asyncio.run(Runner().run_async(agent, "hello"))

assert result.final_output == "Hi from Vertex"
assert seen[0].headers["Authorization"] == "Bearer fake-token"
assert seen[0].url.host == "asia-south1-aiplatform.googleapis.com"
assert "/projects/demo-project/locations/asia-south1/" in seen[0].url.path
assert result.usage.reasoning_tokens == 25
assert result.usage.completion_tokens == 30  # 5 visible + 25 thinking
```

`examples/live/vertex_check.py` is a runnable live check for Vertex AI. It sends three short billed requests (plain text, a tool call and a streamed reply) using your ADC login and prints a JSON report. Run it yourself; tests and CI never do:

```bash
uv run python examples/live/vertex_check.py --project YOUR_PROJECT
```

For OpenAI, Anthropic, Gemini (API key) and Sarvam, `examples/live/provider_check.py`
runs the same checks plus a structured (typed) answer, using the provider's key from your
environment. It refuses to start without a key:

```bash
export OPENAI_API_KEY=...
uv run python examples/live/provider_check.py --provider openai   # or anthropic, gemini, sarvam
```

## LiteLLM (100+ providers)

`LiteLLMModel` sends requests through [LiteLLM](https://docs.litellm.ai/), which reaches Bedrock, Azure OpenAI, Ollama, Groq, Mistral, Together, OpenRouter and many more behind one interface. It is an optional extra, so the core install stays small:

```bash
pip install "nexura-agents[litellm]"
export LITELLM_LOCAL_MODEL_COST_MAP=True   # stop LiteLLM fetching its cost map over HTTP at import
```

Set `LITELLM_LOCAL_MODEL_COST_MAP=True` in your environment before litellm is first imported. The SDK never changes `os.environ` or LiteLLM's globals for you.

Use any LiteLLM model string, either directly or as `"litellm:<model string>"` with `infer_model` and `Agent(model=...)`:

```python notest
from nexura_agents import Agent, LiteLLMModel, RetryModel

# Credentials come from each provider's usual environment variables (AWS_*, AZURE_*, GROQ_API_KEY, ...).
bedrock = LiteLLMModel("bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0", timeout=60)
local = LiteLLMModel("ollama/llama3.1", api_base="http://localhost:11434")

agent = Agent(name="Support", instructions="Be brief.", model=RetryModel(bedrock))
# or: Agent(name="Support", instructions="Be brief.", model="litellm:groq/llama-3.1-8b-instant")
print(agent.run("Summarize our refund policy in one line.").final_output)
```

`api_key`, `api_base` and `timeout` are optional; extra keyword arguments such as `temperature` or `max_tokens` are passed to `litellm.acompletion` unchanged. Messages, tools, tool results and attachments use the OpenAI chat format, so which attachment kinds work depends on the underlying model.

Retries are owned by the SDK. Every call passes `num_retries=0` and `max_retries=0`, so LiteLLM never retries on its own; wrap the model in `RetryModel` or `FallbackModel` (see below). LiteLLM errors are mapped by HTTP status: 401/403 to `ProviderAuthError`, 429 to `ProviderRateLimitedError` (with `retry_after` when the response has a `Retry-After` header), 5xx to `ProviderServerError`, a 4xx about the context window to `ProviderContextLengthError`, other 4xx to `ProviderClientError`, and timeouts and connection failures to `ProviderRequestError`.

Both `LiteLLMModel` and `OpenAIChatModel` stream text when you ask for it: `Runner.stream_async(agent, prompt, stream_text=True)` emits real `TEXT_DELTA` events as chunks arrive, assembles tool-call arguments split across chunks, and ends with the provider's usage and finish reason. `model.generate()` is unchanged and still returns whole responses, and `stream=True` is still rejected in `LiteLLMModel(**options)`. Internally `stream()` sends `stream=True` with `stream_options={"include_usage": True}` (LiteLLM keeps `num_retries=0`). Errors, including ones raised mid-stream, map to the same typed errors as `generate()`; a stream that errors or ends early never produces a final answer (text already emitted as deltas is not committed), and closing or cancelling the run closes the HTTP stream or LiteLLM stream wrapper. Turns that send a native output schema stream too (with `PARTIAL_OUTPUT` events, see [Structured output](structured-output.md#streaming-the-output)); models without `streams_response_schema`, such as the Anthropic provider, answer them in one piece.

## Temperature, max_tokens and stop sequences

`ModelSettings` sets generation parameters. Put them on the `Runner` for every agent, or
on an `Agent`; for each field the agent's value wins, and unset fields fall back to the
runner's. In a handoff team, each agent uses its own merged settings.

```python
from nexura_agents import Agent, FakeModel, ModelSettings, Runner

model = FakeModel(responses=["ok"])
agent = Agent(
    name="Writer",
    instructions="Write short poems.",
    model=model,
    model_settings=ModelSettings(temperature=0.9),
)
Runner(model_settings=ModelSettings(temperature=0.2, max_tokens=300)).run(agent, "hi")

assert model.calls[0]["model_settings"] == ModelSettings(temperature=0.9, max_tokens=300)
```

- Provider models (`"openai:..."`, `"anthropic:..."`, Gemini, Vertex AI and the others
  built by model strings) receive them in the request.
- `OpenAIChatModel` sends `temperature`, `max_tokens` and `stop`, both for normal and
  streaming calls. `LiteLLMModel` passes the same names to LiteLLM, and they override
  the options given to its constructor for that call.
- `RetryModel` and `FallbackModel` forward them to the wrapped models.
- Your own model receives them when its `generate` (or, with `stream_text=True`, its
  `stream`) accepts `model_settings=`. If it does not and a setting is set, the runner
  emits a `RuntimeWarning` instead of silently ignoring it.
- `ModelSettings` checks its values: `temperature` must be a number >= 0,
  `max_tokens` an integer >= 1, and `stop_sequences` a list of non-empty strings (a
  bare string is rejected, because it would become one stop sequence per character).
- An agent used as a tool (`agent.as_tool()`) runs on its own runner, so it does not
  inherit the outer runner's settings. Set `model_settings` on that agent, or pass
  `as_tool(runner=...)`.

## Sampling, seeds, reasoning and extra fields

`ModelSettings` also takes `top_p`, `seed`, `presence_penalty`, `frequency_penalty`,
`reasoning_effort` (`"minimal"`/`"low"`/`"medium"`/`"high"`), `thinking_budget_tokens` and
`extra` (a dict merged into the request body last, for options the SDK does not model).
They merge field by field like the others; `extra` dicts merge key by key.

```python
from nexura_agents import ModelSettings

reasoning = ModelSettings(reasoning_effort="low", seed=7)  # OpenAI reasoning models
thinking = ModelSettings(thinking_budget_tokens=2048, max_tokens=8000)  # Claude, Gemini
custom = ModelSettings(extra={"user": "customer-42"})  # any provider field
assert custom.merged(ModelSettings(extra={"metadata": {"t": 1}})).extra == {
    "user": "customer-42",
    "metadata": {"t": 1},
}
```

| Setting | OpenAI / Ollama / `OpenAIChatModel` | LiteLLM | Gemini / Vertex AI | Anthropic |
|---|---|---|---|---|
| `top_p` | `top_p` | `top_p` | `topP` | `top_p` |
| `seed` | `seed` | `seed` | `seed` | not supported |
| `presence_penalty`, `frequency_penalty` | same names | same names | `presencePenalty`, `frequencyPenalty` | not supported |
| `reasoning_effort` | `reasoning_effort` | `reasoning_effort` | not supported | not supported |
| `thinking_budget_tokens` | not supported | `thinking` | `thinkingConfig.thinkingBudget` (0 = off) | `thinking` (see below; 0 = off) |
| `extra` | merged into the body | passed as keyword arguments | merged into the body | merged into the body |

- A setting that a provider does not support is not sent, and the runner emits a
  `RuntimeWarning` naming it, instead of dropping it silently. Sarvam supports none of
  these except `extra`.
- Values are checked when you create `ModelSettings`: `top_p` from 0 to 1, penalties from
  -2 to 2, an integer `seed`, a known `reasoning_effort`, and a budget of 0 or more.
- `extra` adds fields; it cannot replace one the SDK already sets (`model`, `messages`,
  `stream`, or a setting such as `temperature`). That raises `SDKValidationError`, so use
  the dedicated setting instead.
- The reasoning text itself is not returned or streamed: thinking blocks never become
  the answer or a `TEXT_DELTA`. Reasoning *token counts* are reported in
  `Usage.reasoning_tokens` (see below).
- **Anthropic extended thinking** follows the API's rules, which are checked before
  anything is sent:
  - The budget must be at least 1024 and below `max_tokens`.
  - `temperature` must be unset or 1.
  - `top_p` must be between 0.95 and 1.
  - It works with tools. The thinking blocks the model returns, including their
    `signature`, are kept on the assistant message (`Message.provider_data`) and sent
    back before the tool results, as Anthropic requires. Streamed thinking is assembled
    the same way. The blocks are saved in sessions and checkpoints, so treat stored
    transcripts as containing the model's reasoning. Other providers ignore them.
  - It cannot force a tool call: `tool_choice="required"` or a tool name, and native
    structured output (which forces a tool), raise `SDKValidationError`.

## Per-model headers, extra body and query parameters

`OpenAIChatModel` takes constructor-level defaults for gateways and compatible servers that need them. They apply to both `generate()` and `stream()`:

```python
import asyncio

import httpx
from nexura_agents import Message, ModelSettings
from nexura_agents.models.openai import OpenAIChatModel

seen: list[httpx.Request] = []


def handler(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    )


model = OpenAIChatModel(
    "gpt-4o-mini",
    api_key="sk-demo",
    headers={"X-Team": "support"},
    extra_body={"user": "default-user", "metadata": {"tier": "free"}},
    query_params={"api-version": "2024-06-01"},
    transport=httpx.MockTransport(handler),
)
settings = ModelSettings(extra={"user": "customer-42"})
asyncio.run(model.generate([Message(role="user", content="hi")], model_settings=settings))
assert seen[0].headers["x-team"] == "support"
assert seen[0].url.params["api-version"] == "2024-06-01"
assert b'"customer-42"' in seen[0].content  # per-call extra wins over extra_body
assert b'"tier"' in seen[0].content
```

- `headers` cannot set `Content-Type`, `Accept` or the framing headers `Content-Length`, `Host`, `Transfer-Encoding` and `Connection`, and cannot set `Authorization` when `api_key` is given. Without an `api_key` you may send your own `Authorization` header. Names must be HTTP tokens, used once regardless of letter case, and values a single line. A refused header raises `SDKValidationError` at construction.
- `extra_body` cannot contain `model`, `messages`, `tools`, `stream`, `stream_options` or `response_format`, and must be JSON-serialisable; otherwise `SDKValidationError` is raised at construction. `headers`, `extra_body` and `query_params` must be dicts, and are copied, so changing yours later does not change what is sent.
- Body precedence, highest first: fields the SDK sets, `ModelSettings` fields and per-call `ModelSettings.extra`, then constructor `extra_body`.
- Error messages name the offending keys, never header values. Header values are not logged, shown in `repr()` or added to traces; `repr()` lists header names only.
- `LiteLLMModel` already forwards extra keyword arguments to `litellm.acompletion`, so use `extra_headers={...}` and `extra_body={...}` there.

## Forcing or forbidding tool calls (tool_choice)

`ModelSettings(tool_choice=...)` controls whether the model may, must or must not call a
tool:

```python
from nexura_agents import Agent, FakeModel, ModelSettings, Runner, ToolCall, tool


@tool
def lookup_order(order_id: str) -> str:
    """Find an order."""
    return f"order {order_id}: shipped"


model = FakeModel(
    responses=[[ToolCall(id="c1", name="lookup_order", arguments={"order_id": "A7"})], "Shipped."]
)
agent = Agent(
    name="Orders",
    instructions="Answer from the order system only.",
    model=model,
    tools=[lookup_order],
    model_settings=ModelSettings(tool_choice="lookup_order"),
)
assert Runner().run(agent, "Where is A7?").output == "Shipped."
assert model.calls[0]["model_settings"].tool_choice == "lookup_order"
assert model.calls[1]["model_settings"] is None  # reset to auto after the call
```

- `"auto"` is the model's default and may call tools. `"required"` means it must call
  some tool, and `"none"` that it must not. A tool's name means it must call that tool.
- After the agent has called a tool in a turn, the runner goes back to `"auto"` so the
  model can answer. Only the active agent's own calls count: after a handoff, the new
  agent starts with its configured choice, and this survives durable recovery. A forced choice would otherwise loop until `max_turns`. Pass
  `Agent(reset_tool_choice=False)` to keep the choice for every call. A new user turn
  (the next run) starts with the configured choice again.
- `"required"` or a tool name when the agent offers no tools, or a name the agent does
  not offer on this call (see `enabled` and `prepare_tools`), raises
  `SDKValidationError`. `"none"` without tools is simply dropped.
- Mapping: OpenAI, Ollama and LiteLLM send OpenAI's `tool_choice`. Gemini and Vertex AI
  send `toolConfig.functionCallingConfig` (`AUTO`/`ANY`/`NONE`, with
  `allowedFunctionNames` for one tool). Anthropic sends `tool_choice`
  (`auto`/`any`/`none`/`tool`). Sarvam has no tool support, so it does not apply. Custom
  models get it in `model_settings`.

## Reasoning tokens

Some models spend hidden "thinking" tokens before they answer. When the provider reports them, `Usage.reasoning_tokens` holds the count. It is read from OpenAI's `reasoning_tokens` and from Gemini and Vertex AI's `thoughtsTokenCount`. These tokens are already counted in `completion_tokens` (the Gemini adapter adds `thoughtsTokenCount` to the visible output count; OpenAI's completion count includes them), so do not add the two together. If the provider does not report them, the field is `None`, not zero.

The Anthropic and Sarvam adapters in this SDK do not fill `reasoning_tokens`. The SDK does not currently have a setting to turn on or size a model's reasoning; it only reports what the provider returns.

## Retry and fallback

Two wrappers add resilience. Both wrap anything with a `generate` method, so they can be nested and passed straight to an `Agent`.

`RetryModel(model, max_attempts=3, base_delay=0.5, max_delay=8.0, retry_on=...)` retries transient failures by default: `ProviderRateLimitError`, `ProviderRequestError`, `ProviderResponseError`, `ProviderServerError` (HTTP 5xx) and `TimeoutError`. The wait before retry `n` is `base_delay * 2**(n-1)`, capped at `max_delay`, with random jitter between 50% and 100% of that value. If the raised error has a numeric `retry_after` attribute, that value (capped at `max_delay`) is used instead. The built-in providers set `retry_after` on a 429 error, in seconds, when the response has a numeric `Retry-After` header (an HTTP-date value is ignored), so that delay is honored; without the header you get the exponential delay. `ProviderAuthError` is never retried, and neither are other 4xx errors (`ProviderClientError`). Every retry repeats the request, so it also repeats the token cost.

Both wrappers also stream, passing through the capabilities of the model they wrap. `RetryModel` retries a stream only if it fails before the first delta; once text has been yielded a retry would repeat it, so a later failure is raised as is. `FallbackModel` likewise moves to the next model only before the first delta. A `FallbackModel` streams only when every model it wraps can stream.

`FallbackModel(first, second, ...)` tries each model in order and moves on after any `ProviderError` or `TimeoutError`, except `ProviderAuthError`, which is raised at once so a bad key is not hidden by switching providers. `last_used` tells you which model answered. If every model fails, it raises `ProviderError` that lists each failure. Pass `fallback_on=(...)` to change what triggers a switch.

```python
from nexura_agents import Agent, FakeModel, FallbackModel, RetryModel
from nexura_agents.errors import ProviderRateLimitError

calls = {"primary": 0}


def flaky(messages, tools):
    calls["primary"] += 1
    if calls["primary"] < 3:
        raise ProviderRateLimitError("slow down (HTTP 429)")
    return "primary answer"


def always_down(messages, tools):
    raise ProviderRateLimitError("quota exhausted")


# Retry: fails twice, succeeds on the third attempt. Tiny delays keep the example fast.
retrying = RetryModel(FakeModel(handler=flaky), max_attempts=3, base_delay=0.001, max_delay=0.01)
agent = Agent(name="R", instructions="x", model=retrying)
assert agent.run("hi").final_output == "primary answer"
assert retrying.attempts_made == 3

# Fallback: the first model is down, so the second answers.
backup = FakeModel(responses=["backup answer"])
chain = FallbackModel(
    RetryModel(FakeModel(handler=always_down), max_attempts=2, base_delay=0.001, max_delay=0.01),
    backup,
)
agent = Agent(name="F", instructions="x", model=chain)
assert agent.run("hi").final_output == "backup answer"
assert chain.last_used is backup
```

### When the prompt outgrows the context window

A provider error that says the request is longer than the model's context window (OpenAI
`context_length_exceeded`, Anthropic "prompt is too long", Gemini "exceeds the maximum number
of tokens", xAI "maximum prompt length", llama.cpp and Ollama "exceeds the available context
size", and the same phrases from LiteLLM-backed providers) is raised as
`ProviderContextLengthError`. `LiteLLMModel` also recognises LiteLLM's own
`ContextWindowExceededError` class by name, whatever its message says. Streaming calls are
classified the same way: the providers read the error body of a failed stream before classifying it. It is a subclass of `ProviderClientError`, so it is never retried
unchanged and existing handlers still catch it. The providers read the start of the error body
only to recognise this case; the body is still not included in the message.

`RetryModel(model, trim_history=N)` recovers from it. It drops the oldest whole turn and calls
the model again, up to `N` times. A turn is a user message plus everything up to the next user
message, so a tool call is never separated from its result. System messages and the latest turn
are always kept. `turns_trimmed` counts what was dropped. Trimming is off by default
(`trim_history=0`), because dropping history changes what the model sees. Trimming applies when
the wrapper is called with a plain message list, which is how agents call it, for `generate()`
and for `stream()` alike. A stream trims and restarts only before its first delta; an overflow
raised after text was yielded is raised unchanged. In `FallbackModel(RetryModel(a, trim_history=N), b)`
the primary trims first and `b` is used only if trimming cannot fix it.

```python
from nexura_agents import Agent, FakeModel, Message, RetryModel
from nexura_agents.providers.errors import ProviderContextLengthError


def small_window(messages, tools):
    if sum(1 for m in messages if m.role == "user") > 2:
        raise ProviderContextLengthError("prompt is too long")
    return f"answered with {len(messages)} messages"


model = RetryModel(FakeModel(handler=small_window), trim_history=3)
history = []
for i in range(4):
    history += [Message(role="user", content=f"q{i}"), Message(role="assistant", content=f"a{i}")]
agent = Agent(name="T", instructions="x", model=model)
print(agent.run("latest", history=history).final_output)
assert model.turns_trimmed >= 1
```

## Rate limits and concurrency

`RateLimiter` is a client-side cap on model calls that you can share. `RateLimitedModel(model, limiter)` wraps a model so every `generate` and `stream` call goes through the limiter. Give the same limiter to several wrappers or agents and they are limited together, including runs started from different threads or by the sync `Agent.run`.

`RateLimiter(max_concurrent=None, requests_per_minute=None, tokens_per_minute=None)` takes any combination of three limits. All must be integers of at least 1 or `None` (no limit), otherwise it raises `SDKValidationError`.

- `max_concurrent`: at most this many calls at once. A stream counts as one call until it finishes, fails, is closed or is cancelled.
- `requests_per_minute`: at most this many calls start in any 60 second window. A burst up to the limit starts at once.
- `tokens_per_minute`: once the tokens reported in the last 60 seconds reach this number, new calls wait until older usage ages out. This is approximate. A call's tokens are only known after it finishes, so calls already running can push you past the budget, and a call whose provider reports no usage counts as 0.

Waiting uses no polling loop and respects `asyncio` cancellation: a call cancelled while it waits takes no slot and releases none. Put the limiter inside the retry wrapper, `RetryModel(RateLimitedModel(model, limiter))`, so each retry waits for its own slot. For tests, pass `clock=` (seconds, default `time.monotonic`) and `sleep=` (an awaitable, default `asyncio.sleep`).

```python
import asyncio

from nexura_agents import Agent, FakeModel, RetryModel
from nexura_agents import RateLimitedModel, RateLimiter

limiter = RateLimiter(max_concurrent=2)  # shared by every run below
model = RateLimitedModel(FakeModel(responses=["ok"], repeat_last=True), limiter)
agent = Agent(name="L", instructions="x", model=RetryModel(model))


async def main():
    results = await asyncio.gather(*(agent.run_async("hi") for _ in range(6)))
    assert all(r.final_output == "ok" for r in results)
    assert limiter.in_flight == 0


asyncio.run(main())
```

A common production chain wraps each live provider in its own `RetryModel` and joins them with `FallbackModel`:

```python notest
from nexura_agents import Agent, FallbackModel, RetryModel, infer_model

model = FallbackModel(
    RetryModel(infer_model("vertex:gemini-2.5-flash")),
    RetryModel(infer_model("anthropic:claude-sonnet-4-5")),
)
agent = Agent(name="Support", instructions="Be brief.", model=model)
```

Use the model IDs your account has access to; the ones above are examples.

## See also

- [Testing your agents](testing.md): `FakeModel` and scripted responses.
- [Budgets and usage](budgets-and-usage.md): token and cost accounting.
- [Images, documents and audio](multimodal.md): what each provider accepts as attachments.
- [Troubleshooting](troubleshooting.md): provider errors and fixes.

## Using a real model: Google Vertex AI

`VertexAIProvider` runs Gemini models on Vertex AI using your Google Cloud login, so no
API key is stored anywhere. It is the separate `nexura-vertex` package
([packages/nexura-vertex](../packages/nexura-vertex/README.md)). Any provider adapter can be an
agent's model.

```bash
pip install nexura-vertex
gcloud auth application-default login     # once per machine
gcloud config set project YOUR_PROJECT     # or pass project=...
```

```python notest
from nexura_vertex import VertexAIProvider

from nexura_runtime import Agent, Runner

model = VertexAIProvider("gemini-2.5-flash", location="global")  # project from gcloud
agent = Agent(name="Support", instructions="Be brief.", model=model)
print(Runner().run(agent, "Summarize our refund policy in one line.").final_output)
```

Live calls are billed to your Google Cloud project. The automated tests verify the
Vertex wire format offline with mock HTTP; they never call Google. The one live check
that has been run (by the owner, 2026-10-08) is listed in the [verification record](testing.md#verification-record).

Other providers work the same way as `Agent(model="openai:gpt-4.1-mini")` and similar
strings; `nexura-agents[litellm]` adds `"litellm:<provider>/<model>"` for others.

## Models

| String | Provider | Credentials |
|---|---|---|
| `"vertex:gemini-2.5-flash"` | Google Vertex AI | `gcloud auth application-default login` (`pip install nexura-vertex`) |
| `"gemini:gemini-2.5-flash"` | Gemini API | `GEMINI_API_KEY` |
| `"openai:gpt-4.1-mini"` | OpenAI or any OpenAI-compatible server | `OPENAI_API_KEY` |
| `"anthropic:claude-sonnet-4-5"` | Anthropic | `ANTHROPIC_API_KEY` |
| `"sarvam:sarvam-m"` | Sarvam AI (Indian languages) | `SARVAM_API_KEY` |
| `"ollama:llama3.1"` | Local Ollama | none |
| `"test"` | Offline fake | none |
| `"litellm:bedrock/..."` | Any provider LiteLLM supports | that provider's own (`[litellm]` extra) |

Or construct a provider directly (`from nexura_vertex import VertexAIProvider`;
`VertexAIProvider(project=..., location="asia-south1")`)
for full control. Anything with an async `generate()` method works as a model.

`LiteLLMModel` supports tool calls, attachments and usage, and leaves retries to
`RetryModel` and `FallbackModel`. It streams text and typed output, and it has only been checked
offline against LiteLLM's mock responses (`examples/live/litellm_offline_check.py`), not
against live providers.

## Package overview

**Google Vertex AI (Gemini) for Nexura agents, authenticated with your Google Cloud login.**

```bash
pip install nexura-vertex   # installs nexura-agents and google-auth too
gcloud auth application-default login
```

```python notest
from nexura_agents import Agent, Runner

agent = Agent(name="Support", instructions="Answer briefly.", model="vertex:gemini-2.5-flash")
print(Runner().run(agent, "What is a refund window?").final_output)
```

- **`"vertex:<model>"` model strings.** Once this package is installed, `Agent(model=...)` and
  `nexura_agents.infer_model` build a `VertexAIProvider` from them. Without it they fail with
  "'vertex:' models are provided by the 'nexura-vertex' package, which is not installed.
  Install it with: pip install nexura-vertex".
- **Application Default Credentials.** `gcloud auth application-default login`, a
  service-account key in `GOOGLE_APPLICATION_CREDENTIALS`, or the metadata server of a Google
  Cloud VM. The project comes from the credentials, `GOOGLE_CLOUD_PROJECT`, or `project=`.
  Pass `token_provider=` to supply access tokens yourself.
- **Same wire format as Gemini.** `VertexAIProvider` reuses the Gemini adapter (tools,
  streaming, structured output, multimodal parts, `gs://` file URLs) and changes only the
  endpoint, the OAuth bearer token and embeddings (`:predict`).
- Depends on `nexura-agents` only, not on `nexura-runtime`; Python 3.11 to 3.13.

> Status: **alpha** (0.4). Gemini 2.5 Flash on Vertex AI passed one live check run by the
> repository owner on 2026-10-08 (a plain reply, a tool call and streaming). Everything else is
> tested offline with mock HTTP.

## Offline example

A mock HTTP transport and a fixed token stand in for Google, so this runs without credentials.

```python
import httpx
from nexura_agents import Agent, Runner

from nexura_vertex import VertexAIProvider


def fake_vertex(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer test-token"
    assert "/projects/demo/locations/global/" in request.url.path
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "Hello"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        },
    )


model = VertexAIProvider(
    "gemini-2.5-flash",
    project="demo",
    token_provider=lambda: "test-token",
    transport=httpx.MockTransport(fake_vertex),
)
result = Runner().run(Agent(name="Hi", instructions="Greet.", model=model), "hi")
print(result.final_output)
```
