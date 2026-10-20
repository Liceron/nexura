"""Permanent, self-contained regressions for the P32a independent review."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
from nexura_agents import Agent, FakeModel, Runner, Tool, ToolCall
from nexura_agents.repro import (
    ReproRecorder,
    export_repro_bundle,
    offline_guard,
    replay_repro_bundle,
)
from nexura_agents.tools import ToolRetry

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


@pytest.mark.asyncio
async def test_secret_fields_in_tool_output_are_redacted(tmp_path: Path) -> None:
    async def fetch(url: str) -> dict:
        return {
            "api_key": "fake-unlabelled-key-777",
            "headers": {"Authorization": "Token fake-tkn-888", "Cookie": "sid=fake-999"},
        }

    agent = Agent(
        "a",
        "local",
        FakeModel([[ToolCall("c1", "fetch", {"url": "x"})], "done"]),
        tools=[Tool("fetch", fetch)],
    )
    recorder = ReproRecorder()
    await recorder.run_agent(Runner(), agent, "hi")
    path = tmp_path / "bundle.json"
    export_repro_bundle(recorder).save(path)
    saved = path.read_text()
    assert "fake-unlabelled-key-777" not in saved
    assert "fake-tkn-888" not in saved
    assert "fake-999" not in saved


@pytest.mark.asyncio
async def test_nested_credential_copy_in_raw_arguments_is_redacted(tmp_path: Path) -> None:
    args = {"auth": {"user": "u", "pass": "fake-nested-pass-1"}}
    agent = Agent(
        "a",
        "local",
        FakeModel([[ToolCall("c1", "login", args, raw_arguments=json.dumps(args))], "done"]),
        tools=[Tool("login", lambda auth: "ok")],
    )
    recorder = ReproRecorder()
    await recorder.run_agent(Runner(), agent, "hi")
    path = tmp_path / "bundle.json"
    export_repro_bundle(recorder).save(path)
    saved = json.loads(path.read_text())
    # The structured copy is redacted ...
    assert saved["tool_fixtures"][0]["arguments"] == {"auth": {"$redacted": "auth"}}
    # ... but the raw copy of the same arguments must be too.
    assert "fake-nested-pass-1" not in path.read_text()


SECRET = "fake+Secret/Value=42"


@pytest.mark.asyncio
async def test_encoded_copies_of_declared_credential_are_redacted(tmp_path: Path) -> None:
    b64 = base64.b64encode(f"user:{SECRET}".encode()).decode()
    quoted = urllib.parse.quote(SECRET, safe="")

    async def call(q: str) -> str:
        return f"auth was Basic {b64}; retried https://h.invalid/x?key={quoted}"

    agent = Agent(
        "a",
        "local",
        FakeModel([[ToolCall("c1", "call", {"q": "x"})], "done"]),
        tools=[Tool("call", call)],
    )
    recorder = ReproRecorder(credential_values={"MY_KEY": SECRET})
    await recorder.run_agent(Runner(), agent, "hi")
    path = tmp_path / "bundle.json"
    export_repro_bundle(recorder).save(path)
    saved = path.read_text()
    assert b64 not in saved
    assert quoted not in saved


@pytest.mark.asyncio
async def test_credential_headers_in_tool_arguments_are_redacted(tmp_path: Path) -> None:
    headers = {
        "X-Auth-Token": "fake-xauth-111",
        "PRIVATE-TOKEN": "fake-glpat-222",
        "pairs": [["Cookie", "sid=fake-cookie-333"]],
    }
    agent = Agent(
        "a",
        "local",
        FakeModel([[ToolCall("c1", "http", {"headers": headers})], "done"]),
        tools=[Tool("http", lambda headers: "ok")],
    )
    recorder = ReproRecorder()
    await recorder.run_agent(Runner(), agent, "hi")
    path = tmp_path / "bundle.json"
    export_repro_bundle(recorder).save(path)
    saved = path.read_text()
    assert "fake-xauth-111" not in saved
    assert "fake-glpat-222" not in saved
    assert "fake-cookie-333" not in saved


@pytest.mark.asyncio
async def test_failure_with_param_named_key_reproduces() -> None:
    async def cache_get(key: str) -> str:
        raise ValueError("cache miss for " + key)

    agent = Agent(
        "a",
        "local",
        FakeModel([[ToolCall("c1", "cache_get", {"key": "user:42"})], "done"]),
        tools=[Tool("cache_get", cache_get)],
    )
    recorder = ReproRecorder()
    with pytest.raises(Exception, match="cache miss"):
        await recorder.run_agent(Runner(), agent, "hi")
    result = await replay_repro_bundle(export_repro_bundle(recorder))
    assert result.reproduced, result.first_divergence


async def _record_and_replay(agent: Agent):
    recorder = ReproRecorder()
    await recorder.run_agent(Runner(), agent, "hi")
    return await replay_repro_bundle(export_repro_bundle(recorder))


@pytest.mark.asyncio
async def test_retry_policy_run_reproduces() -> None:
    calls = {"n": 0}

    async def flaky(q: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient local fake")
        return "ok"

    tool = Tool("flaky", flaky, retry=ToolRetry(3, initial_delay=0), idempotent=True)
    script = FakeModel([[ToolCall("c1", "flaky", {"q": "x"})], "done"])
    result = await _record_and_replay(Agent("a", "local", script, tools=[tool]))
    assert result.reproduced, result.first_divergence


@pytest.mark.asyncio
async def test_tool_timeout_run_reproduces() -> None:
    async def slow(q: str) -> str:
        await asyncio.sleep(5)
        return "late"

    tool = Tool("slow", slow, timeout=0.05)
    script = FakeModel([[ToolCall("c1", "slow", {"q": "x"})], "done"])
    result = await _record_and_replay(Agent("a", "local", script, tools=[tool]))
    assert result.reproduced, result.first_divergence


@pytest.mark.asyncio
async def test_cached_tool_run_reproduces() -> None:
    async def cached(q: str) -> str:
        return "v"

    tool = Tool("cached", cached, cache=True, idempotent=True)
    script = FakeModel(
        [[ToolCall("c1", "cached", {"q": "x"})], [ToolCall("c2", "cached", {"q": "x"})], "done"]
    )
    result = await _record_and_replay(Agent("a", "local", script, tools=[tool]))
    assert result.reproduced, result.first_divergence


@pytest.mark.asyncio
async def test_approved_write_gated_map_workflow_reproduces() -> None:
    registry = ModuleRegistry()

    async def writer(state: dict) -> dict:
        return {f"done_{state['item']}": True}

    # Local fake write-capable manifest: every operation needs approval.
    writer.manifest = SimpleNamespace(  # type: ignore[attr-defined]
        write_operations=["w"],
        always_write=False,
        write_operation_key="op",
        write_gate=lambda op: True,
    )
    registry.register("writer", writer)

    async def after(state: dict) -> dict:
        return {"after": True}

    registry.register("after", after)
    spec = WorkflowSpec(
        name="m",
        entry_node="map",
        nodes={
            "map": WorkflowNode(
                id="map", handler="writer", map_over="items", default_transition="next"
            ),
            "next": WorkflowNode(id="next", handler="after"),
        },
    )
    recorder = ReproRecorder()
    paused = await recorder.run_workflow(
        WorkflowEngine(spec, registry, store=MemoryWorkflowStore()), {"items": [1, 2]}
    )
    assert paused.pending_approval is not None
    done = await recorder.resume_workflow(
        paused.workflow_run_id, paused.pending_approval.approval_id
    )
    assert done.status == "completed"
    result = await replay_repro_bundle(export_repro_bundle(recorder))
    assert result.reproduced, result.first_divergence


def test_guard_blocks_gethostbyname_and_sendmsg(monkeypatch: pytest.MonkeyPatch) -> None:
    reached: list[str] = []
    monkeypatch.setattr(socket, "gethostbyname", lambda *a: reached.append("dns") or "0.0.0.0")
    monkeypatch.setattr(socket.socket, "sendmsg", lambda *a: reached.append("sendmsg") or 1)
    with offline_guard():
        with pytest.raises(RuntimeError, match="Network access blocked"):
            socket.gethostbyname("example.invalid")
        with pytest.raises(RuntimeError, match="Network access blocked"):
            socket.socket.sendmsg(None, [b"x"], [], 0, ("192.0.2.1", 9))
    assert reached == []


@pytest.mark.parametrize(
    "value", [{"nested": ["fake-scalar-secret", 987654321]}, ["fake-list-secret"], 876543219]
)
def test_all_secret_scalars_and_encoded_copies(value) -> None:
    from nexura_agents.repro import _scrub

    raw = json.dumps(value)
    result = json.dumps(
        _scrub(
            {"password": value, "copy": raw, "encoded": base64.b64encode(raw.encode()).decode()}, {}
        )
    )
    for secret in ("fake-scalar-secret", "fake-list-secret", "987654321", "876543219"):
        assert secret not in result
    assert base64.b64encode(raw.encode()).decode() not in result


def test_json_strings_headers_and_existing_redaction_values() -> None:
    from nexura_agents.repro import _scrub

    headers = [
        "X-Auth-Token",
        "X-Api-Key",
        "X-Api-Token",
        "PRIVATE-TOKEN",
        "Proxy-Authorization",
        "Set-Cookie",
        "Authorization",
        "Cookie",
        "Ocp-Apim-Subscription-Key",
    ]
    data = {
        "json": json.dumps(
            {"headers": [[name, f"fake-header-{i}"] for i, name in enumerate(headers)]}
        ),
        "text": "Authorization: Token fake-text-secret\nCookie: sid=fake-cookie-secret",
        "$redacted": {"password": "fake-marker-secret"},
    }
    saved = json.dumps(_scrub(data, {}))
    assert "fake-" not in saved
    assert isinstance(_scrub(data, {})["json"], str)


def test_rescrubbing_header_markers_preserves_nonsecret_text() -> None:
    from nexura_agents.repro import _scrub

    cleaned = _scrub(
        {"headers": [["Cookie", "sid=fake-secret"]], "description": "Cookie header"}, {}
    )
    assert cleaned["description"] == "Cookie header"
    assert _scrub(cleaned, {}) == cleaned


@pytest.mark.parametrize(
    "name,value",
    [("key", "user:42"), ("token", "next-page"), ("auth", "basic"), ("auth", {"mode": "basic"})],
)
def test_benign_ambiguous_names_preserve_values_and_schema(name, value) -> None:
    from nexura_agents.repro import _scrub

    data = {name: value, "schema": {"properties": {name: {"type": "string"}}}}
    assert _scrub(data, {}) == data


@pytest.mark.asyncio
async def test_redacted_failure_survives_save_load(tmp_path: Path) -> None:
    from nexura_agents.repro import ReproBundle

    async def fail(api_key: str) -> str:
        raise ValueError("failure with " + api_key)

    recorder = ReproRecorder()
    agent = Agent(
        "a",
        "local",
        FakeModel([[ToolCall("c", "fail", {"api_key": SECRET})]]),
        tools=[Tool("fail", fail, max_retries=0)],
    )
    with pytest.raises(Exception, match="failure with"):
        await recorder.run_agent(Runner(), agent, "hi")
    path = tmp_path / "redacted.json"
    export_repro_bundle(recorder).save(path)
    result = await replay_repro_bundle(ReproBundle.load(path))
    assert result.reproduced, result.first_divergence
    assert SECRET not in path.read_text()


@pytest.mark.asyncio
async def test_timeout_fail_and_cancellation_are_recorded() -> None:
    async def slow(q: str) -> str:
        await asyncio.sleep(5)
        return "late"

    recorder = ReproRecorder()
    agent = Agent(
        "a",
        "local",
        FakeModel([[ToolCall("c", "slow", {"q": "x"})]]),
        tools=[Tool("slow", slow, timeout=0.01, on_timeout="fail")],
    )
    with pytest.raises(Exception, match="slow"):
        await recorder.run_agent(Runner(), agent, "hi")
    assert recorder.data["tool_fixtures"][0]["error"]["type"] == "CancelledError"
    result = await replay_repro_bundle(export_repro_bundle(recorder))
    assert result.reproduced, result.first_divergence

    async def cancelled(q: str) -> str:
        raise asyncio.CancelledError("local cancellation")

    recorder = ReproRecorder()
    agent = Agent(
        "a",
        "local",
        FakeModel([[ToolCall("c", "cancelled", {"q": "x"})]]),
        tools=[Tool("cancelled", cancelled)],
    )
    with pytest.raises(asyncio.CancelledError):
        await recorder.run_agent(Runner(), agent, "hi")
    assert recorder.data["tool_fixtures"][0]["error"]["type"] == "CancelledError"
    result = await replay_repro_bundle(export_repro_bundle(recorder))
    assert result.reproduced, result.first_divergence


@pytest.mark.asyncio
async def test_unconsumed_gate_record_diverges() -> None:
    registry = ModuleRegistry()

    async def local(state: dict) -> dict:
        return {}

    registry.register("local", local)
    spec = WorkflowSpec(
        name="gates", entry_node="one", nodes={"one": WorkflowNode(id="one", handler="local")}
    )
    recorder = ReproRecorder()
    await recorder.run_workflow(WorkflowEngine(spec, registry), {})
    recorder.data["workflow_gates"].append({"node": "one", "needed": False})
    result = await replay_repro_bundle(export_repro_bundle(recorder))
    assert not result.reproduced
    assert "unconsumed workflow gates" in result.first_divergence


@pytest.mark.asyncio
async def test_all_resolver_entrypoints_are_blocked() -> None:
    with offline_guard():
        for name in (
            "gethostbyname",
            "gethostbyname_ex",
            "gethostbyaddr",
            "getaddrinfo",
            "getnameinfo",
        ):
            with pytest.raises(RuntimeError, match="Network access blocked"):
                getattr(socket, name)("example.invalid")
        loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="Network access blocked"):
            await loop.getaddrinfo("example.invalid", 443)
        with pytest.raises(RuntimeError, match="Network access blocked"):
            await loop.getnameinfo(("192.0.2.1", 443))


def test_cli_deeply_nested_and_malformed_bundles(tmp_path: Path, capsys) -> None:
    from nexura_runtime.cli.main import main

    path = tmp_path / "invalid.json"
    for content in ("[" * 2000 + "]" * 2000, '{"schema_version":1}', "null", "not json"):
        path.write_text(content)
        assert main(["repro", "run", str(path)]) == 1
        error = capsys.readouterr().err
        assert "Error:" in error
        assert "Traceback" not in error
