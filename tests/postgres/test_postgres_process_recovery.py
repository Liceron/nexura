"""Real PostgreSQL worker processes and an independently durable effect service."""

from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from nexura_postgres import PostgresRepository

from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableNodeSpec,
    DurableWorkflowSpec,
)


def _spec(effect: bool = False) -> DurableWorkflowSpec:
    return DurableWorkflowSpec(
        workflow_id="process-recovery",
        entry_node="action",
        terminal_nodes=["action"],
        nodes={
            "action": DurableNodeSpec(
                node_id="action",
                handler="action",
                effect_policy="non_idempotent_write" if effect else "read",
            )
        },
    )


def _worker(
    mode: str,
    dsn: str,
    run_id: str,
    queue: Any,
    start: Any,
    effect_url: str = "",
    old_revision: int = 0,
) -> None:
    async def execute() -> None:
        repository = PostgresRepository(dsn=dsn)
        engine = DurableExecutionEngine(repository)

        async def action(state: dict[str, Any]) -> dict[str, Any]:
            if effect_url:

                def send() -> None:
                    request = urllib.request.Request(
                        effect_url,
                        data=json.dumps({"run_id": run_id}).encode(),
                        headers={"Content-Type": "application/json"},
                    )
                    with contextlib.suppress(urllib.error.URLError, OSError):
                        urllib.request.urlopen(request, timeout=5).close()

                await asyncio.to_thread(send)
                queue.put({"phase": "remote_committed"})
                # Parent kills this process after the remote ledger has committed.
                await asyncio.sleep(60)
            return {"finished": True}

        engine.register_handler("action", action)
        try:
            queue.put({"phase": "ready"})
            start.wait(10)
            if mode == "stale":
                await repository.update_run("recovery", run_id, old_revision, "completed")
                queue.put({"phase": "stale_write_accepted"})
                return
            claim = await engine.claim_run(
                "recovery",
                run_id,
                str(os.getpid()),
                lease_duration=0.7,
            )
            queue.put(
                {"phase": "claimed", "token": claim.fencing_token, "revision": claim.revision}
            )
            if mode == "claim":
                await asyncio.sleep(60)
            else:
                result = await engine.execute_step(
                    "recovery",
                    run_id,
                    str(os.getpid()),
                    _spec(bool(effect_url)),
                )
                queue.put({"phase": "finished", "status": result.status})
        except Exception as exc:
            queue.put({"phase": "error", "type": type(exc).__name__})
        finally:
            await repository.close()

    asyncio.run(execute())


def _effect_service(path: str, queue: Any, port: int = 0) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS effects (run_id TEXT NOT NULL)")
                db.execute("INSERT INTO effects VALUES (?)", (data["run_id"],))
            # Simulate success whose HTTP response was lost.
            self.close_connection = True

        def log_message(self, format: str, *args: Any) -> None:
            pass

    class ReusableServer(HTTPServer):
        allow_reuse_address = True

    server = ReusableServer(("127.0.0.1", port), Handler)
    queue.put(server.server_port)
    server.serve_forever()


@pytest.fixture
def postgres_dsn() -> str:
    value = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not value:
        pytest.skip("Disposable PostgreSQL required for actual process recovery tests")
    return value


async def _initialize(dsn: str, run_id: str, effect: bool = False) -> None:
    repository = PostgresRepository(dsn=dsn)
    try:
        await repository.init_db()
        await DurableExecutionEngine(repository).init_run("recovery", run_id, _spec(effect))
    finally:
        await repository.close()


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
        assert sum(result.get("type") == "FencingConflictError" for result in results) == 1
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
        assert claimed["token"] > winner["token"]
        assert _outcome(queue) == {"phase": "finished", "status": "completed"}
    finally:
        _kill(replacement)

    queue = context.Queue()
    stale = context.Process(
        target=_worker, args=("stale", postgres_dsn, run_id, queue, start, "", winner["revision"])
    )
    stale.start()
    try:
        assert _outcome(queue) == {"phase": "error", "type": "SessionConflictError"}
    finally:
        _kill(stale)


def test_lost_remote_response_after_worker_kill_is_not_replayed(
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    ledger = tmp_path / "external-effects.sqlite3"
    queue = context.Queue()
    external = context.Process(target=_effect_service, args=(str(ledger), queue))
    external.start()
    port = queue.get(timeout=15)
    run_id = uuid.uuid4().hex
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        runner.run(_initialize(postgres_dsn, run_id, effect=True))
    start = context.Event()
    start.set()
    output = context.Queue()
    worker = context.Process(
        target=_worker,
        args=("execute", postgres_dsn, run_id, output, start, f"http://127.0.0.1:{port}/effect"),
    )
    worker.start()
    try:
        assert _outcome(output)["phase"] == "claimed"
        assert _outcome(output) == {"phase": "remote_committed"}
    finally:
        _kill(worker)
        _kill(external)

    # Reopen the external service on the same ledger; its state is independent.
    external = context.Process(target=_effect_service, args=(str(ledger), queue, port))
    external.start()
    assert queue.get(timeout=15) == port
    time.sleep(0.8)
    output = context.Queue()
    replacement = context.Process(
        target=_worker,
        args=("execute", postgres_dsn, run_id, output, start, f"http://127.0.0.1:{port}/effect"),
    )
    replacement.start()
    try:
        assert _outcome(output)["phase"] == "claimed"
        assert _outcome(output) == {"phase": "error", "type": "IndeterminateEffectError"}
        with sqlite3.connect(ledger) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM effects WHERE run_id=?", (run_id,)).fetchone()[0]
                == 1
            )
    finally:
        _kill(replacement)
        _kill(external)
