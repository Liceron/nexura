# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Which run step a budget reservation was made for (Execution Contract v1, A6 S11).

A runtime that dispatches a step (an agent's model call or tool call, a workflow node,
branch or mapped item) opens a *dispatch scope* around it. Every reservation the ledger
makes inside that scope records the scope in its metadata:

- ``metadata["execution"]``: the path of enclosing steps, outermost first, one entry per
  run (``workspace_id``, ``run_id``, ``step_key``, ``tracked``);
- ``metadata["execution_run_id"]``, ``metadata["execution_workspace_id"]`` and
  ``metadata["step_key"]``: the innermost entry, for reading.

``tracked`` says that the dispatcher commits a ``StepIntent`` before the step dispatches
(a non-pure tool call or node on a journaled run). For such a step, "no intent in the
journal" proves the step never dispatched. Model calls and pure steps write no intent:
their reservation is taken at admission, right before the provider request, so it may
have been billed. :mod:`nexura_agents.usage.recovery` uses these records after a crash.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Callable, Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DispatchFrame",
    "current_dispatch_path",
    "dispatch_scope",
    "enter_dispatch_scope",
    "frames_of",
    "leave_dispatch_scope",
    "link_metadata",
    "scoped_stream",
]


@dataclass(frozen=True)
class DispatchFrame:
    """One step of one run that budget reservations are made for."""

    workspace_id: str
    run_id: str
    step_key: str
    #: The dispatcher committed a ``StepIntent`` before the step dispatched.
    tracked: bool = False
    #: The run lease's fencing token of the owner that dispatched (``None``: no lease).
    #: A terminal sweep ends only reservations made under a token no newer than its own.
    fencing_token: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "step_key": self.step_key,
            "tracked": self.tracked,
        }
        if self.fencing_token is not None:
            data["fencing_token"] = self.fencing_token
        return data

    @classmethod
    def from_dict(cls, data: Any) -> DispatchFrame | None:
        if not isinstance(data, dict):
            return None
        workspace, run, key = data.get("workspace_id"), data.get("run_id"), data.get("step_key")
        if not all(isinstance(value, str) and value for value in (workspace, run, key)):
            return None
        token = data.get("fencing_token")
        return cls(
            str(workspace),
            str(run),
            str(key),
            data.get("tracked") is True,
            token if isinstance(token, int) and not isinstance(token, bool) else None,
        )


_PATH: ContextVar[tuple[DispatchFrame, ...]] = ContextVar("nexura_dispatch_path", default=())


def current_dispatch_path() -> tuple[DispatchFrame, ...]:
    """The enclosing dispatch scopes, outermost first."""
    return _PATH.get()


def enter_dispatch_scope(
    workspace_id: str,
    run_id: str,
    step_key: str,
    *,
    tracked: bool = False,
    fencing_token: int | None = None,
) -> Token[tuple[DispatchFrame, ...]]:
    """Open a scope; close it with :func:`leave_dispatch_scope` and the returned token.

    A run appears once on the path: entering a step of a run already on it (a tool call
    inside the same run's earlier scope) replaces that run's entry.
    """
    frame = DispatchFrame(
        str(workspace_id or "default"), str(run_id), str(step_key), tracked, fencing_token
    )
    path = tuple(
        f for f in _PATH.get() if (f.workspace_id, f.run_id) != (frame.workspace_id, frame.run_id)
    )
    return _PATH.set((*path, frame))


def leave_dispatch_scope(token: Token[tuple[DispatchFrame, ...]]) -> None:
    _PATH.reset(token)


@contextlib.contextmanager
def dispatch_scope(
    workspace_id: str,
    run_id: str,
    step_key: str,
    *,
    tracked: bool = False,
    fencing_token: int | None = None,
) -> Iterator[None]:
    """Reservations made inside are linked to this run step."""
    token = enter_dispatch_scope(
        workspace_id, run_id, step_key, tracked=tracked, fencing_token=fencing_token
    )
    try:
        yield
    finally:
        _PATH.reset(token)


async def scoped_stream(
    stream: Any,
    workspace_id: str,
    run_id: str,
    step_key: str,
    *,
    tracked: bool = False,
    fencing_token: int | Callable[[], int | None] | None = None,
) -> AsyncGenerator[Any, None]:
    """Advance ``stream`` inside the scope, without leaking it to the consumer.

    ``fencing_token`` may be a callable: it is read at each advance (a run takes its lease
    after its stream starts).
    """

    def enter() -> Token[tuple[DispatchFrame, ...]]:
        current = fencing_token() if callable(fencing_token) else fencing_token
        return enter_dispatch_scope(
            workspace_id, run_id, step_key, tracked=tracked, fencing_token=current
        )

    try:
        while True:
            token = enter()
            try:
                item = await anext(stream)
            except StopAsyncIteration:
                return
            finally:
                _PATH.reset(token)
            yield item
    finally:
        token = enter()
        try:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        finally:
            _PATH.reset(token)


def link_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Reservation metadata with the current dispatch path (unchanged outside a scope)."""
    path = _PATH.get()
    if not path or "execution" in metadata:
        return metadata
    inner = path[-1]
    return {
        **metadata,
        "execution": [frame.to_dict() for frame in path],
        "execution_workspace_id": inner.workspace_id,
        "execution_run_id": inner.run_id,
        "step_key": inner.step_key,
    }


def frames_of(metadata: dict[str, Any]) -> list[DispatchFrame]:
    """The dispatch path recorded on a reservation (empty when it has none)."""
    raw = metadata.get("execution") if isinstance(metadata, dict) else None
    if not isinstance(raw, list):
        return []
    return [frame for frame in (DispatchFrame.from_dict(item) for item in raw) if frame]
