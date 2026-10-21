# Getting started

This page installs `nexura-agents`, builds a first agent that calls a tool, and shows how to run
it. Every runnable example uses `FakeModel`, a scripted model, so nothing here needs an API key or
network access.

## Install from source

From a checkout of this repository, using [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run python -c "import nexura_agents; print(nexura_agents.__version__)"
```

Or with pip, into any virtual environment (Python 3.11 or newer):

```bash
pip install -e packages/nexura-agents
```

The agent SDK depends only on `httpx` and `pydantic`. The platform package
(`nexura_runtime`: workflows, REST service, Studio) lives in `src/` and is not needed for the
examples on this page.

The platform package installs the command `nexura-runtime` (the service, worker, workflow,
red-team and evals CLI, plus `init`, `dev` and `deploy` for
[project templates](project-templates.md); run `nexura-runtime --help`); the separate
`nexura-studio` package adds `nexura-studio`, the older built-in Studio. The bare `nexura`
command belongs to the separate Nexura Cloud client package `nexura`, not to this project. See
[Service and Studio](studio-and-service.md#run-the-service).

## Your first agent

An agent is a name, instructions, a model and optional tools. A `FakeModel` takes a list of
responses and returns them in order, one per model call.

```python
from nexura_agents import Agent, FakeModel

agent = Agent(
    name="Greeter",
    instructions="Greet the user by name.",
    model=FakeModel(responses=["Hello, Asha!"]),
)

result = agent.run("My name is Asha.")
assert result.output == "Hello, Asha!"
assert result.status == "completed"
print(result.output)
```

`result` is a `RunResult`. `result.output` is the final answer (text, or a Pydantic object when
you set `output_type`; see [Structured output](structured-output.md)). `result.messages` holds the
whole conversation and `result.usage` the token counts.

## Add a tool

Decorate a plain function with `@tool`. The type hints become the argument schema the model
sees, and the docstring becomes the description.

To script a tool call, give `FakeModel` a list of `ToolCall` objects as one response. The agent
runs the tool, sends the result back to the model, and the next scripted response is the answer.

```python
from nexura_agents import Agent, FakeModel, ToolCall, tool


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


model = FakeModel(
    responses=[
        [ToolCall(id="call_1", name="add", arguments={"a": 2, "b": 3})],
        "2 + 3 = 5",
    ]
)
agent = Agent(name="Calculator", instructions="Use tools for arithmetic.", model=model, tools=[add])

result = agent.run("What is 2 + 3?")
assert result.output == "2 + 3 = 5"
assert result.tool_calls_count == 1
assert result.turns_count == 2
```

## run and run_async

`agent.run(prompt)` is synchronous. It starts its own event loop with `asyncio.run`, so it
raises an error if you call it from code that is already inside a running loop. In async code
(a web handler, a notebook cell) use `await agent.run_async(prompt)`.

```python
import asyncio

from nexura_agents import Agent, FakeModel


def build() -> Agent:
    return Agent(name="Echo", instructions="Reply briefly.", model=FakeModel(responses=["pong"]))


async def main() -> None:
    result = await build().run_async("ping")
    assert result.output == "pong"


asyncio.run(main())
assert build().run("ping").output == "pong"
```

Both accept the same keyword arguments, such as `deps=`, `history=` and `timeout=`. For hooks,
usage limits, budgets or session storage, create a `Runner` and call `runner.run(agent, prompt)`
or `await runner.run_async(agent, prompt)`; `agent.run` is a shortcut for `Runner().run`.

## Use a real model

Pass a `"provider:model"` string instead of a model object. Nexura builds the matching provider
from it. These need credentials and network access, so they are not run by the docs tests.

```python notest
from nexura_agents import Agent

agent = Agent(
    name="Assistant",
    instructions="Be concise.",
    model="openai:gpt-4.1-mini",  # also "anthropic:...", "gemini:...", "vertex:...",
    # "sarvam:...", "ollama:llama3.1"
)
print(agent.run("Say hello.").output)
```

Supported prefixes are `openai`, `anthropic`, `gemini` (or `google`), `vertex`, `sarvam` and
`ollama`. Credentials and options for each are covered in [Models and providers](providers.md).

## See also

- [Agents](agents.md): instructions, dependencies, sessions, multi-agent patterns.
- [Testing your agents](testing.md): scripting `FakeModel` and asserting on results.
- [Tools](tools.md): function tools, typed dependencies, retries.
- [Structured output](structured-output.md): typed results.
- [Models and providers](providers.md): credentials for real models.

## Quickstart (keyless)

Runs offline with the deterministic `FakeModel`; no API keys.

```python
from nexura_runtime import Agent, FakeModel, Runner, ToolCall, tool


@tool
def lookup_order(order_id: str) -> dict[str, str]:
    """Look up order delivery status."""
    return {"order_id": order_id, "status": "Out for Delivery"}


agent = Agent(
    name="SupportBot",
    instructions="You are a helpful customer support agent.",
    model=FakeModel(
        responses=[
            [ToolCall(id="call_1", name="lookup_order", arguments={"order_id": "ORD-101"})],
            "Order ORD-101 is out for delivery.",
        ]
    ),
    tools=[lookup_order],
)

result = Runner().run(agent, "Where is my order ORD-101?")
print(result.tool_calls_count, result.final_output)
```

## Hello, agent

```python notest
from nexura_agents import Agent, tool


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


agent = Agent(
    name="Calculator",
    instructions="Use tools for arithmetic.",
    model="vertex:gemini-2.5-flash",  # or "openai:gpt-4.1-mini", "anthropic:...", ...
    tools=[add],
)
print(agent.run("What is 2 + 3?").output)
```

The same agent runs offline with a scripted model, which is how you test it:

```python
from nexura_agents import Agent, FakeModel, ToolCall, tool


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


model = FakeModel(
    responses=[
        [ToolCall(id="1", name="add", arguments={"a": 2, "b": 3})],  # turn 1: call the tool
        "2 + 3 = 5",  # turn 2: answer
    ]
)
agent = Agent(name="Calculator", instructions="Use tools.", model=model, tools=[add])
print(agent.run("What is 2 + 3?").output)  # 2 + 3 = 5
```
