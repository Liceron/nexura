# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Wrap validators or an existing Guard as a Nexura Check. No Hub installation or LLM calls."""

from __future__ import annotations

import asyncio
import importlib
import math
import re
import threading
import time
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, Literal

from guardrails import AsyncGuard, Guard  # type: ignore[import-untyped]
from guardrails.errors import ValidationError  # type: ignore[import-untyped]
from guardrails.hub.registry import get_registry  # type: ignore[import-untyped]
from guardrails.validators import Validator  # type: ignore[import-untyped]
from guardrails_ai.types import FailResult, PassResult  # type: ignore[import-untyped]
from nexura_agents.contracts.errors import MissingDependencyError
from nexura_agents.run_context import RunContext
from nexura_guardrails.checks import as_guardrail
from nexura_guardrails.presets import Preset
from nexura_guardrails.verdict import STAGES, CheckInput, Verdict, validate_categories

HUB_CATEGORIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "ToxicLanguage": ("harassment",),
        "DetectPII": ("privacy",),
        "DetectJailbreak": ("jailbreak",),
        "CompetitorCheck": ("other",),
        "RestrictToTopic": ("other",),
        "ProfanityFree": ("harassment",),
    }
)
OnFail = Literal["block", "fix"]
OnError = Literal["block", "allow", "review"]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.rsplit("/", 1)[-1].lower())


class _ValidationFailed(Exception):
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries


class GuardrailsAICheck:
    """Run validators in order, or preserve an existing Guard's validation configuration.

    Validator instances run through ``async_validate`` (the base implementation offloads
    synchronous ``validate``). Fixes flow into subsequent validators. Existing Guards keep
    their own on-fail actions; only a completed string correction can become a rewrite.
    ``deterministic=True`` is a caller assertion that every wrapped validator is rule-based.
    Validator failure messages may contain content: only evidence receives them, truncated.
    """

    def __init__(
        self,
        validators_or_guard: Validator | Iterable[Validator] | Guard[Any],
        *,
        name: str = "guardrails_ai",
        stages: Iterable[str] = ("input", "output", "tool_input", "tool_output"),
        on_fail: OnFail = "block",
        category: str = "other",
        categories: Mapping[str, Iterable[str]] | None = None,
        on_error: OnError = "block",
        timeout: float = 10.0,
        deterministic: bool = False,
    ) -> None:
        if on_fail not in ("block", "fix") or on_error not in ("block", "allow", "review"):
            raise ValueError("on_fail must be block/fix; on_error must be block/allow/review")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", name):
            raise ValueError("name must be a short identifier")
        self.name = name
        self.stages = frozenset(stages)
        if not self.stages or self.stages - set(STAGES):
            raise ValueError("stages must be a nonempty selection of Nexura stages")
        self.on_fail, self.on_error, self.timeout = on_fail, on_error, timeout
        self.deterministic = deterministic
        self._default_categories = validate_categories((category,))
        self._categories = {_key(k): v for k, v in HUB_CATEGORIES.items()}
        for key, ids in (categories or {}).items():
            chosen = validate_categories(ids)
            if not chosen:
                raise ValueError("category mappings must not be empty")
            self._categories[_key(key)] = chosen
        self._guard = validators_or_guard if isinstance(validators_or_guard, Guard) else None
        if self._guard is not None:
            self._validators = tuple(self._guard.get_validators("output"))
        else:
            self._validators = (
                (validators_or_guard,)
                if isinstance(validators_or_guard, Validator)
                else tuple(validators_or_guard)
            )
            if not self._validators or not all(isinstance(v, Validator) for v in self._validators):
                raise TypeError(
                    "expected a Validator, a nonempty iterable of Validators, or a Guard"
                )
        # Hold the sync lock through history extraction, even if wait_for has timed out.
        self._sync_lock = threading.Lock()
        self._async_lock = asyncio.Lock()

    def _cats(self, name: str) -> tuple[str, ...]:
        return self._categories.get(_key(name), self._default_categories)

    @staticmethod
    def _entry(name: str, result: Any) -> dict[str, Any]:
        if not isinstance(result, (PassResult, FailResult)):
            raise TypeError("validator must return PassResult or FailResult")
        entry: dict[str, Any] = {"name": name, "status": result.outcome}
        if isinstance(result, FailResult):
            entry["failure_reason"] = result.error_message[:200]
            # Spans can also echo content in their reason. Keep numeric offsets only.
            entry["error_spans"] = [
                {"start": s.start, "end": s.end} for s in (result.error_spans or [])
            ]
        return entry

    async def _run_validators(self, text: str) -> tuple[list[dict[str, Any]], str | None]:
        entries: list[dict[str, Any]] = []
        current = text
        fixable = True
        failed = False
        for validator in self._validators:
            try:
                if type(validator).async_validate is Validator.async_validate:
                    result = await asyncio.to_thread(validator.validate, current, {})
                else:
                    result = await validator.async_validate(current, {})
            except ValidationError as exc:
                entries.append(
                    {
                        "name": validator.rail_alias,
                        "status": "fail",
                        "failure_reason": str(exc)[:200],
                    }
                )
                raise _ValidationFailed(entries) from exc
            entries.append(self._entry(validator.rail_alias, result))
            if isinstance(result, FailResult):
                failed = True
                if isinstance(result.fix_value, str):
                    if self.on_fail == "fix":
                        current = result.fix_value
                else:
                    fixable = False
        return entries, current if failed and fixable and self.on_fail == "fix" else None

    def _guard_result(self, outcome: Any) -> tuple[list[dict[str, Any]], str | None]:
        assert self._guard is not None
        call = next((c for c in self._guard.history if c.id == outcome.call_id), None)
        logs = list(call.validator_logs) if call else []
        entries = [
            self._entry(log.registered_name or log.validator_name, log.validation_result)
            for log in logs
            if log.validation_result is not None
        ]
        if not entries:
            entries = [
                {
                    "name": s.validator_name,
                    "status": s.validator_status,
                    "failure_reason": (s.failure_reason or "")[:200],
                }
                for s in (outcome.validation_summaries or [])
            ]
        if outcome.error:
            raise RuntimeError("Guard returned an execution error")
        if not outcome.validation_passed and not any(e["status"] == "fail" for e in entries):
            entries.append({"name": "Guard", "status": "fail"})
        failed = any(e["status"] == "fail" for e in entries)
        failures = [
            log.validation_result for log in logs if isinstance(log.validation_result, FailResult)
        ]
        fixable = bool(failures) and all(isinstance(r.fix_value, str) for r in failures)
        # FIX may report validation_passed=True even though validators initially failed.
        replacement = (
            outcome.validated_output if failed and fixable and outcome.validation_passed else None
        )
        return entries, replacement if isinstance(replacement, str) else None

    def _guard_failure(self, exc: Exception) -> _ValidationFailed:
        assert self._guard is not None
        call = self._guard.history.last
        entries = [
            self._entry(log.registered_name or log.validator_name, log.validation_result)
            for log in (call.validator_logs if call else [])
            if log.validation_result is not None
        ]
        if not any(e["status"] == "fail" for e in entries):
            entries.append({"name": "Guard", "status": "fail", "failure_reason": str(exc)[:200]})
        return _ValidationFailed(entries)

    def _sync_guard(self, text: str) -> tuple[list[dict[str, Any]], str | None]:
        assert self._guard is not None
        with self._sync_lock:
            try:
                return self._guard_result(self._guard.validate(text, num_reasks=0))
            except ValidationError as exc:
                raise self._guard_failure(exc) from exc

    async def _run(self, text: str) -> tuple[list[dict[str, Any]], str | None]:
        if isinstance(self._guard, AsyncGuard):
            async with self._async_lock:
                try:
                    return self._guard_result(await self._guard.validate(text, num_reasks=0))
                except ValidationError as exc:
                    raise self._guard_failure(exc) from exc
        if self._guard is not None:
            return await asyncio.to_thread(self._sync_guard, text)
        return await self._run_validators(text)

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        started = time.perf_counter()
        common: dict[str, Any] = {"check": self.name, "backend": "guardrails-ai"}
        try:
            if item.stage not in self.stages:
                raise ValueError("unsupported check stage")
            entries, replacement = await asyncio.wait_for(self._run(item.text), self.timeout)
        except asyncio.CancelledError:
            raise
        except _ValidationFailed as exc:
            # An EXCEPTION on-fail action is an ordinary validation failure, not an outage.
            entries, replacement = exc.entries, None
        except Exception as exc:
            decision: Literal["error", "allow", "review"] = (
                "error" if self.on_error == "block" else self.on_error
            )
            return Verdict(
                decision=decision,
                reason="Guardrails AI could not decide",
                evidence={"error_type": type(exc).__name__, "on_error": self.on_error},
                latency_ms=(time.perf_counter() - started) * 1000,
                **common,
            )
        failures = [e for e in entries if e["status"] == "fail"]
        cats = tuple(dict.fromkeys(c for e in failures for c in self._cats(e["name"])))
        rewrite = (
            bool(failures)
            and self.on_fail == "fix"
            and replacement is not None
            and item.stage in ("output", "tool_output")
        )
        return Verdict(
            decision="rewrite" if rewrite else "block" if failures else "allow",
            replacement=replacement if rewrite else None,
            categories=cats,
            reason="Guardrails AI validation failed"
            if failures
            else "Guardrails AI validation passed",
            evidence={"validators": entries},
            latency_ms=(time.perf_counter() - started) * 1000,
            **common,
        )


def from_hub(name: str, **kwargs: Any) -> Validator:
    """Instantiate an already-installed Hub export or registry id; never download it."""
    key = name.removeprefix("hub://")
    if len(key) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?", key):
        raise ValueError("expected a Hub export name or namespace/validator_name")
    try:
        hub = importlib.import_module("guardrails.hub")
        if "/" in key:
            entry = get_registry().validators.get(key)
            if entry is None or not entry.import_path:
                raise ImportError("validator is absent from the local Hub registry")
            module = importlib.import_module(entry.import_path)
            exports = [getattr(module, export) for export in entry.exports]
            cls = next(c for c in exports if isinstance(c, type) and issubclass(c, Validator))
        else:
            cls = getattr(hub, key)
    except (ImportError, AttributeError, StopIteration) as exc:
        # Lookarounds keep the CamelCase split linear.
        slug = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", key)
        slug = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", slug).lower()
        install_id = key if "/" in key else f"guardrails/{slug}"
        raise MissingDependencyError(
            name,
            message=f"Hub validator {name!r} is not installed. Install it explicitly with "
            f"`guardrails hub install hub://{install_id}` and retry. "
            "The bridge never installs validators at runtime.",
            original_error=exc,
        ) from exc
    if not isinstance(cls, type) or not issubclass(cls, Validator):
        raise TypeError("Hub export is not a Guardrails AI Validator")
    return cls(**kwargs)


def guardrails_ai(*validators: Validator, on_fail: OnFail = "block") -> Preset:
    """Input and output guardrails; network profile is conservatively 'model'."""
    check = GuardrailsAICheck(validators, on_fail=on_fail)
    return Preset(
        name="guardrails_ai",
        network="model",
        input_guardrails=(as_guardrail(check),),
        output_guardrails=(as_guardrail(check, stage="output"),),
        checks=MappingProxyType({"input": check, "output": check}),
    )
