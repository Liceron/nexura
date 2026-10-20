"""Memory lifecycle: expiry (TTL), pruning and near-duplicate consolidation. Offline only."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    HashingEmbedder,
    InMemoryMemoryStore,
    Memory,
    MemoryRecord,
    RunContext,
    ToolCall,
)
from nexura_agents.errors import SDKValidationError


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TableEmbedder:
    """Maps exact texts to chosen vectors so cosine scores are exact."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.table[t] for t in texts]


class NaiveStore:
    """A custom store that knows nothing about expiry and keeps no metadata filtering."""

    def __init__(self) -> None:
        self.records: list[MemoryRecord] = []
        self.deleted: list[str] = []

    async def add(
        self, namespace: str, text: str, metadata: dict[str, Any] | None = None
    ) -> MemoryRecord:
        record = MemoryRecord(
            id=f"id{len(self.records)}",
            text=text,
            namespace=namespace,
            metadata=dict(metadata or {}),
        )
        self.records.append(record)
        return record

    async def search(
        self, namespace: str, query: str, limit: int = 5, min_score: float | None = None
    ) -> list[MemoryRecord]:
        return [r for r in self.records if r.namespace == namespace][:limit]

    async def delete(self, namespace: str, id: str) -> bool:
        before = len(self.records)
        self.records = [r for r in self.records if not (r.namespace == namespace and r.id == id)]
        self.deleted.append(id)
        return len(self.records) < before

    async def list(self, namespace: str, limit: int = 100) -> list[MemoryRecord]:
        return [r for r in self.records if r.namespace == namespace][:limit]


def ctx() -> RunContext[Any]:
    return RunContext(deps=None, run_id="r", agent_name="A")


# ------------------------------------------------------------------ expiry


async def test_expired_hidden_from_search_and_list_then_pruned() -> None:
    clock = Clock()
    store = InMemoryMemoryStore(HashingEmbedder(), clock=clock)
    memory = Memory(store, namespace="u", ttl=60, clock=clock)
    record, _ = await memory.save("u", "likes green tea")
    assert record.expires_at == 1060.0
    await store.add("u", "permanent fact about tea")  # no expiry

    clock.now = 1059.9
    assert len(await store.list("u")) == 2
    clock.now = 1060.0  # the boundary counts as expired
    assert [r.text for r in await store.list("u")] == ["permanent fact about tea"]
    assert [r.text for r in await store.search("u", "green tea")] == ["permanent fact about tea"]

    assert await memory.prune() == 1
    assert await memory.prune() == 0


async def test_store_prune_namespaces_and_return_count() -> None:
    clock = Clock()
    store = InMemoryMemoryStore(HashingEmbedder(), clock=clock)
    await store.add("a", "one", expires_at=1010)
    await store.add("a", "two", expires_at=5000)
    await store.add("b", "three", expires_at=1010)
    clock.now = 2000
    assert await store.prune("a") == 1
    assert await store.prune() == 1  # the rest of the namespaces
    assert [r.text for r in await store.list("a")] == ["two"]
    assert await store.list("b") == []
    with pytest.raises(SDKValidationError):
        await store.prune("  ")
    with pytest.raises(SDKValidationError):
        await store.add("a", "bad", expires_at=float("nan"))


async def test_exact_duplicate_refreshes_expiry_and_expired_text_can_be_saved_again() -> None:
    clock = Clock()
    store = InMemoryMemoryStore(HashingEmbedder(), clock=clock)
    first = await store.add("u", "same", expires_at=1100)
    again = await store.add("u", "same", expires_at=1500)
    assert again.id == first.id and again.expires_at == 1500
    clock.now = 1600
    fresh = await store.add("u", "same", expires_at=1700)
    assert fresh.id != first.id
    assert len(await store.list("u")) == 1


def test_records_without_expires_at_load_and_roundtrip() -> None:
    legacy = {
        "id": "1",
        "text": "x",
        "namespace": "n",
        "metadata": {"a": 1},
        "created_at": 5.0,
        "score": None,
    }
    record = MemoryRecord.from_dict(legacy)
    assert record.expires_at is None
    assert MemoryRecord.from_dict(record.to_dict()) == record
    assert MemoryRecord.from_dict({**legacy, "expires_at": 9}).expires_at == 9.0


async def test_custom_store_without_expiry_support_still_works_and_is_filtered() -> None:
    clock = Clock()
    store = NaiveStore()
    memory = Memory(store, namespace="u", ttl=10, clock=clock)
    record, _ = await memory.save("u", "short lived")
    assert record.metadata["expires_at"] == 1010.0  # carried in metadata
    clock.now = 1005
    block, _ = await memory.recall_block(ctx(), "short")
    assert "short lived" in block
    clock.now = 1010
    block, _ = await memory.recall_block(ctx(), "short")
    assert block == ""
    assert memory.live(await store.search("u", "x")) == []
    assert await memory.prune("u") == 1  # fallback: list + delete
    assert store.records == []


async def test_prune_fallback_needs_a_namespace_for_function_namespaces() -> None:
    memory = Memory(NaiveStore(), namespace=lambda c: "x")
    with pytest.raises(SDKValidationError):
        await memory.prune()


async def test_recall_tool_skips_expired_records_from_naive_store() -> None:
    clock = Clock()
    store = NaiveStore()
    memory = Memory(store, namespace="u", ttl=30, clock=clock)
    await memory.save("u", "likes oolong tea")
    await store.add("u", "plain permanent tea fact")

    async def run_recall() -> str:
        model = FakeModel(
            responses=[[ToolCall(id="1", name="recall", arguments={"query": "tea"})], "ok"]
        )
        agent = Agent("A", "x", model, memory=memory)
        result = await agent.run_async("go")
        return next(m.content or "" for m in result.messages if m.role == "tool")

    assert "oolong" in await run_recall()
    clock.now += 31
    out = await run_recall()
    assert "oolong" not in out and "permanent" in out


def test_ttl_and_clock_validation() -> None:
    store = InMemoryMemoryStore(HashingEmbedder())
    for bad in (0, -1, True, "5", float("inf")):
        with pytest.raises(SDKValidationError):
            Memory(store, ttl=bad)  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError):
        Memory(store, clock="now")  # type: ignore[arg-type]


# ------------------------------------------------------------------ consolidation


def table_store() -> tuple[InMemoryMemoryStore, Memory]:
    embedder = TableEmbedder(
        {
            "base": [1.0, 0.0],
            "close": [3.0, 4.0],  # cosine with base is exactly 3/5
            "far": [0.0, 1.0],
        }
    )
    store = InMemoryMemoryStore(embedder)
    return store, Memory(store, namespace="n", dedupe_threshold=0.6)


async def test_dedupe_replaces_at_the_threshold_boundary() -> None:
    store, memory = table_store()
    first, _ = await memory.save("n", "base")
    second, replaced = await memory.save("n", "close")  # score 0.6 == threshold
    assert replaced == first.id and second.id != first.id
    assert [r.text for r in await store.list("n")] == ["close"]


async def test_dedupe_adds_just_below_the_threshold() -> None:
    store = InMemoryMemoryStore(TableEmbedder({"base": [1.0, 0.0], "close": [3.0, 4.0]}))
    memory = Memory(store, namespace="n", dedupe_threshold=0.6000001)
    await memory.save("n", "base")
    _, replaced = await memory.save("n", "close")
    assert replaced is None
    assert len(await store.list("n")) == 2


async def test_dedupe_default_none_keeps_adding() -> None:
    store = InMemoryMemoryStore(HashingEmbedder())
    memory = Memory(store, namespace="n")
    await memory.save("n", "The user likes green tea a lot")
    await memory.save("n", "The user likes green tea very much")
    assert len(await store.list("n")) == 2


async def test_dedupe_refreshes_expiry_and_keeps_newer_text() -> None:
    clock = Clock()
    embedder = TableEmbedder({"base": [1.0, 0.0], "close": [3.0, 4.0]})
    store = InMemoryMemoryStore(embedder, clock=clock)
    memory = Memory(store, namespace="n", ttl=100, dedupe_threshold=0.5, clock=clock)
    await memory.save("n", "base")
    clock.now = 1090
    record, replaced = await memory.save("n", "close")
    assert replaced is not None and record.expires_at == 1190.0
    clock.now = 1150  # the old record would have expired at 1100
    assert [r.text for r in await store.list("n")] == ["close"]


async def test_dedupe_identical_text_keeps_one_record() -> None:
    store, memory = table_store()
    first, _ = await memory.save("n", "base")
    again, replaced = await memory.save("n", "base")
    assert again.id == first.id and replaced == first.id
    assert len(await store.list("n")) == 1


async def test_dedupe_ignores_expired_matches_and_other_namespaces() -> None:
    clock = Clock()
    embedder = TableEmbedder({"base": [1.0, 0.0], "close": [3.0, 4.0]})
    store = InMemoryMemoryStore(embedder, clock=clock)
    memory = Memory(store, namespace="n", dedupe_threshold=0.5, clock=clock)
    await store.add("n", "base", expires_at=1050)
    other = await store.add("other", "base")
    clock.now = 1100
    _, replaced = await memory.save("n", "close")
    assert replaced is None  # the match had expired
    _, replaced = await memory.save("n2", "close")
    assert replaced is None  # n2 is empty
    assert [r.id for r in await store.list("other")] == [other.id]  # untouched


async def test_namespaces_stay_isolated_with_ttl_and_dedupe() -> None:
    clock = Clock()
    store = InMemoryMemoryStore(HashingEmbedder(), clock=clock)
    memory = Memory(
        store, namespace=lambda c: str(c.deps), ttl=10, dedupe_threshold=0.8, clock=clock
    )
    a = RunContext(deps="a", run_id="r", agent_name="A")
    b = RunContext(deps="b", run_id="r", agent_name="A")
    await memory.save(memory.namespace_for(a), "the user likes green tea")
    await memory.save(memory.namespace_for(b), "the user likes green tea")
    assert len(await store.list("a")) == 1 and len(await store.list("b")) == 1
    clock.now += 11
    assert await memory.prune("a") == 1
    assert await store.list("a") == []
    assert await store.list("b") == []  # hidden, still stored
    assert await store.prune() == 1


async def test_remember_tool_reports_update() -> None:
    store, memory = table_store()
    model = FakeModel(
        responses=[
            [ToolCall(id="1", name="remember", arguments={"text": "base"})],
            [ToolCall(id="2", name="remember", arguments={"text": "close"})],
            "done",
        ]
    )
    result = await Agent("A", "x", model, memory=memory).run_async("go")
    tool_msgs = [m.content or "" for m in result.messages if m.role == "tool"]
    assert '"status": "remembered"' in tool_msgs[0]
    assert '"status": "updated"' in tool_msgs[1] and '"replaced"' in tool_msgs[1]
    assert [r.text for r in await store.list("n")] == ["close"]


def test_dedupe_threshold_validation() -> None:
    store = InMemoryMemoryStore(HashingEmbedder())
    for bad in (0, -0.1, 1.5, True, "0.9"):
        with pytest.raises(SDKValidationError):
            Memory(store, dedupe_threshold=bad)  # type: ignore[arg-type]
    assert Memory(store, dedupe_threshold=1).dedupe_threshold == 1
