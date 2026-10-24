# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Conversation persistence and session storage protocols and implementations.

Provides durable SQLite and in-memory session persistence with compare-and-swap
optimistic concurrency control, versioned JSON serialization (no pickle), and
independent snapshots.
"""

from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from nexura_agents._lazy import LazyModule
from nexura_agents.attachments import Attachment
from nexura_agents.errors import (
    SDKValidationError,
    SessionConflictError,
    StorageError,
    StorageSerializationError,
)
from nexura_agents.history import _is_retry_feedback
from nexura_agents.types import Message, ToolCall

if TYPE_CHECKING:
    import asyncio

    from nexura_agents.agent import Agent
    from nexura_agents.runner import Runner
    from nexura_agents.types import OutputT, RunResult
else:
    asyncio = LazyModule("asyncio")

SCHEMA_VERSION = 1
_CleanupT = TypeVar("_CleanupT")


@dataclass(frozen=True)
class SessionSnapshot:
    """A point-in-time snapshot of a conversation session."""

    session_id: str
    messages: list[Message]
    revision: int
    # The run state saved with the conversation (RunContext.state); empty when none.
    state: dict[str, Any] = field(default_factory=dict)


def serialize_session(
    session_id: str,
    revision: int,
    messages: list[Message],
    state: dict[str, Any] | None = None,
) -> str:
    """Serialize conversation messages to a versioned UTF-8 JSON payload.

    Preserves nested tool calls and Indic/Unicode text without pickle.
    """
    serialized_messages: list[dict[str, Any]] = []
    for msg in messages:
        msg_dict: dict[str, Any] = {
            "role": msg.role,
            "content": msg.content,
            "name": msg.name,
            "tool_call_id": msg.tool_call_id,
        }
        if msg.tool_calls is not None:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "raw_arguments": tc.raw_arguments,
                }
                for tc in msg.tool_calls
            ]
        else:
            msg_dict["tool_calls"] = None
        if msg.attachments:
            msg_dict["attachments"] = [a.to_dict() for a in msg.attachments]
        if msg.provider_data:
            msg_dict["provider_data"] = msg.provider_data
        serialized_messages.append(msg_dict)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "revision": revision,
        "messages": serialized_messages,
    }
    if state:
        from nexura_agents.types import _json_state

        payload["state"] = _json_state(state)
    return json.dumps(payload, ensure_ascii=False)


def session_state(data_str: str) -> dict[str, Any]:
    """The run state stored in a serialized session (empty when it has none)."""
    try:
        value = json.loads(data_str).get("state")
    except (ValueError, AttributeError):
        return {}
    return value if isinstance(value, dict) else {}


def deserialize_session(data_str: str) -> tuple[str, int, list[Message]]:
    """Deserialize a JSON payload into session messages.

    Raises:
        StorageSerializationError: If payload is invalid or schema version is unsupported.
    """
    try:
        data = json.loads(data_str)
    except Exception as exc:
        raise StorageSerializationError(
            f"Failed to decode session JSON: {exc}", original_error=exc
        ) from exc

    if not isinstance(data, dict):
        raise StorageSerializationError("Malformed session payload: expected JSON object.")

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StorageSerializationError(
            f"Unsupported session schema version: {version!r} (expected {SCHEMA_VERSION})."
        )

    session_id = data.get("session_id")
    revision = data.get("revision")
    if not isinstance(session_id, str) or not isinstance(revision, int):
        raise StorageSerializationError("Missing or invalid session_id or revision in payload.")

    raw_messages = data.get("messages", [])
    messages: list[Message] = []
    for raw in raw_messages:
        tool_calls: list[ToolCall] | None = None
        if raw.get("tool_calls") is not None:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["name"],
                    arguments=tc.get("arguments", {}),
                    raw_arguments=tc.get("raw_arguments"),
                )
                for tc in raw["tool_calls"]
            ]
        messages.append(
            Message(
                role=raw["role"],
                content=raw.get("content"),
                tool_calls=tool_calls,
                tool_call_id=raw.get("tool_call_id"),
                name=raw.get("name"),
                attachments=[Attachment.from_dict(a) for a in raw["attachments"]]
                if raw.get("attachments")
                else None,
                provider_data=raw.get("provider_data")
                if isinstance(raw.get("provider_data"), dict)
                else None,
            )
        )
    return session_id, revision, messages


@runtime_checkable
class SessionStore(Protocol):
    """Protocol for conversation session storage."""

    async def get(self, session_id: str) -> SessionSnapshot | None:
        """Retrieve the session snapshot, or None if the session does not exist."""
        ...

    async def save(
        self,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
    ) -> SessionSnapshot:
        """Save conversation messages using compare-and-swap on expected_revision.

        Args:
            session_id: Unique session identifier.
            messages: New list of conversation messages to persist.
            expected_revision: Revision expected before update (0 for a new session).

        Returns:
            Updated SessionSnapshot with the incremented revision.

        Raises:
            SessionConflictError: If expected_revision does not match current store revision.
        """
        ...

    async def close(self) -> None:
        """Release any database or store resources."""
        ...


def _fork_points(messages: list[Message]) -> list[int]:
    """Message counts where a cut keeps no tool call without results that the source has.

    A call that never got a result (a crashed turn) has nothing to separate, so it does
    not block later points.
    """
    answered = {m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id}
    points = [0]
    open_calls: set[str] = set()
    for index, message in enumerate(messages, start=1):
        if message.role == "assistant" and message.tool_calls:
            open_calls |= {call.id for call in message.tool_calls if call.id in answered}
        elif message.role == "tool" and message.tool_call_id is not None:
            open_calls.discard(message.tool_call_id)
        if not open_calls:
            points.append(index)
    return points


def _accepts_state(save: Any) -> bool:
    try:
        params = inspect.signature(save).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "state" or p.kind is p.VAR_KEYWORD for p in params)


async def fork_session(
    store: SessionStore, source_id: str, new_id: str, *, at: int | None = None
) -> SessionSnapshot:
    """Copy a session into a new one, whole or up to a message, to branch the conversation.

    ``at`` is how many of the stored messages to keep (``None``: all of them), so ``at=4``
    keeps ``snapshot.messages[:4]``, which starts with the system message, and the next run
    on ``new_id`` continues from there. The source session
    is not changed. The saved run state (``RunContext.state``) is copied too, when the store
    keeps state.

    Raises:
        SDKValidationError: ``new_id`` equals ``source_id``, ``source_id`` does not exist,
            ``at`` is out of range, or ``at`` would separate a tool call from its results
            (the message lists the counts that are allowed).
        SessionConflictError: ``new_id`` already exists; a fork never overwrites a session.
    """
    if new_id == source_id:
        raise SDKValidationError("fork_session needs a new session id, not the source id.")
    source = await store.get(source_id)
    if source is None:
        raise SDKValidationError(f"Session '{source_id}' does not exist.")
    messages = source.messages
    if at is not None:
        if isinstance(at, bool) or not isinstance(at, int) or not 0 <= at <= len(messages):
            raise SDKValidationError(
                f"at must be an integer from 0 to {len(messages)} (the session's messages)."
            )
        points = _fork_points(messages)
        if at not in points:
            nearest = sorted(points, key=lambda p: (abs(p - at), p))[:2]
            raise SDKValidationError(
                f"at={at} would separate a tool call from its results; "
                f"use one of {sorted(nearest)} instead."
            )
        messages = messages[:at]
    if _accepts_state(store.save):
        return await store.save(  # type: ignore[call-arg]
            new_id,
            copy.deepcopy(messages),
            expected_revision=0,
            state=copy.deepcopy(source.state),
        )
    return await store.save(new_id, copy.deepcopy(messages), expected_revision=0)


async def delete_session(store: SessionStore, session_id: str) -> bool:
    """Delete a session. Returns ``True`` if it existed, ``False`` if it did not.

    Needs a store with an optional ``delete(session_id)`` method (both built-in stores have
    one); otherwise raises ``SDKValidationError``.
    """
    delete = getattr(store, "delete", None)
    if not callable(delete):
        raise SDKValidationError(
            f"{type(store).__name__} has no delete(session_id) method; "
            "add one to support delete_session."
        )
    return bool(await delete(session_id))


async def list_sessions(store: SessionStore) -> list[str]:
    """Return the ids of all stored sessions, sorted.

    Needs a store with an optional ``list_sessions()`` method (both built-in stores have
    one); otherwise raises ``SDKValidationError``.
    """
    lister = getattr(store, "list_sessions", None)
    if not callable(lister):
        raise SDKValidationError(
            f"{type(store).__name__} has no list_sessions() method; "
            "add one to support list_sessions."
        )
    return [str(sid) for sid in await lister()]


async def _pop_last_turn(
    store: SessionStore, session_id: str
) -> tuple[Message, SessionSnapshot, SessionSnapshot] | None:
    """``(removed user message, snapshot before, snapshot after)``, or ``None``."""
    snapshot = await store.get(session_id)
    if snapshot is None:
        return None
    last_user = next(
        (
            i
            for i in range(len(snapshot.messages) - 1, -1, -1)
            if snapshot.messages[i].role == "user" and not _is_retry_feedback(snapshot.messages[i])
        ),
        None,
    )
    if last_user is None:
        return None
    removed = snapshot.messages[last_user]
    kept = copy.deepcopy(snapshot.messages[:last_user])
    if _accepts_state(store.save):
        saved = await store.save(  # type: ignore[call-arg]
            session_id,
            kept,
            expected_revision=snapshot.revision,
            state=copy.deepcopy(snapshot.state),
        )
    else:
        saved = await store.save(session_id, kept, expected_revision=snapshot.revision)
    return removed, snapshot, saved


async def pop_last_turn(store: SessionStore, session_id: str) -> SessionSnapshot | None:
    """Remove the last user turn and everything after it; return the saved snapshot.

    The turn is the last user message plus its tool calls, tool results and reply. Earlier
    turns, the system message and saved run state stay. Returns ``None`` (and changes
    nothing) when the session is missing or has no user message.

    The save is compare-and-swap on the revision that was read, so a concurrent save
    raises ``SessionConflictError`` instead of losing data.
    """
    popped = await _pop_last_turn(store, session_id)
    return None if popped is None else popped[2]


async def retry_last_turn(
    runner: Runner, agent: Agent[OutputT], session_id: str, **run_kwargs: Any
) -> RunResult[OutputT]:
    """Pop the last turn of ``session_id`` and run its user input again.

    This is a helper function rather than a ``Runner`` method so ``runner.py`` stays
    unchanged. The runner must have a ``session_store``. Earlier turns stay; the retried turn
    gets a new reply. ``run_kwargs`` (``deps``, ``timeout``, ``cancel``, ...) go to
    ``runner.run_async``. If the retry fails or is cancelled, the original turn is put back
    (unless the session changed meanwhile, in which case nothing is overwritten). Raises
    ``SDKValidationError`` when there is no turn to retry and ``SessionConflictError`` when
    the session changed during the pop.

    Returned ``cancelled`` or ``failed`` results also restore before returning. A
    ``paused`` result stays in progress so its approval can resume the popped session.
    """
    store = runner.session_store
    if store is None:
        raise SDKValidationError("retry_last_turn needs a Runner with a session_store.")
    if "session_id" in run_kwargs:
        raise SDKValidationError("retry_last_turn takes the session id as its own argument.")
    popping = asyncio.create_task(_pop_last_turn(store, session_id))
    try:
        popped = await asyncio.shield(popping)
    except asyncio.CancelledError as cancelled:
        # save() can commit before returning (notably SQLite's thread offload).
        # Complete that pop before restoring, so cancellation cannot lose its result.
        try:
            popped = await _finish_cleanup(popping)
            if popped is not None:
                await _finish_cleanup(
                    asyncio.create_task(_restore(store, session_id, popped[1], popped[2]))
                )
        finally:
            raise cancelled
    if popped is None:
        raise SDKValidationError(f"Session '{session_id}' has no user turn to retry.")
    removed, before, after = popped
    try:
        result = await runner.run_async(agent, [removed], session_id=session_id, **run_kwargs)
    except BaseException:
        await _finish_cleanup(asyncio.create_task(_restore(store, session_id, before, after)))
        raise
    if result.status in ("cancelled", "failed"):
        await _finish_cleanup(asyncio.create_task(_restore(store, session_id, before, after)))
    return result


async def _finish_cleanup(  # noqa: UP047 - keeps Python 3.11 support
    task: asyncio.Task[_CleanupT],
) -> _CleanupT:
    """Finish rollback before returning, even if cancellation is requested again."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _restore(
    store: SessionStore, session_id: str, before: SessionSnapshot, after: SessionSnapshot
) -> None:
    """Put a popped turn back, only if nobody saved the session since the pop."""
    try:
        if _accepts_state(store.save):
            await store.save(  # type: ignore[call-arg]
                session_id,
                before.messages,
                expected_revision=after.revision,
                state=copy.deepcopy(before.state),
            )
        else:
            await store.save(session_id, before.messages, expected_revision=after.revision)
    except SessionConflictError:
        pass  # the session moved on; never overwrite newer work


class MemorySessionStore:
    """In-memory session store with compare-and-swap concurrency control.

    Useful for tests and transient execution where durability across restarts
    is not needed.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[int, str]] = {}
        # Last revision of deleted sessions: a recreated session continues the count, so a
        # snapshot taken before the delete can never match the new session's revision.
        self._deleted: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> SessionSnapshot | None:
        """Return a copy of the stored snapshot, or ``None`` if the session is unknown."""
        async with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            revision, data_str = entry
            _, rev, messages = deserialize_session(data_str)
            return SessionSnapshot(
                session_id=session_id,
                messages=copy.deepcopy(messages),
                revision=rev,
                state=session_state(data_str),
            )

    async def save(
        self,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
        state: dict[str, Any] | None = None,
    ) -> SessionSnapshot:
        """Store messages if ``expected_revision`` matches, else raise ``SessionConflictError``."""
        async with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                if expected_revision != 0:
                    raise SessionConflictError(
                        session_id=session_id,
                        expected_revision=expected_revision,
                        actual_revision=0,
                    )
                new_revision = self._deleted.pop(session_id, 0) + 1
            else:
                actual_revision, _ = entry
                if actual_revision != expected_revision:
                    raise SessionConflictError(
                        session_id=session_id,
                        expected_revision=expected_revision,
                        actual_revision=actual_revision,
                    )
                new_revision = actual_revision + 1

            serialized = serialize_session(session_id, new_revision, messages, state)
            self._sessions[session_id] = (new_revision, serialized)
            return SessionSnapshot(
                session_id=session_id,
                messages=copy.deepcopy(messages),
                revision=new_revision,
                state=copy.deepcopy(state or {}),
            )

    async def delete(self, session_id: str) -> bool:
        """Delete a session. Returns ``True`` if it existed."""
        async with self._lock:
            entry = self._sessions.pop(session_id, None)
            if entry is None:
                return False
            self._deleted[session_id] = entry[0]
            return True

    async def list_sessions(self) -> list[str]:
        """Return all stored session ids, sorted."""
        async with self._lock:
            return sorted(self._sessions)

    async def close(self) -> None:
        """No-op for in-memory store."""
        pass

    async def __aenter__(self) -> MemorySessionStore:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


class SQLiteSessionStore:
    """Durable conversation session store backed by standard library sqlite3.

    Uses thread offloading (asyncio.to_thread) to prevent blocking the event loop
    during database I/O, explicit lifecycle management, and compare-and-swap
    optimistic locking.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self._lock = threading.Lock()
        self._closed = False

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_sessions (
                    session_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            # Last revision of deleted sessions, so a recreated session continues the count.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_session_tombstones (
                    session_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL
                );
                """
            )

    def _ensure_open(self) -> sqlite3.Connection:
        if self._closed or self._conn is None:
            raise StorageError("SQLiteSessionStore connection is closed.")
        return self._conn

    def _get_sync(self, session_id: str) -> SessionSnapshot | None:
        with self._lock:
            conn = self._ensure_open()
            cursor = conn.execute(
                "SELECT revision, data FROM nexura_sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            revision, data_str = row
            _, rev, messages = deserialize_session(data_str)
            return SessionSnapshot(
                session_id=session_id,
                messages=copy.deepcopy(messages),
                revision=rev,
                state=session_state(data_str),
            )

    def _save_sync(
        self,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
        state: dict[str, Any] | None = None,
    ) -> SessionSnapshot:
        with self._lock:
            conn = self._ensure_open()
            with conn:
                cursor = conn.execute(
                    "SELECT revision FROM nexura_sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    if expected_revision != 0:
                        raise SessionConflictError(
                            session_id=session_id,
                            expected_revision=expected_revision,
                            actual_revision=0,
                        )
                    tomb = conn.execute(
                        "SELECT revision FROM nexura_session_tombstones WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    new_revision = (int(tomb[0]) if tomb else 0) + 1
                    conn.execute(
                        "DELETE FROM nexura_session_tombstones WHERE session_id = ?", (session_id,)
                    )
                    data_str = serialize_session(session_id, new_revision, messages, state)
                    conn.execute(
                        """
                        INSERT INTO nexura_sessions (session_id, revision, data, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, new_revision, data_str, time.time()),
                    )
                else:
                    actual_revision = int(row[0])
                    if actual_revision != expected_revision:
                        raise SessionConflictError(
                            session_id=session_id,
                            expected_revision=expected_revision,
                            actual_revision=actual_revision,
                        )
                    new_revision = actual_revision + 1
                    data_str = serialize_session(session_id, new_revision, messages, state)
                    conn.execute(
                        """
                        UPDATE nexura_sessions
                        SET revision = ?, data = ?, updated_at = ?
                        WHERE session_id = ? AND revision = ?
                        """,
                        (new_revision, data_str, time.time(), session_id, actual_revision),
                    )

            return SessionSnapshot(
                session_id=session_id,
                messages=copy.deepcopy(messages),
                revision=new_revision,
                state=copy.deepcopy(state or {}),
            )

    def _close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None

    async def get(self, session_id: str) -> SessionSnapshot | None:
        """Asynchronously retrieve session snapshot without blocking the event loop."""
        return await asyncio.to_thread(self._get_sync, session_id)

    def _updated_at_sync(self, session_ids: list[str]) -> dict[str, float]:
        with self._lock:
            conn = self._ensure_open()
            found: dict[str, float] = {}
            for start in range(0, len(session_ids), 500):  # stay under SQLite's variable cap
                chunk = session_ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT session_id, updated_at FROM nexura_sessions "
                    f"WHERE session_id IN ({marks})",
                    chunk,
                ).fetchall()
                found.update({sid: float(ts) for sid, ts in rows})
            return found

    async def updated_at(self, session_ids: list[str]) -> dict[str, float]:
        """Last-save time (Unix seconds) of each stored session among ``session_ids``."""
        return await asyncio.to_thread(self._updated_at_sync, list(session_ids))

    async def save(
        self,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
        state: dict[str, Any] | None = None,
    ) -> SessionSnapshot:
        """Asynchronously save session snapshot using CAS without blocking the event loop."""
        return await asyncio.to_thread(
            self._save_sync, session_id, messages, expected_revision, state
        )

    def _delete_sync(self, session_id: str) -> bool:
        with self._lock:
            conn = self._ensure_open()
            with conn:
                row = conn.execute(
                    "SELECT revision FROM nexura_sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    return False
                conn.execute("DELETE FROM nexura_sessions WHERE session_id = ?", (session_id,))
                conn.execute(
                    "INSERT OR REPLACE INTO nexura_session_tombstones (session_id, revision) "
                    "VALUES (?, ?)",
                    (session_id, int(row[0])),
                )
            return True

    def _list_sync(self) -> list[str]:
        with self._lock:
            conn = self._ensure_open()
            rows = conn.execute("SELECT session_id FROM nexura_sessions").fetchall()
            return sorted(str(r[0]) for r in rows)

    async def delete(self, session_id: str) -> bool:
        """Delete a session. Returns ``True`` if it existed."""
        return await asyncio.to_thread(self._delete_sync, session_id)

    async def list_sessions(self) -> list[str]:
        """Return all stored session ids, sorted."""
        return await asyncio.to_thread(self._list_sync)

    async def close(self) -> None:
        """Asynchronously close database connection."""
        await asyncio.to_thread(self._close_sync)

    def close_sync(self) -> None:
        """Synchronously close database connection."""
        self._close_sync()

    async def __aenter__(self) -> SQLiteSessionStore:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def __enter__(self) -> SQLiteSessionStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close_sync()
