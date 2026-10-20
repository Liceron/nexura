"""Regression tests for the V1 Langflow importer review findings."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from nexura_agents import FakeModel, MemorySessionStore

from nexura_runtime.integrations.langflow import LangflowImportError, build, load

OE = "œ"


def _node(
    nid: str, cls: str, tpl: dict[str, Any] | None = None, display: str | None = None
) -> dict:
    code = "class X:\n    pass\n"
    t = {
        k: {"type": "str", "value": v, "_input_type": "MessageTextInput"}
        for k, v in (tpl or {}).items()
    }
    t["code"] = {"type": "code", "value": code}
    return {
        "id": nid,
        "type": "genericNode",
        "data": {
            "id": nid,
            "type": cls,
            "node": {
                "display_name": display or cls,
                "template": t,
                "metadata": {"code_hash": hashlib.sha256(code.encode()).hexdigest()[:12]},
            },
        },
    }


def _edge(src: str, out: str, dst: str, fld: str) -> dict:
    sh = {"dataType": "x", "id": src, "name": out, "output_types": ["Message"]}
    th = {"fieldName": fld, "id": dst, "inputTypes": ["Message"], "type": "str"}
    return {
        "id": f"e-{src}-{dst}-{fld}",
        "source": src,
        "target": dst,
        "sourceHandle": json.dumps(sh).replace('"', OE),
        "targetHandle": json.dumps(th).replace('"', OE),
        "data": {"sourceHandle": sh, "targetHandle": th},
    }


def _agent(nid: str, **tpl: Any) -> dict:
    tpl.setdefault("model", [{"name": "m", "provider": "OpenAI"}])
    tpl.setdefault("system_prompt", "sys")
    n = _node(nid, "Agent", tpl)
    n["data"]["node"]["template"]["model"]["type"] = "model"
    return n


def _chat(agent_id: str = "A") -> dict:
    return {
        "name": "t",
        "data": {
            "nodes": [_node("I", "ChatInput"), _agent(agent_id), _node("O", "ChatOutput")],
            "edges": [
                _edge("I", "message", agent_id, "input_value"),
                _edge(agent_id, "response", "O", "input_value"),
            ],
        },
    }


# ---------------------------------------------------------------- crafted cycle crashes load()
def test_loop_cycle_followed_by_other_node_raises_import_error_not_valueerror() -> None:
    flow = {
        "name": "c",
        "data": {
            "nodes": [
                _node("R", "ChatInput"),
                _node("L", "LoopComponent"),
                _node("X", "ChatOutput"),
                _node("A", "ChatOutput"),
            ],
            "edges": [
                _edge("R", "message", "L", "input_value"),
                _edge("L", "message", "X", "input_value"),
                _edge("X", "message", "L", "input_value"),
                _edge("A", "message", "X", "input_value"),
            ],
        },
    }
    try:
        load(flow, strict=False)
    except LangflowImportError:
        return  # acceptable: a clean library error
    # no exception at all is also acceptable


def test_non_loop_cycle_hidden_behind_loop_cycle_is_reported() -> None:
    flow = {
        "name": "c",
        "data": {
            "nodes": [
                _node("L", "LoopComponent"),
                _node("X", "ChatOutput"),
                _node("P", "ChatOutput"),
                _node("Q", "ChatOutput"),
            ],
            "edges": [
                _edge("L", "message", "X", "input_value"),
                _edge("X", "message", "L", "input_value"),
                _edge("P", "message", "Q", "input_value"),
                _edge("Q", "message", "P", "input_value"),
            ],
        },
    }
    with pytest.raises(LangflowImportError):
        load(flow, strict=False)


# ---------------------------------------------------------------- session key collision
async def test_session_ids_with_colons_do_not_share_history() -> None:
    # session "s" on node "b:c" -> key "s:b:c"; session "s:b" on node "c" -> key "s:b:c"
    flow = {
        "name": "t",
        "data": {
            "nodes": [
                _node("I", "ChatInput"),
                _agent("c"),
                _agent("b:c"),
                _node("O", "ChatOutput"),
            ],
            "edges": [
                _edge("I", "message", "c", "input_value"),
                _edge("c", "response", "b:c", "input_value"),
                _edge("b:c", "response", "O", "input_value"),
            ],
        },
    }
    mc = FakeModel(handler=lambda m, t: "LEAK-TOKEN", repeat_last=True)
    mb = FakeModel(handler=lambda m, t: "ok", repeat_last=True)
    built = build(load(flow), models={"c": mc, "b:c": mb})
    store = MemorySessionStore()
    await built.run_async("one", session_id="s", session_store=store)
    mc.calls.clear()
    await built.run_async("two", session_id="s:b", session_store=store)
    seen = " ".join(str(x.content) for call in mc.calls for x in call["messages"])
    assert "LEAK-TOKEN" not in seen


async def test_agent_context_id_does_not_merge_distinct_sessions() -> None:
    flow = _chat()
    flow["data"]["nodes"][1]["data"]["node"]["template"]["context_id"] = {
        "type": "str",
        "value": "shared",
    }
    m = FakeModel(handler=lambda msgs, tools: "ok", repeat_last=True)
    built = build(load(flow), models=m)
    store = MemorySessionStore()
    await built.run_async("ALICE-PRIVATE", session_id="alice", session_store=store)
    m.calls.clear()
    await built.run_async("hi", session_id="bob", session_store=store)
    seen = " ".join(str(x.content) for call in m.calls for x in call["messages"])
    assert "ALICE-PRIVATE" not in seen


# ---------------------------------------------------------------- URL tool fails open
async def test_url_tool_with_no_domains_does_not_allow_every_site() -> None:
    flow = {
        "name": "u",
        "data": {
            "nodes": [
                _node("I", "ChatInput"),
                _agent("A"),
                _node("U", "URLComponent", {"urls": []}),
                _node("O", "ChatOutput"),
            ],
            "edges": [
                _edge("I", "message", "A", "input_value"),
                _edge("U", "component_as_tool", "A", "tools"),
                _edge("A", "response", "O", "input_value"),
            ],
        },
    }
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="data")

    built = build(load(flow), models=FakeModel(["x"]), http_transport=httpx.MockTransport(handler))
    out = await built.tools["U"].fn("https://93.184.216.34/")
    assert out.get("error") == "refused", out


# ---------------------------------------------------------------- template text corruption
async def test_prompt_does_not_rewrite_braces_in_user_input() -> None:
    flow = {
        "name": "p",
        "data": {
            "nodes": [
                _node("I", "ChatInput"),
                _node("P", "Prompt", {"template": "Q: {question}"}),
                _node("O", "ChatOutput"),
            ],
            "edges": [
                _edge("I", "message", "P", "question"),
                _edge("P", "prompt", "O", "input_value"),
            ],
        },
    }
    res = await build(load(flow), models=FakeModel(["x"])).run_async('json {{"a": 1}}')
    assert res.output == 'Q: json {{"a": 1}}'


# ---------------------------------------------------------------- long string treated as path
def test_overlong_string_input_raises_import_error() -> None:
    with pytest.raises(LangflowImportError):
        load("x" * 5000)
