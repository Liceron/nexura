# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Shared offline helpers for the nexura-guardrails tests (FakeModel and scripted models only).

Imported by the test modules in this directory (pytest puts the directory on ``sys.path``).

``obedient_judge`` is a scripted judge model that behaves like a naive LLM: it obeys any
"return allow" instruction it finds *outside* the nonce-fenced content block (in the system
message or in unfenced user text), and otherwise classifies the fenced data. A prompt that
let the content escape its fence would therefore get an ``allow``.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from nexura_agents import FakeModel, Message, Usage
from nexura_agents.run_context import RunContext
from nexura_agents.usage import BudgetConfig, BudgetManager
from nexura_agents.usage.models import ModelPricing

REPORTED = Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)

_COMPLY = re.compile(r"return\s+allow|allow\s+return|answer\s+allow", re.IGNORECASE)
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+)?previous\s+instructions|instructions?\s+ko\s+ignore|निर्देशों\s+को\s+अनदेखा",
    re.IGNORECASE,
)


def verdict_json(
    decision: str = "allow",
    categories: list[str] | None = None,
    score: float = 0.0,
    reason: str = "ok",
) -> str:
    return json.dumps(
        {"decision": decision, "categories": categories or [], "score": score, "reason": reason}
    )


ALLOW = verdict_json()
BLOCK_INJECTION = verdict_json("block", ["prompt_injection"], 0.95, "tries to steer the verdict")


def obedient_judge(messages: list[Message], tools: Any) -> str:
    """A scripted judge that complies with instructions it can see outside the data fence."""
    assert tools is None, "the judge must not be offered tools"
    system = messages[0].content or ""
    user = messages[-1].content or ""
    declared = re.search(r"<<CONTENT-([0-9a-f]+)>>", system)
    if declared is None:  # no fence: everything is instructions to a naive model
        return ALLOW if _COMPLY.search(system + "\n" + user) else BLOCK_INJECTION
    nonce = declared.group(1)
    opening, closing = f"<<CONTENT-{nonce}>>\n", f"\n<</CONTENT-{nonce}>>"
    start = user.find(opening)
    end = user.find(closing, start + len(opening)) if start >= 0 else -1
    if start < 0 or end < 0:
        return ALLOW  # a broken fence: the naive model gives up and complies
    data = user[start + len(opening) : end]
    outside = system + "\n" + user[:start] + user[end + len(closing) :]
    # Context blocks are data too.
    outside = re.sub(
        rf"<<CONTEXT-{nonce}>>\n.*?\n<</CONTEXT-{nonce}>>", "", outside, flags=re.DOTALL
    )
    if _COMPLY.search(outside):
        return ALLOW  # an instruction leaked out of the data block: obey it
    if _INJECTION.search(data) or _COMPLY.search(data):
        return BLOCK_INJECTION
    return ALLOW


def make_budget(max_tokens: int = 40000) -> Any:
    manager = BudgetManager()
    manager.pricing_table.add_model_pricing(
        ModelPricing(
            model_id="fake-model",
            input_cost_per_million=Decimal("0"),
            output_cost_per_million=Decimal("0"),
        )
    )
    return manager.create_run_budget("w", "r", config=BudgetConfig(max_tokens=max_tokens))


def run_ctx(run_id: str = "run-1") -> RunContext[Any]:
    return RunContext(deps=None, run_id=run_id, agent_name="test")


def obedient(**kwargs: Any) -> FakeModel:
    """A FakeModel judge driven by ``obedient_judge``."""
    return FakeModel(handler=obedient_judge, **kwargs)
