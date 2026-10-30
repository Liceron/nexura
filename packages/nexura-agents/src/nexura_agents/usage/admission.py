# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Shared, fail-closed admission for providers and legacy model attempts.

The task-local scope follows SDK-created child tasks and nested runners. It does
not follow detached jobs or arbitrary external model calls.
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from nexura_agents import tracing
from nexura_agents.types import Message, ModelResponse, Usage
from nexura_agents.usage.errors import UsageError

active_budget: ContextVar[Any] = ContextVar("nexura_active_budget", default=None)

#: Internal: called right before a run sends anything (once a model call is admitted under
#: a budget, and before each tool call). A workflow node uses it to mark its step started
#: only when something is about to happen, so a budget refusal of the first call proves
#: that nothing was sent. Raising refuses that call (an admitted reservation is released).
dispatch_gate: ContextVar[Callable[[], None] | None] = ContextVar(
    "nexura_dispatch_gate", default=None
)


def before_dispatch() -> None:
    """Run the :data:`dispatch_gate` of the current context, if any."""
    gate = dispatch_gate.get()
    if gate is not None:
        gate()


def effective_budget(context: Any = None) -> Any:
    """Enforce the active scope together with an explicit context's policy."""
    return combine_budgets(active_budget.get(), getattr(context, "budget", None))


def _budgets(budget: Any) -> list[Any]:
    return (
        list(budget.budgets)
        if isinstance(budget, _CombinedBudget)
        else ([] if budget is None else [budget])
    )


def _descendant(inner: Any, outer: Any) -> bool:
    from nexura_agents.usage.manager import RunBudget

    if inner is outer:
        return True
    if not isinstance(inner, RunBudget) or not isinstance(outer, RunBudget):
        return False
    same_ledger = inner.manager.ledger is outer.manager.ledger
    path = inner.manager.ledger.db_path
    if path and outer.manager.ledger.db_path:
        from pathlib import Path

        same_ledger = Path(path).resolve() == Path(outer.manager.ledger.db_path).resolve()
    return bool(
        same_ledger
        and inner.workspace_id == outer.workspace_id
        and (inner.run_id == outer.run_id or outer.run_id in inner.ancestor_configs)
    )


def combine_budgets(*scopes: Any) -> Any:
    """Keep the deepest hierarchical scopes, and every independent allowance."""
    budgets: list[Any] = []
    for scope in scopes:
        for candidate in _budgets(scope):
            if any(_descendant(held, candidate) for held in budgets):
                continue
            budgets = [held for held in budgets if not _descendant(candidate, held)]
            budgets.append(candidate)
    if not budgets:
        return None
    return budgets[0] if len(budgets) == 1 else _CombinedBudget(tuple(budgets))


class _CombinedBudget:
    """All-or-nothing pre-dispatch reservations against independent budget scopes."""

    def __init__(self, budgets: tuple[Any, ...]) -> None:
        self.budgets = budgets
        self.reservations: dict[str, list[tuple[Any, str]]] = {}

    async def admit_provider_call_async(
        self, model_id: str, *, estimated_tokens: int, metadata: dict[str, Any]
    ) -> str:
        held: list[tuple[Any, str]] = []
        try:
            for budget in self.budgets:
                reservation = await _admit_scope(
                    budget, model_id, estimated_tokens, metadata["method"]
                )
                assert reservation is not None
                held.append((budget, reservation))
        except BaseException:
            for budget, reservation in reversed(held):
                await settle(budget, reservation, model_id, None, failed=True, pre_dispatch=True)
            raise
        reservation_id = str(uuid.uuid4())
        self.reservations[reservation_id] = held
        return reservation_id

    async def settle_provider_call_async(self, reservation: str, **kwargs: Any) -> None:
        for budget, inner in self.reservations[reservation]:
            await settle(
                budget,
                inner,
                kwargs["model_id"],
                kwargs.get("usage"),
                failed=kwargs.get("is_cancelled", False),
                pre_dispatch=kwargs.get("is_pre_dispatch", False),
            )

    def record_turn(self) -> None:
        for budget in self.budgets:
            if callable(getattr(budget, "record_turn", None)):
                budget.record_turn()

    def record_tool_call(self, name: str) -> None:
        for budget in self.budgets:
            if callable(getattr(budget, "record_tool_call", None)):
                budget.record_tool_call(name)


@dataclass
class _AdmittedCall:
    model_id: str
    method: str
    budget: Any
    consumed: bool = False


_admitted_call: ContextVar[_AdmittedCall | None] = ContextVar("nexura_admitted_call", default=None)


def provider_budget(context: Any, model_id: str, method: str) -> Any:
    """Consume one matching wrapper admission; additional provider calls still reserve."""
    budget = effective_budget(context)
    held = _admitted_call.get()
    if (
        held is not None
        and not held.consumed
        and (held.model_id, held.method) == (model_id, method)
    ):
        held_scopes = _budgets(held.budget)
        needed = _budgets(budget)
        if all(
            any(
                _descendant(scope, covered) and _descendant(covered, scope)
                for covered in held_scopes
            )
            for scope in needed
        ):
            held.consumed = True
            return None
    return budget


async def budget_stream(stream: Any, budget: Any) -> AsyncGenerator[Any, None]:
    """Scope each iterator advancement, without leaking context into the consumer."""
    try:
        while True:
            token = active_budget.set(combine_budgets(active_budget.get(), budget))
            try:
                event = await anext(stream)
            except StopAsyncIteration:
                return
            finally:
                active_budget.reset(token)
            yield event
    finally:
        token = active_budget.set(combine_budgets(active_budget.get(), budget))
        try:
            await stream.aclose()
        finally:
            active_budget.reset(token)


async def admit(budget: Any, model_id: str, tokens: int, method: str) -> str | None:
    """Reserve allowance for one model call, then run the :data:`dispatch_gate`."""
    reservation = await _admit_scope(budget, model_id, tokens, method)
    if reservation is not None and dispatch_gate.get() is not None:
        try:
            before_dispatch()
        except BaseException:
            await settle(budget, reservation, model_id, None, failed=True, pre_dispatch=True)
            raise
    return reservation


async def _admit_scope(budget: Any, model_id: str, tokens: int, method: str) -> str | None:
    if budget is None:
        return None
    try:
        reservation = await _admit(budget, model_id, tokens, method)
    except Exception as exc:
        if not isinstance(budget, _CombinedBudget):  # each scope reports its own refusal
            tracing.budget_event(
                "refuse",
                str(model_id),
                method=method,
                estimated_tokens=tokens,
                limit_type=getattr(exc, "limit_type", None),
                error_type=type(exc).__name__,
            )
        raise
    if not isinstance(budget, _CombinedBudget):
        tracing.budget_event(
            "reserve", model_id, method=method, reservation_id=reservation, estimated_tokens=tokens
        )
    return reservation


async def _admit(budget: Any, model_id: str, tokens: int, method: str) -> str:
    if not model_id or isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise UsageError("Budget admission refused: model or token estimate is unavailable.")
    admit_fn = getattr(budget, "admit_provider_call_async", None)
    settle_fn = getattr(budget, "settle_provider_call_async", None)
    if not callable(settle_fn) and not callable(getattr(budget, "settle_provider_call", None)):
        raise UsageError("Budget admission refused: budget cannot settle reservations.")
    kw = {"estimated_tokens": tokens, "metadata": {"provider": model_id, "method": method}}
    if callable(admit_fn):
        reservation = await admit_fn(model_id, **kw)
    elif callable(getattr(budget, "admit_provider_call", None)):
        reservation = budget.admit_provider_call(model_id, **kw)
    else:
        raise UsageError("Budget admission refused: budget cannot reserve model calls.")
    if not isinstance(reservation, str) or not reservation:
        raise UsageError("Budget admission refused: no reservation was granted.")
    return reservation


async def settle(
    budget: Any,
    reservation: str | None,
    model_id: str,
    usage: Usage | None,
    *,
    failed: bool = False,
    pre_dispatch: bool = False,
) -> None:
    if reservation is None:
        return
    kw = dict(usage=usage, model_id=model_id, is_cancelled=failed, is_pre_dispatch=pre_dispatch)
    fn = getattr(budget, "settle_provider_call_async", None)
    if callable(fn):
        await fn(reservation, **kw)
    else:
        budget.settle_provider_call(reservation, **kw)
    if not isinstance(budget, _CombinedBudget):
        tracing.budget_event(
            "settle",
            model_id,
            reservation_id=reservation,
            outcome="released" if failed or pre_dispatch else "settled",
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )


def _admission_model(model: Any) -> Any:
    """Look through the SDK's transparent recorder, while preserving its dispatch hook."""
    from nexura_agents.repro import _RecordingModel

    seen: set[int] = set()
    while isinstance(model, _RecordingModel):
        if id(model) in seen:
            raise UsageError("Budget admission refused: cyclic recording model wrapper.")
        seen.add(id(model))
        model = model._model
    return model


def _managed(model: Any) -> bool:
    from nexura_agents.models.ratelimit import RateLimitedModel
    from nexura_agents.models.resilience import FallbackModel, RetryModel
    from nexura_agents.providers.base import BaseProvider

    return isinstance(
        _admission_model(model), (BaseProvider, RetryModel, FallbackModel, RateLimitedModel)
    )


def _estimate(model: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, int]:
    from nexura_agents.models.fake import FakeModel
    from nexura_agents.providers.types import GenerationRequest

    model = _admission_model(model)
    model_id = getattr(model, "model_id", None) or getattr(model, "model", None)
    model_id = model_id or getattr(model, "model_name", None)
    if not model_id and isinstance(model, FakeModel):
        model_id = "fake-model"
    if not isinstance(model_id, str) or not model_id:
        raise UsageError("Budget admission refused: model needs a model_id/model/model_name.")
    request = args[0] if args else kwargs.get("messages")
    messages: Any
    if isinstance(request, GenerationRequest):
        messages, tools, schema, output = (
            request.messages,
            request.tools,
            request.schema,
            request.max_tokens,
        )
    else:
        messages = request
        tools = args[1] if len(args) > 1 else kwargs.get("tools")
        schema = kwargs.get("response_schema")
        output = getattr(kwargs.get("model_settings"), "max_tokens", None)
    if not isinstance(messages, list) or not all(isinstance(m, Message) for m in messages):
        raise UsageError("Budget admission refused: cannot estimate this model request.")
    # UTF-8 bytes conservatively cover text, tool arguments/schema and non-Latin text.
    # Unbounded output gets a 4096-token admission buffer, not a guaranteed hard ceiling.
    payload = [m.to_dict() for m in messages]
    try:
        size = len(json.dumps([payload, tools, schema], ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise UsageError("Budget admission refused: request is not estimable.") from exc
    return model_id, max(256, size + attachment_surcharge(messages) + (output or 4096))


def attachment_surcharge(messages: Any) -> int:
    """Shared provider/legacy image and document admission buffer."""
    from nexura_agents.providers.multimodal import attachments_in

    return sum(
        1600
        if item.kind == "image"
        else 4000
        if item.size_bytes is None
        else max(1000, item.size_bytes // 4)
        for item in attachments_in(messages)
    )


def _model_kwargs(model: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    # context is an admission input for legacy models, not part of their call signature.
    from nexura_agents.models.resilience import _accepts_run_context

    return (
        kwargs
        if _accepts_run_context(model)
        else {key: value for key, value in kwargs.items() if key != "context"}
    )


async def generate(model: Any, *args: Any, **kwargs: Any) -> ModelResponse:
    """Admit a model attempt, including explicit contexts on legacy adapters."""
    token = active_budget.set(effective_budget(kwargs.get("context")))
    try:
        return await _generate(model, *args, **kwargs)
    finally:
        active_budget.reset(token)


async def _generate(model: Any, *args: Any, **kwargs: Any) -> ModelResponse:
    budget = effective_budget(kwargs.get("context"))
    kwargs = _model_kwargs(model, kwargs)
    if budget is None or _managed(model):
        response = await _invoke_generate(model, args, kwargs)
        return response
    model_id, tokens = _estimate(model, args, kwargs)
    reservation = await admit(budget, model_id, tokens, "generate")
    usage = None
    failed = True
    token = _admitted_call.set(_AdmittedCall(model_id, "generate", budget))
    try:
        response = await _invoke_generate(model, args, kwargs)
        usage = response.usage
        failed = False
        return response
    finally:
        _admitted_call.reset(token)
        await settle(budget, reservation, model_id, usage, failed=failed)


async def _invoke_generate(
    model: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> ModelResponse:
    result = model.generate(*args, **kwargs)
    response: ModelResponse = await result if inspect.isawaitable(result) else result
    return response


async def stream(model: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
    """Admit streaming attempts without leaking task-local policy to consumers."""
    async for delta in budget_stream(
        _stream(model, *args, **kwargs), effective_budget(kwargs.get("context"))
    ):
        yield delta


async def _stream(model: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
    budget = effective_budget(kwargs.get("context"))
    kwargs = _model_kwargs(model, kwargs)
    reservation = None
    model_id = ""
    if budget is not None and not _managed(model):
        model_id, tokens = _estimate(model, args, kwargs)
        reservation = await admit(budget, model_id, tokens, "stream")
    inner = None
    usage = None
    failed = True
    try:
        inner = model.stream(*args, **kwargs)
        if reservation is not None:
            inner = _admitted_stream(inner, _AdmittedCall(model_id, "stream", budget))
        async for delta in inner:
            if getattr(delta, "usage", None) is not None:
                usage = delta.usage
            yield delta
        failed = False
    finally:
        try:
            if inner is not None:
                await inner.aclose()
        finally:
            await settle(budget, reservation, model_id, usage, failed=failed)


async def _admitted_stream(inner: Any, permit: _AdmittedCall) -> AsyncGenerator[Any, None]:
    try:
        while True:
            token = _admitted_call.set(permit)
            try:
                delta = await anext(inner)
            except StopAsyncIteration:
                return
            finally:
                _admitted_call.reset(token)
            yield delta
    finally:
        await inner.aclose()


async def embed(model: Any, texts: list[str], **kwargs: Any) -> Any:
    """Admit embedding adapters, including those used by SDK memory."""
    token = active_budget.set(effective_budget(kwargs.get("context")))
    try:
        return await _embed(model, texts, **kwargs)
    finally:
        active_budget.reset(token)


async def _embed(model: Any, texts: list[str], **kwargs: Any) -> Any:
    from nexura_agents.memory import HashingEmbedder, ProviderEmbedder
    from nexura_agents.providers.base import BaseProvider

    budget = effective_budget(kwargs.get("context"))
    if "context" in kwargs:
        try:
            params = dict(inspect.signature(model.embed).parameters)
        except (TypeError, ValueError):
            params = {}
        if "context" not in params and not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            kwargs = {k: v for k, v in kwargs.items() if k != "context"}
    if budget is None or isinstance(model, (BaseProvider, ProviderEmbedder, HashingEmbedder)):
        return await model.embed(texts, **kwargs)
    model_id = getattr(model, "model_id", None) or getattr(model, "model", None)
    if not isinstance(model_id, str) or not model_id:
        raise UsageError("Budget admission refused: embedding model needs a model_id/model.")
    tokens = max(64, sum(len(t.encode("utf-8")) for t in texts))
    reservation = await admit(budget, model_id, tokens, "embed")
    usage = None
    failed = True
    token = _admitted_call.set(_AdmittedCall(model_id, "embed", budget))
    try:
        result = await model.embed(texts, **kwargs)
        usage = getattr(result, "usage", None)
        failed = False
        return result
    finally:
        _admitted_call.reset(token)
        await settle(budget, reservation, model_id, usage, failed=failed)
