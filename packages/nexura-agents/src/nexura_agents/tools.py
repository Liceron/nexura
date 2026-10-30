# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed tool definition, schema generation, argument validation, and invocation.

Supports both synchronous and asynchronous functions. Note that cancelling a
sync function executing in a thread cannot undo external effects or forcibly
stop that thread. Exactly-once execution is not guaranteed, and side-effectful
tools are never automatically retried.
"""

from __future__ import annotations

import copy
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_origin, get_type_hints, overload

from pydantic import BaseModel, create_model

from nexura_agents._lazy import LazyModule
from nexura_agents.errors import (
    SDKValidationError,
    ToolError,
    ToolExecutionError,
    ToolResultSerializationError,
    ToolValidationError,
)

if TYPE_CHECKING:
    import asyncio
else:
    asyncio = LazyModule("asyncio")

if TYPE_CHECKING:
    from nexura_agents.contracts.execution import EffectClass
    from nexura_agents.guardrails import GuardrailFn
    from nexura_agents.run_context import RunContext

ApprovalRule = bool | Callable[["RunContext[Any]", dict[str, Any]], bool]
# The recovery promise of a tool after a crash (Execution Contract v1, docs/durable-execution.md).
EffectName = Literal["pure", "idempotent", "non_idempotent", "reconcilable"]
# reconcile(ctx, intent) -> Applied(result) | NotApplied() | Unknown(), sync or async.
ReconcileFn = Callable[["RunContext[Any]", Any], Any]
# True/False, or a function of the RunContext (sync or async) checked before each model call.
EnabledRule = bool | Callable[["RunContext[Any]"], bool | Awaitable[bool]]


OnTimeout = Literal["tell_model", "fail"]


@dataclass(frozen=True)
class ToolInfo:
    """What a hook, guardrail or policy rule may know about a tool: ``ctx.tool``.

    ``origin`` says where the tool comes from: ``python:<module>.<qualname>`` for a
    ``@tool`` function, ``mcp:<server>:<tool>`` and ``openapi:<METHOD>:<path>`` for MCP and
    OpenAPI tools, ``module:<module_id>`` for other module tools, ``agent:<name>`` for
    ``Agent.as_tool``, ``builtin:<name>`` for the SDK's own tools, or ``None`` when unknown.
    ``effect`` is the tool's effect class (``pure``, ``idempotent``, ``non_idempotent`` or
    ``reconcilable``). ``requires_approval`` is ``True`` when the tool declares an approval
    flag or rule (a rule may still say no for a given call). ``tags`` are labels such as
    ``toolset:<name>`` (MCP server or OpenAPI toolset) or your own ``@tool(tags=...)``.
    """

    name: str
    origin: str | None = None
    effect: str = "non_idempotent"
    requires_approval: bool = False
    tags: frozenset[str] = frozenset()

    @property
    def toolsets(self) -> frozenset[str]:
        """The names in this tool's ``toolset:<name>`` tags."""
        return frozenset(t[len("toolset:") :] for t in self.tags if t.startswith("toolset:"))


def _check_origin(name: str, origin: Any) -> str | None:
    if origin is not None and (not isinstance(origin, str) or not origin):
        raise SDKValidationError(f"Tool '{name}': origin must be a non-empty string or None.")
    return origin


def _check_tags(name: str, tags: Any) -> frozenset[str]:
    if isinstance(tags, (str, bytes)):
        raise SDKValidationError(f"Tool '{name}': tags must be a list of strings, not a string.")
    items = frozenset(tags)
    if not all(isinstance(t, str) and t for t in items):
        raise SDKValidationError(f"Tool '{name}': every tag must be a non-empty string.")
    return items


def python_origin(fn: Any) -> str | None:
    """``python:<module>.<qualname>`` for a function, or ``None`` when it has no name."""
    module = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", None)
    if not isinstance(qualname, str) or not qualname:
        return None
    if isinstance(module, str) and module:
        return f"python:{module}.{qualname}"
    return f"python:{qualname}"


class ToolRetry:
    """Retry a tool automatically when it fails with a transient error.

    ``@tool(idempotent=True, retry=ToolRetry(max_attempts=3))``: the call is tried up to
    ``max_attempts`` times in total, waiting ``initial_delay`` seconds, then
    ``initial_delay * backoff``, ... (at most ``max_delay``) between attempts. Only
    exceptions of the ``retry_on`` types are retried (default: connection and timeout
    errors, which includes the tool's own ``timeout``). The model sees only the final
    outcome. Requires ``idempotent=True``, because a failed attempt may have had effects.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        *,
        initial_delay: float = 0.5,
        backoff: float = 2.0,
        max_delay: float = 8.0,
        retry_on: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError),
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise SDKValidationError(f"max_attempts must be an integer >= 1, got {max_attempts!r}.")
        for name, value in (("initial_delay", initial_delay), ("max_delay", max_delay)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise SDKValidationError(f"{name} must be a number >= 0, got {value!r}.")
        if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or backoff < 1:
            raise SDKValidationError(f"backoff must be a number >= 1, got {backoff!r}.")
        if not isinstance(retry_on, tuple) or not all(
            isinstance(t, type) and issubclass(t, BaseException) for t in retry_on
        ):
            raise SDKValidationError("retry_on must be a tuple of exception types.")
        self.max_attempts = max_attempts
        self.initial_delay = float(initial_delay)
        self.backoff = float(backoff)
        self.max_delay = float(max_delay)
        self.retry_on = retry_on

    def delay(self, attempt: int) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        return min(self.max_delay, self.initial_delay * self.backoff ** (attempt - 1))

    def __repr__(self) -> str:
        return f"ToolRetry(max_attempts={self.max_attempts}, initial_delay={self.initial_delay})"


def _check_timeout(name: str, timeout: Any, on_timeout: Any) -> tuple[float | None, str]:
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise SDKValidationError(
            f"Tool '{name}': timeout must be a positive number of seconds, got {timeout!r}."
        )
    if on_timeout not in ("tell_model", "fail"):
        raise SDKValidationError(
            f"Tool '{name}': on_timeout must be 'tell_model' or 'fail', got {on_timeout!r}."
        )
    return (None if timeout is None else float(timeout)), on_timeout


def _check_effect(
    name: str, effect: Any, idempotent: Any, reconcile: Any
) -> tuple[EffectClass, ReconcileFn | None]:
    """Resolve ``effect=`` and its ``idempotent=True`` alias; undeclared is non_idempotent."""
    from nexura_agents.contracts.execution import EffectClass

    if not isinstance(idempotent, bool):
        raise SDKValidationError(f"Tool '{name}': idempotent must be True or False.")
    if effect is None:
        effect_class = EffectClass.IDEMPOTENT if idempotent else EffectClass.NON_IDEMPOTENT
    else:
        try:
            effect_class = EffectClass(effect)
        except ValueError:
            raise SDKValidationError(
                f"Tool '{name}': effect must be 'pure', 'idempotent', 'non_idempotent' or "
                f"'reconcilable', got {effect!r}."
            ) from None
        if idempotent and effect_class is not EffectClass.IDEMPOTENT:
            raise SDKValidationError(
                f"Tool '{name}': idempotent=True is an alias of effect='idempotent'; "
                f"it cannot be combined with effect={effect_class.value!r}."
            )
    if effect_class is EffectClass.RECONCILABLE:
        if not callable(reconcile):
            raise SDKValidationError(
                f"Tool '{name}': effect='reconcilable' needs reconcile=fn(ctx, intent) returning "
                "Applied(result), NotApplied() or Unknown()."
            )
    elif reconcile is not None:
        raise SDKValidationError(
            f"Tool '{name}': reconcile= is only used with effect='reconcilable'."
        )
    return effect_class, reconcile


def _check_retry_and_cache(
    name: str, retry: Any, cache: Any, idempotent: bool
) -> tuple[ToolRetry | None, bool]:
    if retry is not None and not isinstance(retry, ToolRetry):
        raise SDKValidationError(f"Tool '{name}': retry must be a ToolRetry, got {retry!r}.")
    if not isinstance(cache, bool):
        raise SDKValidationError(f"Tool '{name}': cache must be True or False, got {cache!r}.")
    if (retry is not None or cache) and not idempotent:
        raise SDKValidationError(
            f"Tool '{name}': retry and cache run a call again or skip it, so they need "
            "idempotent=True or effect='pure'/'idempotent' (safe to repeat)."
        )
    return retry, cache


def _check_guardrails(name: str, guardrails: Any) -> tuple[GuardrailFn, ...]:
    if callable(guardrails) or isinstance(guardrails, (str, bytes)):
        raise SDKValidationError(f"Tool '{name}': guardrails must be a list of functions.")
    checks = tuple(guardrails)
    if not all(callable(g) for g in checks):
        raise SDKValidationError(f"Tool '{name}': every guardrail must be a function.")
    return checks


def _check_enabled_rule(name: str, enabled: Any) -> Any:
    if not isinstance(enabled, bool) and not callable(enabled):
        raise SDKValidationError(
            f"Tool '{name}': enabled must be a bool or a function of the RunContext."
        )
    return enabled


def _context_param(fn: Callable[..., Any]) -> str | None:
    """Name of the first parameter annotated ``RunContext[...]``, if any."""
    from nexura_agents.run_context import RunContext

    try:
        hints = get_type_hints(fn)
    except Exception:
        return None
    for name in inspect.signature(fn).parameters:
        hint = hints.get(name)
        if hint is RunContext or get_origin(hint) is RunContext:
            return name
    return None


def _build_args_model(
    fn: Callable[..., Any], tool_name: str, skip: str | None = None
) -> type[BaseModel]:
    """Inspect function signature and build a Pydantic model for parameter validation."""
    sig = inspect.signature(fn)
    try:
        type_hints = get_type_hints(fn)
    except Exception:
        type_hints = {}

    fields: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls") or param_name == skip:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        param_type = type_hints.get(param_name, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default

        fields[param_name] = (param_type, default)

    model_name = f"{tool_name}_Args"
    return create_model(model_name, **fields)


class ToolOutput:
    """A tool result with files: ``return ToolOutput("Here is the chart.", [chart])``.

    A tool can also return an ``Attachment`` or a list holding attachments and text. The
    text becomes the tool result; the files are shown to the model in the next message (see
    ``docs/multimodal.md``).
    """

    def __init__(self, text: str = "", attachments: Sequence[Any] = ()) -> None:
        from nexura_agents.attachments import Attachment

        if not isinstance(text, str):
            raise SDKValidationError("ToolOutput text must be a string.")
        items = list(attachments)
        if not all(isinstance(a, Attachment) for a in items):
            raise SDKValidationError("ToolOutput attachments must be Attachment objects.")
        self.text = text
        self.attachments = items

    def __repr__(self) -> str:
        return f"ToolOutput(text={self.text!r}, attachments={len(self.attachments)})"


def _split_files(result: Any) -> tuple[Any, list[Any]]:
    """Separate attachments from a tool's return value: (value for the text result, files)."""
    from nexura_agents.attachments import Attachment

    if isinstance(result, ToolOutput):
        files = list(result.attachments)
        return (result.text or _files_note(files)), files
    if isinstance(result, Attachment):
        return _files_note([result]), [result]
    if isinstance(result, (list, tuple)) and any(isinstance(r, Attachment) for r in result):
        files = [r for r in result if isinstance(r, Attachment)]
        texts = [
            r if isinstance(r, str) else json.dumps(r, default=str)
            for r in result
            if not isinstance(r, Attachment)
        ]
        return ("\n".join(texts) or _files_note(files)), files
    return result, []


def _files_note(files: list[Any]) -> str:
    names = ", ".join(f"{a.name or 'file'} ({a.mime_type})" for a in files)
    return f"Returned {len(files)} file(s): {names}. They are shown in the next message."


class StopAtTools:
    """``Agent(tool_use_behavior=StopAtTools(["name", ...]))``: a successful call to one of
    these tools ends the run, and its result is the final output (no further model call)."""

    def __init__(self, tool_names: Sequence[str]) -> None:
        if isinstance(tool_names, str) or not all(isinstance(n, str) for n in tool_names):
            raise SDKValidationError("StopAtTools needs a list of tool names.")
        if not tool_names:
            raise SDKValidationError("StopAtTools needs at least one tool name.")
        self.tool_names: frozenset[str] = frozenset(tool_names)

    def __repr__(self) -> str:
        return f"StopAtTools({sorted(self.tool_names)!r})"


class Tool:
    """Represents a typed, executable tool for an agent."""

    idempotent: bool = False  # True for effect "pure" or "idempotent": safe to repeat
    effect: EffectClass  # set by __init__; see effect_class() for the default
    reconcile: ReconcileFn | None = None
    enabled: EnabledRule = True
    input_guardrails: tuple[GuardrailFn, ...] = ()
    output_guardrails: tuple[GuardrailFn, ...] = ()
    timeout: float | None = None
    on_timeout: str = "tell_model"
    retry: ToolRetry | None = None
    cache: bool = False
    asks_user: bool = False  # only the built-in Agent(ask_user=True) tool sets this
    # Additional effect target identity, bound with canonical arguments at approval.
    approval_scope: str | None = None
    approval_scope_per_run: bool = False
    # Where the tool comes from and labels for policies (see ToolInfo). Metadata only:
    # neither changes how the tool runs.
    origin: str | None = None
    tags: frozenset[str] = frozenset()

    def __init__(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        description: str | None = None,
        args_schema: type[BaseModel] | None = None,
        requires_approval: ApprovalRule = False,
        max_retries: int | None = None,
        idempotent: bool = False,
        enabled: EnabledRule = True,
        input_guardrails: Sequence[GuardrailFn] = (),
        output_guardrails: Sequence[GuardrailFn] = (),
        timeout: float | None = None,
        on_timeout: OnTimeout = "tell_model",
        retry: ToolRetry | None = None,
        cache: bool = False,
        effect: EffectName | None = None,
        reconcile: ReconcileFn | None = None,
        origin: str | None = None,
        tags: Iterable[str] = (),
    ) -> None:
        self.name = name
        self.fn = fn
        self.origin = _check_origin(name, origin) if origin is not None else python_origin(fn)
        self.tags = _check_tags(name, tags)
        self.effect, self.reconcile = _check_effect(name, effect, idempotent, reconcile)
        idempotent = self.effect.value in ("pure", "idempotent")
        self.timeout, self.on_timeout = _check_timeout(name, timeout, on_timeout)
        self.retry, self.cache = _check_retry_and_cache(name, retry, cache, idempotent)
        # False, or a predicate returning False: not offered to the model this step.
        self.enabled = _check_enabled_rule(name, enabled)
        # Checked around each call: arguments before it runs, the result text after.
        self.input_guardrails = _check_guardrails(name, input_guardrails)
        self.output_guardrails = _check_guardrails(name, output_guardrails)
        # True: safe to run again after a crash (durable runs re-run it on recovery).
        # ``effect`` is the full recovery promise; ``idempotent`` stays as its alias.
        self.idempotent = idempotent
        self.description = description or inspect.getdoc(fn) or f"Execute {name}"
        self.is_async = inspect.iscoroutinefunction(fn)
        self.context_param = _context_param(fn)
        self.args_schema: type[BaseModel] = args_schema or _build_args_model(
            fn, name, skip=self.context_param
        )
        # True, or a predicate over (ctx, arguments): pause the run for a human decision.
        self.requires_approval = requires_approval
        # Retries granted after ModelRetry (default 1). Invalid arguments are sent back
        # to the model only when max_retries is set explicitly; otherwise they raise.
        self.max_retries = max_retries

    def effect_class(self) -> EffectClass:
        """The tool's effect class (``non_idempotent`` when a subclass never set one)."""
        from nexura_agents.contracts.execution import EffectClass

        effect = self.__dict__.get("effect")
        return EffectClass(effect) if effect is not None else EffectClass.NON_IDEMPOTENT

    def info(self) -> ToolInfo:
        """This tool's metadata for hooks, guardrails and policies (``ctx.tool``)."""
        rule = self.requires_approval
        return ToolInfo(
            name=self.name,
            origin=self.origin,
            effect=self.effect_class().value,
            requires_approval=callable(rule) or bool(rule),
            tags=frozenset(self.tags),
        )

    def needs_approval(self, ctx: RunContext[Any], arguments: dict[str, Any]) -> bool:
        """Whether this call needs human approval, from the ``requires_approval`` flag or rule."""
        rule = self.requires_approval
        return bool(rule(ctx, arguments)) if callable(rule) else bool(rule)

    async def is_enabled(self, ctx: RunContext[Any]) -> bool:
        """Whether the model may see and call this tool now (the ``enabled`` flag or rule)."""
        rule = self.enabled
        if not callable(rule):
            return bool(rule)
        result = rule(ctx)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def to_schema(self) -> dict[str, Any]:
        """Generate OpenAI-compatible tool JSON schema."""
        parameters = self.args_schema.model_json_schema()
        # Clean up title from Pydantic schema to keep parameters concise
        parameters.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    def run(self, **kwargs: Any) -> Any:
        """Synchronously execute tool with given keyword arguments."""
        try:
            validated = self.args_schema.model_validate(kwargs)
            fn_kwargs = {
                field_name: getattr(validated, field_name)
                for field_name in self.args_schema.model_fields
            }
        except Exception as exc:
            raise ToolValidationError(
                tool_name=self.name,
                message=str(exc),
                arguments=kwargs,
                original_error=exc,
            ) from exc

        if self.is_async:
            return asyncio.run(self.fn(**fn_kwargs))
        return self.fn(**fn_kwargs)

    async def arun(self, **kwargs: Any) -> Any:
        """Asynchronously execute tool with given keyword arguments."""
        try:
            validated = self.args_schema.model_validate(kwargs)
            fn_kwargs = {
                field_name: getattr(validated, field_name)
                for field_name in self.args_schema.model_fields
            }
        except Exception as exc:
            raise ToolValidationError(
                tool_name=self.name,
                message=str(exc),
                arguments=kwargs,
                original_error=exc,
            ) from exc

        if self.is_async:
            return await self.fn(**fn_kwargs)
        return await asyncio.to_thread(self.fn, **fn_kwargs)

    def dispatch_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The arguments exactly as :meth:`execute` gives them to the function: validated
        and coerced by the tool's schema (``"5000"`` becomes ``5000`` for an ``int``
        parameter), unknown names dropped (as before: a stray key never fails a call), with
        typed values kept in their Python type (a ``Decimal`` stays a ``Decimal``, nested
        models become dicts). Raises ``ToolValidationError`` when they do not validate.

        Policies and hooks check these together with the raw arguments (P39 G6).
        """
        known = {k: v for k, v in arguments.items() if k in self.args_schema.model_fields}
        try:
            validated = self.args_schema.model_validate(known)
            dumped: dict[str, Any] = validated.model_dump(mode="python")
        except Exception as exc:
            raise ToolValidationError(
                tool_name=self.name,
                message=str(exc),
                arguments=dict(arguments),
                original_error=exc,
            ) from exc
        return dumped

    def coerced_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        """:meth:`dispatch_arguments`, or ``None`` when they do not validate."""
        try:
            return self.dispatch_arguments(arguments)
        except ToolValidationError:
            return None

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        """Raise ``ToolValidationError`` if ``arguments`` do not fit this tool's schema.

        Unknown names are refused too: the model's schema would silently drop them.
        """
        unknown = sorted(set(arguments) - set(self.args_schema.model_fields))
        if unknown:
            raise ToolValidationError(
                tool_name=self.name,
                message=f"unknown arguments: {', '.join(unknown)}",
                arguments=arguments,
            )
        try:
            self.args_schema.model_validate(arguments)
        except Exception as exc:
            raise ToolValidationError(
                tool_name=self.name, message=str(exc), arguments=arguments, original_error=exc
            ) from exc

    async def execute(self, arguments: dict[str, Any], ctx: RunContext[Any] | None = None) -> str:
        """Validate arguments, execute the tool, and return JSON-serialized result.

        Raises:
            ToolValidationError: If input arguments fail schema validation.
            ToolExecutionError: If the tool raises an exception during execution.
            ToolResultSerializationError: If the result is not JSON-serializable.
        """
        # 1. Validate arguments against schema
        try:
            validated = self.args_schema.model_validate(arguments)
            # Pass typed values directly (e.g., nested Pydantic models preserved)
            kwargs = {
                field_name: getattr(validated, field_name)
                for field_name in self.args_schema.model_fields
            }
        except Exception as exc:
            raise ToolValidationError(
                tool_name=self.name,
                message=str(exc),
                arguments=arguments,
                original_error=exc,
            ) from exc

        if self.context_param is not None:
            if ctx is None:
                raise ToolValidationError(
                    tool_name=self.name,
                    message="This tool needs a RunContext; run it through a Runner.",
                    arguments=arguments,
                )
            kwargs[self.context_param] = ctx

        # 2. Execute function (sync in worker thread, async awaited)
        try:
            if self.is_async:
                result = await self.fn(**kwargs)
            else:
                result = await asyncio.to_thread(self.fn, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            from nexura_agents.run_context import ModelRetry

            if isinstance(exc, ModelRetry):
                raise
            raise ToolExecutionError(
                tool_name=self.name,
                original_error=exc,
            ) from exc

        # 3. Files go to the run (shown to the model); the rest becomes the text result
        result, files = _split_files(result)
        if files and ctx is not None:
            ctx.tool_attachments.extend(files)
        return self._serialize_result(result)

    def _serialize_result(self, result: Any) -> str:
        """Validate result for JSON serialization and return serialized string."""
        if isinstance(result, BaseModel):
            try:
                return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
            except Exception as exc:
                raise ToolResultSerializationError(
                    tool_name=self.name,
                    original_error=exc,
                ) from exc

        if isinstance(result, str):
            # Verify string itself is valid JSON serializable (valid unicode/characters)
            try:
                json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                raise ToolResultSerializationError(
                    tool_name=self.name,
                    original_error=exc,
                ) from exc
            return result

        try:
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            raise ToolResultSerializationError(
                tool_name=self.name,
                original_error=exc,
            ) from exc

    def __repr__(self) -> str:
        return (
            f"Tool(name={self.name!r}, description={self.description!r}, is_async={self.is_async})"
        )


SchemaHandler = Callable[["RunContext[Any]", dict[str, Any]], Any]


class SchemaTool(Tool):
    """A tool described by a raw JSON Schema instead of a Python signature.

    Used for tools whose arguments are defined elsewhere (MCP servers, OpenAPI specs).
    The handler receives ``(ctx, arguments)`` after the arguments pass the schema.
    """

    def __init__(
        self,
        name: str,
        parameters: dict[str, Any],
        handler: SchemaHandler,
        *,
        description: str = "",
        requires_approval: ApprovalRule = False,
        max_retries: int | None = 1,
        enabled: EnabledRule = True,
        input_guardrails: Sequence[GuardrailFn] = (),
        output_guardrails: Sequence[GuardrailFn] = (),
        timeout: float | None = None,
        on_timeout: OnTimeout = "tell_model",
        retry: ToolRetry | None = None,
        cache: bool = False,
        idempotent: bool = False,
        effect: EffectName | None = None,
        reconcile: ReconcileFn | None = None,
        origin: str | None = None,
        tags: Iterable[str] = (),
    ) -> None:
        self.name = name
        self.fn = handler
        # MCP and OpenAPI tools get theirs from module_tool; otherwise unknown.
        self.origin = _check_origin(name, origin)
        self.tags = _check_tags(name, tags)
        self.effect, self.reconcile = _check_effect(name, effect, idempotent, reconcile)
        idempotent = self.effect.value in ("pure", "idempotent")
        self.idempotent = idempotent
        self.timeout, self.on_timeout = _check_timeout(name, timeout, on_timeout)
        self.retry, self.cache = _check_retry_and_cache(name, retry, cache, idempotent)
        self.enabled = _check_enabled_rule(name, enabled)
        self.input_guardrails = _check_guardrails(name, input_guardrails)
        self.output_guardrails = _check_guardrails(name, output_guardrails)
        self.description = description or f"Execute {name}"
        self.is_async = inspect.iscoroutinefunction(handler)
        self.context_param = None
        self.parameters = _normalize_parameters(parameters)
        self.args_schema = create_model(f"{name}_Args")  # unused; schema comes from parameters
        self.requires_approval = requires_approval
        # Defaults to 1: argument errors go back to the model once so it can fix them.
        self.max_retries = max_retries

    def to_schema(self) -> dict[str, Any]:
        """Return the OpenAI-style function schema built from the stored JSON Schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def dispatch_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The arguments the handler receives: JSON Schema validation does not coerce or
        drop names (extra keys pass unless ``additionalProperties`` is false), so a copy of
        the raw arguments. Raises ``ToolValidationError`` when they do not validate."""
        copied = copy.deepcopy(dict(arguments))
        self.validate_arguments(copied)
        return copied

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        """Raise ``ToolValidationError`` if ``arguments`` do not fit the JSON Schema."""
        from nexura_agents.contracts.errors import ContractError
        from nexura_agents.contracts.schema_validator import validate_schema

        try:
            validate_schema(arguments, self.parameters, path="arguments", schema_kind="input")
        except ContractError as exc:
            raise ToolValidationError(
                tool_name=self.name, message=str(exc), arguments=arguments, original_error=exc
            ) from exc

    async def execute(self, arguments: dict[str, Any], ctx: RunContext[Any] | None = None) -> str:
        """Validate arguments against the schema, run the handler and return its result."""
        from nexura_agents.contracts.errors import ContractError
        from nexura_agents.contracts.schema_validator import validate_schema

        try:
            validate_schema(arguments, self.parameters, path="arguments", schema_kind="input")
        except ContractError as exc:
            raise ToolValidationError(
                tool_name=self.name, message=str(exc), arguments=arguments, original_error=exc
            ) from exc
        if ctx is None:
            raise ToolValidationError(
                tool_name=self.name,
                message="This tool needs a RunContext; run it through a Runner.",
                arguments=arguments,
            )
        try:
            result = self.fn(ctx, arguments)
            if inspect.isawaitable(result):
                result = await result
        except ToolError:
            raise
        except Exception as exc:
            raise ToolExecutionError(tool_name=self.name, original_error=exc) from exc
        result, files = _split_files(result)
        if files:
            ctx.tool_attachments.extend(files)
        return self._serialize_result(result)

    def run(self, **kwargs: Any) -> Any:
        """Always raises ``ToolValidationError``; schema tools run only through a Runner."""
        raise ToolValidationError(
            tool_name=self.name, message="Schema tools run only through a Runner.", arguments=kwargs
        )

    async def arun(self, **kwargs: Any) -> Any:
        """Async counterpart of ``run``; always raises because schema tools need a Runner."""
        return self.run(**kwargs)


def _normalize_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Model APIs require an object schema with ``properties``."""
    schema = dict(parameters or {})
    schema.setdefault("type", "object")
    if schema["type"] == "object":
        schema.setdefault("properties", {})
    return schema


@overload
def tool(fn: Callable[..., Any]) -> Tool: ...


@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    args_schema: type[BaseModel] | None = None,
    requires_approval: ApprovalRule = False,
    max_retries: int | None = None,
    idempotent: bool = False,
    enabled: EnabledRule = True,
    input_guardrails: Sequence[GuardrailFn] = (),
    output_guardrails: Sequence[GuardrailFn] = (),
    timeout: float | None = None,
    on_timeout: OnTimeout = "tell_model",
    retry: ToolRetry | None = None,
    cache: bool = False,
    effect: EffectName | None = None,
    reconcile: ReconcileFn | None = None,
    tags: Iterable[str] = (),
) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    args_schema: type[BaseModel] | None = None,
    requires_approval: ApprovalRule = False,
    max_retries: int | None = None,
    idempotent: bool = False,
    enabled: EnabledRule = True,
    input_guardrails: Sequence[GuardrailFn] = (),
    output_guardrails: Sequence[GuardrailFn] = (),
    timeout: float | None = None,
    on_timeout: OnTimeout = "tell_model",
    retry: ToolRetry | None = None,
    cache: bool = False,
    effect: EffectName | None = None,
    reconcile: ReconcileFn | None = None,
    tags: Iterable[str] = (),
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """Decorator to define a typed Agent tool from a function.

    ``effect`` is what a durable run may do with the call after a crash: ``"pure"`` and
    ``"idempotent"`` are re-run (the latter with the same ``ctx.idempotency_key``),
    ``"non_idempotent"`` (the default, also for undeclared tools) pauses the run, and
    ``"reconcilable"`` runs ``reconcile(ctx, intent)`` to find out. ``idempotent=True``
    is an alias of ``effect="idempotent"``.

    ``tags`` are labels that hooks, guardrails and policy rules can match
    (``ctx.tool.tags``); the tool's ``origin`` is ``python:<module>.<qualname>``.
    """
    if fn is not None:
        return Tool(
            name=name or fn.__name__,
            fn=fn,
            description=description,
            args_schema=args_schema,
            requires_approval=requires_approval,
            max_retries=max_retries,
            idempotent=idempotent,
            enabled=enabled,
            input_guardrails=input_guardrails,
            output_guardrails=output_guardrails,
            timeout=timeout,
            on_timeout=on_timeout,
            retry=retry,
            cache=cache,
            effect=effect,
            reconcile=reconcile,
            tags=tags,
        )

    def decorator(func: Callable[..., Any]) -> Tool:
        return Tool(
            name=name or func.__name__,
            fn=func,
            description=description,
            args_schema=args_schema,
            requires_approval=requires_approval,
            max_retries=max_retries,
            idempotent=idempotent,
            enabled=enabled,
            input_guardrails=input_guardrails,
            output_guardrails=output_guardrails,
            timeout=timeout,
            on_timeout=on_timeout,
            retry=retry,
            cache=cache,
            effect=effect,
            reconcile=reconcile,
            tags=tags,
        )

    return decorator
