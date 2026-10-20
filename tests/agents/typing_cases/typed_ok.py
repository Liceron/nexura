"""Static typing cases for typed results; checked by mypy in test_typed_results.py."""

from typing import Any, assert_type

from nexura_agents import Agent, FakeModel, RunContext, Runner
from nexura_agents.types import RunResult
from pydantic import BaseModel


class Invoice(BaseModel):
    number: str
    total: float


class Support(Agent[Invoice]):
    """A subclass keeps the parameter."""


model = FakeModel(responses=["{}"])

typed = Agent(name="a", instructions="x", model=model, output_type=Invoice)
assert_type(typed, Agent[Invoice])
assert_type(typed.run("hi"), RunResult[Invoice])
assert_type(typed.run("hi").output, Invoice)
assert_type(Runner().run(typed, "hi").output, Invoice)
assert_type(typed.clone(name="b"), Agent[Invoice])  # clone keeps the output type

plain = Agent(name="p", instructions="x", model=model)
assert_type(plain, Agent[str])
assert_type(Runner().run(plain, "hi"), RunResult[str])
assert_type(Runner().run(plain, "hi").output, str)

schema = Agent(name="d", instructions="x", model=model, output_type={"type": "object"})
assert_type(schema.run("hi").output, dict[str, Any])


async def later() -> None:
    result = await Runner().run_async(typed, "hi")
    assert_type(result.output, Invoice)
    assert_type(result.final_output, str)


def instructions(ctx: RunContext[int]) -> str:
    return str(ctx.deps)


with_deps = Agent(name="c", instructions=instructions, model=model, output_type=Invoice)
assert_type(with_deps.run("hi", deps=1).output, Invoice)

# Teams of mixed agents: handoffs and as_tool accept any output type.
lead = Agent(name="lead", instructions="x", model=model, handoffs=[typed, plain])
lead_tools = Agent(name="l2", instructions="x", model=model, tools=[typed.as_tool()])
with typed.override(model=model) as inside:
    inside.run("hi")


def accepts_any(agent: Agent) -> None:
    Runner().run(agent, "hi")


accepts_any(typed)
accepts_any(plain)
