# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Compose checks: ``tiered`` (in order), ``parallel`` (together) and ``cached``.

Every composition is itself a ``Check``, so compositions nest and convert with
``as_guardrail`` like any other check. All three fail closed: an ``error`` from any member
is never turned into ``allow``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import random
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Literal

from nexura_agents.run_context import RunContext
from nexura_agents.types import Usage

from nexura_guardrails.checks import Check, is_hard_error, run_check
from nexura_guardrails.verdict import CheckInput, Verdict

__all__ = [
    "Cached",
    "Parallel",
    "Tiered",
    "VerdictCache",
    "cached",
    "check_identity",
    "is_fallback",
    "parallel",
    "tiered",
]

_STOP = ("block", "error", "review")


def _add_usage(total: Usage | None, verdict: Verdict) -> Usage | None:
    if verdict.cached or verdict.usage is None:
        return total
    return verdict.usage if total is None else total.add(verdict.usage)


def _trace(verdict: Verdict) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "check": verdict.check,
        "decision": verdict.decision,
        "latency_ms": round(verdict.latency_ms, 3),
        "cached": verdict.cached,
    }
    if verdict.evidence.get("error_type"):
        trace["error_type"] = verdict.evidence["error_type"]
    if is_fallback(verdict):
        trace["fallback"] = True  # carried up through every level of nesting
    return trace


def is_fallback(verdict: Verdict) -> bool:
    """True when the verdict, or a member of a composite verdict, stands in for a failure.

    That covers ``error`` verdicts and the ``allow`` / ``review`` a check returns instead of
    an error when told to (``on_error="allow"`` or ``"review"``); they carry
    ``evidence["error_type"]``. Such verdicts describe an outage, not the content, and are
    never cached. Composites record ``fallback`` in each member's trace, so a fallback any
    number of levels down is found.
    """
    if verdict.decision == "error" or verdict.evidence.get("error_type"):
        return True
    if verdict.evidence.get("fallback") is True:
        return True
    members = [*verdict.evidence.get("tiers", ()), *verdict.evidence.get("members", ())]
    return any(isinstance(m, dict) and (m.get("error_type") or m.get("fallback")) for m in members)


def check_identity(check: Any) -> str:
    """The cache identity of ``check``.

    A check's own ``cache_identity`` when it gives one (a non-empty string). Otherwise an
    object identity (``object:<class>:<name>:<id>``); ``Cached`` then pins the check in the
    cache for the cache's lifetime, so the id can never be reused by another object while
    entries keyed by it exist.
    """
    identity = getattr(check, "cache_identity", None)
    if isinstance(identity, str) and identity:
        return identity
    cls = type(check)
    return f"object:{cls.__module__}.{cls.__qualname__}:{getattr(check, 'name', '')}:{id(check)}"


def _common_stages(checks: tuple[Check, ...]) -> frozenset[str]:
    stages: frozenset[str] | None = None
    for check in checks:
        own = frozenset(getattr(check, "stages", frozenset()))
        stages = own if stages is None else stages & own
    return stages or frozenset()


def _validate_members(kind: str, checks: tuple[Check, ...]) -> None:
    if not checks:
        raise ValueError(f"{kind}() needs at least one check.")
    for check in checks:
        if not callable(check) or not isinstance(getattr(check, "name", None), str):
            raise TypeError(f"{kind}() members must be checks (a name and an async call).")


# --------------------------------------------------------------------------- cache


class VerdictCache:
    """A small LRU of verdicts keyed by a content hash.

    Without ``ttl`` entries are scoped to one run (the key includes the run id), so a
    policy or model change never serves an old verdict to a new run. With ``ttl`` (seconds)
    entries are shared across runs in this process and expire after ``ttl``. ``error``
    verdicts and on_error fallbacks are never stored. Only hashes are kept, never the checked text.
    """

    def __init__(
        self,
        maxsize: int = 256,
        *,
        ttl: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize < 1:
            raise ValueError("maxsize must be a positive integer.")
        if ttl is not None and (isinstance(ttl, bool) or ttl <= 0):
            raise ValueError("ttl must be a positive number of seconds or None.")
        self.maxsize = maxsize
        self.ttl = ttl
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, Verdict]] = OrderedDict()
        self._pinned: dict[int, object] = {}
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._items)

    @staticmethod
    def digest(*parts: str | None) -> str:
        """sha256 over the parts, unambiguous: each part is tagged (``N`` for None, ``S`` for
        a string) and strings are length-prefixed, so ("ab", "c") != ("a", "bc") and None is
        never equal to any string."""
        h = hashlib.sha256()
        for part in parts:
            if part is None:
                h.update(b"N")
                continue
            data = part.encode("utf-8", "surrogatepass")
            h.update(b"S")
            h.update(len(data).to_bytes(8, "big"))
            h.update(data)
        return h.hexdigest()

    def key(self, ctx: RunContext[Any], *parts: str | None) -> str:
        """The cache key for ``parts`` in this run (or process-wide when ``ttl`` is set)."""
        scope = "process" if self.ttl is not None else f"run:{ctx.run_id}"
        return self.digest(scope, *parts)

    def get(self, key: str) -> Verdict | None:
        """The stored verdict marked ``cached=True`` with no usage, or None."""
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        stored_at, verdict = entry
        if self.ttl is not None and self._clock() - stored_at > self.ttl:
            del self._items[key]
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return dataclasses.replace(verdict, cached=True, usage=None, latency_ms=0.0)

    def put(self, key: str, verdict: Verdict) -> None:
        """Store ``verdict`` unless it is an ``error`` or a fallback (see ``is_fallback``)."""
        if is_fallback(verdict):
            return
        self._items[key] = (self._clock(), verdict)
        self._items.move_to_end(key)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry."""
        self._items.clear()

    def pin(self, obj: object) -> None:
        """Keep ``obj`` alive as long as this cache, so its ``id()`` stays unique here."""
        self._pinned[id(obj)] = obj


class Cached:
    """Serve repeated identical checks from a ``VerdictCache``.

    The key covers the member's identity (``check_identity``: its ``cache_identity``, or an
    object identity pinned in the cache), the stage, role, tool name, text and context.
    """

    def __init__(self, check: Check, cache: VerdictCache, name: str | None = None) -> None:
        _validate_members("cached", (check,))
        self.check = check
        self.cache = cache
        self.name = name or check.name
        self.stages = frozenset(check.stages)
        self.deterministic = bool(getattr(check, "deterministic", False))
        self._identity = check_identity(check)
        if "object:" in self._identity:
            cache.pin(check)  # its id (or a member's) is part of the key

    @property
    def cache_identity(self) -> str:
        """Identity of the wrapped check."""
        return self._identity

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        """Return the cached verdict for ``item`` or run the check and store it."""
        key = self.cache.key(
            ctx,
            self.cache_identity,
            item.stage,
            item.role,
            item.tool_name,
            item.text,
            item.context,
        )
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        verdict = await run_check(self.check, ctx, item)
        self.cache.put(key, verdict)
        return verdict


def cached(
    check: Check,
    *,
    maxsize: int = 256,
    ttl: float | None = None,
    cache: VerdictCache | None = None,
    name: str | None = None,
) -> Cached:
    """Wrap ``check`` with a verdict cache (per run by default, process-wide with ``ttl``)."""
    # ``is not None``: an empty VerdictCache is falsy (it defines __len__).
    chosen = cache if cache is not None else VerdictCache(maxsize, ttl=ttl)
    return Cached(check, chosen, name=name)


# --------------------------------------------------------------------------- tiered

Escalate = str  # "always" | "on_signal" | "sample:<p>"


class Tiered:
    """Run checks in order; the first ``block``, ``error`` or ``review`` stops the chain.

    A ``rewrite`` passes its replacement to the later tiers and the result is a rewrite
    unless a later tier stops. ``escalate`` decides whether a later tier runs when the
    earlier ones allowed:

    - ``"always"`` (default): every tier runs;
    - ``"on_signal"``: later tiers run only when an earlier tier saw a signal (a score
      above 0, for example an injection signal below the threshold, or a rewrite). Cheap,
      but clean-looking paraphrases never reach the model tier;
    - ``"sample:0.1"``: later tiers run for a random 10% of calls (decided once per call).
    """

    def __init__(
        self,
        *checks: Check,
        escalate: Escalate = "always",
        name: str = "tiered",
        rng: random.Random | None = None,
    ) -> None:
        _validate_members("tiered", checks)
        self._sample: float | None = None
        if escalate.startswith("sample:"):
            try:
                rate = float(escalate.removeprefix("sample:"))
            except ValueError:
                rate = -1.0
            if not 0.0 <= rate <= 1.0:
                raise ValueError("escalate='sample:<p>' needs 0 <= p <= 1.")
            self._sample = rate
        elif escalate not in ("always", "on_signal"):
            raise ValueError("escalate must be 'always', 'on_signal' or 'sample:<p>'.")
        self.checks = checks
        self.escalate = escalate
        self.name = name
        self.stages = _common_stages(checks)
        self.deterministic = all(getattr(c, "deterministic", False) for c in checks)
        self._rng = rng if rng is not None else random.Random()

    @property
    def cache_identity(self) -> str:
        """Identity of the chain (its members in order)."""
        members = ",".join(check_identity(c) for c in self.checks)
        return f"tiered[{self.escalate}]({members})"

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        """Run the tiers and return the deciding verdict."""
        started = time.perf_counter()
        usage: Usage | None = None
        trace: list[dict[str, Any]] = []
        current = item
        rewritten = False
        signal = False
        sampled: bool | None = None
        last: Verdict | None = None
        scores: list[float] = []
        for index, check in enumerate(self.checks):
            if index > 0:
                if self.escalate == "on_signal" and not signal:
                    break
                if self._sample is not None:
                    if sampled is None:
                        sampled = self._rng.random() < self._sample
                    if not sampled:
                        break
            verdict = await run_check(check, ctx, current)
            usage = _add_usage(usage, verdict)
            trace.append(_trace(verdict))
            last = verdict
            if verdict.score is not None:
                scores.append(verdict.score)
            if verdict.decision in _STOP:
                return dataclasses.replace(
                    verdict,
                    usage=usage,
                    cached=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    evidence={**verdict.evidence, "composite": self.name, "tiers": trace},
                )
            if verdict.decision == "rewrite" and verdict.replacement is not None:
                current = dataclasses.replace(current, text=verdict.replacement)
                rewritten = True
                signal = True
            if verdict.score is not None and verdict.score > 0:
                signal = True
        evidence: dict[str, Any] = {"composite": self.name, "tiers": trace}
        elapsed = (time.perf_counter() - started) * 1000
        if rewritten:
            return Verdict(
                decision="rewrite",
                replacement=current.text,
                categories=last.categories if last else (),
                reason="rewritten by an earlier tier",
                check=self.name,
                backend="tiered",
                latency_ms=elapsed,
                usage=usage,
                evidence=evidence,
            )
        return Verdict(
            decision="allow",
            score=max(scores) if scores else None,
            check=self.name,
            backend="tiered",
            latency_ms=elapsed,
            usage=usage,
            evidence=evidence,
        )


def tiered(
    *checks: Check,
    escalate: Escalate = "always",
    name: str = "tiered",
    rng: random.Random | None = None,
) -> Tiered:
    """Run ``checks`` in order and stop at the first block, error or review."""
    return Tiered(*checks, escalate=escalate, name=name, rng=rng)


# --------------------------------------------------------------------------- parallel


class Parallel:
    """Run checks concurrently.

    A member's hard error (``is_hard_error``, for example a judge's ``ContentTooLong``) wins
    over every other outcome as soon as it arrives, whatever the member order, the mode or
    the deadline, and the rest are cancelled.

    ``mode="any_block"`` (default): the first ``block`` wins and cancels the rest; otherwise
    any ``error`` gives ``error``, any ``review`` gives ``review``. ``mode="majority"``:
    ``block`` when more than half block; otherwise errors and reviews as above, and a
    **minority block is overridden**: the result is ``allow`` with
    ``evidence["minority_block"] = True`` and the blocking members in
    ``evidence["minority_blocks"]``. ``timeout`` (seconds) bounds the whole group; members
    still running at the deadline count as ``error``. Members cannot rewrite (two rewrites
    cannot be merged): a rewrite counts as ``error``. Usage of cancelled members is not
    reported back (their budget reservations are still settled by the model layer).
    """

    def __init__(
        self,
        *checks: Check,
        mode: Literal["any_block", "majority"] = "any_block",
        timeout: float | None = None,
        name: str = "parallel",
    ) -> None:
        _validate_members("parallel", checks)
        if mode not in ("any_block", "majority"):
            raise ValueError("mode must be 'any_block' or 'majority'.")
        if timeout is not None and (isinstance(timeout, bool) or timeout <= 0):
            raise ValueError("timeout must be a positive number of seconds or None.")
        self.checks = checks
        self.mode = mode
        self.timeout = timeout
        self.name = name
        self.stages = _common_stages(checks)
        self.deterministic = all(getattr(c, "deterministic", False) for c in checks)

    @property
    def cache_identity(self) -> str:
        """Identity of the group."""
        members = ",".join(check_identity(c) for c in self.checks)
        return f"parallel[{self.mode}]({members})"

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        """Run all members and combine their verdicts."""
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        deadline = None if self.timeout is None else loop.time() + self.timeout
        tasks = {asyncio.ensure_future(run_check(c, ctx, item)): c for c in self.checks}
        done_verdicts: dict[asyncio.Future[Verdict], Verdict] = {}
        pending = set(tasks)
        first_block: Verdict | None = None
        hard: Verdict | None = None
        try:
            while pending:
                remaining = None if deadline is None else max(0.0, deadline - loop.time())
                if remaining == 0.0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break
                for task in done:
                    verdict = task.result()
                    done_verdicts[task] = verdict
                    if hard is None and is_hard_error(verdict):
                        hard = verdict
                    if (
                        self.mode == "any_block"
                        and verdict.decision == "block"
                        and first_block is None
                    ):
                        first_block = verdict
                if first_block is not None or hard is not None:
                    break
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        verdicts = [done_verdicts[t] for t in tasks if t in done_verdicts]
        timed_out = [tasks[t].name for t in tasks if t not in done_verdicts]
        usage: Usage | None = None
        for verdict in verdicts:
            usage = _add_usage(usage, verdict)
        evidence: dict[str, Any] = {
            "composite": self.name,
            "members": [_trace(v) for v in verdicts],
        }
        elapsed = (time.perf_counter() - started) * 1000

        def result(base: Verdict) -> Verdict:
            return dataclasses.replace(
                base,
                usage=usage,
                cached=False,
                latency_ms=elapsed,
                evidence={**base.evidence, **evidence},
            )

        if hard is not None:
            # A hard error (content too long to judge) wins over every other outcome,
            # whatever the member order or the deadline, so no on_error can soften it.
            return result(hard)
        if first_block is not None:
            return result(first_block)
        blocks = [v for v in verdicts if v.decision == "block"]
        if self.mode == "majority" and len(blocks) * 2 > len(self.checks):
            categories = tuple(dict.fromkeys(c for v in blocks for c in v.categories))
            return result(
                dataclasses.replace(blocks[0], categories=categories, reason="majority blocked")
            )
        if timed_out and first_block is None:
            evidence["timed_out"] = timed_out
            return result(
                Verdict(
                    decision="error",
                    reason=f"{len(timed_out)} check(s) missed the deadline",
                    check=self.name,
                    backend="parallel",
                    evidence={"error_type": "TimeoutError"},
                )
            )
        for verdict in verdicts:
            if verdict.decision == "error":
                return result(verdict)
        for verdict in verdicts:
            if verdict.decision == "rewrite":
                return result(
                    Verdict(
                        decision="error",
                        reason="parallel checks cannot merge rewrites; use tiered()",
                        check=verdict.check,
                        backend="parallel",
                        evidence={"error_type": "RewriteInParallel"},
                    )
                )
        for verdict in verdicts:
            if verdict.decision == "review":
                return result(verdict)
        if blocks:  # majority mode, minority blocked: overridden, but visible to audits
            evidence["minority_block"] = True
            evidence["minority_blocks"] = [v.check for v in blocks]
        scores = [v.score for v in verdicts if v.score is not None]
        return result(
            Verdict(
                decision="allow",
                score=max(scores) if scores else None,
                check=self.name,
                backend="parallel",
            )
        )


def parallel(
    *checks: Check,
    mode: Literal["any_block", "majority"] = "any_block",
    timeout: float | None = None,
    name: str = "parallel",
) -> Parallel:
    """Run ``checks`` together; see ``Parallel`` for how verdicts combine."""
    return Parallel(*checks, mode=mode, timeout=timeout, name=name)
