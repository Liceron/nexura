"""Regressions from the F52 review: retry keeps the turn on failure, delete keeps CAS safe."""

import asyncio
import contextlib

import pytest
from nexura_agents import Agent, FakeModel, MemorySessionStore, Runner, SQLiteSessionStore
from nexura_agents.errors import SessionConflictError
from nexura_agents.session import delete_session, retry_last_turn
from nexura_agents.types import Message


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemorySessionStore()
    else:
        s = SQLiteSessionStore(tmp_path / "s.db")
        yield s
        s.close_sync()


def test_retry_failure_loses_user_turn(store):
    """retry commits the pop before running; a failed run must not lose the user turn."""

    async def go():
        calls = {"n": 0}

        def handler(messages, tools):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise RuntimeError("provider down")
            return f"r{calls['n']}"

        agent = Agent(name="A", instructions="i", model=FakeModel(handler=handler))
        runner = Runner(session_store=store)
        await runner.run_async(agent, "first", session_id="s")
        await runner.run_async(agent, "second", session_id="s")
        with contextlib.suppress(Exception):
            await retry_last_turn(runner, agent, "s")
        msgs = (await store.get("s")).messages
        assert "second" in [m.content for m in msgs], "user turn 'second' lost after failed retry"

    asyncio.run(go())


def test_retry_cancel_loses_user_turn(store):
    """Cancellation during the re-run also leaves the session without the popped turn."""

    async def go():
        gate = asyncio.Event()
        calls = {"n": 0}

        class Slow(FakeModel):
            async def generate(self, *a, **k):
                calls["n"] += 1
                if calls["n"] >= 3:
                    await gate.wait()
                return await super().generate(*a, **k)

        agent = Agent(name="A", instructions="i", model=Slow(responses=["a", "b", "c"]))
        runner = Runner(session_store=store)
        await runner.run_async(agent, "first", session_id="s")
        await runner.run_async(agent, "second", session_id="s")
        task = asyncio.create_task(retry_last_turn(runner, agent, "s"))
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        msgs = (await store.get("s")).messages
        assert "second" in [m.content for m in msgs], "user turn lost after cancelled retry"

    asyncio.run(go())


def test_delete_recreate_defeats_cas(store):
    """delete resets revision to 0, so a stale revision can match a recreated session (ABA)."""

    async def go():
        m1 = [Message(role="user", content="old")]
        m2 = [Message(role="user", content="new")]
        await store.save("s", m1, expected_revision=0)
        stale = await store.get("s")  # revision 1
        await delete_session(store, "s")
        await store.save("s", m2, expected_revision=0)  # also revision 1
        with pytest.raises(SessionConflictError):
            await store.save("s", m1, expected_revision=stale.revision)

    asyncio.run(go())
