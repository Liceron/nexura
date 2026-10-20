"""P32a: credential-free offline recordings exercise the existing executors."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest
from nexura_agents import Agent, FakeModel, Runner, Tool, ToolCall
from nexura_agents.repro import (
    ReproBundle,
    ReproRecorder,
    export_repro_bundle,
    offline_guard,
    replay_repro_bundle,
)

from nexura_runtime.cli.main import main
from nexura_runtime.storage.repository import SQLiteRepository
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


async def agent_record(*, approval: bool = False, secret: str = "") -> ReproRecorder:
    async def lookup(item: int) -> str:
        if item == 2:
            raise ValueError("fixture failed " + secret)
        return "first output " + secret

    agent = Agent(
        "recorded",
        "Use lookup",
        FakeModel(
            [
                [ToolCall("a", "lookup", {"item": 1})],
                [ToolCall("b", "lookup", {"item": 2})],
            ]
        ),
        tools=[Tool("lookup", lookup, max_retries=0, requires_approval=approval)],
    )
    recorder = ReproRecorder(credential_values={"DEMO_KEY": secret} if secret else {})
    if approval:
        first = await recorder.run_agent(Runner(), agent, "input")
        second = await recorder.resume_agent(first.interruption, approve=True)
        with pytest.raises(Exception, match="fixture failed"):
            await recorder.resume_agent(second.interruption, approve=True)
    else:
        with pytest.raises(Exception, match="fixture failed"):
            await recorder.run_agent(Runner(), agent, "input")
    return recorder


@pytest.mark.asyncio
async def test_agent_roundtrip_failure_and_real_tool_never_called(tmp_path: Path) -> None:
    recorder = await agent_record()
    bundle = export_repro_bundle(recorder)
    path = tmp_path / "agent.json"
    bundle.save(path)
    # This blocks every original function Tool entrypoint, not just the demo function.
    with (
        patch.object(Tool, "execute", side_effect=AssertionError("real tool called")),
        patch.object(socket.socket, "connect", side_effect=AssertionError("real network")),
    ):
        result = await replay_repro_bundle(ReproBundle.load(path))
    assert result.reproduced, result.first_divergence
    assert result.outcome["status"] == "failed"
    assert result.outcome["failing_step"] == "lookup"
    assert len(bundle.data["model_responses"]) == 2
    assert bundle.data["tool_fixtures"][0]["output"] == "first output "


@pytest.mark.asyncio
async def test_approval_decisions_replayed_without_real_tools() -> None:
    bundle = export_repro_bundle(await agent_record(approval=True))
    assert [d["approve"] for d in bundle.data["approvals"]] == [True, True]
    with patch.object(Tool, "execute", side_effect=AssertionError("real tool called")):
        result = await replay_repro_bundle(bundle)
    assert result.reproduced, result.first_divergence


@pytest.mark.asyncio
async def test_workflow_roundtrip_and_approval(tmp_path: Path) -> None:
    registry = ModuleRegistry()
    calls: list[str] = []

    @registry.register_handler("first")
    async def first(state: dict) -> dict:
        calls.append("first")
        return {"seen": state["item"]}

    @registry.register_handler("fail")
    async def fail(state: dict) -> dict:
        calls.append("fail")
        raise ValueError("workflow fixture failure")

    spec = WorkflowSpec(
        name="recorded",
        entry_node="one",
        nodes={
            "one": WorkflowNode(id="one", handler="first", default_transition="two"),
            "two": WorkflowNode(id="two", handler="fail", requires_approval=True),
        },
    )
    recorder = ReproRecorder()
    paused = await recorder.run_workflow(
        WorkflowEngine(spec, registry, store=MemoryWorkflowStore()), {"item": 3}
    )
    with pytest.raises(Exception, match="workflow fixture failure"):
        await recorder.resume_workflow(paused.workflow_run_id, paused.pending_approval.approval_id)
    path = tmp_path / "workflow.json"
    export_repro_bundle(recorder).save(path)
    result = await replay_repro_bundle(ReproBundle.load(path))
    assert result.reproduced, result.first_divergence
    assert result.outcome["failing_step"] == "two"
    assert calls == ["first", "fail"]


@pytest.mark.asyncio
async def test_edited_fixture_diverges() -> None:
    bundle = export_repro_bundle(await agent_record())
    bundle.data["tool_fixtures"][0]["output"] = "changed"
    result = await replay_repro_bundle(bundle)
    assert not result.reproduced
    assert "digest mismatch" in result.first_divergence


@pytest.mark.asyncio
async def test_unknown_arguments_refused_even_with_updated_fingerprint() -> None:
    bundle = export_repro_bundle(await agent_record())
    bundle.data["tool_fixtures"][0]["arguments"] = {"item": 99}
    bundle = export_repro_bundle(bundle)
    result = await replay_repro_bundle(bundle)
    assert not result.reproduced
    assert "arguments diverged" in result.first_divergence


@pytest.mark.asyncio
async def test_secrets_never_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "planted-fake-key-NEVER-WRITE-123"
    monkeypatch.setenv("REF_KEY", secret)
    recorder = await agent_record(secret=secret)
    recorder.data["extras"] = {
        "api_key": secret,
        "headers": {"Authorization": "Bearer " + secret, "Cookie": secret},
        "connection": {"ref": "REF_KEY", "value": secret},
        "copy": "prefix " + secret,
    }
    path = tmp_path / "secret.json"
    export_repro_bundle(recorder).save(path)
    saved = path.read_text()
    assert secret not in saved
    assert "Bearer " not in saved
    assert json.loads(saved)["extras"]["connection"] == {"$redacted": "REF_KEY"}


def test_schema_version_mismatch_and_incomplete_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version": 99}')
    with pytest.raises(ValueError, match="schema_version"):
        ReproBundle.load(path)
    with pytest.raises(ValueError, match="schema_version"):
        export_repro_bundle({"status": "failed", "state": {}})


def test_network_guard_blocks_connect_dns_and_datagrams() -> None:
    # Invoke the guarded methods directly: this sandbox forbids AF_INET socket creation.
    with offline_guard():
        for operation in (
            lambda: socket.socket.connect(None, ("127.0.0.1", 9)),
            lambda: socket.socket.connect_ex(None, ("127.0.0.1", 9)),
            lambda: socket.getaddrinfo("example.invalid", 443),
            lambda: socket.socket.sendto(None, b"x", ("127.0.0.1", 9)),
        ):
            with pytest.raises(RuntimeError, match="Network access blocked"):
                operation()


@pytest.mark.asyncio
async def test_guard_is_active_during_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = export_repro_bundle(await agent_record())
    original = FakeModel.generate
    attempts: list[str] = []

    async def probe(self, *args, **kwargs):
        with pytest.raises(RuntimeError, match="Network access blocked"):
            socket.socket.connect(None, ("127.0.0.1", 9))
        attempts.append("blocked")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(FakeModel, "generate", probe)
    result = await replay_repro_bundle(bundle)
    assert not result.reproduced
    assert result.first_divergence == "Network access attempted during repro replay"
    assert attempts == ["blocked", "blocked"]


def test_cli_storage_export_replay_and_divergence(tmp_path: Path, capsys) -> None:
    path = tmp_path / "bundle.json"
    db = tmp_path / "runs.sqlite3"

    async def prepare() -> None:
        bundle = export_repro_bundle(await agent_record())
        repository = SQLiteRepository(db)
        await repository.create_run("default", "demo", metadata={"repro": bundle.data})
        await repository.close()

    asyncio.run(prepare())
    assert main(["repro", "export", "demo", "--db", str(db), "-o", str(path)]) == 0
    assert main(["repro", "run", str(path)]) == 0
    assert "reproduced: True" in capsys.readouterr().out
    data = json.loads(path.read_text())
    data["tool_fixtures"][0]["output"] = "edited"
    path.write_text(json.dumps(data))
    assert main(["repro", "run", str(path)]) == 1


@pytest.mark.asyncio
async def test_usage_limit_failure_and_budget_config_are_portable() -> None:
    from nexura_agents import UsageLimits
    from nexura_agents.usage import BudgetConfig, BudgetManager

    budget = BudgetManager().create_run_budget(
        "local", "recorded", config=BudgetConfig(max_turns=4)
    )
    recorder = ReproRecorder()
    runner = Runner(budget=budget, usage_limits=UsageLimits(request_limit=0))
    with pytest.raises(Exception, match="request_limit"):
        await recorder.run_agent(runner, Agent("limited", "local", FakeModel(["done"])), "hi")
    bundle = export_repro_bundle(recorder)
    assert bundle.data["budget_config"]["max_turns"] == 4
    replay = await replay_repro_bundle(bundle)
    assert replay.reproduced, replay.first_divergence


@pytest.mark.asyncio
async def test_completed_and_rejected_recordings() -> None:
    recorder = ReproRecorder()
    await recorder.run_agent(Runner(), Agent("ok", "local", FakeModel(["done"])), "hello")
    assert (await replay_repro_bundle(export_repro_bundle(recorder))).reproduced

    async def forbidden() -> str:
        raise AssertionError("rejected tool executed")

    recorder = ReproRecorder()
    agent = Agent(
        "gated",
        "local",
        FakeModel([[ToolCall("gate", "forbidden", {})], "denied"]),
        tools=[Tool("forbidden", forbidden, requires_approval=True)],
    )
    paused = await recorder.run_agent(Runner(), agent, "hello")
    await recorder.resume_agent(paused.interruption, reject={"gate": "no"})
    assert (await replay_repro_bundle(export_repro_bundle(recorder))).reproduced


@pytest.mark.asyncio
async def test_workflow_parallel_node_is_an_opaque_fixture() -> None:
    registry = ModuleRegistry()

    @registry.register_handler("branch")
    async def branch(state: dict) -> dict:
        return {"seen": True}

    spec = WorkflowSpec(
        name="composite",
        entry_node="parallel",
        nodes={
            "parallel": WorkflowNode(id="parallel", handler="", parallel=["branch"]),
        },
    )
    recorder = ReproRecorder()
    await recorder.run_workflow(WorkflowEngine(spec, registry), {})
    result = await replay_repro_bundle(export_repro_bundle(recorder))
    assert result.reproduced, result.first_divergence
