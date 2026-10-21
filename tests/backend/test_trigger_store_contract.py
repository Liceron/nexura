"""A6 S10b: one contract for every trigger store (memory, SQLite, Postgres).

``TriggerStore`` is what ``TriggerManager`` and ``TriggerWorker`` rely on: webhook and
schedule configs owned by one workspace, de-duplicated receipts, a schedule compare-and-swap,
and the job queue (claim, heartbeat, reclaim, owner-guarded completion). The same tests run
against ``MemoryTriggerStore``, ``SqliteTriggerStore`` and ``nexura_postgres.PostgresTriggerStore``.

The Postgres parameter needs ``NEXURA_TEST_POSTGRES_URI`` (a disposable database) and skips
without it; each test works in its own schema, dropped afterwards. Races use a second store
instance on the same database (a second connection, or pool, as another worker would).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.service.triggers.errors import TriggerConflictError
from nexura_runtime.service.triggers.models import (
    DurableTriggerJob,
    ScheduleConfig,
    WebhookConfig,
    WebhookReceipt,
)
from nexura_runtime.service.triggers.store import (
    MemoryTriggerStore,
    SqliteTriggerStore,
    TriggerStore,
)

POSTGRES_ENV = "NEXURA_TEST_POSTGRES_URI"


async def _admin(uri: str, statement: str) -> None:
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(uri)
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


class Backend:
    """Opens stores of one kind on one database; ``open()`` again is another worker."""

    def __init__(self, kind: str, tmp_path: Path) -> None:
        self.kind = kind
        self.tmp_path = tmp_path
        self._opened: list[Any] = []
        self._memory: MemoryTriggerStore | None = None
        self.schema: str | None = None
        self.uri: str | None = None
        if kind == "postgres":
            uri = os.environ.get(POSTGRES_ENV)
            if not uri:
                pytest.skip(f"{POSTGRES_ENV} is not configured")
            self.uri = uri
            self.schema = f"trig_contract_{uuid.uuid4().hex[:12]}"
            asyncio.run(_admin(uri, f'CREATE SCHEMA "{self.schema}"'))

    def open(self) -> TriggerStore:
        if self.kind == "memory":
            if self._memory is None:
                self._memory = MemoryTriggerStore()
            return self._memory
        store: Any
        if self.kind == "sqlite":
            store = SqliteTriggerStore(str(self.tmp_path / "triggers.sqlite3"))
        else:
            from nexura_postgres import PostgresTriggerStore

            assert self.uri is not None
            dsn = f"{self.uri}{'&' if '?' in self.uri else '?'}search_path={self.schema}"
            store = PostgresTriggerStore(dsn, secret_key_file=self.tmp_path / "pg.trigger.key")
        self._opened.append(store)
        return store  # type: ignore[no-any-return]

    def close(self) -> None:
        for store in self._opened:
            store.close()
        if self.uri is not None:
            asyncio.run(_admin(self.uri, f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    made = Backend(request.param, tmp_path)
    try:
        yield made
    finally:
        made.close()


@pytest.fixture
def store(backend: Backend) -> TriggerStore:
    return backend.open()


def _job(job_id: str, workspace_id: str = "ws", created_at: float = 100.0) -> DurableTriggerJob:
    return DurableTriggerJob(
        job_id=job_id,
        workspace_id=workspace_id,
        trigger_type="webhook",
        trigger_source_id="ep",
        workflow_id="wf",
        release_id="current",
        payload={"n": 1, "nested": {"list": [1, 2.5, "x"]}},
        created_at=created_at,
    )


def _webhook(endpoint_id: str = "ep", workspace_id: str = "ws", **extra: Any) -> WebhookConfig:
    values: dict[str, Any] = {
        "endpoint_id": endpoint_id,
        "workspace_id": workspace_id,
        "workflow_id": "wf",
        "release_id": "current",
        "secret": "s3cret-value",
    }
    values.update(extra)
    return WebhookConfig(**values)


def _schedule(schedule_id: str = "s1", workspace_id: str = "ws", **extra: Any) -> ScheduleConfig:
    values: dict[str, Any] = {
        "schedule_id": schedule_id,
        "workspace_id": workspace_id,
        "workflow_id": "wf",
        "release_id": "rev:2",
        "interval_seconds": 60.0,
        "timezone": "Asia/Kolkata",
        "missed_run_policy": "fire_all",
        "next_run_at": 1000.25,
        "payload": {"msg": "hi", "n": [1, 2]},
    }
    values.update(extra)
    return ScheduleConfig(**values)


def _race(workers: list[Callable[[], Any]]) -> list[Any]:
    """Run the callables in threads released together; their results in order."""
    barrier = threading.Barrier(len(workers))
    results: list[Any] = [None] * len(workers)
    errors: list[BaseException] = []

    def run(index: int, work: Callable[[], Any]) -> None:
        try:
            barrier.wait()
            results[index] = work()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=pair) for pair in enumerate(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, errors
    return results


# --------------------------------------------------------------------------- webhooks


def test_webhook_configs_round_trip_and_stay_with_their_workspace(store: TriggerStore) -> None:
    store.save_webhook_config(_webhook(replay_window_seconds=90, allow_legacy_signature=False))
    store.save_webhook_config(_webhook("other", workspace_id="ws2"))
    got = store.get_webhook_config("ep")
    assert got == _webhook(replay_window_seconds=90, allow_legacy_signature=False)
    assert store.get_webhook_config("missing") is None
    assert [c.endpoint_id for c in store.list_webhook_configs()] == ["ep", "other"]
    assert [c.endpoint_id for c in store.list_webhook_configs("ws2")] == ["other"]

    # Same workspace: an update; another workspace: refused, nothing changes.
    store.save_webhook_config(_webhook(secret="rotated", is_active=False))
    updated = store.get_webhook_config("ep")
    assert updated is not None and updated.secret == "rotated" and not updated.is_active
    with pytest.raises(TriggerConflictError):
        store.save_webhook_config(_webhook(workspace_id="intruder", secret="stolen"))
    kept = store.get_webhook_config("ep")
    assert kept is not None and kept.workspace_id == "ws" and kept.secret == "rotated"

    assert store.delete_webhook_config("ep") is True
    assert store.delete_webhook_config("ep") is False
    assert store.get_webhook_config("ep") is None


def test_receipts_deduplicate_deliveries_per_endpoint(store: TriggerStore) -> None:
    first = _job("j1")
    receipt = WebhookReceipt(
        event_id="evt-1", endpoint_id="ep", workspace_id="ws", job_id="j1", received_at=5.0
    )
    is_new, job = store.record_webhook_receipt_and_enqueue_job(receipt, first)
    assert is_new and job.job_id == "j1"
    assert store.get_job("j1") == first

    # The same event again: no second job; the existing job comes back as it is now.
    claimed = store.claim_pending_job("j1", "w1", lease_seconds=30, now=10.0)
    assert claimed is not None
    duplicate = receipt.model_copy(update={"job_id": "j2"})
    is_new, job = store.record_webhook_receipt_and_enqueue_job(duplicate, _job("j2"))
    assert not is_new and job.job_id == "j1" and job.status == "claimed"
    assert store.get_job("j2") is None

    # The same event id on another endpoint is another delivery.
    elsewhere = WebhookReceipt(
        event_id="evt-1", endpoint_id="ep2", workspace_id="ws", job_id="j3", received_at=6.0
    )
    is_new, job = store.record_webhook_receipt_and_enqueue_job(elsewhere, _job("j3"))
    assert is_new and job.job_id == "j3"


def test_concurrent_duplicate_deliveries_enqueue_one_job(backend: Backend) -> None:
    stores = [backend.open(), backend.open()]

    def deliver(index: int) -> tuple[bool, DurableTriggerJob]:
        receipt = WebhookReceipt(
            event_id="evt", endpoint_id="ep", workspace_id="ws", job_id=f"j{index}"
        )
        return stores[index].record_webhook_receipt_and_enqueue_job(receipt, _job(f"j{index}"))

    results = _race([lambda: deliver(0), lambda: deliver(1)])
    assert sorted(is_new for is_new, _ in results) == [False, True]
    assert len({job.job_id for _, job in results}) == 1
    assert len(stores[0].list_pending_jobs()) == 1


# -------------------------------------------------------------------------- schedules


def test_schedules_round_trip_and_stay_with_their_workspace(store: TriggerStore) -> None:
    store.save_schedule(_schedule())
    store.save_schedule(_schedule("s2", workspace_id="ws2", cron_expr="0 9 * * *"))
    assert store.get_schedule("s1") == _schedule()
    assert store.get_schedule("missing") is None
    assert sorted(s.schedule_id for s in store.list_schedules()) == ["s1", "s2"]
    assert [s.schedule_id for s in store.list_schedules("ws2")] == ["s2"]

    store.save_schedule(_schedule(is_active=False, payload={"changed": True}))
    updated = store.get_schedule("s1")
    assert updated is not None and not updated.is_active and updated.payload == {"changed": True}
    with pytest.raises(TriggerConflictError):
        store.save_schedule(_schedule(workspace_id="intruder"))
    kept = store.get_schedule("s1")
    assert kept is not None and kept.workspace_id == "ws"

    store.update_schedule_next_fire("s1", next_run_at=2000.5)
    moved = store.get_schedule("s1")
    assert moved is not None and moved.next_run_at == 2000.5 and moved.last_run_at is None
    store.update_schedule_next_fire("s1", next_run_at=3000.0, last_run_at=2000.5)
    moved = store.get_schedule("s1")
    assert moved is not None and (moved.next_run_at, moved.last_run_at) == (3000.0, 2000.5)

    assert store.delete_schedule("s1") is True
    assert store.delete_schedule("s1") is False


def test_advance_schedule_is_a_compare_and_swap(backend: Backend) -> None:
    store = backend.open()
    store.save_schedule(_schedule())
    assert not store.advance_schedule("s1", 999.0, 1060.25)  # stale expectation
    assert not store.advance_schedule("missing", 1000.25, 1060.25)
    assert store.advance_schedule("s1", 1000.25, 1060.25, last_run_at=1000.25)
    got = store.get_schedule("s1")
    assert got is not None and (got.next_run_at, got.last_run_at) == (1060.25, 1000.25)
    # last_run_at is kept when the caller passes none.
    assert store.advance_schedule("s1", 1060.25, 1120.25)
    got = store.get_schedule("s1")
    assert got is not None and got.last_run_at == 1000.25

    other = backend.open()
    results = _race(
        [
            lambda: store.advance_schedule("s1", 1120.25, 1180.25),
            lambda: other.advance_schedule("s1", 1120.25, 1180.25),
        ]
    )
    assert sorted(results) == [False, True]  # one due fire, one winner


# ------------------------------------------------------------------------------- jobs


def test_pending_jobs_are_listed_oldest_first(store: TriggerStore) -> None:
    store.enqueue_trigger_job(_job("late", created_at=300.0))
    store.enqueue_trigger_job(_job("early", created_at=100.0))
    store.enqueue_trigger_job(_job("other-ws", workspace_id="ws2", created_at=50.0))
    store.enqueue_trigger_job(_job("mid", created_at=200.0))
    assert [j.job_id for j in store.list_pending_jobs()] == ["other-ws", "early", "mid", "late"]
    assert [j.job_id for j in store.list_pending_jobs(limit=2)] == ["other-ws", "early"]
    assert [j.job_id for j in store.list_pending_jobs("ws")] == ["early", "mid", "late"]
    store.claim_pending_job("early", "w", lease_seconds=5, now=1.0)
    assert [j.job_id for j in store.list_pending_jobs("ws")] == ["mid", "late"]
    assert store.get_job("missing") is None
    got = store.get_job("late")
    assert got is not None and got.payload == {"n": 1, "nested": {"list": [1, 2.5, "x"]}}


def test_claim_takes_a_pending_job_once_and_counts_the_attempt(store: TriggerStore) -> None:
    store.enqueue_trigger_job(_job("j1"))
    claimed = store.claim_pending_job("j1", "w1", lease_seconds=30, now=1000.0)
    assert claimed is not None
    assert (claimed.status, claimed.claimed_by, claimed.claimed_at) == ("claimed", "w1", 1000.0)
    assert claimed.lease_expires_at == 1030.0 and claimed.attempts == 1
    assert store.claim_pending_job("j1", "w2", lease_seconds=30, now=1001.0) is None
    assert store.claim_pending_job("missing", "w2") is None
    stored = store.get_job("j1")
    assert stored is not None and stored.claimed_by == "w1"


def test_racing_claims_have_one_winner(backend: Backend) -> None:
    first, second = backend.open(), backend.open()
    first.enqueue_trigger_job(_job("j1"))
    results = _race(
        [
            lambda: first.claim_pending_job("j1", "w1", lease_seconds=30, now=10.0),
            lambda: second.claim_pending_job("j1", "w2", lease_seconds=30, now=10.0),
        ]
    )
    winners = [job for job in results if job is not None]
    assert len(winners) == 1 and winners[0].attempts == 1
    stored = first.get_job("j1")
    assert stored is not None and stored.claimed_by == winners[0].claimed_by


def test_only_the_owner_renews_the_lease(store: TriggerStore) -> None:
    store.enqueue_trigger_job(_job("j1"))
    store.claim_pending_job("j1", "w1", lease_seconds=30, now=1000.0)
    assert store.renew_job_lease("j1", "w1", lease_seconds=30, now=1020.0)
    renewed = store.get_job("j1")
    assert renewed is not None and renewed.lease_expires_at == 1050.0
    assert not store.renew_job_lease("j1", "w2", lease_seconds=30, now=1021.0)
    assert not store.renew_job_lease("missing", "w1")
    store.complete_job("j1", expected_worker="w1")
    assert not store.renew_job_lease("j1", "w1")  # finished jobs have no lease


def test_set_job_run_is_owner_only_and_a_job_keeps_one_run(store: TriggerStore) -> None:
    store.enqueue_trigger_job(_job("j1"))
    assert not store.set_job_run("j1", "w1", "run-1")  # not claimed yet
    store.claim_pending_job("j1", "w1", lease_seconds=30, now=1.0)
    assert not store.set_job_run("j1", "intruder", "run-x")
    assert store.set_job_run("j1", "w1", "run-1")
    assert store.set_job_run("j1", "w1", "run-1")  # idempotent
    job = store.get_job("j1")
    assert job is not None and job.run_id == "run-1" and job.previous_run_ids == []
    # A different run replaces it only when one could not be continued (kept as history).
    assert store.set_job_run("j1", "w1", "run-2")
    job = store.get_job("j1")
    assert job is not None and job.run_id == "run-2" and job.previous_run_ids == ["run-1"]


def _claimed_with_run(store: TriggerStore, job_id: str, *, now: float, lease: float) -> None:
    store.enqueue_trigger_job(_job(job_id, created_at=now))
    assert store.claim_pending_job(job_id, "dead", lease_seconds=lease, now=now) is not None
    assert store.set_job_run(job_id, "dead", f"run-{job_id}")


def test_reclaim_keeps_the_run_for_the_new_owner(store: TriggerStore) -> None:
    """A6 S10: the reclaimed job keeps its run; the new owner continues it."""
    _claimed_with_run(store, "j1", now=100.0, lease=10.0)
    assert store.reclaim_expired_jobs("w2", lease_seconds=10, now=105.0) == []  # still live
    got = store.reclaim_expired_jobs("w2", lease_seconds=20, now=110.0)
    assert len(got) == 1
    job = got[0]
    assert (job.status, job.claimed_by, job.attempts) == ("claimed", "w2", 2)
    assert (job.claimed_at, job.lease_expires_at) == (110.0, 130.0)
    assert job.run_id == "run-j1" and job.previous_run_ids == []
    assert store.get_job("j1") == job
    # The old owner lost it: no renewal, no run change, no completion.
    assert not store.renew_job_lease("j1", "dead", lease_seconds=10)
    assert not store.set_job_run("j1", "dead", "run-other")
    stale = store.complete_job("j1", error_message="late", expected_worker="dead")
    assert stale is not None and stale.status == "claimed" and stale.claimed_by == "w2"


def test_reclaim_in_new_run_mode_moves_the_run_aside(store: TriggerStore) -> None:
    """Deprecated ``retry_mode="new_run"``: the run id moves to ``previous_run_ids``."""
    _claimed_with_run(store, "j1", now=100.0, lease=10.0)
    got = store.reclaim_expired_jobs("w2", lease_seconds=10, now=200.0, keep_run=False)
    assert len(got) == 1
    assert got[0].run_id is None and got[0].previous_run_ids == ["run-j1"]
    assert store.set_job_run("j1", "w2", "run-new")
    job = store.get_job("j1")
    assert job is not None and job.run_id == "run-new" and job.previous_run_ids == ["run-j1"]


def test_reclaim_honours_exclusions_limit_and_age_order(store: TriggerStore) -> None:
    for index, job_id in enumerate(("a", "b", "c")):
        _claimed_with_run(store, job_id, now=100.0 + index, lease=5.0)
    assert store.reclaim_expired_jobs("w2", lease_seconds=5, now=500.0, limit=0) == []
    got = store.reclaim_expired_jobs(
        "w2", lease_seconds=5, now=500.0, limit=1, exclude_job_ids={"a"}
    )
    assert [j.job_id for j in got] == ["b"]  # oldest claim first, "a" excluded
    got = store.reclaim_expired_jobs("w3", lease_seconds=5, now=500.0)
    assert sorted(j.job_id for j in got) == ["a", "c"]  # "b" is live again (w2's lease)


def test_reclaim_fails_jobs_out_of_attempts(store: TriggerStore) -> None:
    _claimed_with_run(store, "j1", now=100.0, lease=5.0)
    got = store.reclaim_expired_jobs("w2", lease_seconds=5, max_attempts=2, now=200.0)
    assert [j.attempts for j in got] == [2]
    got = store.reclaim_expired_jobs("w3", lease_seconds=5, max_attempts=2, now=300.0, limit=0)
    assert len(got) == 1  # an exhausted job fails even when no retry slot is free
    failed = got[0]
    assert failed.status == "failed" and failed.error_message == "worker lease expired 2 times"
    assert failed.lease_expires_at is None and failed.completed_at == 300.0
    assert failed.run_id == "run-j1"  # it keeps its run
    assert store.reclaim_expired_jobs("w4", lease_seconds=5, max_attempts=2, now=999.0) == []


def test_racing_reclaimers_have_one_winner(backend: Backend) -> None:
    first, second = backend.open(), backend.open()
    for job_id in ("j1", "j2"):
        _claimed_with_run(first, job_id, now=100.0, lease=5.0)
    results = _race(
        [
            lambda: first.reclaim_expired_jobs("w1", lease_seconds=60, now=200.0),
            lambda: second.reclaim_expired_jobs("w2", lease_seconds=60, now=200.0),
        ]
    )
    won = [job for jobs in results for job in jobs]
    assert sorted(job.job_id for job in won) == ["j1", "j2"]  # each job exactly once
    for job in won:
        stored = first.get_job(job.job_id)
        assert stored is not None and stored.claimed_by == job.claimed_by
        assert stored.attempts == 2 and stored.run_id == f"run-{job.job_id}"


def test_complete_job_is_guarded_by_the_owner(store: TriggerStore) -> None:
    store.enqueue_trigger_job(_job("j1"))
    store.claim_pending_job("j1", "w1", lease_seconds=30, now=1.0)
    store.set_job_run("j1", "w1", "run-1")
    assert store.complete_job("missing") is None
    not_owner = store.complete_job("j1", expected_worker="w2")
    assert not_owner is not None and not_owner.status == "claimed"
    done = store.complete_job("j1", expected_worker="w1")
    assert done is not None and done.status == "completed" and done.run_id == "run-1"
    assert done.lease_expires_at is None and done.completed_at is not None
    assert done.error_message is None
    # Finished: an owner-guarded completion is a no-op.
    again = store.complete_job("j1", error_message="late", expected_worker="w1")
    assert again is not None and again.status == "completed"

    store.enqueue_trigger_job(_job("j2"))
    store.claim_pending_job("j2", "w1", lease_seconds=30, now=1.0)
    failed = store.complete_job("j2", error_message="boom", run_id="run-2", expected_worker="w1")
    assert failed is not None and failed.status == "failed"
    assert failed.error_message == "boom" and failed.run_id == "run-2"


def test_the_store_clock_is_used_when_no_time_is_given(store: TriggerStore) -> None:
    """Without ``now`` a store times leases itself (Postgres: the database clock)."""
    store.enqueue_trigger_job(_job("j1"))
    before = time.time()
    claimed = store.claim_pending_job("j1", "w1", lease_seconds=30)
    assert claimed is not None and claimed.claimed_at is not None
    assert claimed.lease_expires_at is not None
    assert abs(claimed.claimed_at - before) < 5
    assert claimed.lease_expires_at - claimed.claimed_at == pytest.approx(30.0)
    assert store.renew_job_lease("j1", "w1", lease_seconds=60)
    renewed = store.get_job("j1")
    assert renewed is not None and renewed.lease_expires_at is not None
    assert renewed.lease_expires_at > claimed.lease_expires_at
    # A live lease is not reclaimed on the store's clock.
    assert store.reclaim_expired_jobs("w2", lease_seconds=60) == []
