"""P35 A2: offline SDK approval authenticity, expiry and atomic single use."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import Agent, ApproveWith, FakeModel, Runner, ToolCall, tool
from nexura_agents.approvals import payload
from nexura_agents.durable import MemoryCheckpointStore, SQLiteCheckpointStore
from nexura_agents.errors import ApprovalDecisionError, SDKValidationError
from nexura_agents.types import RunInterruption

SECRET = b"local-test-signing-key-with-32-bytes"


async def pause(runner: Runner) -> tuple[Agent, RunInterruption, list[int]]:
    ran: list[int] = []

    @tool(requires_approval=True)
    async def transfer(amount: int) -> str:
        """Local fake transfer: record an integer, with no external side effect."""
        ran.append(amount)
        await asyncio.sleep(0)
        return "transferred"

    agent = Agent(
        name="A",
        instructions="",
        tools=[transfer],
        model=FakeModel(
            responses=[[ToolCall("c", "transfer", {"amount": 5})], "done"], repeat_last=True
        ),
    )
    result = await runner.run_async(agent, "go")
    assert result.interruption is not None
    return agent, result.interruption, ran


@pytest.mark.parametrize("field", ["arguments", "digest", "expiry", "run", "transcript", "state"])
async def test_signed_roundtrip_tampering_is_refused(field: str) -> None:
    issuer = Runner(approval_signing_secret=SECRET)
    agent, original, ran = await pause(issuer)
    carried = RunInterruption.from_json(original.to_json())
    if field == "arguments":
        carried.pending[0].arguments["amount"] = 5000
        carried.messages[-1].tool_calls[0].arguments["amount"] = 5000  # type: ignore[index]
        carried.action_digests = carried.pending_digests()  # attacker recomputes public hashes
    elif field == "digest":
        carried.action_digests["c"] = "0" * 64
    elif field == "expiry":
        carried.expires_at += 100000
    elif field == "run":
        carried.run_id = "another-run"
    elif field == "transcript":
        carried.messages[0].content = "changed instructions"
    else:
        carried.state["new"] = "forged"
    with pytest.raises(ApprovalDecisionError, match="signature"):
        await Runner(approval_signing_secret=SECRET).resume_async(agent, carried, approve=True)
    assert ran == []


async def test_signed_copy_allows_cross_runner_approval_and_edits() -> None:
    agent, original, ran = await pause(Runner(approval_signing_secret=SECRET))
    carried = RunInterruption.from_json(original.to_json())
    old_digest = carried.pending_digests()["c"]
    result = await Runner(approval_signing_secret=SECRET).resume_async(
        agent, carried, approve={"c": ApproveWith(arguments={"amount": 3}, digest=old_digest)}
    )
    assert ran == [3]
    executed = next(c for m in result.messages for c in m.tool_calls or [])
    assert executed.arguments == {"amount": 3}
    assert RunInterruption.action_digest(executed) != old_digest
    assert original.pending[0].arguments == {"amount": 5}


async def test_unsigned_fresh_runner_requires_independently_reviewed_digests() -> None:
    agent, original, ran = await pause(Runner())
    reviewed = original.pending_digests()
    carried = RunInterruption.from_json(original.to_json())
    for approve in (True, ["c"], {"c": ApproveWith(arguments={"amount": 3})}):
        with pytest.raises(ApprovalDecisionError, match="reviewed"):
            await Runner().resume_async(agent, carried, approve=approve)
    assert ran == []
    with pytest.raises(ApprovalDecisionError, match="does not match"):
        await Runner().resume_async(agent, carried, approve={"c": "wrong-reviewed-digest"})
    result = await Runner().resume_async(agent, carried, approve=reviewed)
    assert result.output == "done" and ran == [5]


@pytest.mark.parametrize("copied", [False, True])
async def test_internal_snapshot_follows_in_memory_continuation(copied: bool) -> None:
    agent, original, ran = await pause(Runner())
    carried = copy.deepcopy(original) if copied else original
    assert carried._approval_snapshot is not None
    result = await Runner().resume_async(agent, carried, approve=True)
    assert result.output == "done" and ran == [5]


async def test_transferred_snapshot_still_refuses_tampered_arguments() -> None:
    agent, original, ran = await pause(Runner())
    original.pending[0].arguments["amount"] = 5000
    original.messages[-1].tool_calls[0].arguments["amount"] = 5000  # type: ignore[index]
    original.action_digests = original.pending_digests()
    with pytest.raises(ApprovalDecisionError, match="Stored interruption differs"):
        await Runner().resume_async(agent, original, approve=True)
    assert ran == []


async def test_client_json_cannot_supply_private_snapshot_or_trust() -> None:
    agent, original, ran = await pause(Runner())
    assert "_trusted_approval_payload" not in dataclasses.asdict(original)
    raw = json.loads(original.to_json())
    assert "_trusted_approval_payload" not in raw and "_approval_snapshot" not in raw
    raw["_trusted_approval_payload"] = original._approval_snapshot
    raw["_approval_snapshot"] = original._approval_snapshot
    carried = RunInterruption.from_json(json.dumps(raw))
    assert carried._approval_snapshot is None
    with pytest.raises(ApprovalDecisionError, match="reviewed"):
        await Runner().resume_async(agent, carried, approve=True)
    assert ran == []


async def test_store_backed_pause_does_not_issue_a_storeless_shortcut() -> None:
    store = MemoryCheckpointStore()
    agent, original, ran = await pause(Runner(approval_store=store))
    assert original._approval_snapshot is None
    with pytest.raises(ApprovalDecisionError, match="reviewed"):
        await Runner().resume_async(agent, original, approve=True)
    assert ran == []
    result = await Runner(approval_store=store).resume_async(agent, original, approve=True)
    assert result.output == "done" and ran == [5]


async def test_fresh_unsigned_process_cannot_approve_json_without_review() -> None:
    _, original, _ = await pause(Runner())
    code = '''
import sys
from nexura_agents import Agent, FakeModel, Runner, tool
from nexura_agents.errors import ApprovalDecisionError
from nexura_agents.types import RunInterruption
ran = []
@tool(requires_approval=True)
def transfer(amount: int) -> str:
    """Local fake transfer."""
    ran.append(amount)
    return "done"
interruption = RunInterruption.from_json(sys.stdin.read())
agent = Agent(name="A", instructions="", tools=[transfer], model=FakeModel(["done"]))
try:
    Runner().resume(agent, interruption, approve=True)
except ApprovalDecisionError as exc:
    assert "reviewed" in str(exc)
else:
    raise AssertionError("fresh unsigned process approved client JSON")
assert ran == []
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=original.to_json(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_same_runner_copy_rejects_mutation_even_with_recomputed_digest() -> None:
    runner = Runner()
    agent, original, ran = await pause(runner)
    original.pending[0].arguments["amount"] = 5000
    original.messages[-1].tool_calls[0].arguments["amount"] = 5000  # type: ignore[index]
    original.action_digests = original.pending_digests()
    with pytest.raises(ApprovalDecisionError, match="Stored interruption differs"):
        await runner.resume_async(agent, original, approve=True)
    assert ran == []


async def test_expired_and_legacy_missing_expiry_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    runner = Runner(approval_ttl=10, approval_signing_secret=SECRET)
    agent, original, ran = await pause(runner)
    assert now + 9 < original.expires_at < now + 11
    monkeypatch.setattr("nexura_agents.approvals.time.time", lambda: original.expires_at)
    with pytest.raises(ApprovalDecisionError, match="expired"):
        await runner.resume_async(agent, original, approve=True)
    raw = json.loads(original.to_json())
    raw.pop("expires_at")
    legacy = RunInterruption.from_json(json.dumps(raw))
    assert legacy.expires_at == 0
    with pytest.raises(ApprovalDecisionError, match="expired"):
        await Runner().resume_async(agent, legacy, approve=original.pending_digests())
    assert ran == []


async def test_wrong_agent_and_wrong_signing_key_refused() -> None:
    agent, original, ran = await pause(Runner(approval_signing_secret=SECRET))
    with pytest.raises(ApprovalDecisionError, match="belongs to agent"):
        await Runner(approval_signing_secret=SECRET).resume_async(
            agent.clone(name="other"), original, approve=True
        )
    with pytest.raises(ApprovalDecisionError, match="signature"):
        await Runner(approval_signing_secret=b"wrong-key").resume_async(
            agent, original, approve=True
        )
    stripped = copy.deepcopy(original)
    stripped.signature = None
    with pytest.raises(ApprovalDecisionError, match="signature"):
        await Runner(approval_signing_secret=SECRET).resume_async(agent, stripped, approve=True)
    assert ran == []


@pytest.mark.parametrize("signature", ["not-a-digest", "é" * 64, "f" * 64])
async def test_invalid_signature_refused_with_approval_error(signature: str) -> None:
    agent, original, ran = await pause(Runner(approval_signing_secret=SECRET))
    original.signature = signature
    with pytest.raises(ApprovalDecisionError, match="signature"):
        await Runner(approval_signing_secret=SECRET).resume_async(agent, original, approve=True)
    assert ran == []


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("checkpoint", [False, True])
async def test_two_resumes_race_one_consumes_and_replay_survives_cleanup(
    tmp_path: Path, durable: bool, checkpoint: bool
) -> None:
    first: Any = (
        SQLiteCheckpointStore(tmp_path / "approvals.sqlite") if durable else MemoryCheckpointStore()
    )
    second: Any = SQLiteCheckpointStore(tmp_path / "approvals.sqlite") if durable else first
    option = "checkpoint_store" if checkpoint else "approval_store"
    issuer = Runner(**{option: first})
    agent, original, ran = await pause(issuer)
    # Two independent callers, and SQLite handles when durable, race the same envelope.
    results = await asyncio.gather(
        *(
            Runner(**{option: store}).resume_async(
                agent, RunInterruption.from_json(original.to_json()), approve=True
            )
            for store in (first, second)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ApprovalDecisionError) for result in results) == 1
    assert sum(getattr(result, "status", None) == "completed" for result in results) == 1
    assert ran == [5]
    if checkpoint:
        assert await first.load(original.run_id) is None
    if durable:
        first.close()
        second.close()
        second = SQLiteCheckpointStore(tmp_path / "approvals.sqlite")
    with pytest.raises(ApprovalDecisionError, match="consumed"):
        await Runner(**{option: second}).resume_async(agent, original, approve=True)
    assert ran == [5]
    if durable:
        second.close()


async def test_invalid_edit_does_not_consume_stored_approval() -> None:
    store = MemoryCheckpointStore()
    runner = Runner(approval_store=store)
    agent, original, ran = await pause(runner)
    with pytest.raises(ApprovalDecisionError, match="do not fit"):
        await runner.resume_async(agent, original, approve={"c": ApproveWith({"amount": "bad"})})
    await runner.resume_async(agent, original, approve={"c": ApproveWith({"amount": 2})})
    assert ran == [2]
    assert not await store.consume_approval(original.interruption_id, payload(original))


@pytest.mark.parametrize("durable", [False, True])
async def test_store_refuses_tampering_and_checks_its_own_expiry(
    tmp_path: Path, durable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    store: Any = (
        SQLiteCheckpointStore(tmp_path / "expire.sqlite") if durable else MemoryCheckpointStore()
    )
    runner = Runner(approval_store=store)
    agent, original, ran = await pause(runner)
    tampered = RunInterruption.from_json(original.to_json())
    tampered.pending[0].arguments["amount"] = 100
    tampered.messages[-1].tool_calls[0].arguments["amount"] = 100  # type: ignore[index]
    tampered.action_digests = tampered.pending_digests()
    with pytest.raises(ApprovalDecisionError, match="Stored interruption differs"):
        await Runner(approval_store=store).resume_async(agent, tampered, approve=True)
    assert ran == []
    with pytest.raises(ApprovalDecisionError, match="Stored interruption differs"):
        await store.save_approval(original.interruption_id, payload(tampered), original.expires_at)
    # Atomic store admission uses its clock, independently of Runner's earlier check.
    monkeypatch.setattr("nexura_agents.durable.time.time", lambda: original.expires_at)
    assert not await store.consume_approval(original.interruption_id, payload(original))
    if durable:
        store.close()


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
def test_invalid_ttl_is_refused(ttl: Any) -> None:
    with pytest.raises(SDKValidationError, match="approval_ttl"):
        Runner(approval_ttl=ttl)
