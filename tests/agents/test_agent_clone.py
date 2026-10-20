"""agent.clone(**overrides) and agent.override(...) (F31)."""

from __future__ import annotations

import pytest
from nexura_agents import Agent, FakeModel, Memory, ModelSettings, Runner, ToolCall, tool
from nexura_agents.errors import SDKValidationError


@tool
def lookup(q: str) -> str:
    """Lookup."""
    return q


def base() -> Agent:
    return Agent(
        name="Support",
        instructions="Help.",
        model=FakeModel(responses=["hi"]),
        tools=[lookup],
        max_turns=4,
        model_settings=ModelSettings(temperature=0.2),
    )


def test_clone_copies_configuration_and_applies_overrides() -> None:
    original = base()
    copy = original.clone(name="Support EU", model_settings=ModelSettings(temperature=0.9))
    assert copy is not original
    assert copy.name == "Support EU" and original.name == "Support"
    assert copy.model_settings == ModelSettings(temperature=0.9)
    assert original.model_settings == ModelSettings(temperature=0.2)
    assert [t.name for t in copy.tools] == ["lookup"]
    assert copy.max_turns == 4 and copy.instructions == "Help."
    assert copy.model is original.model  # shared, not re-created


def test_clone_validates_like_a_new_agent() -> None:
    with pytest.raises(SDKValidationError, match="max_turns"):
        base().clone(max_turns=0)
    with pytest.raises(SDKValidationError, match="unknown Agent arguments: colour"):
        base().clone(colour="blue")


def test_clone_keeps_handoffs_added_later() -> None:
    billing = Agent(name="Billing", instructions="i", model=FakeModel())
    triage = Agent(name="Triage", instructions="i", model=FakeModel())
    triage.add_handoffs(billing)
    assert [h.agent.name for h in triage.clone().handoffs] == ["Billing"]
    assert triage.clone(handoffs=[]).handoffs == ()


def test_clone_with_memory_does_not_duplicate_memory_tools() -> None:
    from nexura_agents.memory import HashingEmbedder, InMemoryMemoryStore

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(),
        memory=Memory(InMemoryMemoryStore(HashingEmbedder())),
    )
    names = [t.name for t in agent.clone().tools]
    assert names.count("remember") == 1 and names.count("recall") == 1


async def test_override_swaps_the_model_and_restores_it() -> None:
    agent = base()
    real_model = agent.model
    fake = FakeModel(responses=[[ToolCall(id="c1", name="lookup", arguments={"q": "x"})], "faked"])
    with agent.override(model=fake) as same:
        assert same is agent
        result = await Runner().run_async(agent, "go")
    assert result.output == "faked"
    assert agent.model is real_model
    assert (await Runner().run_async(agent, "go")).output == "hi"


async def test_override_is_seen_through_handoffs_and_restored_after_an_error() -> None:
    billing = Agent(name="Billing", instructions="i", model=FakeModel(responses=["real"]))
    triage = Agent(
        name="Triage",
        instructions="i",
        model=FakeModel(
            responses=[[ToolCall(id="h1", name="transfer_to_billing", arguments={})]],
            repeat_last=True,
        ),
        handoffs=[billing],
    )
    with billing.override(model=FakeModel(responses=["fake billing"])):
        assert (await Runner().run_async(triage, "pay")).output == "fake billing"
    with pytest.raises(RuntimeError), billing.override(instructions="changed"):
        raise RuntimeError("test failure inside the block")
    assert billing.instructions == "i"


def test_override_validates_before_changing_anything() -> None:
    agent = base()
    with pytest.raises(SDKValidationError), agent.override(max_turns=-1):
        pass
    assert agent.max_turns == 4
