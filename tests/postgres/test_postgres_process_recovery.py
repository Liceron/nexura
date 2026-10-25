"""Real PostgreSQL worker processes contending for one run lease of the execution journal.

This test ran on the legacy ``DurableExecutionEngine`` until A6 S12 removed that engine. Its
assertions now run on the replacement, ``PostgresExecutionStore`` (the lease and fencing every
durable path uses), in spawned processes against a disposable database:

- two processes race for the run: exactly one gets the lease, the other is refused;
- the winner is killed; once its lease expired (database clock) a new process takes the run
  with a higher fencing token and commits;
- a process still holding the killed owner's lease handle cannot write anything.

The lost-response case (SIGKILL after the remote committed; recovery must not repeat it) runs
on ``WorkflowEngine`` with ``PostgresWorkflowStore`` in
``tests/durability/test_durability_workflows.py::test_mid_node_kill_recover_pauses_effect_unknown``.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
import uuid
from typing import Any

import pytest
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.execution_store import ExecutionLease
from nexura_postgres.execution_store import PostgresExecutionStore

WORKSPACE = "recovery"
DIGEST = "process-recovery-v1"


def _worker(
    mode: str,
    dsn: str,
    run_id: str,
    queue: Any,
    start: Any,
    stale: dict[str, Any] | None = None,
) -> None:
    async def execute() -> None:
        store = PostgresExecutionStore(dsn)
        identity = ExecutionId(WORKSPACE, run_id)
        try:
            queue.put({"phase": "ready"})
            start.wait(10)
            if mode == "stale":
                assert stale is not None
                lease = ExecutionLease(identity, stale["owner"], stale["token"], stale["expires"])
                await store.commit(lease, state={"written_by": "stale"}, status="completed")
                queue.put({"phase": "stale_write_accepted"})
                return
            owner = f"worker-{os.getpid()}"
            lease = await store.claim(identity, owner, 0.7, definition_digest=DIGEST)
            if lease is None:
                queue.put({"phase": "refused"})
                return
            queue.put(
                {
                    "phase": "claimed",
                    "owner": lease.owner,
                    "token": lease.fencing_token,
                    "expires": lease.expires_at,
                }
            )
            if mode == "claim":
                await asyncio.sleep(60)  # killed here by the parent
            else:
                await store.commit(lease, state={"written_by": owner}, status="completed")
                queue.put({"phase": "finished", "status": "completed"})
        except Exception as exc:
            queue.put({"phase": "error", "type": type(exc).__name__})
        finally:
            await store.close()

    asyncio.run(execute())


@pytest.fixture
def postgres_dsn() -> str:
    value = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not value:
        pytest.skip("Disposable PostgreSQL required for actual process recovery tests")
    return value


async def _initialize(dsn: str, run_id: str) -> None:
    store = PostgresExecutionStore(dsn)
    try:
        await store.init_db()
        run = await store.create(
            ExecutionId(WORKSPACE, run_id), owner="initializer", definition_digest=DIGEST
        )
        await store.release(run.lease)
    finally:
        await store.close()


async def _snapshot(dsn: str, run_id: str) -> Any:
    store = PostgresExecutionStore(dsn)
    try:
        return await store.get(ExecutionId(WORKSPACE, run_id))
    finally:
        await store.close()


def _kill(process: Any) -> None:
    if process.is_alive():
        process.kill()
    process.join(timeout=5)
    assert not process.is_alive()


def _outcome(queue: Any) -> dict[str, Any]:
    while True:
        result: dict[str, Any] = queue.get(timeout=15)
        if result["phase"] != "ready":
            return result


def test_real_postgres_workers_contend_and_stale_process_is_fenced(postgres_dsn: str) -> None:
    run_id = uuid.uuid4().hex
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        runner.run(_initialize(postgres_dsn, run_id))
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outputs = [context.Queue(), context.Queue()]
    workers = [
        context.Process(target=_worker, args=("claim", postgres_dsn, run_id, queue, start))
        for queue in outputs
    ]
    for worker in workers:
        worker.start()
    for queue in outputs:
        assert queue.get(timeout=15) == {"phase": "ready"}
    start.set()
    try:
        results = [_outcome(queue) for queue in outputs]
        assert sum(result["phase"] == "claimed" for result in results) == 1
        assert sum(result["phase"] == "refused" for result in results) == 1
        winner = next(result for result in results if result["phase"] == "claimed")
    finally:
        for worker in workers:
            _kill(worker)

    # A new process takes over the expired lease after the owner was killed.
    time.sleep(0.8)
    queue = context.Queue()
    replacement = context.Process(
        target=_worker, args=("execute", postgres_dsn, run_id, queue, start)
    )
    replacement.start()
    try:
        claimed = _outcome(queue)
        assert claimed["phase"] == "claimed", claimed
        assert claimed["token"] > winner["token"]
        assert _outcome(queue) == {"phase": "finished", "status": "completed"}
    finally:
        _kill(replacement)

    queue = context.Queue()
    stale = context.Process(
        target=_worker, args=("stale", postgres_dsn, run_id, queue, start, winner)
    )
    stale.start()
    try:
        assert _outcome(queue) == {"phase": "error", "type": "LeaseLostError"}
    finally:
        _kill(stale)
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        snapshot = runner.run(_snapshot(postgres_dsn, run_id))
    assert snapshot.run.status == "completed"
    assert snapshot.run.state == {"written_by": claimed["owner"]}
