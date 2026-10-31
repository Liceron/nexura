# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Review regressions: text components stay linear on adversarial input.

The component time limit is checked only after the work finishes, so a backtracking regex
could hold the worker for seconds. ``text.case`` once used ``([A-Z]+)([A-Z][a-z])`` and
``text.markdown_to_text`` once used ``^\\s*`` and unbounded link/emphasis patterns.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from nexura_agents.contracts.context import ExecutionContext

import nexura_runtime.modules.textops  # noqa: F401  (registers the components)
from nexura_runtime.modules import default_registry

BUDGET_SECONDS = 1.0
SIZE = 60_000


def _elapsed(module_id: str, inputs: dict[str, str]) -> float:
    module = default_registry.get(module_id, "1.0.0")
    started = time.monotonic()
    # Rejecting the input is fine; only the time matters here.
    with contextlib.suppress(Exception):
        asyncio.run(module.execute(ExecutionContext(run_id="p44", node_id="n"), inputs, {}))
    return time.monotonic() - started


@pytest.mark.parametrize("mode", ["snake", "kebab", "camel"])
def test_case_conversion_is_linear_on_capital_runs(mode: str) -> None:
    assert _elapsed("nexura.text.case", {"text": "A" * SIZE, "mode": mode}) < BUDGET_SECONDS


@pytest.mark.parametrize(
    "text",
    ["\n" * SIZE, " \n" * (SIZE // 2), "[" * SIZE, "[a](" * (SIZE // 4), "_a" * (SIZE // 2)],
    ids=["newlines", "blank-lines", "brackets", "unclosed-links", "emphasis"],
)
def test_markdown_to_text_is_linear(text: str) -> None:
    assert _elapsed("nexura.text.markdown_to_text", {"text": text}) < BUDGET_SECONDS


def test_markdown_output_is_unchanged_for_normal_documents() -> None:
    module = default_registry.get("nexura.text.markdown_to_text", "1.0.0")
    source = "# Title\n**bold** and `a*b` [link](http://x) ![alt](i.png)\n---\n> quote\n- item"
    result = asyncio.run(
        module.execute(ExecutionContext(run_id="p44", node_id="n"), {"text": source}, {})
    )
    assert result.outputs["text"] == "Title\nbold and a*b link alt\nquote\nitem"
