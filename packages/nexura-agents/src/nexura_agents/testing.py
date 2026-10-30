# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Testing helpers: block real model requests and assert on run results.

Nothing here imports ``pytest`` at module import time. ``block_model_requests`` is a plain
context manager; the ``pytest`` fixture is created only when you call ``pytest_fixture()``.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import threading
from collections.abc import Callable, Iterator
from typing import Any

from nexura_agents.errors import NexuraError
from nexura_agents.events import RunEvent, RunEventType
from nexura_agents.types import RunResult

BLOCK_ENV_VAR = "NEXURA_BLOCK_MODEL_REQUESTS"

__all__ = [
    "BLOCK_ENV_VAR",
    "ModelRequestsBlockedError",
    "assert_tool_called",
    "block_model_requests",
    "block_model_requests_from_env",
    "collect_events",
    "event_types",
    "pytest_fixture",
    "tool_calls",
]


class ModelRequestsBlockedError(NexuraError):
    """Raised when a provider-backed model is used while model requests are blocked."""


def _blocked(label: str) -> Callable[..., Any]:
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise ModelRequestsBlockedError(
            f"Real model request blocked ({label}). Model requests are disabled by "
            "nexura_agents.testing.block_model_requests(); use FakeModel or "
            "StreamingFakeProvider in tests."
        )

    return _raise


_PROVIDER_MODULES = (
    "nexura_agents.providers.openai.provider",
    "nexura_agents.providers.anthropic.provider",
    "nexura_agents.providers.gemini.provider",
    "nexura_vertex.provider",  # the nexura-vertex package, when installed
    "nexura_agents.providers.sarvam.provider",
)


def _all_subclasses(cls: type) -> Iterator[type]:
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


def _targets() -> list[tuple[type, str]]:
    """Every (class, method) that would send a real request."""
    from nexura_agents.models.litellm import LiteLLMModel
    from nexura_agents.models.openai import OpenAIChatModel
    from nexura_agents.providers.base import BaseProvider

    for name in _PROVIDER_MODULES:
        with contextlib.suppress(ImportError):  # optional extra missing: nothing to block
            importlib.import_module(name)
    found: list[tuple[type, str]] = [
        (OpenAIChatModel, "generate"),
        (OpenAIChatModel, "stream"),
        (LiteLLMModel, "generate"),
        (LiteLLMModel, "stream"),
    ]
    # BaseProvider.generate/stream/embed all dispatch to these hooks, so patching the hooks
    # of the real providers blocks them without touching the fake provider.
    for cls in _all_subclasses(BaseProvider):
        # nexura_vertex: the Vertex AI provider, moved to the nexura-vertex package.
        if not cls.__module__.startswith(("nexura_agents.providers.", "nexura_vertex.")) or (
            cls.__module__ == "nexura_agents.providers.fake"
        ):
            continue
        for method in ("_generate_internal", "_stream_internal", "_embed_internal"):
            if method in cls.__dict__:
                found.append((cls, method))
    return found


# Guards share one patch: the originals are saved when the first guard starts and restored
# when the last one is released, in any order.
_ACTIVE: list[block_model_requests] = []
_ORIGINALS: list[tuple[type, str, Any]] = []
_LOCK = threading.Lock()


class block_model_requests:  # noqa: N801 - used like a function or a context manager
    """Make provider/network-backed models raise before any request is sent.

    Blocks ``OpenAIChatModel``, ``LiteLLMModel`` and the real classes under
    ``nexura_agents.providers`` (and ``nexura_vertex``, when that package is installed).
    ``FakeModel`` and ``StreamingFakeProvider`` keep working.
    Use as a context manager, or call it and later ``.release()`` the returned object.
    Guards may overlap and be released in any order; requests stay blocked until the last
    one is released. Subclasses you define yourself that override ``generate`` are not
    covered.
    """

    def __init__(self) -> None:
        with _LOCK:
            if not _ACTIVE:
                for cls, method in _targets():
                    _ORIGINALS.append((cls, method, cls.__dict__[method]))
                    setattr(cls, method, _blocked(f"{cls.__name__}.{method}"))
            _ACTIVE.append(self)

    def release(self) -> None:
        """Stop this guard (safe to call more than once)."""
        with _LOCK:
            if not any(guard is self for guard in _ACTIVE):
                return
            _ACTIVE[:] = [guard for guard in _ACTIVE if guard is not self]
            if not _ACTIVE:
                while _ORIGINALS:
                    cls, method, original = _ORIGINALS.pop()
                    setattr(cls, method, original)

    def __enter__(self) -> block_model_requests:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def block_model_requests_from_env() -> block_model_requests | None:
    """Block requests when ``NEXURA_BLOCK_MODEL_REQUESTS`` is ``1``/``true``/``yes``.

    Call it once from ``conftest.py``. Returns the guard, or ``None`` if the variable is unset.
    """
    if os.environ.get(BLOCK_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}:
        return block_model_requests()
    return None


def pytest_fixture(*, autouse: bool = True) -> Any:
    """Build a ``pytest`` fixture that blocks model requests for each test.

    In ``conftest.py``: ``block_models = pytest_fixture()``. Imports ``pytest`` lazily.
    """
    import pytest

    @pytest.fixture(autouse=autouse)
    def block_models() -> Iterator[None]:
        with block_model_requests():
            yield

    return block_models


def collect_events(agent: Any, prompt: str, **kwargs: Any) -> list[RunEvent]:
    """Run ``agent`` through ``Runner.stream_async`` and return every event.

    Pass ``stream_text=True`` to include TEXT_DELTA / TOOL_CALL_DELTA events. Not for use
    inside a running event loop; ``async for`` over ``Runner().stream_async`` there.
    """
    import asyncio

    from nexura_agents.runner import Runner

    async def _collect() -> list[RunEvent]:
        return [e async for e in Runner().stream_async(agent, prompt, **kwargs)]

    return asyncio.run(_collect())


def event_types(result: RunResult) -> list[str]:
    """The event type values of a run, in order (for asserting on run structure)."""
    return [str(e.type.value) for e in result.events]


def tool_calls(result: RunResult) -> list[tuple[str, dict[str, Any]]]:
    """``(name, arguments)`` of every tool the run started, in order."""
    events: list[RunEvent] = result.events
    return [
        (e.data["tool_name"], dict(e.data.get("arguments") or {}))
        for e in events
        if e.type == RunEventType.TOOL_STARTED
    ]


def _same(expected: Any, actual: Any) -> bool:
    """Equal values of the same kind: ``True`` does not match ``1``, nor ``1.0`` ``1``."""
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            _same(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, (list, tuple)):
        return len(expected) == len(actual) and all(map(_same, expected, actual))
    return bool(expected == actual)


def assert_tool_called(result: RunResult, name: str, **arguments: Any) -> None:
    """Assert the run called tool ``name`` with at least the given arguments.

    Each given argument must match exactly, type included (``True`` does not match ``1``).
    """
    calls = tool_calls(result)
    for call_name, call_args in calls:
        if call_name == name and all(
            k in call_args and _same(v, call_args[k]) for k, v in arguments.items()
        ):
            return
    raise AssertionError(
        f"Tool {name!r} with arguments {arguments!r} was not called; calls: {calls!r}"
    )
