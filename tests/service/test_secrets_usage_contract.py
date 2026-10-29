# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Encrypted secret store and usage ledger contracts on both backends (MVP Postgres WP5).

Every parametrised test runs the same assertions against the SQLite stores
(``EncryptedSecretStore``, ``UsageLedger``) and the Postgres ones (``PostgresSecretStore``,
``PostgresUsageLedger``; skipped without ``NEXURA_TEST_POSTGRES_URI``). Each ``open`` on Postgres
is a new instance with a pool of its own, as another service instance has. The guarantees:

* secrets are envelope encrypted at rest, bound to workspace and name, and fail closed with
  ``MasterKeyMismatchError`` under another master key; a replace keeps ``created_at`` and
  ``created_by``; concurrent writes of one name never fail; the envelope format is the same on
  both backends (a row copied across decrypts with the same key);
* concurrent reservations from several instances, and from several **processes**, never exceed a
  cap; a ledger sees what another instance wrote; a failed operation leaves no phantom state;
* on Postgres every ledger operation waits for the global lock, while ``get_entries`` (run
  diagnostics) does not take it.

Secret values in these tests are fixtures. No test prints one: assertions compare values, and
failure messages never include them. Everything is local: no model, no network beyond the test
database.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.usage.errors import (
    BudgetExceededError,
    CrossWorkspaceAccessError,
    ReservationNotFoundError,
)
from nexura_agents.usage.ledger import UsageLedger
from nexura_agents.usage.models import BudgetConfig, TokenUsage

from nexura_runtime.secrets.errors import (
    MasterKeyMismatchError,
    SecretNotFoundError,
    SecretsError,
)
from nexura_runtime.secrets.keys import StaticMasterKeyProvider, generate_master_key
from nexura_runtime.secrets.store import EncryptedSecretStore

SENTINEL = "sk-live-WP5-SENTINEL-0123456789abcdefXYZ"
CHILD = Path(__file__).with_name("_usage_budget_child.py")


def _provider() -> StaticMasterKeyProvider:
    return StaticMasterKeyProvider.from_text(generate_master_key())


class _Backend:
    """Opens stores of one kind on this test's storage; each call is a new instance."""

    def __init__(self, backend: str, tmp_path: Path, request: pytest.FixtureRequest) -> None:
        self.backend = backend
        self.path = tmp_path / "service.sqlite3"
        self.url = request.getfixturevalue("service_database_url") if backend == "postgres" else ""
        self.databases: list[Any] = []

    def database(self) -> Any:
        from nexura_postgres import Database

        database = Database(self.url)  # a pool of its own, as another instance has
        self.databases.append(database)
        return database

    def secrets(self, provider: Any) -> Any:
        if self.backend == "sqlite":
            return EncryptedSecretStore(provider, self.path)
        from nexura_postgres import PostgresSecretStore

        return PostgresSecretStore(provider, self.database())

    def ledger(self) -> Any:
        if self.backend == "sqlite":
            return UsageLedger(str(self.path))
        from nexura_postgres import PostgresUsageLedger

        return PostgresUsageLedger(self.database())

    def sql(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        """One statement on the store's own tables (``?`` placeholders), rows as tuples."""
        if self.backend == "sqlite":
            with closing(sqlite3.connect(self.path)) as conn, conn:
                return [tuple(r) for r in conn.execute(sql, params).fetchall()]
        import re

        counter = iter(range(1, len(params) + 1))
        statement = re.sub(r"\?", lambda _: f"${next(counter)}", sql)

        async def run(conn: Any) -> Any:
            return await conn.fetch(statement, *params)

        return [tuple(r) for r in self.databases[0].run(run)]

    def close(self) -> None:
        for database in self.databases:
            database.close()


@pytest.fixture
def stores(backend: str, tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_Backend]:
    opened = _Backend(backend, tmp_path, request)
    try:
        yield opened
    finally:
        opened.close()
    if backend == "postgres":
        assert not opened.path.exists()  # nothing touches SQLite


def _race(workers: list[Callable[[], Any]]) -> list[Any]:
    """Run ``workers`` in threads released together; results or exceptions, in order."""
    barrier = threading.Barrier(len(workers))
    results: list[Any] = [None] * len(workers)

    def run(index: int, work: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            results[index] = work()
        except BaseException as exc:  # collected for the assertions
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i, w)) for i, w in enumerate(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    return results


# ================================================================================= secrets


def test_secret_round_trip_is_encrypted_at_rest_and_masked(stores: _Backend) -> None:
    provider = _provider()
    store = stores.secrets(provider)
    info = store.put("ws", "openai", SENTINEL, provider="openai", created_by="alice")
    assert info.masked_hint == "••••" + SENTINEL[-4:]
    assert (info.workspace_id, info.name, info.provider, info.created_by) == (
        "ws",
        "openai",
        "openai",
        "alice",
    )
    assert info.last_verified_at is None and info.status == "active"
    assert store.resolve("ws", "openai") == SENTINEL
    rows = stores.sql("SELECT * FROM nexura_secrets")
    assert len(rows) == 1
    for value in rows[0]:
        raw = bytes(value) if isinstance(value, (bytes, memoryview)) else str(value).encode()
        assert SENTINEL.encode() not in raw, "plaintext found at rest"
        assert SENTINEL[:12].encode() not in raw
    # Another instance (another pool on Postgres) with the same key reads it.
    assert stores.secrets(provider).resolve("ws", "openai") == SENTINEL


def test_each_secret_has_its_own_data_key(stores: _Backend) -> None:
    store = stores.secrets(_provider())
    store.put("ws", "a", SENTINEL)
    store.put("ws", "b", SENTINEL)
    rows = stores.sql("SELECT ciphertext, nonce, wrapped_key FROM nexura_secrets ORDER BY name")
    (ca, na, wa), (cb, nb, wb) = ((bytes(x) for x in row) for row in rows)
    assert ca != cb and na != nb and wa != wb


def test_ciphertext_is_bound_to_workspace_and_name(stores: _Backend) -> None:
    store = stores.secrets(_provider())
    store.put("ws-a", "k", SENTINEL)
    store.put("ws-b", "k", "other-secret-value-0000")
    ciphertext, nonce, wrapped = stores.sql(
        "SELECT ciphertext, nonce, wrapped_key FROM nexura_secrets WHERE workspace_id = ?",
        "ws-a",
    )[0]
    stores.sql(
        "UPDATE nexura_secrets SET ciphertext = ?, nonce = ?, wrapped_key = ? "
        "WHERE workspace_id = ?",
        bytes(ciphertext),
        bytes(nonce),
        bytes(wrapped),
        "ws-b",
    )
    with pytest.raises(MasterKeyMismatchError):
        store.resolve("ws-b", "k")
    assert store.resolve("ws-a", "k") == SENTINEL


def test_wrong_master_key_fails_closed(stores: _Backend) -> None:
    right = _provider()
    stores.secrets(right).put("ws", "k", SENTINEL)
    with pytest.raises(MasterKeyMismatchError) as caught:
        stores.secrets(_provider())  # another key: refused when the store opens
    assert SENTINEL not in str(caught.value)
    assert caught.value.code == "MASTER_KEY_MISMATCH"
    store = stores.secrets(right)
    store._provider = _provider()  # the key changes under a running store
    with pytest.raises(MasterKeyMismatchError):
        store.resolve("ws", "k")
    # An empty store accepts any key (nothing to check against).
    stores.secrets(right).delete("ws", "k")
    stores.secrets(_provider())


def test_replace_keeps_created_fields_and_resets_verification(stores: _Backend) -> None:
    store = stores.secrets(_provider())
    first = store.put("w", "k", "v1-value-value", created_by="alice", description="one")
    store.mark_verified("w", "k", 1234.5)
    time.sleep(0.02)
    second = stores.secrets(store._provider).put(
        "w", "k", "v2-value-value", created_by="bob", description="two", provider="anthropic"
    )
    assert second.created_at == first.created_at and second.created_by == "alice"
    assert second.updated_at > first.updated_at
    assert (second.description, second.provider, second.last_verified_at) == (
        "two",
        "anthropic",
        None,
    )
    assert store.resolve("w", "k") == "v2-value-value"
    assert len(stores.sql("SELECT name FROM nexura_secrets")) == 1


def test_metadata_listing_status_and_delete(stores: _Backend) -> None:
    store = stores.secrets(_provider())
    for name in ("b-key", "B-key", "_key", "a-key"):
        store.put("ws", name, SENTINEL, environment_variable="X_" + name.upper().strip("_"))
    store.put("other", "z", SENTINEL)
    assert [i.name for i in store.list_info("ws")] == ["B-key", "_key", "a-key", "b-key"]
    assert [(i.workspace_id, i.name) for i in store.list_all_info()] == [
        ("other", "z"),
        ("ws", "B-key"),
        ("ws", "_key"),
        ("ws", "a-key"),
        ("ws", "b-key"),
    ]
    assert store.list_info("nobody") == []
    assert store.get_info("ws", "a-key").environment_variable == "X_A-KEY"

    revoked = store.set_status("ws", "a-key", "revoked")
    assert revoked.status == "revoked" and revoked.updated_at >= revoked.created_at
    verified = store.mark_verified("ws", "a-key", 42.0)
    assert verified.last_verified_at == 42.0 and verified.status == "revoked"
    for call in (
        lambda: store.get_info("ws", "missing"),
        lambda: store.resolve("ws", "missing"),
        lambda: store.set_status("ws", "missing", "revoked"),
        lambda: store.mark_verified("ws", "missing", 1.0),
        lambda: store.get_info("other", "a-key"),  # workspace isolation
    ):
        with pytest.raises(SecretNotFoundError) as caught:
            call()
        assert caught.value.code == "SECRET_NOT_FOUND"
    assert store.delete("ws", "a-key") is True
    assert store.delete("ws", "a-key") is False
    with pytest.raises(SecretNotFoundError):
        store.resolve("ws", "a-key")
    assert store.resolve("other", "z") == SENTINEL


def test_concurrent_writes_of_one_name_never_fail(stores: _Backend) -> None:
    """Concurrent first writes and replaces of one name: no error, one row, and every writer is
    told the same creation fields. On Postgres two instances race (one atomic upsert); on SQLite
    one store shared by the threads, as the service opens one per database file."""
    provider = _provider()
    one = stores.secrets(provider)
    instances = [one, one] if stores.backend == "sqlite" else [one, stores.secrets(provider)]
    for round_ in range(3):
        name = f"race-{round_}"
        values = [f"value-{round_}-{i}-padding-padding" for i in range(8)]
        results = _race(
            [
                lambda i=i, v=v, n=name: instances[i % 2].put("ws", n, v, created_by=f"user{i}")
                for i, v in enumerate(values)
            ]
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, [type(e).__name__ for e in errors]
        stored = instances[0].get_info("ws", name)
        assert {(r.created_at, r.created_by) for r in results} == {
            (stored.created_at, stored.created_by)
        }
        assert instances[1].resolve("ws", name) in values
    assert len(stores.sql("SELECT name FROM nexura_secrets")) == 3


@pytest.mark.parametrize("backend", ["postgres"])
def test_postgres_secret_columns_and_nul_refusal(stores: _Backend) -> None:
    store = stores.secrets(_provider())
    store.put("ws", "k", SENTINEL)
    types = dict(
        stores.sql(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'nexura_secrets' AND table_schema = current_schema()"
        )
    )
    assert {types[c] for c in ("ciphertext", "nonce", "wrapped_key")} == {"bytea"}
    assert types["created_at"] == "double precision"
    for args in (("w\x00s", "k", SENTINEL), ("ws", "k\x00", SENTINEL), ("ws", "k2", "a\x00b")):
        with pytest.raises(SecretsError) as caught:
            store.put(*args)
        assert caught.value.code == "SECRET_INVALID"
        assert "\x00" not in str(caught.value) and SENTINEL not in str(caught.value)
    with pytest.raises(SecretsError):
        store.put("ws", "k3", SENTINEL, description="d\x00")
    with pytest.raises(SecretNotFoundError):
        store.resolve("ws", "k\x00")
    assert store.list_info("ws\x00") == [] and store.delete("ws", "k\x00") is False
    assert [i.name for i in store.list_info("ws")] == ["k"]


@pytest.mark.parametrize("backend", ["postgres"])
def test_envelope_format_is_identical_across_backends(stores: _Backend, tmp_path: Path) -> None:
    """A row written by one backend's code decrypts on the other with the same master key."""
    from nexura_postgres import PostgresSecretStore

    provider = _provider()
    sqlite_path = tmp_path / "other.sqlite3"
    sqlite_store = EncryptedSecretStore(provider, sqlite_path)
    sqlite_store.put("ws", "from-sqlite", SENTINEL)
    pg = stores.secrets(provider)
    pg.put("ws", "from-postgres", SENTINEL[::-1])
    columns = (
        "workspace_id, name, ciphertext, nonce, wrapped_key, key_id, credential_type, "
        "provider, description, environment_variable, status, masked_hint, created_at, "
        "updated_at, last_verified_at, created_by"
    )
    with closing(sqlite3.connect(sqlite_path)) as conn, conn:
        row = conn.execute(f"SELECT {columns} FROM nexura_secrets").fetchone()
        stores.sql(f"INSERT INTO nexura_secrets ({columns}) VALUES ({','.join('?' * 16)})", *row)
        pg_row = stores.sql(
            f"SELECT {columns} FROM nexura_secrets WHERE name = ?", "from-postgres"
        )[0]
        conn.execute(
            f"INSERT INTO nexura_secrets ({columns}) VALUES ({','.join('?' * 16)})",
            tuple(bytes(v) if isinstance(v, memoryview) else v for v in pg_row),
        )
    assert PostgresSecretStore(provider, stores.database()).resolve("ws", "from-sqlite") == SENTINEL
    assert (
        EncryptedSecretStore(provider, sqlite_path).resolve("ws", "from-postgres")
        == (SENTINEL[::-1])
    )
    with pytest.raises(MasterKeyMismatchError):
        PostgresSecretStore(_provider(), stores.database())


@pytest.mark.parametrize("backend", ["postgres"])
def test_secret_errors_never_contain_the_value(stores: _Backend) -> None:
    """A write the server rejects, and a write to a closed pool: no error text holds the
    secret (only its ciphertext and masked hint are ever sent)."""
    from nexura_postgres import Database, PostgresSecretStore

    store = stores.secrets(_provider())
    stores.sql("ALTER TABLE nexura_secrets ADD CONSTRAINT wp5_no_writes CHECK (false) NOT VALID")
    database = Database(stores.url)
    closed = PostgresSecretStore(_provider(), database)
    database.close()  # the database goes away under the store
    for target in (store, closed):
        with pytest.raises(Exception) as caught:
            target.put("ws", "k", SENTINEL)
        text = f"{caught.value!r} {caught.value!s} {caught.value.__cause__!r}"
        text += " ".join(str(getattr(caught.value, a, "")) for a in ("detail", "hint", "args"))
        assert SENTINEL not in text and SENTINEL[-12:] not in text and SENTINEL[:12] not in text


# ============================================================================ usage ledger

CAP = BudgetConfig(max_cost_usd=Decimal("1.00"))


def _reserve(ledger: Any, cost: str = "0.10", **kw: Any) -> Any:
    kw.setdefault("workspace_id", "ws")
    kw.setdefault("run_id", "run")
    return ledger.reserve(estimated_cost_usd=Decimal(cost), **kw)


def test_ledger_reserve_reconcile_release_and_reopen(stores: _Backend) -> None:
    ledger = stores.ledger()
    r1 = _reserve(ledger, "0.20", estimated_tokens=100, model_id="m", provider_call_id="call-1")
    r2 = _reserve(ledger, "0.05", node_id="n2")
    assert r1.status == "active" and r1.estimated_cost_usd == Decimal("0.20000000")
    entry = ledger.reconcile(
        r1.reservation_id,
        workspace_id="ws",
        actual_cost_usd=Decimal("0.15"),
        usage=TokenUsage(prompt_tokens=60, completion_tokens=30, state="reported"),
    )
    assert (entry.entry_type, entry.amount_usd, entry.tokens) == (
        "reconcile",
        Decimal("0.15000000"),
        90,
    )
    # Duplicate settlement is idempotent and returns the same entry.
    assert ledger.reconcile(r1.reservation_id).entry_id == entry.entry_id
    released = ledger.release(r2.reservation_id, "unused")
    assert released.entry_type == "release" and ledger.release(r2.reservation_id).entry_id == (
        released.entry_id
    )
    ledger.record_commit(
        workspace_id="ws", run_id="child", parent_run_id="run", amount_usd=Decimal("0.01"), tokens=5
    )
    with pytest.raises(ReservationNotFoundError):
        ledger.reconcile("res_missing")
    with pytest.raises(CrossWorkspaceAccessError):
        ledger.get_reservation(r1.reservation_id, "other")
    with pytest.raises(ValueError):
        _reserve(ledger, "-1")

    status = ledger.get_status("ws", config=CAP)
    reopened = stores.ledger()  # a restart, or another instance
    assert reopened.get_status("ws", config=CAP) == status
    assert status.total_spent_usd == Decimal("0.16000000")
    assert status.total_reserved_usd == Decimal("0") and status.provider_calls_count == 1
    assert status.remaining_cost_usd == Decimal("0.84000000")
    assert (
        [e.entry_type for e in reopened.get_entries("ws", "run")]
        == [e.entry_type for e in ledger.get_entries("ws", "run")]
        == ["reservation", "reservation", "reconcile", "release", "commit"]
    )
    assert [e.run_id for e in reopened.get_entries(run_id="child")] == ["child"]
    assert reopened.get_entries("nobody") == [] and len(reopened.get_entries()) == 5
    got = reopened.get_reservation(r1.reservation_id, "ws")
    assert (got.status, got.provider_call_id, got.estimated_tokens) == ("committed", "call-1", 100)


def test_ledger_budget_exceeded_leaves_no_state(stores: _Backend) -> None:
    ledger = stores.ledger()
    _reserve(ledger, "0.90", workspace_config=CAP)
    with pytest.raises(BudgetExceededError):
        _reserve(ledger, "0.20", workspace_config=CAP)
    with pytest.raises(BudgetExceededError):
        _reserve(
            ledger, "0.01", run_id="r2", config=BudgetConfig(max_tokens=10), estimated_tokens=11
        )
    assert len(ledger.get_entries()) == 1
    assert stores.ledger().get_status("ws", config=CAP).total_reserved_usd == Decimal("0.90000000")
    _reserve(ledger, "0.10", workspace_config=CAP)  # exactly the cap is allowed
    assert ledger.get_status("ws", config=CAP).is_exhausted


def test_ledger_budget_policies(stores: _Backend) -> None:
    a, b = stores.ledger(), stores.ledger()
    assert a.budget_policy("run", "r1") is None
    assert a.budget_policy("run", "r1", "first") == "first"
    assert b.budget_policy("run", "r1", "second") == "first"  # first registration wins
    assert b.budget_policy("run", "r1", "third", replace=True) == "third"
    assert a.budget_policy("run", "r1") == "third"
    assert a.budget_policy("workspace", "r1") is None
    results = _race(
        [lambda i=i: (a, b)[i % 2].budget_policy("run", "r2", f"p{i}") for i in range(6)]
    )
    assert len(set(results)) == 1 and not isinstance(results[0], BaseException)


def test_ledger_instances_see_each_others_changes(stores: _Backend) -> None:
    a, b = stores.ledger(), stores.ledger()
    assert b.get_status("ws").total_reserved_usd == Decimal("0")  # b has loaded (empty)
    res = _reserve(a, "0.30")
    assert b.get_status("ws").total_reserved_usd == Decimal("0.30000000")
    b.reconcile(
        res.reservation_id,
        actual_cost_usd=Decimal("0.25"),
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, state="reported"),
    )
    assert a.get_reservation(res.reservation_id, "ws").status == "committed"
    assert a.get_status("ws").total_spent_usd == Decimal("0.25000000")
    assert [r.reservation_id for r in b.open_reservations("ws", "run", include_unlinked=True)] == []
    other = _reserve(b, "0.05")
    assert [r.reservation_id for r in a.open_reservations("ws", "run", include_unlinked=True)] == [
        other.reservation_id
    ]
    # A reconcile racing on two instances settles once.
    results = _race([lambda x=x: x.reconcile(other.reservation_id) for x in (a, b, a, b)])
    assert len({r.entry_id for r in results}) == 1
    assert len([e for e in a.get_entries() if e.entry_type == "reconcile"]) == 2


def test_concurrent_reservations_on_two_instances_never_exceed_the_cap(stores: _Backend) -> None:
    instances = [stores.ledger(), stores.ledger()]
    cap = BudgetConfig(max_cost_usd=Decimal("1.00"), max_tokens=1000)
    granted: list[Any] = []

    def work(ledger: Any) -> None:
        while True:
            try:
                granted.append(
                    _reserve(ledger, "0.07", estimated_tokens=10, workspace_config=cap, config=cap)
                )
            except BudgetExceededError:
                return

    results = _race([lambda i=i: work(instances[i % 2]) for i in range(8)])
    assert not [r for r in results if isinstance(r, BaseException)]
    assert len(granted) == 14  # floor(1.00 / 0.07): all capacity used, never more
    for ledger in instances:
        status = ledger.get_status("ws", config=cap)
        assert status.total_reserved_usd == Decimal("0.98000000") <= cap.max_cost_usd


@pytest.mark.parametrize("processes", [3])
def test_concurrent_reservations_from_separate_processes_never_exceed_the_cap(
    stores: _Backend, processes: int, tmp_path: Path
) -> None:
    """Real separate processes (each its own interpreter, ledger and pool) reserve at once."""
    target = stores.url if stores.backend == "postgres" else str(stores.path)
    stores.ledger()  # create the tables before the children start
    env = {
        **os.environ,
        "NEXURA_WP5_CHILD_TARGET": target,
        "NEXURA_WP5_CHILD_BACKEND": stores.backend,
    }
    go = tmp_path / "go"
    children = [
        subprocess.Popen(
            [sys.executable, str(CHILD), str(go), "4", "1.00", "0.03"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=tmp_path,
        )
        for _ in range(processes)
    ]
    for child in children:  # every child has its ledger open before any reserves
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
    go.touch()
    outputs = []
    for child in children:
        out, err = child.communicate(timeout=240)
        assert child.returncode == 0, err[-2000:]
        outputs.append(json.loads(out.strip().splitlines()[-1]))
    granted = sum(o["granted"] for o in outputs)
    assert all(o["errors"] == 0 for o in outputs), outputs
    assert granted == 33, outputs  # floor(1.00 / 0.03): all capacity used, never more
    assert sum(1 for o in outputs if o["granted"]) >= 2, outputs  # the processes really raced
    rows = stores.sql(
        "SELECT estimated_cost_usd FROM "
        + ("nexura_usage_reservations" if stores.backend == "postgres" else "reservations")
        + " WHERE status = 'active'"
    )
    assert sum(Decimal(r[0]) for r in rows) == Decimal("0.99")


@pytest.mark.parametrize("backend", ["postgres"])
def test_postgres_failed_write_leaves_no_phantom_state(stores: _Backend) -> None:
    ledger = stores.ledger()
    _reserve(ledger, "0.40")
    with pytest.raises(Exception):  # noqa: B017 - Postgres refuses the NUL at commit
        _reserve(ledger, "0.50", run_id="bad\x00run")
    assert ledger.get_status("ws").total_reserved_usd == Decimal("0.40000000")
    assert len(ledger.get_entries()) == 1
    assert ledger.get_entries(run_id="bad\x00run") == []
    assert stores.ledger().get_status("ws").total_reserved_usd == Decimal("0.40000000")


class _HeldLock:
    """Holds the ledger's global lock in another transaction until ``release``; ``writes``
    are made under it first and committed on release (another instance mid-operation)."""

    def __init__(self, database: Any, writes: Sequence[tuple[str, tuple[Any, ...]]] = ()) -> None:
        from nexura_postgres.usage import LEDGER_LOCK

        self.taken: Future[None] = Future()
        self.done: Future[None] = Future()

        async def hold(conn: Any) -> None:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", LEDGER_LOCK)
            for sql, args in writes:
                await conn.execute(sql, *args)
            self.taken.set_result(None)
            await asyncio.wait_for(asyncio.wrap_future(self.done), 60)

        self.outer = database.submit(hold)
        self.taken.result(30)

    def release(self) -> None:
        self.done.set_result(None)
        self.outer.result(30)


@pytest.mark.parametrize("backend", ["postgres"])
def test_postgres_operations_wait_for_the_global_lock_but_entry_reads_do_not(
    stores: _Backend,
) -> None:
    ledger = stores.ledger()
    _reserve(ledger, "0.10", run_id="r1")
    held = _HeldLock(stores.database())
    try:
        started = time.monotonic()
        assert [e.run_id for e in ledger.get_entries(run_id="r1")] == ["r1"]
        assert time.monotonic() - started < 5  # diagnostics reads never wait for the lock
        outcome: list[Any] = []
        worker = threading.Thread(target=lambda: outcome.append(_reserve(ledger, "0.10")))
        worker.start()
        worker.join(1.0)
        assert worker.is_alive() and not outcome, "a reservation ran without the global lock"
    finally:
        held.release()
    worker.join(30)
    assert len(outcome) == 1
    assert ledger.get_status("ws").total_reserved_usd == Decimal("0.20000000")


@pytest.mark.parametrize("backend", ["postgres"])
def test_postgres_ledger_tables_and_incremental_reload(stores: _Backend) -> None:
    a, b = stores.ledger(), stores.ledger()
    tables = {
        r[0]
        for r in stores.sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        )
    }
    assert {
        "nexura_usage_entries",
        "nexura_usage_reservations",
        "nexura_usage_budget_policies",
    } <= tables
    assert not tables & {"usage_entries", "reservations", "budget_policies"}
    applied = stores.sql("SELECT version FROM nexura_schema_migrations WHERE component = 'usage'")
    assert applied == [(1,)]  # two instances opened: applied once
    for _ in range(3):
        _reserve(a, "0.01")
    b.get_status("ws")
    seen = b._seen
    _reserve(a, "0.01")
    # b reloads only what changed since it last looked, and its view is complete.
    assert b.get_status("ws").total_reserved_usd == Decimal("0.04000000")
    assert b._seen > seen and len(b._entries) == 4 and len(b._reservations) == 4


@pytest.mark.parametrize("backend", ["postgres"])
async def test_postgres_reserve_async_keeps_the_dispatch_path_and_the_loop(
    stores: _Backend,
) -> None:
    from nexura_agents.usage.linkage import dispatch_scope

    ledger = stores.ledger()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    task = asyncio.create_task(ticker())
    held = _HeldLock(stores.database())
    try:
        with dispatch_scope("ws", "wf-1", "step-a"):
            pending = asyncio.ensure_future(
                ledger.reserve_async(
                    workspace_id="ws", run_id="r", estimated_cost_usd=Decimal("0.01")
                )
            )
            before = ticks
            await asyncio.sleep(0.3)
            assert not pending.done() and ticks > before + 10  # the loop kept running
    finally:
        await asyncio.to_thread(held.release)
    res = await pending
    task.cancel()
    assert res.metadata["execution_run_id"] == "wf-1" and res.metadata["step_key"] == "step-a"


# =============================================================== fix round (review WP5 M1–M4)


def _instances(stores: _Backend, provider: Any) -> list[Any]:
    """Two instances on Postgres (separate pools); on SQLite one store shared by the threads,
    as the service opens one per database file."""
    one = stores.secrets(provider)
    return [one, one] if stores.backend == "sqlite" else [one, stores.secrets(provider)]


def test_no_write_lowers_a_revoked_status(stores: _Backend) -> None:
    """M1: only ``set_status`` undoes a revocation; an upsert or a replace keeps it."""
    store = stores.secrets(_provider())
    store.put("ws", "k", SENTINEL)
    store.set_status("ws", "k", "revoked")
    assert store.put("ws", "k", "new-value-padding-1", status="active").status == "revoked"
    assert (
        store.put("ws", "k", "new-value-padding-2", status="active", mode="replace").status
        == "revoked"
    )
    assert store.get_info("ws", "k").status == "revoked"
    assert store.resolve("ws", "k") == "new-value-padding-2"  # the value was replaced
    assert store.set_status("ws", "k", "active").status == "active"  # the deliberate way back


def test_a_replace_racing_a_revoke_on_another_instance_keeps_it_revoked(stores: _Backend) -> None:
    """M1, the enterprise ``PUT /secrets/{name}`` steps interleaved by hand: instance A reads
    the status (active), B revokes, then A writes the new value with the status it read."""
    from nexura_runtime.credentials.errors import CredentialAccessDeniedError
    from nexura_runtime.credentials.store import ScopedCredentialStore

    a_backend, b_backend = _instances(stores, _provider())
    a, b = ScopedCredentialStore(a_backend), ScopedCredentialStore(b_backend)
    if stores.backend == "sqlite":
        b = a  # one instance: its cache is the store's own state
    a.store("ws", "stripe", "api_key", SENTINEL)
    old = a.get_metadata("ws", "stripe")
    assert old.status == "active"
    b.revoke("ws", "stripe")
    for mode in ("replace", "upsert"):
        meta = a.store(
            "ws",
            "stripe",
            old.credential_type,
            "replacement-value-0123456789",
            status=old.status,
            mode=mode,  # type: ignore[arg-type]
        )
        assert meta.status == "revoked"
    for store in (a, b):
        assert store.get_metadata("ws", "stripe").status == "revoked"
        with pytest.raises(CredentialAccessDeniedError):
            store.resolve("ws", "stripe")


def test_create_only_refuses_an_existing_name(stores: _Backend) -> None:
    from nexura_runtime.secrets.errors import SecretExistsError

    store = stores.secrets(_provider())
    store.put("ws", "k", SENTINEL, mode="create", created_by="alice")
    with pytest.raises(SecretExistsError) as caught:
        store.put("ws", "k", "other-value-padding", mode="create", created_by="bob")
    assert caught.value.code == "SECRET_EXISTS" and SENTINEL not in str(caught.value)
    assert store.resolve("ws", "k") == SENTINEL and store.get_info("ws", "k").created_by == "alice"
    store.put("other", "k", "elsewhere-value-pad", mode="create")  # per workspace


def test_concurrent_creates_of_one_name_have_one_winner(stores: _Backend) -> None:
    """M2: of 8 creates of one name on two instances exactly one succeeds; the stored value is
    the winner's, never silently replaced by a loser's."""
    from nexura_runtime.secrets.errors import SecretExistsError

    instances = _instances(stores, _provider())
    for round_ in range(3):
        name = f"create-{round_}"
        values = [f"value-{round_}-{i}-padding-padding" for i in range(8)]
        results = _race(
            [
                lambda i=i, v=v, n=name: instances[i % 2].put("ws", n, v, mode="create")
                for i, v in enumerate(values)
            ]
        )
        won = [i for i, r in enumerate(results) if not isinstance(r, BaseException)]
        lost = [r for r in results if isinstance(r, BaseException)]
        assert len(won) == 1, [type(r).__name__ for r in results]
        assert all(isinstance(r, SecretExistsError) for r in lost)
        assert instances[1].resolve("ws", name) == values[won[0]]


def test_replace_only_requires_the_secret(stores: _Backend) -> None:
    """A replace never re-creates a secret deleted meanwhile (on any instance)."""
    a, b = _instances(stores, _provider())
    with pytest.raises(SecretNotFoundError):
        a.put("ws", "k", SENTINEL, mode="replace")
    a.put("ws", "k", SENTINEL, created_by="alice")
    assert a.put("ws", "k", "second-value-padding", mode="replace").created_by == "alice"
    b.delete("ws", "k")
    with pytest.raises(SecretNotFoundError):
        a.put("ws", "k", "third-value-padding", mode="replace")
    with pytest.raises(SecretNotFoundError):
        a.get_info("ws", "k")


def test_credential_store_modes_map_to_credential_errors(stores: _Backend) -> None:
    from nexura_runtime.credentials.errors import (
        CredentialAlreadyExistsError,
        CredentialNotFoundError,
    )
    from nexura_runtime.credentials.store import ScopedCredentialStore

    for credentials in (
        ScopedCredentialStore(stores.secrets(_provider())),
        ScopedCredentialStore(),
    ):
        credentials.store("ws", "k", "api_key", SENTINEL, mode="create")
        with pytest.raises(CredentialAlreadyExistsError):
            credentials.store("ws", "k", "api_key", "other-value-padding", mode="create")
        with pytest.raises(CredentialNotFoundError):
            credentials.store("ws", "missing", "api_key", SENTINEL, mode="replace")
        credentials.revoke("ws", "k")
        assert credentials.store("ws", "k", "api_key", "v2-value-padding").status == "revoked"
        credentials.delete("ws", "k")


def test_credential_store_read_through_is_thread_safe(stores: _Backend) -> None:
    """L1: lookups and listings from many threads while another thread refreshes the shared
    map never fail spuriously (no missing key, no "changed size during iteration")."""
    from nexura_runtime.credentials.store import ScopedCredentialStore

    credentials = ScopedCredentialStore(stores.secrets(_provider()))
    for i in range(20):
        credentials.store("ws", f"k{i}", "api_key", f"value-{i}-padding-padding")
    stop = threading.Event()
    errors: list[BaseException] = []

    def lister() -> None:
        while not stop.is_set():
            try:
                assert len(credentials.list_credentials("ws")) == 20
            except BaseException as exc:  # collected for the assertion
                errors.append(exc)
                return

    def resolver() -> None:
        while not stop.is_set():
            try:
                assert credentials.resolve("ws", "k7").reveal() == "value-7-padding-padding"
            except BaseException as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=f) for f in (lister, lister, resolver, resolver)]
    for thread in threads:
        thread.start()
    time.sleep(1.0)
    stop.set()
    for thread in threads:
        thread.join(30)
    assert not errors, [type(e).__name__ for e in errors]


@pytest.mark.parametrize("backend", ["postgres"])
async def test_postgres_async_budget_paths_never_block_the_event_loop(stores: _Backend) -> None:
    """M3: with the ledger lock held by another transaction (another instance), every async
    path that makes a ledger call waits in a worker thread; the loop keeps running."""
    from nexura_agents.usage.manager import BudgetManager, budget_call
    from nexura_agents.usage.module import UsageBudgetModule
    from nexura_runtime.contracts.context import ExecutionContext

    from nexura_runtime.workflow import (
        MemoryWorkflowStore,
        ModuleRegistry,
        WorkflowEngine,
        WorkflowNode,
        WorkflowSpec,
    )

    ledger = stores.ledger()
    manager = BudgetManager(ledger=ledger)
    budget = manager.create_run_budget("ws", "run-m3", config=BudgetConfig(max_tokens=10**6))
    reservation = budget.admit_provider_call("gpt-4o", estimated_tokens=100)
    second = budget.admit_provider_call("gpt-4o", estimated_tokens=100)
    module = UsageBudgetModule(manager)
    ctx = ExecutionContext(run_id="run-m3", node_id="n", workspace_id="ws")

    registry = ModuleRegistry()

    async def step(state: dict[str, Any]) -> dict[str, Any]:
        return {"done": True}

    registry.register("step", step)
    spec = WorkflowSpec(
        name="m3",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="step")},
        terminal_nodes=["a"],
    )
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore(), budget=budget)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    calls = {
        "settle_provider_call_async": lambda: budget.settle_provider_call_async(reservation),
        "get_status_async": budget.get_status_async,
        "to_dict_async": budget.to_dict_async,
        "is_exhausted (budget_call)": lambda: budget_call(budget, budget.is_exhausted),
        "module check_budget": lambda: module.invoke(ctx, {"operation": "check_budget"}, {}),
        "module check_budget (workspace, no run budget)": lambda: module.invoke(
            ExecutionContext(run_id="no-budget", node_id="n", workspace_id="ws"),
            {"operation": "check_budget"},
            {},
        ),
        "module reconcile": lambda: module.invoke(
            ctx, {"operation": "reconcile", "reservation_id": second}, {}
        ),
        "module record_usage": lambda: module.invoke(
            ctx, {"operation": "record_usage", "usage": {"prompt_tokens": 1}}, {}
        ),
        "engine run (checkpoint budget state)": lambda: engine.run({}, workflow_run_id="wf-m3"),
    }
    task = asyncio.create_task(ticker())
    for name, call in calls.items():
        held = _HeldLock(stores.database())
        try:
            pending = asyncio.ensure_future(call())
            before = ticks
            await asyncio.sleep(0.3)
            assert not pending.done(), f"{name} did not wait for the ledger lock"
            assert ticks > before + 10, f"{name} blocked the event loop"
        finally:
            await asyncio.to_thread(held.release)
        await asyncio.wait_for(pending, 30)
    task.cancel()


def _seed(ledger: Any, rows: int) -> None:
    for i in range(rows):
        ledger.record_commit(
            workspace_id="history", run_id=f"old-{i}", amount_usd=Decimal("0.01"), tokens=1
        )


@pytest.mark.parametrize("backend", ["postgres"])
def test_postgres_first_load_runs_without_the_global_lock(stores: _Backend) -> None:
    """M4: a fresh instance loads the history before it waits for the lock; a reservation
    another instance has made but not committed when that snapshot is taken still counts."""
    from nexura_agents.usage.ledger import reservation_values
    from nexura_agents.usage.models import Reservation
    from nexura_postgres.usage import _RESERVATION_UPSERT

    _seed(stores.ledger(), 30)
    pending = Reservation(
        workspace_id="ws",
        run_id="other-instance",
        estimated_cost_usd=Decimal("0.95"),
        status="active",
        created_at=time.time(),
    )
    held = _HeldLock(stores.database(), writes=[(_RESERVATION_UPSERT, reservation_values(pending))])
    fresh = stores.ledger()
    outcome: list[Any] = []

    def work() -> None:
        try:
            outcome.append(_reserve(fresh, "0.10", workspace_config=CAP))
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=work)
    try:
        worker.start()
        deadline = time.monotonic() + 10
        while not fresh._loaded and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fresh._loaded, "the first load waited for the global lock"
        assert len(fresh._entries) == 30 and not outcome  # history loaded; still waiting
    finally:
        held.release()
    worker.join(30)
    assert len(outcome) == 1 and isinstance(outcome[0], BudgetExceededError), outcome
    assert fresh.get_status("ws", config=CAP).total_reserved_usd == Decimal("0.95000000")


@pytest.mark.parametrize("backend", ["postgres"])
def test_postgres_cold_instances_racing_reservations_stay_exact(stores: _Backend) -> None:
    """M4: three instances start cold (first load without the lock) while the others already
    reserve; together they reserve exactly up to the cap and every instance agrees."""
    _seed(stores.ledger(), 40)
    warm = stores.ledger()
    _reserve(warm, "0.10", workspace_config=CAP)
    cold = [stores.ledger() for _ in range(3)]
    granted: list[Any] = []

    def work(ledger: Any) -> None:
        while True:
            try:
                granted.append(_reserve(ledger, "0.06", workspace_config=CAP))
            except BudgetExceededError:
                return

    results = _race([lambda x=x: work(x) for x in (*cold, *cold, warm)])
    assert not [r for r in results if isinstance(r, BaseException)]
    assert len(granted) == 15  # floor(0.90 / 0.06)
    for ledger in (*cold, warm, stores.ledger()):
        assert ledger.get_status("ws", config=CAP).total_reserved_usd == Decimal("1.00000000")
