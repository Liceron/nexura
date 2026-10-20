"""Regression tests for the V3 review findings (caps, flow isolation, coverage, server option)."""

from __future__ import annotations

import contextlib
import json
import tracemalloc
import uuid
from pathlib import Path
from typing import Any

from nexura_agents import FakeModel, MemorySessionStore, ToolCall

from nexura_runtime.integrations.langflow import V3Options, build, load, registry
from nexura_runtime.integrations.langflow import components as comp
from nexura_runtime.integrations.sql import SqlConnectionConfig, SqlConnectionManager

FIX = Path(__file__).parent / "fixtures"


def doc(name: str) -> dict[str, Any]:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def mgr() -> SqlConnectionManager:
    m = SqlConnectionManager()
    m.register_connection(
        SqlConnectionConfig(
            connection_id="db",
            workspace_id="*",
            database_path=f"file:r_{uuid.uuid4().hex}?mode=memory&cache=shared",
            read_only=True,
            initial_sql="CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT);"
            "INSERT INTO t VALUES (1, 'aaaaaaaaaa');INSERT INTO t VALUES (2, 'bbbbbbbbbb');",
        )
    )
    return m


async def test_sql_result_cap_applies_before_materialising() -> None:
    opts = V3Options(sql_manager=mgr(), sql_connection="db", sql_max_bytes=1000)
    tracemalloc.start()
    try:
        try:
            await comp.run_sql(opts, "SELECT zeroblob(20000000) AS b")
        except comp.ComponentRefusedError:
            pass
        else:
            raise AssertionError("oversized blob was not refused")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000, f"python-side peak {peak} bytes before the 1000 byte cap refused"


async def test_sql_cap_stops_running_total_and_small_results_still_work() -> None:
    small = V3Options(sql_manager=mgr(), sql_connection="db", sql_max_bytes=100_000)
    out = await comp.run_sql(
        small, "SELECT 1 AS id, 'aaaaaaaaaa' AS s UNION ALL SELECT 2, 'bbbbbbbbbb'"
    )
    assert out["row_count"] == 2 and not out["truncated"]
    tight = V3Options(sql_manager=small.sql_manager, sql_connection="db", sql_max_bytes=60)
    try:
        await comp.run_sql(
            tight, "SELECT 1 AS id, 'aaaaaaaaaa' AS s UNION ALL SELECT 2, 'bbbbbbbbbb'"
        )
    except comp.ComponentRefusedError as exc:
        assert "byte limit" in str(exc)
    else:
        raise AssertionError("running total over the cap was not refused")
    # the connection limit is restored, so a later larger query on the same pool works
    again = await comp.run_sql(small, "SELECT replace(hex(zeroblob(2000)), '0', 'ab') AS b")
    assert again["row_count"] == 1


async def test_file_node_multiple_paths_have_an_aggregate_cap(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text("x" * 600_000, encoding="utf-8")
    built = build(
        load(doc("v3_file_output"), extended=True),
        tweaks={"File-f1": {"path": ["f0.txt", "f1.txt", "f2.txt"]}},
        v3=V3Options(files_root=root, max_file_bytes=1_000_000),
    )
    try:
        await built.run_async("x")
    except comp.ComponentRefusedError as exc:
        assert "aggregate" in str(exc)
    else:
        raise AssertionError("aggregate file cap not enforced")


async def test_file_node_within_aggregate_cap_and_path_count_cap(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text("y" * 100, encoding="utf-8")
    imp = load(doc("v3_file_output"), extended=True)
    ok = build(
        imp,
        tweaks={"File-f1": {"path": ["f0.txt", "f1.txt", "f2.txt"]}},
        v3=V3Options(files_root=root),
    )
    assert len((await ok.run_async("x")).output) == 100 * 3 + 4
    many = build(
        imp,
        tweaks={"File-f1": {"path": ["f0.txt", "f1.txt", "f2.txt"]}},
        v3=V3Options(files_root=root, max_file_paths=2),
    )
    try:
        await many.run_async("x")
    except comp.ComponentRefusedError as exc:
        assert "path limit" in str(exc)
    else:
        raise AssertionError("path count cap not enforced")


async def test_split_text_bound_checked_after_building_all_chunks() -> None:
    d = doc("v3_split_loop")
    imp = load(d, extended=True)
    split_id = next(n for n, v in imp.nodes.items() if v.kind == registry.SPLIT_TEXT)
    built = build(
        imp,
        tweaks={split_id: {"chunk_size": 1, "chunk_overlap": 0, "separator": ""}},
        v3=V3Options(max_split_chunks=10),
    )
    tracemalloc.start()
    try:
        with contextlib.suppress(Exception):
            await built.run_async("a" * 300_000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000, f"peak {peak} bytes while refusing a >10 chunk split"


async def test_memory_tool_does_not_cross_flows_sharing_a_store() -> None:
    store = MemorySessionStore()
    a = doc("v3_memory_agent")
    await build(load(a, extended=True), models=FakeModel(["Noted."])).run_async(
        "FLOW-A-PRIVATE teal colour", session_id="s1", session_store=store
    )
    b = json.loads(json.dumps(a))
    b["name"] = "a different flow"
    model = FakeModel([[ToolCall("c1", "search_memory", {"search_query": "teal colour"})], "x"])
    await build(load(b, extended=True), models=model).run_async(
        "what colour?", session_id="s1", session_store=store
    )
    seen = [m.content or "" for m in model.calls[-1]["messages"] if m.role == "tool"]
    assert "FLOW-A-PRIVATE" not in "".join(seen)


async def test_same_flow_still_sees_its_own_history() -> None:
    store = MemorySessionStore()
    a = doc("v3_memory_agent")
    await build(load(a, extended=True), models=FakeModel(["Noted."])).run_async(
        "OWN-HISTORY teal colour", session_id="s1", session_store=store
    )
    model = FakeModel([[ToolCall("c1", "search_memory", {"search_query": "teal colour"})], "x"])
    await build(load(a, extended=True), models=model).run_async(
        "what colour?", session_id="s1", session_store=store
    )
    seen = [m.content or "" for m in model.calls[-1]["messages"] if m.role == "tool"]
    assert "OWN-HISTORY" in "".join(seen)


def test_every_class_counted_supported_by_v3_has_a_fixture() -> None:
    used = set()
    for p in FIX.glob("v3_*.json"):
        used |= {
            n["data"]["type"] for n in json.loads(p.read_text())["data"]["nodes"] if "data" in n
        }
    added = (
        "File",
        "MemoryBase",
        "KnowledgeBase",
        "Knowledge",
        "LoopComponent",
        "SQLComponent",
        "SplitText",
    )
    after = registry.coverage(extended=True)["by_class"]
    unproven = [c for c in added if after[c][1] and c not in used]
    assert not unproven, f"counted supported but never exercised by a fixture: {unproven}"


def test_knowledge_class_fixture_loads_and_coverage_states_its_basis() -> None:
    imported = load(doc("v3_knowledge_class"), extended=True)
    assert imported.report.unsupported == []
    cov = registry.coverage(extended=True)
    assert "by class" in cov["basis"] and "unsupported options" in cov["basis"]
    assert "Knowledge" in cov["options_ignored_classes"]
