"""The local service releases its SQLite database file when it shuts down.

Windows cannot delete a file another handle still has open, so a service that kept its
trigger, outbox and evaluation stores open (or left ``with sqlite3.connect(...)``
connections to the garbage collector) made the temporary directory of
``examples/oss/o07.py`` fail to clean up there. The stores reopen on next use, so an app
started again after its shutdown keeps working.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nexura_runtime.evaluation.store import SqliteEvaluationStore
from nexura_runtime.integrations.channels.outbox import SqliteChannelStore
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.store import SqliteTriggerStore

SCHEDULE = {
    "schedule_id": "sched_shutdown",
    "workflow_id": "wf_shutdown",
    "interval_seconds": 60.0,
    "timezone": "UTC",
}
SPEC = {
    "name": "T",
    "entry_node": "a",
    "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
    "terminal_nodes": ["a"],
}


def _open_handles_under(directory: Path) -> list[str]:
    found = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(str(directory)):
            found.append(target)
    return found


def _client(app: object) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))  # type: ignore[arg-type]


# SQLite file handles: a full Postgres service (MVP WP8) has no database file to hold or release.
@pytest.mark.sqlite_only
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/self/fd")
def test_shutdown_closes_every_handle_to_the_database(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "service.sqlite3", profile="local-owner")
    headers = {"X-Workspace-ID": "default", "X-Nexura-Session": app.state.session_token}
    with _client(app) as client:
        created = client.post(
            "/api/v1/workflows", json={"workflow_id": "wf_shutdown", "spec": SPEC}, headers=headers
        )
        assert created.status_code in (200, 201), created.text
        assert client.get("/api/v1/evaluations/datasets", headers=headers).status_code == 200
        assert _open_handles_under(tmp_path), "the running service should hold the database"

    # The app object is still alive: nothing may wait for the garbage collector.
    assert _open_handles_under(tmp_path) == []


# The SQLite stores reopen their file after the shutdown closed it. A Postgres-mode app object is
# not restartable after its lifespan (its shutdown closes the shared pool every Postgres store
# holds; true of every store since MVP WP2, a WP8 report limitation); a new app is started instead.
@pytest.mark.sqlite_only
def test_restarted_app_reopens_its_stores(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "service.sqlite3", profile="local-owner")
    headers = {"X-Workspace-ID": "default", "X-Nexura-Session": app.state.session_token}
    with _client(app) as client:
        created = client.post(
            "/api/v1/workflows", json={"workflow_id": "wf_shutdown", "spec": SPEC}, headers=headers
        )
        assert created.status_code in (200, 201), created.text
        res = client.post("/api/v1/triggers/schedules", json=SCHEDULE, headers=headers)
        assert res.status_code == 200, res.text

    with _client(app) as client:
        schedules = client.get("/api/v1/triggers/schedules", headers=headers)
        assert schedules.status_code == 200, schedules.text
        assert "sched_shutdown" in schedules.text
        assert client.get("/api/v1/evaluations/datasets", headers=headers).status_code == 200


@pytest.mark.parametrize(
    "store_cls", [SqliteTriggerStore, SqliteChannelStore, SqliteEvaluationStore]
)
def test_file_store_reopens_after_close(tmp_path: Path, store_cls: type) -> None:
    db = str(tmp_path / "stores.sqlite3")
    store = store_cls(db)
    store.close()
    store.close()  # idempotent
    # Used again, it reopens the file instead of failing on a closed connection.
    assert store._conn.execute("SELECT 1").fetchone()[0] == 1
    store.close()


@pytest.mark.parametrize(
    "store_cls", [SqliteTriggerStore, SqliteChannelStore, SqliteEvaluationStore]
)
def test_memory_store_stays_closed(store_cls: type) -> None:
    store = store_cls(":memory:")
    store.close()
    # Reopening would silently start an empty database.
    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1")
