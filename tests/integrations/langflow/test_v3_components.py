"""V3 component families: File, Memory, Knowledge, Loop, SQL, SplitText. Offline, FakeModel only."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import FakeModel, MemorySessionStore, ToolCall

from nexura_runtime.integrations.langflow import (
    ComponentRefusedError,
    FlowBuildError,
    V3Options,
    build,
    ingest_knowledge,
    load,
    registry,
)
from nexura_runtime.integrations.langflow import components as comp
from nexura_runtime.integrations.sql import SqlConnectionConfig, SqlConnectionManager
from nexura_runtime.knowledge.vector import InMemoryVectorStore

FIX = Path(__file__).parent / "fixtures"


def imp(name: str, **kw: Any) -> Any:
    doc = json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
    return load(doc, extended=True, **kw)


def tool_results(model: FakeModel) -> list[str]:
    return [m.content or "" for m in model.calls[-1]["messages"] if m.role == "tool"]


VOCAB = ["refund", "shipping", "holiday", "teal", "policy", "days", "office", "colour"]


def embed(text: str) -> list[float]:
    words = re.findall(r"\w+", text.lower())
    return [float(sum(1 for w in words if w.startswith(v))) + 0.01 for v in VOCAB]


# ------------------------------------------------------------------------------- coverage
def test_default_load_still_reports_v3_classes_unsupported() -> None:
    doc = json.loads((FIX / "v3_file_output.json").read_text(encoding="utf-8"))
    report = load(doc, strict=False).report
    assert [u.class_name for u in report.unsupported] == ["File"]


def test_coverage_after_v3_report(capsys: pytest.CaptureFixture[str]) -> None:
    before = registry.coverage(extended=False)
    after = registry.coverage(extended=True)
    assert before["total"] == after["total"] == 146
    assert after["supported"] > before["supported"]
    # every V0 class used in 3+ flows is covered after V3 except sticky notes (not nodes here)
    for name in ("File", "MemoryBase", "LoopComponent", "SQLComponent", "KnowledgeBase"):
        assert after["by_class"][name][1] and not before["by_class"][name][1]
    for name, (count, ok) in sorted(after["by_class"].items(), key=lambda kv: -kv[1][0]):
        print(f"{name}|{count}|{'yes' if ok else 'no'}")
    print(f"TOTAL|{before['supported']}/{before['total']}|{after['supported']}/{after['total']}")
    assert "TOTAL" in capsys.readouterr().out


def test_unsupported_options_are_reported() -> None:
    kb = imp("v3_knowledge_data").report.warnings
    assert any("search_type" in w and "not supported" in w for w in kb)
    assert not any("knowledge_base" in w for w in kb)
    f = imp("v3_file_output").report.warnings
    assert not any("silent_errors" in w for w in f)  # False means default
    mem = imp("v3_memory_agent").report.warnings
    assert any("session_id" in w for w in mem)


def test_sql_database_url_is_never_a_required_variable() -> None:
    imported = imp("v3_sql_agent")
    assert imported.report.required_variables == []
    assert any("never used" in w for w in imported.report.warnings)


# ------------------------------------------------------------------------------------ File
def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "hello.txt").write_text("Hello from a file", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    return root


async def test_file_reads_text(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    built = build(imp("v3_file_output"), v3=V3Options(files_root=root))
    assert (await built.run_async("x")).output == "Hello from a file"


async def test_file_needs_a_root() -> None:
    with pytest.raises(FlowBuildError, match="files_root"):
        build(imp("v3_file_output"))


async def run_file(tmp_path: Path, path: str, **opts: Any) -> Any:
    root = make_root(tmp_path)
    built = build(
        imp("v3_file_output"),
        tweaks={"File-f1": {"path": path}},
        v3=V3Options(files_root=root, **opts),
    )
    return await built.run_async("x")


@pytest.mark.parametrize("path", ["../secret.txt", "notes/../../secret.txt", "notes/../hello.txt"])
async def test_file_path_traversal_refused(tmp_path: Path, path: str) -> None:
    with pytest.raises(ComponentRefusedError, match="traversal"):
        await run_file(tmp_path, path)


async def test_file_absolute_path_outside_root_refused(tmp_path: Path) -> None:
    with pytest.raises(ComponentRefusedError, match="outside the files root"):
        await run_file(tmp_path, str(tmp_path / "secret.txt"))


async def test_file_symlink_escape_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    os.symlink(tmp_path / "secret.txt", root / "notes" / "link.txt")
    with pytest.raises(ComponentRefusedError, match="outside the files root"):
        await run_file(tmp_path, "notes/link.txt")


async def test_file_too_large_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "notes" / "big.txt").write_text("x" * 5000, encoding="utf-8")
    with pytest.raises(ComponentRefusedError, match="byte limit"):
        await run_file(tmp_path, "notes/big.txt", max_file_bytes=1000)


async def test_file_type_and_binary_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "notes" / "run.exe").write_bytes(b"MZ")
    (root / "notes" / "bin.txt").write_bytes(b"ab\x00cd")
    with pytest.raises(ComponentRefusedError, match="not supported"):
        await run_file(tmp_path, "notes/run.exe")
    with pytest.raises(ComponentRefusedError, match="binary"):
        await run_file(tmp_path, "notes/bin.txt")
    with pytest.raises(ComponentRefusedError, match="not found"):
        await run_file(tmp_path, "notes/missing.txt")


async def test_file_pdf_text_and_bad_pdf(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    root = make_root(tmp_path)
    body = b"BT /F1 12 Tf 72 700 Td (Quarterly report text) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    (root / "notes" / "r.pdf").write_bytes(pdf)
    (root / "notes" / "bad.pdf").write_bytes(b"not a pdf")
    out = await run_file(tmp_path, "notes/r.pdf")
    assert "Quarterly report text" in out.output
    with pytest.raises(ComponentRefusedError, match="could not be read"):
        await run_file(tmp_path, "notes/bad.pdf")


# ---------------------------------------------------------------------------------- Memory
async def test_memory_tool_searches_only_this_session() -> None:
    store = MemorySessionStore()
    first = FakeModel(["Noted."])
    built = build(imp("v3_memory_agent"), models=first)
    await built.run_async("my favourite colour is teal", session_id="s1", session_store=store)

    model = FakeModel([[ToolCall("c1", "search_memory", {"search_query": "colour"})], "teal"])
    built2 = build(imp("v3_memory_agent"), models=model)
    await built2.run_async("what colour?", session_id="s1", session_store=store)
    assert "teal" in tool_results(model)[0]

    other = FakeModel([[ToolCall("c1", "search_memory", {"search_query": "colour"})], "none"])
    built3 = build(imp("v3_memory_agent"), models=other)
    await built3.run_async("what colour?", session_id="s2", session_store=store)
    assert "teal" not in tool_results(other)[0]  # session s2 never sees s1's history

    nosess = FakeModel([[ToolCall("c1", "search_memory", {"search_query": "colour"})], "none"])
    await build(imp("v3_memory_agent"), models=nosess).run_async("hi")
    assert "no_session" in tool_results(nosess)[0]


# -------------------------------------------------------------------------------- Knowledge
async def seeded(**kw: Any) -> V3Options:
    opts = V3Options(vector_store=InMemoryVectorStore(), embedder=embed, **kw)
    await ingest_knowledge(opts, "handbook", ["refund policy: 30 days", "shipping takes 5 days"])
    return opts


async def test_knowledge_tool_and_data_node() -> None:
    opts = await seeded()
    model = FakeModel([[ToolCall("c1", "search_knowledge", {"search_query": "refund"})], "ok"])
    await build(imp("v3_knowledge_agent"), models=model, v3=opts).run_async("q")
    assert "refund policy" in tool_results(model)[0]

    out = await build(imp("v3_knowledge_data"), v3=opts).run_async("shipping")
    assert out.output == "shipping takes 5 days"  # top_k=1


async def test_knowledge_needs_embedder() -> None:
    with pytest.raises(FlowBuildError, match="embedder"):
        build(imp("v3_knowledge_data"))


async def test_knowledge_workspace_isolation() -> None:
    store = InMemoryVectorStore()
    a = V3Options(vector_store=store, embedder=embed, workspace_id="ws-a")
    b = V3Options(vector_store=store, embedder=embed, workspace_id="ws-b")
    await ingest_knowledge(a, "handbook", ["refund policy: 30 days"])
    assert "refund" in (await build(imp("v3_knowledge_data"), v3=a).run_async("refund")).output
    assert (await build(imp("v3_knowledge_data"), v3=b).run_async("refund")).output == ""


async def test_knowledge_session_isolation() -> None:
    store = InMemoryVectorStore()
    opts = V3Options(
        vector_store=store, embedder=embed, workspace_id="ws", knowledge_scope="session"
    )
    await ingest_knowledge(opts, "handbook", ["refund policy: 30 days"], session_id="s1")
    flow = build(imp("v3_knowledge_data"), v3=opts)
    same = await flow.run_async("refund", session_id="s1")
    other = await flow.run_async("refund", session_id="s2")
    none = await flow.run_async("refund")
    assert "refund" in same.output
    assert other.output == "" and none.output == ""


async def test_knowledge_name_cannot_forge_a_collection() -> None:
    doc = json.loads((FIX / "v3_knowledge_data.json").read_text(encoding="utf-8"))
    for n in doc["data"]["nodes"]:
        if n["id"] == "KnowledgeBase-k1":
            n["data"]["node"]["template"]["knowledge_base"]["value"] = "handbook::s::abc"
    with pytest.raises(FlowBuildError, match="knowledge base name"):
        build(load(doc, extended=True), v3=V3Options(embedder=embed))


# ------------------------------------------------------------------------------- SplitText
def test_split_text_chunks_and_overlap() -> None:
    assert comp.split_text("aaa\nbbb\nccc", 7, 0, "\n") == ["aaa\nbbb", "ccc"]
    assert comp.split_text("abcdefghij", 4, 1, "") == ["abcd", "defg", "ghij"]
    with pytest.raises(ComponentRefusedError):
        comp.split_text("x", 4, 4, "")


# ---------------------------------------------------------------------------------- Loop
async def test_loop_runs_body_per_item_and_collects() -> None:
    model = FakeModel(["A", "B", "C"])
    built = build(imp("v3_loop_agent"), models=model)
    assert built.loops["Loop-l1"].body == ["Agent-b2"]
    r = await built.run_async("one\ntwo\nthree")
    assert r.output == "A\nB\nC"
    assert [m.content for m in model.calls[1]["messages"] if m.role == "user"] == ["two"]
    assert r.node_outputs["Loop-l1"]["items"] == ["A", "B", "C"]


async def test_loop_accepts_json_list_and_split_text_feeds_it() -> None:
    r = await build(imp("v3_split_loop")).run_async("alpha\nbeta\ngamma")
    assert r.output == "[alpha]\n[beta]\n[gamma]"
    r = await build(imp("v3_split_loop")).run_async("")
    assert r.output == ""


async def test_loop_history_is_not_shared_between_items() -> None:
    store = MemorySessionStore()
    model = FakeModel(["A", "B"])
    await build(imp("v3_loop_agent"), models=model).run_async(
        '["x", "y"]', session_id="s", session_store=store
    )
    users = [m.content for m in model.calls[-1]["messages"] if m.role == "user"]
    assert users == ["y"]


async def test_loop_bound_enforced() -> None:
    built = build(
        imp("v3_loop_agent"), models=FakeModel(["a"] * 10), v3=V3Options(max_loop_items=2)
    )
    with pytest.raises(ComponentRefusedError, match="exceeds the limit of 2"):
        await built.run_async("1\n2\n3")
    ok = await build(
        imp("v3_loop_agent"), models=FakeModel(["a", "b"]), v3=V3Options(max_loop_items=2)
    ).run_async("1\n2")
    assert ok.output == "a\nb"


def test_loop_without_feedback_is_a_build_error() -> None:
    doc = json.loads((FIX / "v3_loop_agent.json").read_text(encoding="utf-8"))
    doc["data"]["edges"] = [e for e in doc["data"]["edges"] if e["target"] != "Loop-l1"][:0] + [
        e
        for e in doc["data"]["edges"]
        if not (e["source"] == "Agent-b2" and e["target"] == "Loop-l1")
    ]
    with pytest.raises(FlowBuildError, match="nothing feeds back"):
        build(load(doc, extended=True), models=FakeModel(["x"]))


# ------------------------------------------------------------------------------------ SQL
def sql_manager(read_only: bool = True) -> SqlConnectionManager:
    mgr = SqlConnectionManager()
    mgr.register_connection(
        SqlConnectionConfig(
            connection_id="db",
            workspace_id="*",
            database_path=f"file:v3_{uuid.uuid4().hex}?mode=memory&cache=shared",
            read_only=read_only,
        ),
        initial_sql="CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO t (id, name) VALUES (1, 'ann'), (2, 'bob'), (3, 'cy');",
    )
    return mgr


def sql_opts(mgr: SqlConnectionManager, **kw: Any) -> V3Options:
    return V3Options(sql_manager=mgr, sql_connection="db", **kw)


async def test_sql_tool_parameterised_read() -> None:
    mgr = sql_manager()
    model = FakeModel(
        [
            [
                ToolCall(
                    "c1",
                    "sql_query",
                    {"query": "SELECT name FROM t WHERE id = ?", "parameters": [2]},
                )
            ],
            "bob it is",
        ]
    )
    built = build(imp("v3_sql_agent"), models=model, v3=sql_opts(mgr))
    r = await built.run_async("who is 2?")
    assert r.output == "bob it is"
    assert json.loads(tool_results(model)[0])["rows"] == [{"name": "bob"}]


async def test_sql_injection_in_parameter_is_just_data() -> None:
    mgr = sql_manager()
    out = await comp.run_sql(
        sql_opts(mgr), "SELECT * FROM t WHERE name = ?", ["x'; DROP TABLE t;--"]
    )
    assert out["rows"] == []
    assert (await comp.run_sql(sql_opts(mgr), "SELECT count(*) AS n FROM t"))["rows"] == [{"n": 3}]


async def test_sql_write_refused_by_default() -> None:
    mgr = sql_manager(read_only=False)  # even a writable connection stays read-only by default
    for stmt in (
        "INSERT INTO t VALUES (9, 'z')",
        "UPDATE t SET name = 'z'",
        "DELETE FROM t",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT 9, 'z'",
        "REPLACE INTO t VALUES (1, 'z')",
        "DROP TABLE t",
        "CREATE TABLE u (a)",
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE ':memory:' AS x",
    ):
        with pytest.raises(ComponentRefusedError):
            await comp.run_sql(sql_opts(mgr), stmt)
    assert (await comp.run_sql(sql_opts(mgr), "SELECT count(*) AS n FROM t"))["rows"] == [{"n": 3}]


async def test_sql_write_needs_flag_and_writable_connection() -> None:
    ro = sql_manager(read_only=True)
    with pytest.raises(ComponentRefusedError, match="read-only"):
        await comp.run_sql(sql_opts(ro, sql_allow_write=True), "INSERT INTO t VALUES (9, 'z')")
    rw = sql_manager(read_only=False)
    opts = sql_opts(rw, sql_allow_write=True)
    done = await comp.run_sql(opts, "INSERT INTO t VALUES (?, ?)", [9, "z"])
    assert done == {"rows_affected": 1}
    with pytest.raises(ComponentRefusedError, match="DROP"):
        await comp.run_sql(opts, "DROP TABLE t")  # DDL stays refused even with the flag


@pytest.mark.parametrize(
    "stmt",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE t",
        "SELECT 1;\n-- c\nDELETE FROM t",
        "SELECT 1 /* x */; SELECT 2;",
        ";SELECT 1;SELECT 2",
    ],
)
async def test_sql_multi_statement_refused(stmt: str) -> None:
    with pytest.raises(ComponentRefusedError):
        comp.validate_sql(stmt, allow_write=False)
    with pytest.raises(ComponentRefusedError):
        comp.validate_sql(stmt, allow_write=True)


def test_sql_validator_accepts_safe_shapes() -> None:
    assert comp.validate_sql("SELECT 1;", allow_write=False) == "SELECT 1"
    assert comp.validate_sql("select ';' as s -- ; x", allow_write=False).startswith("select")
    assert comp.validate_sql("SELECT replace(name, 'a', 'b') FROM t", allow_write=False)
    assert comp.validate_sql("WITH c AS (SELECT 1 AS a) SELECT a FROM c", allow_write=False)
    with pytest.raises(ComponentRefusedError, match="unterminated"):
        comp.validate_sql("SELECT 'abc", allow_write=False)


async def test_sql_tool_refusal_is_returned_to_the_model() -> None:
    mgr = sql_manager()
    model = FakeModel(
        [[ToolCall("c1", "sql_query", {"query": "DELETE FROM t", "parameters": None})], "no"]
    )
    await build(imp("v3_sql_agent"), models=model, v3=sql_opts(mgr)).run_async("wipe")
    assert "refused" in tool_results(model)[0]
    assert (await comp.run_sql(sql_opts(mgr), "SELECT count(*) AS n FROM t"))["rows"] == [{"n": 3}]


async def test_sql_row_cap_and_connection_required() -> None:
    mgr = sql_manager()
    out = await comp.run_sql(sql_opts(mgr, sql_max_rows=2), "SELECT * FROM t")
    assert out["row_count"] == 2 and out["truncated"] is True
    with pytest.raises(FlowBuildError, match="sql_connection"):
        build(imp("v3_sql_agent"), models=FakeModel(["x"]))


async def test_sql_fixed_query_flow_value_is_validated_at_build() -> None:
    doc = json.loads((FIX / "v3_sql_agent.json").read_text(encoding="utf-8"))
    for n in doc["data"]["nodes"]:
        if n["id"] == "SQLComponent-q1":
            n["data"]["node"]["template"]["query"]["value"] = "DELETE FROM t"
    with pytest.raises(ComponentRefusedError, match="write"):
        build(load(doc, extended=True), models=FakeModel(["x"]), v3=sql_opts(sql_manager()))
