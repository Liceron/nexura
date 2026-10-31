# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Expose Nexura checks as registered Guardrails AI validators."""

from __future__ import annotations

import asyncio
import contextvars
import re
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from guardrails import OnFailAction  # type: ignore[import-untyped]
from guardrails.validators import (  # type: ignore[import-untyped]
    FailResult,
    PassResult,
    Validator,
    register_validator,
)
from nexura_guardrails import prefilters
from nexura_guardrails.checks import Check, evaluate, value_text
from nexura_guardrails.verdict import CATEGORIES, Stage

_CLASSES: dict[str, type[_NexuraValidator]] = {}
_REGISTRATION_LOCK = threading.Lock()


class _NexuraValidator(Validator):  # type: ignore[misc]  # Upstream has no py.typed marker.
    def __init__(self, check: Check, stage: Stage, on_fail: OnFailAction) -> None:
        super().__init__(on_fail=on_fail, use_local=True)
        self.check, self.stage = check, stage

    async def async_validate(self, value: Any, metadata: dict[str, Any]) -> PassResult | FailResult:
        verdict = await evaluate(self.check, value_text(value), stage=self.stage)
        if verdict.decision == "allow":
            return PassResult()
        # No free-text reason or evidence crosses into a Guard's errors or reask prompts.
        categories = ", ".join(c for c in verdict.categories if c in CATEGORIES) or "other"
        message = f"Nexura check '{self.rail_alias}' failed (categories: {categories})"
        return FailResult(
            error_message=message,
            fix_value=verdict.replacement if verdict.decision == "rewrite" else None,
        )

    def validate(self, value: Any, metadata: dict[str, Any]) -> PassResult | FailResult:
        def run() -> PassResult | FailResult:
            return asyncio.run(self.async_validate(value, metadata))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run()
        # A synchronous API necessarily blocks its caller; the check runs on a fresh loop
        # in a worker, retaining budget/context variables without nesting asyncio.run.
        context = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(context.run, run).result()


def as_validator(
    check: Check,
    *,
    name: str | None = None,
    on_fail: OnFailAction = OnFailAction.EXCEPTION,
    stage: Stage = "output",
) -> Validator:
    """Register once per name; each instance owns its check, stage and on-fail action."""
    chosen = check.name if name is None else name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", chosen):
        raise ValueError("validator name must be a short identifier")
    if stage not in check.stages:
        raise ValueError(f"check does not support stage {stage!r}")
    alias = f"nexura/{chosen}"
    with _REGISTRATION_LOCK:
        cls = _CLASSES.get(alias)
        if cls is None:
            cls = type(f"Nexura_{chosen}", (_NexuraValidator,), {})
            register_validator(name=alias, data_type="string")(cls)
            _CLASSES[alias] = cls
    return cls(check, stage, on_fail)


def nexura_validators(
    *,
    pii_action: Literal["block", "redact"] = "block",
    chars: int = 100_000,
    keywords: Iterable[str] = (),
    deny: Iterable[str] = (),
    on_fail: OnFailAction = OnFailAction.EXCEPTION,
) -> tuple[Validator, ...]:
    """Filters for JSON output: injection, PII, length, optional topics and JSON syntax.

    Choose ``pii_action='redact'`` for the alternative PII validator. It uses FIX while
    the other filters retain ``on_fail``; stacking block and redact would prevent fixes.
    Topics are included when keywords/deny are supplied; length caps 100,000 chars.
    For plain text or individually configured filters, use ``as_validator`` directly.
    """
    if pii_action not in ("block", "redact"):
        raise ValueError("pii_action must be block or redact")
    checks: list[Check] = [
        prefilters.injection(),
        prefilters.pii(action=pii_action, name=f"pii_{pii_action}"),
        prefilters.length(chars=chars),
    ]
    keywords, deny = tuple(keywords), tuple(deny)
    if keywords or deny:
        checks.append(prefilters.topics(keywords=keywords, deny=deny))
    checks.append(prefilters.json_only())
    return tuple(
        as_validator(
            check,
            on_fail=OnFailAction.FIX if index == 1 and pii_action == "redact" else on_fail,
        )
        for index, check in enumerate(checks)
    )
