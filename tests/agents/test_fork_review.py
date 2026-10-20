"""Regressions from the review of fork_session (B15)."""

import asyncio
import tempfile

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemorySessionStore,
    Runner,
    SQLiteSessionStore,
    fork_session,
)
from nexura_agents.errors import SDKValidationError, SessionConflictError
from nexura_agents.session import SessionSnapshot
from nexura_agents.types import Message, ToolCall


def tc(i):
    return ToolCall(id=i, name="f", arguments={})


def run(c):
    return asyncio.run(c)


PAR = [
    Message(role="system", content="s"),
    Message(role="user", content="u"),
    Message(role="assistant", tool_calls=[tc("a"), tc("b")]),
    Message(role="tool", tool_call_id="a", content="1", name="f"),
    Message(role="tool", tool_call_id="b", content="2", name="f"),
    Message(role="assistant", content="done"),
]


async def seeded(store, msgs=PAR, state=None):
    kw = {"state": state} if state is not None else {}
    await store.save("src", msgs, expected_revision=0, **kw)


def test_parallel_mid_rejected_and_boundaries():
    async def go():
        s = MemorySessionStore()
        await seeded(s)
        for at in (3, 4):
            with pytest.raises(SDKValidationError):
                await fork_session(s, "src", f"x{at}", at=at)
        for at in (0, 2, 5, 6):
            snap = await fork_session(s, "src", f"y{at}", at=at)
            assert len(snap.messages) == at

    run(go())


def test_at0_then_run_gets_system_prompt():
    async def go():
        s = MemorySessionStore()
        await seeded(s)
        await fork_session(s, "src", "z", at=0)
        a = Agent(name="A", instructions="Be nice.", model=FakeModel(responses=["ok"]))
        await Runner(session_store=s).run_async(a, "hi", session_id="z")
        msgs = (await s.get("z")).messages
        assert msgs[0].role == "system"

    run(go())


def test_dangling_call_at_end_full_len_forkable():
    # at == len(messages) is documented as "keep all"; at=None works, at=len should too
    msgs = PAR[:3]

    async def go():
        s = MemorySessionStore()
        await seeded(s, msgs)
        await fork_session(s, "src", "all")
        await fork_session(s, "src", "all2", at=len(msgs))

    run(go())


def test_dangling_call_mid_blocks_all_later_forks():
    msgs = PAR[:3] + [
        Message(role="user", content="next"),
        Message(role="assistant", content="r"),
        Message(role="user", content="more"),
        Message(role="assistant", content="r2"),
    ]

    async def go():
        s = MemorySessionStore()
        await seeded(s, msgs)
        await fork_session(s, "src", "ok", at=len(msgs) - 2)

    run(go())


def test_orphan_tool_message_point():
    msgs = [
        Message(role="system", content="s"),
        Message(role="tool", tool_call_id="zz", content="x"),
        Message(role="user", content="u"),
    ]

    async def go():
        s = MemorySessionStore()
        await seeded(s, msgs)
        await fork_session(s, "src", "o", at=2)

    run(go())


def test_provider_data_attachments_roundtrip_and_isolation():
    msgs = [
        Message(role="system", content="s"),
        Message(role="assistant", content="x", provider_data={"thinking": [{"sig": "abc"}]}),
    ]

    async def go():
        for s in (MemorySessionStore(), SQLiteSessionStore(tempfile.mkdtemp() + "/s.db")):
            await s.save("src", msgs, expected_revision=0)
            f = await fork_session(s, "src", "f")
            assert f.messages[1].provider_data == {"thinking": [{"sig": "abc"}]}
            f.messages[1].provider_data["thinking"][0]["sig"] = "X"
            assert (await s.get("src")).messages[1].provider_data["thinking"][0]["sig"] == "abc"

    run(go())


def test_state_isolation_memory_and_sqlite():
    async def go():
        for s in (MemorySessionStore(), SQLiteSessionStore(tempfile.mkdtemp() + "/s.db")):
            await seeded(s, state={"a": {"b": [1]}})
            f = await fork_session(s, "src", "f")
            assert f.state == {"a": {"b": [1]}}
            f.state["a"]["b"].append(2)
            assert (await s.get("src")).state == {"a": {"b": [1]}}
            assert (await s.get("f")).state == {"a": {"b": [1]}}

    run(go())


class RefStore:
    """custom store keeping references, like a naive dict store"""

    def __init__(self):
        self.d = {}

    async def get(self, sid):
        e = self.d.get(sid)
        return None if e is None else SessionSnapshot(sid, e[0], e[1], e[2])

    async def save(self, session_id, messages, expected_revision, state=None):
        cur = self.d.get(session_id, (0, 0, {}))[1]
        if cur != expected_revision:
            raise SessionConflictError(
                session_id=session_id, expected_revision=expected_revision, actual_revision=cur
            )
        self.d[session_id] = (messages, cur + 1, state or {})
        return await self.get(session_id)

    async def close(self):
        pass


def test_custom_store_state_not_aliased():
    async def go():
        s = RefStore()
        await s.save("src", list(PAR), 0, state={"k": [1]})
        await fork_session(s, "src", "f")
        s.d["f"][2]["k"].append(2)
        assert s.d["src"][2] == {"k": [1]}

    run(go())


class KwStore(RefStore):
    async def save(self, session_id, messages, expected_revision, **kwargs):
        return await super().save(session_id, messages, expected_revision, kwargs.get("state"))


def test_kwargs_store_receives_state():
    async def go():
        s = KwStore()
        await s.save("src", list(PAR), 0, state={"k": 1})
        await fork_session(s, "src", "f")
        assert s.d["f"][2] == {"k": 1}

    run(go())


class KwOnlyStore(RefStore):
    async def save(self, session_id, messages, *, expected_revision, state=None):
        return await super().save(session_id, messages, expected_revision, state)


def test_kwonly_store_ok():
    async def go():
        s = KwOnlyStore()
        await s.save("src", list(PAR), expected_revision=0, state={"k": 1})
        await fork_session(s, "src", "f")
        assert s.d["f"][2] == {"k": 1}

    run(go())


def test_concurrent_forks_same_target_one_wins():
    async def go():
        s = MemorySessionStore()
        await seeded(s)
        r = await asyncio.gather(
            *[fork_session(s, "src", "t") for _ in range(5)], return_exceptions=True
        )
        assert sum(not isinstance(x, Exception) for x in r) == 1
        assert all(isinstance(x, SessionConflictError) for x in r if isinstance(x, Exception))

    run(go())


def test_fork_while_source_saving_is_consistent_and_fork_of_fork():
    async def go():
        s = MemorySessionStore()
        await seeded(s)

        async def saver():
            for i in range(20):
                cur = await s.get("src")
                await s.save(
                    "src", cur.messages + [Message(role="user", content=str(i))], cur.revision
                )

        await asyncio.gather(saver(), fork_session(s, "src", "f"))
        f = await s.get("f")
        assert f.revision == 1 and len(f.messages) >= 6
        g = await fork_session(s, "f", "g", at=2)
        assert g.revision == 1 and [m.role for m in g.messages] == ["system", "user"]
        assert (await s.get("f")).revision == 1

    run(go())


def test_fork_of_missing_and_bad_at():
    async def go():
        s = MemorySessionStore()
        await seeded(s)
        for bad in (-1, 7, True, 2.0, "2"):
            with pytest.raises(SDKValidationError):
                await fork_session(s, "src", "n", at=bad)
        with pytest.raises(SDKValidationError):
            await fork_session(s, "nope", "n")
        assert await s.get("n") is None

    run(go())


def test_sqlite_fork_existing_target_conflict_and_no_overwrite():
    async def go():
        s = SQLiteSessionStore(tempfile.mkdtemp() + "/s.db")
        await seeded(s)
        await s.save("t", PAR[:2], expected_revision=0)
        with pytest.raises(SessionConflictError):
            await fork_session(s, "src", "t")
        assert len((await s.get("t")).messages) == 2

    run(go())
