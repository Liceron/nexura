"""Core imports stay light: provider adapters load lazily, on first attribute access."""

from __future__ import annotations

import statistics
import subprocess
import sys
import time

import pytest

_CORE_IMPORT = "import nexura_agents; from nexura_agents import Agent, Runner, tool"
_HEAVY = ("anthropic", "gemini", "vertex", "sarvam", "openai")


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )


def test_core_import_loads_no_provider_adapter() -> None:
    code = (
        f"{_CORE_IMPORT}\n"
        "import sys\n"
        f"bad = [m for m in sys.modules if any(m == 'nexura_agents.providers.' + n "
        f"or m.startswith('nexura_agents.providers.' + n + '.') for n in {_HEAVY!r})]\n"
        "print(bad)\n"
        "assert not bad, bad\n"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr


def test_lazy_names_resolve_and_import_adapters_on_demand() -> None:
    code = (
        "import sys, nexura_agents.providers as p, nexura_agents.models as m\n"
        "for name in p.__all__:\n"
        "    assert getattr(p, name) is not None, name\n"
        "for name in m.__all__:\n"
        "    assert getattr(m, name) is not None, name\n"
        "from nexura_agents.providers import OpenAIProvider\n"
        "assert OpenAIProvider.__module__.startswith('nexura_agents.providers.openai')\n"
        "assert 'nexura_agents.providers.openai' in sys.modules\n"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr


def test_unknown_attribute_raises_attribute_error() -> None:
    import nexura_agents.contracts as c
    import nexura_agents.models as m
    import nexura_agents.providers as p

    for module in (p, m, c):
        with pytest.raises(AttributeError):
            module.DoesNotExist  # noqa: B018


def test_core_import_time_bound() -> None:
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        result = _run(_CORE_IMPORT)
        timings.append(time.perf_counter() - start)
        assert result.returncode == 0, result.stderr
    # Includes interpreter start-up; the bound is generous to avoid CI flakiness.
    assert statistics.median(timings) < 1.5, timings


def test_lazy_packages_keep_submodule_attribute_access() -> None:
    code = (
        "import nexura_agents.providers as p, nexura_agents.contracts as c, "
        "nexura_agents.models as m\n"
        "p.openai.OpenAIProvider; p.errors.ProviderError\n"
        "c.events.ModuleEvent; m.openai.OpenAIChatModel\n"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr


def test_core_import_does_not_load_asyncio() -> None:
    code = f"{_CORE_IMPORT}\nimport sys\nassert 'asyncio' not in sys.modules\n"
    result = _run(code)
    assert result.returncode == 0, result.stderr


def test_lazy_asyncio_proxy_forwards_attributes() -> None:
    import asyncio

    from nexura_agents import runner

    assert runner.asyncio.CancelledError is asyncio.CancelledError
