"""Self-tests of the A6 kill harness, plus the legacy DurableExecutionEngine kill row.

The legacy row is the one path with a Postgres store today, so it is what exercises the
harness against ``NEXURA_TEST_POSTGRES_URI`` (skipped when that is unset).
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
import urllib.request
import uuid
from typing import Any

import pytest
from durability_harness import Backend, Harness, HarnessError, process_state

pytestmark = pytest.mark.durability


def _post(url: str, body: dict[str, Any]) -> bool:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        applied: bool = json.loads(response.read())["applied"]
    return applied


def test_effect_service_counts_real_effects_and_deduplicates_keys(harness: Harness) -> None:
    url = harness.effects.url
    assert _post(url, {"effect": "pay", "who": "a"}) is True
    assert _post(url, {"effect": "pay", "who": "b"}) is True  # no key: both happen
    assert _post(url, {"effect": "ship", "key": "k1", "who": "a"}) is True
    assert _post(url, {"effect": "ship", "key": "k1", "who": "b"}) is False  # de-duplicated
    ledger = harness.ledger
    assert (ledger.applied("pay"), ledger.requests("pay")) == (2, 2)
    assert (ledger.applied("ship"), ledger.requests("ship")) == (1, 2)
    assert ledger.applied(who="b") == 1
    assert [row["key"] for row in ledger.request_rows("ship")] == ["k1", "k1"]


def test_sigkill_at_a_point_and_reap(harness: Harness) -> None:
    child = harness.spawn("selftest", "victim", block={"ready": "forever"})
    marker = child.wait_marker("ready")
    assert marker["pid"] == child.pid
    child.kill()
    assert child.proc.returncode == -signal.SIGKILL
    assert not child.reached("after")
    assert process_state(child.pid) is None


def test_sigstop_freezes_a_child_until_sigcont(harness: Harness) -> None:
    child = harness.spawn("selftest", "frozen", block={"ready": "go"})
    child.wait_marker("ready")
    child.stop()
    assert process_state(child.pid) == "T"
    child.go("ready")
    time.sleep(0.3)
    assert not child.reached("ready.released")  # stopped: it cannot see the go file
    child.cont()
    child.wait_marker("ready.released")
    assert child.result() == {"ok": True}


def test_child_that_dies_early_is_a_harness_error_not_an_assertion(harness: Harness) -> None:
    child = harness.spawn("selftest", "broken", crash=True)
    with pytest.raises(HarnessError, match="selftest child crashed on purpose"):
        child.wait_marker("ready")
    with pytest.raises(HarnessError):
        child.result()


def test_store_wrapper_blocks_after_the_commit(harness: Harness) -> None:
    """The point fires after the checkpoint is committed: a kill there leaves it on disk."""
    from nexura_agents import SQLiteCheckpointStore

    child = harness.spawn(
        "agent", "run", mode="run", tool="charge", block={"after_tools_pending": "forever"}
    )
    run_id = child.wait_marker("after_tools_pending")["run_id"]
    child.kill()
    store = SQLiteCheckpointStore(harness.db_path)
    try:
        raw = asyncio.run(store.load(run_id))
    finally:
        store.close()
    assert raw is not None
    envelope = json.loads(raw)
    assert envelope["phase"] == "tools_pending"
    assert [c["id"] for c in json.loads(envelope["interruption"])["pending"]] == ["c1"]
    assert harness.ledger.applied() == 0  # blocked before the tool ran


def test_close_leaves_no_child_running(harness: Harness) -> None:
    stopped = harness.spawn("selftest", "stopped", block={"ready": "forever"})
    sleeping = harness.spawn("selftest", "sleeping", block={"ready": "forever"})
    stopped.wait_marker("ready")
    sleeping.wait_marker("ready")
    stopped.stop()
    harness.close()
    for child in (stopped, sleeping):
        assert child.proc.returncode == -signal.SIGKILL
        assert process_state(child.pid) is None
    harness.close()  # idempotent: the fixture closes it again


@pytest.mark.parametrize("backend", ["sqlite", "postgres"], indirect=True)
def test_legacy_engine_kill_after_remote_commit_is_indeterminate(
    harness: Harness, backend: Backend
) -> None:
    """Legacy ``DurableExecutionEngine``, K3': already correct on both backends.

    SIGKILL after the remote committed; a new process takes over the expired lease and
    gets ``IndeterminateEffectError`` instead of repeating the transfer.
    """
    backend.require("legacy")
    run_id = f"legacy-{uuid.uuid4().hex[:12]}"
    first = harness.spawn(
        "legacy", "owner", run_id=run_id, init=True, block={"in_effect": "forever"}
    )
    first.wait_marker("in_effect", timeout=30)
    first.kill()
    assert harness.ledger.applied("transfer") == 1

    second = harness.spawn("legacy", "taker", run_id=run_id)
    result = second.result()
    assert result["error_type"] == "IndeterminateEffectError", result
    assert harness.ledger.applied("transfer") == 1
