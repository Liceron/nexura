"""VectorMemoryStore: the agents MemoryStore protocol on the platform's vector store."""

from __future__ import annotations

import json

import pytest
from nexura_agents import Agent, FakeModel, HashingEmbedder, Memory, MemoryStore, ToolCall
from nexura_agents.errors import SDKValidationError

from nexura_runtime.knowledge.vector.store import InMemoryVectorStore
from nexura_runtime.memory.vector_memory import VectorMemoryStore


def make() -> tuple[VectorMemoryStore, InMemoryVectorStore]:
    vectors = InMemoryVectorStore()
    return VectorMemoryStore(vectors, HashingEmbedder(), workspace_id="ws1"), vectors


async def test_implements_the_protocol_and_round_trips() -> None:
    store, _ = make()
    assert isinstance(store, MemoryStore)
    rec = await store.add("alice", "The user likes green tea", {"source": "chat"})
    assert rec.namespace == "alice" and rec.metadata == {"source": "chat"}
    hits = await store.search("alice", "which tea does the user like")
    assert [h.id for h in hits] == [rec.id]
    assert hits[0].score is not None and hits[0].score > 0
    assert hits[0].metadata == {"source": "chat"} and hits[0].created_at == pytest.approx(
        rec.created_at, abs=1e-3
    )
    json.dumps(hits[0].to_dict())


async def test_namespaces_isolated_including_delete() -> None:
    store, vectors = make()
    a = await store.add("alice", "alice secret blue")
    await store.add("bob", "bob secret red")
    assert [h.text for h in await store.search("alice", "secret")] == ["alice secret blue"]
    assert [h.text for h in await store.search("bob", "secret")] == ["bob secret red"]
    assert not await store.search("carol", "secret")
    assert await store.delete("bob", a.id) is False
    assert len(await store.list("alice")) == 1
    assert await store.delete("alice", a.id) is True
    assert await store.delete("alice", a.id) is False
    assert not await store.list("alice")
    # A different workspace sees nothing either.
    other = VectorMemoryStore(vectors, HashingEmbedder(), workspace_id="ws2")
    assert not await other.search("bob", "secret")


async def test_namespace_text_cannot_collide_or_escape() -> None:
    store, _ = make()
    await store.add("a/b", "slash namespace fact")
    await store.add("a_b", "underscore namespace fact")
    assert [h.text for h in await store.search("a/b", "namespace fact")] == ["slash namespace fact"]
    assert [h.text for h in await store.search("a_b", "namespace fact")] == [
        "underscore namespace fact"
    ]


async def test_limit_min_score_and_list_order() -> None:
    store, _ = make()
    for text in ("tea with milk", "tea with honey", "tea with lemon", "football scores"):
        await store.add("u", text)
    assert len(await store.search("u", "tea", limit=2)) == 2
    assert not await store.search("u", "tea", min_score=0.99)
    listed = await store.list("u", limit=3)
    assert len(listed) == 3
    assert [r.created_at for r in listed] == sorted((r.created_at for r in listed), reverse=True)
    with pytest.raises(SDKValidationError):
        await store.search("u", "tea", limit=0)


async def test_validation() -> None:
    store, _ = make()
    with pytest.raises(SDKValidationError):
        await store.add("", "x")
    with pytest.raises(SDKValidationError):
        await store.add("u", " ")
    with pytest.raises(SDKValidationError):
        VectorMemoryStore(object(), HashingEmbedder())  # type: ignore[arg-type]


async def test_works_end_to_end_with_an_agent() -> None:
    store, _ = make()
    memory = Memory(store, namespace=lambda ctx: ctx.deps, auto_recall=True)
    first = FakeModel(
        responses=[
            [ToolCall(id="c", name="remember", arguments={"text": "User likes oolong tea"})],
            "ok",
        ]
    )
    await Agent("A", "Base.", first, memory=memory).run_async("I like oolong", deps="u1")
    second = FakeModel(responses=["oolong"])
    await Agent("A", "Base.", second, memory=memory).run_async("which tea do I like", deps="u1")
    system = "".join(m.content or "" for m in second.calls[0]["messages"] if m.role == "system")
    assert "User likes oolong tea" in system
    third = FakeModel(responses=["?"])
    await Agent("A", "Base.", third, memory=memory).run_async("which tea do I like", deps="u2")
    assert "oolong" not in "".join(m.content or "" for m in third.calls[0]["messages"])
