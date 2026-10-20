"""Shared run state: RunContext.state, saved with sessions, interruptions and checkpoints (F37)."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemoryCheckpointStore,
    MemorySessionStore,
    RunContext,
    Runner,
    SQLiteSessionStore,
    ToolCall,
    tool,
)
from nexura_agents.errors import StorageSerializationError


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


@tool
def add_to_cart(ctx: RunContext[Any], item: str) -> str:
    """Add an item to the cart."""
    cart = ctx.state.setdefault("cart", [])
    cart.append(item)
    return f"cart: {', '.join(cart)}"


def shop(responses: list[Any]) -> Agent:
    return Agent(
        name="Shop",
        instructions=lambda ctx: f"Cart has {len(ctx.state.get('cart', []))} items.",
        model=FakeModel(responses=responses),
        tools=[add_to_cart],
    )


async def test_tools_write_state_and_the_result_returns_it() -> None:
    result = await Runner().run_async(
        shop([call("add_to_cart", item="pen"), call("add_to_cart", "c2", item="ink"), "ok"]),
        "buy",
    )
    assert result.state == {"cart": ["pen", "ink"]}


async def test_initial_state_and_instructions_reading_it() -> None:
    agent = shop(["ok"])
    model = agent.model
    result = await Runner().run_async(agent, "hi", state={"cart": ["book"]})
    assert model.calls[0]["messages"][0].content == "Cart has 1 items."  # type: ignore[attr-defined]
    assert result.state == {"cart": ["book"]}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_state_is_saved_with_the_session(kind: str, tmp_path: Path) -> None:
    store: Any = MemorySessionStore() if kind == "memory" else SQLiteSessionStore(tmp_path / "s.db")
    runner = Runner(session_store=store)
    await runner.run_async(shop([call("add_to_cart", item="pen"), "ok"]), "a", session_id="u1")
    second = await runner.run_async(
        shop([call("add_to_cart", item="ink"), "ok"]), "b", session_id="u1"
    )
    assert second.state == {"cart": ["pen", "ink"]}
    snapshot = await store.get("u1")
    assert snapshot is not None and snapshot.state == {"cart": ["pen", "ink"]}
    if kind == "sqlite":
        await store.close()


async def test_state_survives_an_approval_pause_and_json() -> None:
    from nexura_agents import RunInterruption

    @tool(requires_approval=True)
    def checkout(ctx: RunContext[Any]) -> str:
        """Checkout."""
        return f"bought {ctx.state['cart']}"

    agent = Agent(
        name="Shop",
        instructions="i",
        model=FakeModel(
            responses=[call("add_to_cart", item="pen"), call("checkout", "c2"), "done"]
        ),
        tools=[add_to_cart, checkout],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "buy")
    assert paused.status == "paused" and paused.state == {"cart": ["pen"]}
    restored = RunInterruption.from_json(paused.interruption.to_json())  # type: ignore[union-attr]
    done = await runner.resume_async(agent, restored, approve=restored.pending_digests())
    assert done.output == "done"
    assert "bought ['pen']" in str([m.content for m in done.messages])


async def test_state_survives_durable_recovery() -> None:
    class Crash(BaseException):
        pass

    def crash_on_second(messages: Any, tools: Any) -> Any:
        if any(m.role == "tool" for m in messages):
            raise Crash
        return call("add_to_cart", item="pen")

    store = MemoryCheckpointStore()
    first = shop([])
    first.model = FakeModel(handler=crash_on_second)
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(first, "buy")
    (run_id,) = await store.list_incomplete()
    result = await Runner(checkpoint_store=store).recover_async(shop(["ok"]), run_id)
    assert result.state == {"cart": ["pen"]}


async def test_unserialisable_state_fails_clearly_when_saved() -> None:
    @tool
    def bad(ctx: RunContext[Any]) -> str:
        """Bad."""
        ctx.state["conn"] = object()
        return "ok"

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[call("bad"), "x"]), tools=[bad]
    )
    with pytest.raises(StorageSerializationError, match="JSON-serialisable"):
        await Runner(session_store=MemorySessionStore()).run_async(agent, "go", session_id="s")


async def test_a_store_without_state_support_warns_but_works() -> None:
    from nexura_agents.session import SessionSnapshot

    class OldStore:
        def __init__(self) -> None:
            self.saved: list[Any] = []

        async def get(self, session_id: str) -> SessionSnapshot | None:
            return None

        async def save(
            self, session_id: str, messages: list[Any], expected_revision: int
        ) -> SessionSnapshot:
            self.saved.append(messages)
            return SessionSnapshot(session_id=session_id, messages=messages, revision=1)

        async def close(self) -> None:
            return None

    store = OldStore()
    with pytest.warns(RuntimeWarning, match="state is not saved"):
        await Runner(session_store=store).run_async(  # type: ignore[arg-type]
            shop([call("add_to_cart", item="pen"), "ok"]), "a", session_id="s"
        )
    assert store.saved
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no state -> no warning
        await Runner(session_store=OldStore()).run_async(  # type: ignore[arg-type]
            Agent(name="A", instructions="i", model=FakeModel(responses=["x"])),
            "a",
            session_id="s",
        )


async def test_state_is_shared_across_handoffs() -> None:
    seen: list[Any] = []

    def billing_rules(ctx: RunContext[Any]) -> str:
        seen.append(dict(ctx.state))
        return "Bill."

    billing = Agent(name="Billing", instructions=billing_rules, model=FakeModel(responses=["paid"]))
    triage = Agent(
        name="Triage",
        instructions="i",
        model=FakeModel(
            responses=[
                call("add_to_cart", item="pen"),
                [ToolCall(id="h", name="transfer_to_billing", arguments={})],
            ]
        ),
        tools=[add_to_cart],
        handoffs=[billing],
    )
    result = await Runner().run_async(triage, "buy")
    assert seen == [{"cart": ["pen"]}] and result.state == {"cart": ["pen"]}


async def test_recovering_a_completed_checkpoint_returns_the_state() -> None:
    class LoseDelete(MemoryCheckpointStore):
        """A crash after the "completed" checkpoint, before its cleanup."""

        async def delete(self, run_id: str, *args: Any, **kwargs: Any) -> None:
            return None

    store = LoseDelete()
    agent = shop([call("add_to_cart", item="pen"), "done"])
    runner = Runner(checkpoint_store=store)
    first = await runner.run_async(agent, "buy", state={"user": "u1"})
    assert first.state == {"user": "u1", "cart": ["pen"]}
    again = await runner.recover_async(shop([]), first.run_id)
    assert again.status == "completed"
    assert again.state == {"user": "u1", "cart": ["pen"]}
