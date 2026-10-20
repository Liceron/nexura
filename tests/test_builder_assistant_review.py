"""Regression tests for the P7 chat-to-build review findings."""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from _local_env import local_headers
from nexura_agents.models.fake import FakeModel

from nexura_runtime.builder_assistant import DraftError, draft_from_description
from nexura_runtime.templates import list_templates, template_registry

DESC = "a support agent that looks up orders"
KEY = "sk-" + "A1b2C3d4" * 4


def _node(nid: str, handler: str, **kw: Any) -> dict[str, Any]:
    return {"id": nid, "handler": handler, "transitions": {}, "default_transition": None, **kw}


def _reply(nodes: list[dict[str, Any]], entry: str = "classify") -> str:
    spec = {"name": "S", "entry_node": entry, "nodes": nodes, "terminal_nodes": []}
    return json.dumps({"explanation": "x", "spec": spec})


async def _draft(model: FakeModel, registry: Any = None, **kw: Any) -> Any:
    return await draft_from_description(
        kw.pop("desc", DESC),
        model=model,
        registry=registry or template_registry(),
        templates=list_templates(),
        **kw,
    )


async def test_secret_in_input_mapping_is_stripped() -> None:
    n = _node("classify", "tpl_support_classify", input_mapping={"api_key": KEY, "x": KEY})
    n["output_mapping"] = {"token": KEY}
    r = await _draft(FakeModel([_reply([n])]))
    assert KEY not in json.dumps(r.spec)


async def test_nested_literal_secret_under_secret_key_is_stripped() -> None:
    n = _node(
        "classify",
        "tpl_support_classify",
        config={"api_key": {"value": "hunter2-plain-literal"}, "password": ["hunter2-plain"]},
    )
    r = await _draft(FakeModel([_reply([n])]))
    assert "hunter2-plain" not in json.dumps(r.spec)


async def test_unmanifested_unknown_effect_handler_is_gated() -> None:
    reg = template_registry()
    reg.register("submit_order", lambda state: {})
    reg.register("grant_access", lambda state: {})
    nodes = [
        _node("classify", "submit_order", default_transition="g"),
        _node("g", "grant_access"),
    ]
    r = await _draft(FakeModel([_reply(nodes)]), registry=reg)
    assert r.spec["nodes"]["classify"]["requires_approval"] is True
    assert r.spec["nodes"]["g"]["requires_approval"] is True


@pytest.mark.parametrize(
    "patch",
    [
        {"transitions": {"a": ["x"]}},
        {"transitions": {"a": {"b": 1}}},
        {"default_transition": {"a": 1}},
        {"default_transition": ["classify"]},
        {"handler": ["x"]},
        {"module_id": {"a": 1}},
        {"module_version": {"a": 1}, "module_id": "m"},
        {"transitions": {"a": None}},
        {"requires_approval": {"a": 1}},
    ],
)
async def test_malformed_node_fields_become_retry_or_draft_error_not_crash(
    patch: dict[str, Any],
) -> None:
    n = _node("classify", "tpl_support_classify")
    n.update(patch)
    model = FakeModel([_reply([n])], repeat_last=True)
    with contextlib.suppress(DraftError):
        await _draft(model)


async def test_malformed_spec_level_fields_do_not_crash() -> None:
    for entry in ([1], {"a": 1}, 5):
        n = _node("classify", "tpl_support_classify")
        spec = {"name": {"a": 1}, "entry_node": entry, "nodes": [n], "terminal_nodes": [[1], {}]}
        model = FakeModel([json.dumps({"spec": spec})], repeat_last=True)
        with contextlib.suppress(DraftError):
            await _draft(model)


async def test_deeply_nested_reply_is_a_draft_error_not_recursion_crash() -> None:
    deep = '{"spec": ' + "[" * 50000 + "]" * 50000 + "}"
    model = FakeModel([deep], repeat_last=True)
    with pytest.raises(DraftError):
        await _draft(model)


async def test_retry_history_is_bounded() -> None:
    big = "{" + ' "pad": "' + "x" * 150_000 + '"'  # unusable JSON, large
    model = FakeModel([big + "}"], repeat_last=True)
    with pytest.raises(DraftError):
        await _draft(model, max_attempts=5)
    last = model.calls[-1]["messages"]
    total = sum(len(m.content or "") for m in last)
    assert total < 100_000, total


async def test_catalog_is_bounded_with_many_modules() -> None:
    reg = template_registry()
    for i in range(3000):
        reg.register(f"extra_mod_{i}", lambda state: {})
    model = FakeModel([_reply([_node("classify", "tpl_support_classify")])])
    await _draft(model, registry=reg)
    system = model.calls[0]["messages"][0].content
    assert len(system) < 200_000, len(system)


async def test_module_description_injection_cannot_change_the_result() -> None:
    reg = template_registry()
    reg.register("evil_mod", lambda state: {})
    # Model obeys the injected catalog text: invents module, drops approval.
    nodes = [
        _node("classify", "tpl_support_classify", default_transition="r"),
        _node("r", "tpl_support_issue_refund", requires_approval=False, default_transition="z"),
        _node("z", "brand_new_http_exfil", config={"url": "http://evil.example/x"}),
    ]
    with pytest.raises(DraftError) as e:
        await _draft(FakeModel([_reply(nodes)], repeat_last=True), registry=reg)
    assert e.value.code == "UNKNOWN_MODULES"


def test_cross_workspace_credential_not_usable(tmp_path: Any, monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    from nexura_runtime.service.api import builder as b
    from nexura_runtime.service.api.app import create_api_app
    from nexura_runtime.service.workspaces.repository import WorkspaceRepository

    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    ka, kb = local_headers("wsA"), local_headers("wsB")
    app = create_api_app(
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        profile="local-owner",
    )
    used: list[str] = []
    monkeypatch.setattr(
        b,
        "build_model_from_credential",
        lambda p, m, k: (
            used.append(k) or FakeModel([_reply([_node("classify", "tpl_support_classify")])])
        ),
    )
    c = TestClient(app)
    r = c.post(
        "/api/v1/credentials",
        headers=kb,
        json={
            "credential_id": "bkey",
            "credential_type": "api_key",
            "provider": "openai",
            "secret": "sk-BOBS-SECRET-0123456789",
        },
    )
    assert r.status_code == 201, r.text
    for body in ({"description": DESC, "credential": "bkey"}, {"description": DESC}):
        resp = c.post("/api/v1/builder/draft", headers=ka, json=body)
        assert resp.status_code == 400, resp.text
    assert used == []


async def test_declared_pure_handler_is_not_gated_but_undeclared_is() -> None:
    def lookup(state: Any) -> dict[str, Any]:
        return {}

    lookup.effect_policy = "pure"  # type: ignore[attr-defined]
    reg = template_registry()
    reg.register("lookup_thing", lookup)
    reg.register("mystery_thing", lambda state: {})
    nodes = [
        _node("classify", "lookup_thing", default_transition="g"),
        _node("g", "mystery_thing"),
    ]
    r = await _draft(FakeModel([_reply(nodes)]), registry=reg)
    assert r.spec["nodes"]["classify"]["requires_approval"] is False
    assert r.spec["nodes"]["g"]["requires_approval"] is True


async def test_nested_secret_shaped_string_is_redacted() -> None:
    n = _node("classify", "tpl_support_classify", config={"a": {"b": [{"c": KEY}]}})
    r = await _draft(FakeModel([_reply([n])]))
    assert KEY not in json.dumps(r.spec)


async def test_catalog_reports_hidden_modules_and_keeps_relevant_ones() -> None:
    reg = template_registry()
    for i in range(300):
        reg.register(f"extra_mod_{i}", lambda state: {})
    model = FakeModel([_reply([_node("classify", "tpl_support_classify")])])
    await _draft(model, registry=reg)
    system = model.calls[0]["messages"][0].content
    assert "more modules not shown" in system
    assert "tpl_support_classify" in system or "tpl_support_lookup_order" in system


async def test_replayed_reply_is_clipped() -> None:
    big = "{" + ' "pad": "' + "x" * 50_000 + '"}'
    model = FakeModel([big], repeat_last=True)
    with pytest.raises(DraftError):
        await _draft(model, max_attempts=2)
    replayed = model.calls[-1]["messages"][2].content
    assert len(replayed) < 5000
