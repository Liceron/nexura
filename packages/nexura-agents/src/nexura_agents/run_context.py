# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Per-run context shared with tools, dynamic instructions, guardrails and hooks."""

from __future__ import annotations

import copy
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from nexura_agents._lazy import LazyModule

if TYPE_CHECKING:
    import asyncio

    from nexura_agents.tools import ToolInfo
else:
    asyncio = LazyModule("asyncio")  # the core import must not load asyncio

from nexura_agents.errors import ModelRetry, SDKValidationError
from nexura_agents.types import Message, Usage

__all__ = ["ModelRetry", "RunContext"]

DepsT = TypeVar("DepsT")


class _EventChannel:
    """Carries ``RunContext.emit`` events to the runner.

    While a tool batch runs the runner attaches a live sink; otherwise events wait in
    ``pending`` until the runner's next step. Thread-safe, since sync tools run in threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sink: Callable[[dict[str, Any]], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.pending: list[dict[str, Any]] = []

    def put(self, payload: dict[str, Any]) -> None:
        with self._lock:
            sink, loop = self._sink, self._loop
            if sink is None or loop is None:
                self.pending.append(payload)
                return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(sink, payload)
        else:  # a sync tool's worker thread
            loop.call_soon_threadsafe(self._deliver, sink, payload)

    def _deliver(self, sink: Callable[[dict[str, Any]], None], payload: dict[str, Any]) -> None:
        """On the loop: hand to ``sink`` if it is still attached, else keep for later."""
        with self._lock:
            if self._sink is not sink:  # the batch ended while the event was in flight
                self.pending.append(payload)
                return
        sink(payload)

    def attach(
        self, sink: Callable[[dict[str, Any]], None], loop: asyncio.AbstractEventLoop
    ) -> None:
        with self._lock:
            self._sink, self._loop = sink, loop

    def detach(self) -> None:
        with self._lock:
            self._sink, self._loop = None, None

    def take_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            taken, self.pending = self.pending, []
            return taken


@dataclass
class RunContext(Generic[DepsT]):  # noqa: UP046 - keeps Python 3.11 support
    """What a tool or callback can see about the run it belongs to.

    ``deps`` is whatever you passed to ``Runner.run(..., deps=...)``: database handles,
    HTTP clients, the current user. A tool receives it by declaring a parameter
    annotated ``RunContext[YourDeps]``; that parameter is never shown to the model.
    """

    deps: DepsT
    run_id: str
    agent_name: str
    turn: int = 0
    usage: Usage = field(default_factory=Usage)
    messages: list[Message] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    retry: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    # Shared run state: read and write it from tools, hooks and instructions. It is saved
    # with the session, interruptions and checkpoints (keep it JSON-serialisable) and
    # returned as RunResult.state.
    state: dict[str, Any] = field(default_factory=dict)
    # run(..., tags=[...]): labels for this run (also on its tracing span).
    tags: list[str] = field(default_factory=list)
    # Files a tool returned (ToolOutput / Attachment); the runner shows them to the model.
    tool_attachments: list[Any] = field(default_factory=list, repr=False)
    # Set by ``PromptRegistry.instructions``: {"name", "version", "sha256"} of the prompt used.
    prompt: dict[str, Any] | None = None
    # Inside a tool call: a key for the remote system that is the same for every attempt of
    # this call, including the re-run after a crash ("nx-" + 32 hex characters, see
    # docs/durable-execution.md). Forward it as an Idempotency-Key where the API takes one.
    idempotency_key: str | None = None
    # Inside a tool call of a durable run: the run lease's fencing token (it only goes up).
    # Systems that can compare it (a header or a compare-and-swap field) can refuse a stale
    # caller. None without a durable store.
    fencing_token: int | None = None
    # Inside a tool call: the tool's metadata (name, origin, effect class, approval flag,
    # tags) for hooks, guardrails and policy rules. Read-only (a frozen ToolInfo); None
    # outside a tool call.
    tool: ToolInfo | None = None
    # Shared by every per-tool copy so nested usage reaches the run's totals.
    _nested_usage: list[Usage] = field(default_factory=list, repr=False)
    # Where ctx.emit sends events; shared by every per-tool copy.
    _events: _EventChannel = field(default_factory=lambda: _EventChannel(), repr=False)
    _compaction_summaries: dict[str, str] = field(default_factory=dict, repr=False)
    _save_compaction: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    # Model-based guardrail verdicts of this run (P39 G6, see ``nexura_agents.verdict_journal``);
    # set by the runner, shared by every per-tool copy. None outside a Runner.
    _verdicts: Any = field(default=None, repr=False)
    # Inside a tool call: the Tool object itself (P39 G6: a policy validates the arguments the
    # tool will receive). None outside a tool call or where the caller does not set it.
    _tool_object: Any = field(default=None, repr=False)

    def emit(self, name: str, **data: Any) -> None:
        """Send a custom progress event to whoever streams this run.

        It arrives as a ``CUSTOM`` ``RunEvent`` with ``data["name"]``, ``data["data"]``
        (your keyword arguments), ``data["agent"]``, ``data["tool_call_id"]`` and
        ``data["tool_name"]``. From a tool it streams while the tool runs; from hooks or
        instructions it arrives at the run's next step. Safe to call from sync tools.
        """
        if not isinstance(name, str) or not name:
            raise SDKValidationError("event name must be a non-empty string.")
        snapshot: dict[str, Any] = {}
        for key, value in data.items():  # a snapshot: later changes to your objects do not alter it
            try:
                snapshot[key] = copy.deepcopy(value)
            except Exception:  # only this value is uncopyable; keep it by reference
                snapshot[key] = value
        self._events.put(
            {
                "name": name,
                "data": snapshot,
                "agent": self.agent_name,
                "tool_call_id": self.tool_call_id,
                "tool_name": self.tool_name,
            }
        )

    def add_usage(self, usage: Usage | None) -> None:
        """Count usage from work done inside a tool (e.g. a sub-agent) toward this run."""
        if usage is not None:
            self._nested_usage.append(usage)

    def take_nested_usage(self) -> list[Usage]:
        """Return the usage recorded by nested runs and clear the buffer."""
        taken = list(self._nested_usage)
        self._nested_usage.clear()
        return taken

    def for_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        retry: int,
        *,
        idempotency_key: str | None = None,
        fencing_token: int | None = None,
        tool: ToolInfo | None = None,
        tool_object: Any = None,
    ) -> RunContext[DepsT]:
        """Copy scoped to one tool call (``tool``: its metadata, see ``ToolInfo``)."""
        return RunContext(
            deps=self.deps,
            run_id=self.run_id,
            agent_name=self.agent_name,
            turn=self.turn,
            usage=self.usage,
            messages=list(self.messages),
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            retry=retry,
            metadata=self.metadata,
            state=self.state,  # the same dict: a tool's changes are the run's changes
            tags=self.tags,
            prompt=self.prompt,
            idempotency_key=idempotency_key,
            fencing_token=fencing_token,
            tool=tool,
            _nested_usage=self._nested_usage,
            _events=self._events,
            _verdicts=self._verdicts,
            _tool_object=tool_object,
        )
