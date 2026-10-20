"""Kill harness for P35 A6 durability tests (step S0).

Parent side (pytest): :class:`Harness` spawns child processes that run
``durability_child.py``, waits for marker files the child writes at chosen points, and
kills (SIGKILL) or freezes (SIGSTOP / SIGCONT) them there. :class:`EffectService` is an
external fake effect service: an HTTP server with its own SQLite ledger, so a test can
count how many times each side effect really happened, whatever the run stores say.

Child side: :func:`point` marks a point inside a run (a handler, a tool, or a store
wrapper right after a commit) and, when the test asked for it, blocks there: forever
(the parent kills the process) or until the parent writes a "go" file. :func:`effect`
sends one side effect to the effect service. No production hooks are used: points are
reached through handlers and store subclasses defined by the tests.

Postgres: a :class:`Backend` named ``postgres`` carries ``NEXURA_TEST_POSTGRES_URI``.
Paths without a Postgres store today are reported by :meth:`Backend.require`.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CHILD_SCRIPT = Path(__file__).with_name("durability_child.py")
POSTGRES_ENV = "NEXURA_TEST_POSTGRES_URI"

# Postgres stores the A6 steps add (design §5 "API changes"); absent today.
POSTGRES_STORES: dict[str, tuple[str, str]] = {
    "agent": ("PostgresCheckpointStore", "A6 S5: no Postgres SDK checkpoint store yet"),
    "workflow": ("PostgresWorkflowStore", "A6 S8: no PostgresWorkflowStore yet"),
    "trigger": ("PostgresTriggerStore", "A6 S10b: no PostgresTriggerStore yet"),
    "legacy": ("PostgresRepository", "PostgresRepository is missing"),
}


class HarnessError(RuntimeError):
    """The harness itself failed (a child died early, a marker never came, a timeout).

    Deliberately not an ``AssertionError``: characterization tests are marked
    ``xfail(raises=AssertionError)``, so a broken harness fails loudly instead of
    passing as an expected failure.
    """


# --------------------------------------------------------------------------- backends


@dataclass(frozen=True)
class Backend:
    """Where a scenario keeps its run state: a SQLite file or a Postgres database."""

    name: str
    dsn: str | None = None

    def require(self, kind: str) -> None:
        """Assert that ``kind`` (agent, workflow, trigger, legacy) has a store here.

        Raises ``AssertionError`` so the Postgres rows of paths that have no Postgres store
        yet are strict expected failures that flip once the store lands.
        """
        if self.name == "sqlite":
            return
        attr, reason = POSTGRES_STORES[kind]
        try:
            import nexura_postgres
        except ImportError as exc:  # pragma: no cover - the package is a dev dependency
            raise AssertionError(f"nexura_postgres is not importable: {exc}") from exc
        assert hasattr(nexura_postgres, attr), reason

    def to_config(self) -> dict[str, Any]:
        return {"name": self.name, "dsn": self.dsn}


# ---------------------------------------------------------------------- effect service


class EffectLedger:
    """The effect service's own ledger: one SQLite file nobody else writes.

    ``requests`` holds every call that reached the service; ``applied`` holds the effects
    that actually happened. A call with an idempotency ``key`` already applied for the
    same effect is recorded as a request but not applied again (a remote that de-duplicates).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "effect TEXT NOT NULL, key TEXT, who TEXT, extra TEXT, at REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS applied (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "effect TEXT NOT NULL, key TEXT, who TEXT, at REAL NOT NULL)"
            )

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=10.0)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, effect: str, key: str | None, who: str, extra: Any) -> bool:
        """Record one request; return True when the effect was applied (not a duplicate)."""
        with self._lock, self._connect() as db:
            now = time.time()
            db.execute(
                "INSERT INTO requests (effect, key, who, extra, at) VALUES (?, ?, ?, ?, ?)",
                (effect, key, who, json.dumps(extra), now),
            )
            if key is not None:
                seen = db.execute(
                    "SELECT 1 FROM applied WHERE effect = ? AND key = ?", (effect, key)
                ).fetchone()
                if seen is not None:
                    return False
            db.execute(
                "INSERT INTO applied (effect, key, who, at) VALUES (?, ?, ?, ?)",
                (effect, key, who, now),
            )
            return True

    def _count(self, table: str, effect: str | None, who: str | None) -> int:
        where, params = [], []
        if effect is not None:
            where.append("effect = ?")
            params.append(effect)
        if who is not None:
            where.append("who = ?")
            params.append(who)
        sql = f"SELECT COUNT(*) FROM {table}" + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock, self._connect() as db:
            return int(db.execute(sql, params).fetchone()[0])

    def applied(self, effect: str | None = None, who: str | None = None) -> int:
        """How many times the effect really happened."""
        return self._count("applied", effect, who)

    def requests(self, effect: str | None = None, who: str | None = None) -> int:
        """How many calls reached the service (duplicates included)."""
        return self._count("requests", effect, who)

    def request_rows(self, effect: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT key, who, extra FROM requests WHERE effect = ? ORDER BY seq", (effect,)
            ).fetchall()
        return [{"key": r[0], "who": r[1], "extra": json.loads(r[2])} for r in rows]


class EffectService:
    """An HTTP effect service in a thread of the test process, outside every child."""

    def __init__(self, ledger_path: Path) -> None:
        self.ledger = EffectLedger(ledger_path)
        ledger = self.ledger

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                applied = ledger.record(
                    str(body["effect"]),
                    body.get("key"),
                    str(body.get("who", "")),
                    body.get("extra"),
                )
                payload = json.dumps({"applied": applied}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}/effect"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ----------------------------------------------------------------------- parent side


@dataclass
class Child:
    """One spawned child process running a scenario."""

    name: str
    proc: subprocess.Popen[bytes]
    workdir: Path
    log_path: Path
    result_path: Path

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def marker_dir(self) -> Path:
        return self.workdir / "markers" / self.name

    def log(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-4000:]
        except OSError:
            return ""

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait_marker(self, point: str, timeout: float = 20.0) -> dict[str, Any]:
        """Wait until the child reached ``point``; fail fast if it exits first."""
        path = self.marker_dir / point
        deadline = time.monotonic() + timeout
        while True:
            if path.exists():
                data: dict[str, Any] = json.loads(path.read_text())
                return data
            if not self.alive():
                if path.exists():
                    continue
                raise HarnessError(
                    f"child {self.name} exited ({self.proc.returncode}) before point "
                    f"{point!r}; log:\n{self.log()}"
                )
            if time.monotonic() > deadline:
                raise HarnessError(
                    f"child {self.name} did not reach {point!r} in {timeout}s; log:\n{self.log()}"
                )
            time.sleep(0.02)

    def reached(self, point: str) -> bool:
        return (self.marker_dir / point).exists()

    def go(self, point: str) -> None:
        """Release a child waiting at a ``go`` point."""
        go_dir = self.workdir / "go" / self.name
        go_dir.mkdir(parents=True, exist_ok=True)
        (go_dir / point).touch()

    def kill(self) -> None:
        """SIGKILL the child and reap it."""
        if self.alive():
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGCONT)  # a stopped process dies either way
                os.kill(self.pid, signal.SIGKILL)
        self.proc.wait(timeout=10)
        if self.proc.returncode is None:  # pragma: no cover - wait() raised instead
            raise HarnessError(f"child {self.name} survived SIGKILL")

    def stop(self) -> None:
        os.kill(self.pid, signal.SIGSTOP)
        deadline = time.monotonic() + 5
        while process_state(self.pid) != "T":
            if time.monotonic() > deadline:
                raise HarnessError(f"child {self.name} did not stop")
            time.sleep(0.01)

    def cont(self) -> None:
        os.kill(self.pid, signal.SIGCONT)

    def result(self, timeout: float = 30.0) -> dict[str, Any]:
        """Wait for a clean exit and return the JSON result the scenario wrote."""
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.kill()
            raise HarnessError(
                f"child {self.name} did not finish in {timeout}s; log:\n{self.log()}"
            ) from exc
        if self.proc.returncode != 0 or not self.result_path.exists():
            raise HarnessError(
                f"child {self.name} failed (exit {self.proc.returncode}); log:\n{self.log()}"
            )
        data: dict[str, Any] = json.loads(self.result_path.read_text())
        return data


def process_state(pid: int) -> str | None:
    """Linux process state letter from /proc (R, S, T, Z...), or None when it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[0]


@dataclass
class Harness:
    """Spawns scenario children against one working directory and one effect service."""

    workdir: Path
    backend: Backend
    effects: EffectService
    children: list[Child] = field(default_factory=list)

    @property
    def ledger(self) -> EffectLedger:
        return self.effects.ledger

    @property
    def db_path(self) -> Path:
        return self.workdir / "nexura.sqlite3"

    def spawn(
        self,
        scenario: str,
        name: str,
        *,
        block: dict[str, str] | None = None,
        **params: Any,
    ) -> Child:
        """Start ``durability_child.py`` running ``scenario``.

        ``block`` maps a point name to ``"forever"`` (block until killed) or ``"go"`` (block
        until :meth:`Child.go`). Extra keyword arguments reach the scenario as config.
        """
        if any(child.name == name for child in self.children):
            raise HarnessError(f"child name {name!r} already used")
        config = {
            "scenario": scenario,
            "name": name,
            "workdir": str(self.workdir),
            "db": str(self.db_path),
            "backend": self.backend.to_config(),
            "effect_url": self.effects.url,
            "block": block or {},
            **params,
        }
        config_path = self.workdir / f"{name}.config.json"
        config_path.write_text(json.dumps(config))
        log_path = self.workdir / f"{name}.log"
        result_path = self.workdir / f"{name}.result.json"
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                [sys.executable, str(CHILD_SCRIPT), str(config_path)],
                cwd=str(self.workdir),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        child = Child(name, proc, self.workdir, log_path, result_path)
        self.children.append(child)
        return child

    def close(self) -> None:
        """Kill every child still running (also stopped ones) and stop the effect service."""
        try:
            for child in self.children:
                child.kill()
        finally:
            self.effects.close()
        leftovers = [c.name for c in self.children if c.proc.returncode is None]
        if leftovers:  # pragma: no cover - kill() raises first
            raise HarnessError(f"children left running: {leftovers}")


# ------------------------------------------------------------------------ child side


class ChildContext:
    """What a running child knows about its test: config, markers, effect service."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.name: str = config["name"]
        self.workdir = Path(config["workdir"])
        self.marker_dir = self.workdir / "markers" / self.name
        self.go_dir = self.workdir / "go" / self.name
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        self.block: dict[str, str] = dict(config.get("block") or {})
        self.effect_url: str = config["effect_url"]

    def mark(self, point: str, **data: Any) -> None:
        payload = json.dumps({"pid": os.getpid(), "point": point, **data})
        tmp = self.marker_dir / f".{point}.tmp"
        tmp.write_text(payload)
        os.replace(tmp, self.marker_dir / point)

    def point(self, point: str, **data: Any) -> None:
        self.mark(point, **data)
        mode = self.block.get(point)
        if mode == "forever":
            while True:  # the parent kills this process here
                time.sleep(0.05)
        elif mode == "go":
            self.block.pop(point)
            while not (self.go_dir / point).exists():
                time.sleep(0.02)
            self.mark(f"{point}.released", **data)

    def effect(self, effect: str, *, key: str | None = None, extra: Any = None) -> bool:
        body = json.dumps({"effect": effect, "key": key, "who": self.name, "extra": extra}).encode()
        request = urllib.request.Request(
            self.effect_url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            applied: bool = json.loads(response.read())["applied"]
        return applied

    def write_result(self, result: dict[str, Any]) -> None:
        path = self.workdir / f"{self.name}.result.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, default=str))
        os.replace(tmp, path)


_CONTEXT: ChildContext | None = None


def set_child_context(context: ChildContext) -> None:
    global _CONTEXT
    _CONTEXT = context


def child() -> ChildContext:
    if _CONTEXT is None:
        raise HarnessError("not running inside a durability child")
    return _CONTEXT


def point(name: str, **data: Any) -> None:
    """Mark ``name`` reached; block there if the test asked for it."""
    child().point(name, **data)


def effect(name: str, *, key: str | None = None, extra: Any = None) -> bool:
    """Perform one side effect through the external effect service."""
    return child().effect(name, key=key, extra=extra)
