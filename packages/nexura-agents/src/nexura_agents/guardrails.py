# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Input and output guardrails that can stop a run (a "tripwire").

A guardrail is any function ``fn(ctx, value) -> GuardrailResult`` (sync or async).
Input guardrails receive the user's prompt text before the first model call. Output
guardrails receive the final output (the validated ``output_type`` object when one is
set, otherwise the text) before the run completes.

Tool guardrails (``@tool(input_guardrails=..., output_guardrails=...)``) receive a tool
call's arguments before it runs and its result text after; they can also reject a call
(``GuardrailResult.rejected``), which tells the model instead of stopping the run.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nexura_agents.run_context import RunContext


@dataclass(frozen=True)
class GuardrailResult:
    """Outcome of one guardrail check. ``tripwire=True`` stops the run."""

    tripwire: bool
    info: Any = None
    # Output guardrails only: replaces the final text output when set (see ``rewritten``).
    replacement: str | None = None
    # Tool guardrails only: block the call (or its result) and tell the model this instead.
    rejection: str | None = None

    @classmethod
    def passed(cls, info: Any = None) -> GuardrailResult:
        """Build a result that lets the run continue."""
        return cls(tripwire=False, info=info)

    @classmethod
    def tripped(cls, info: Any = None) -> GuardrailResult:
        """Build a result whose tripwire stops the run."""
        return cls(tripwire=True, info=info)

    @classmethod
    def rewritten(cls, replacement: str, info: Any = None) -> GuardrailResult:
        """Build a non-tripping result that replaces the final text output.

        Only output guardrails may rewrite, and only text output (no ``output_type``).
        Later guardrails see the rewritten text. The replacement is what the caller
        receives and what is stored in the session.
        """
        return cls(tripwire=False, info=info, replacement=replacement)

    @classmethod
    def rejected(cls, message: str, info: Any = None) -> GuardrailResult:
        """Tool guardrails only: do not run the call (input) or withhold its result
        (output); the model is told ``message`` and the run continues."""
        return cls(tripwire=False, info=info, rejection=message)


GuardrailFn = Callable[[RunContext[Any], Any], "GuardrailResult | Awaitable[GuardrailResult]"]


async def run_guardrail(
    fn: GuardrailFn, ctx: RunContext[Any], value: Any, *, tool_stage: bool = False
) -> GuardrailResult:
    result = fn(ctx, value)
    if inspect.isawaitable(result):
        result = await result
    name = getattr(fn, "__name__", repr(fn))
    if not isinstance(result, GuardrailResult):
        raise TypeError(f"Guardrail '{name}' must return GuardrailResult, got {type(result)}.")
    if result.rejection is not None and not tool_stage:
        raise TypeError(
            f"Guardrail '{name}' returned rejected(); only tool guardrails can reject a call."
        )
    return result


async def _tool_check(fn: GuardrailFn, ctx: RunContext[Any], value: Any, stage: str) -> Any:
    from nexura_agents.errors import GuardrailTripwireTriggered, ToolError, ToolExecutionError

    name = getattr(fn, "__name__", "guardrail")
    tool_name = ctx.tool_name or "?"
    try:
        outcome = await run_guardrail(fn, ctx, value, tool_stage=True)
    except (GuardrailTripwireTriggered, ToolError):
        raise
    except Exception as exc:
        raise ToolExecutionError(tool_name=tool_name, original_error=exc) from exc
    if stage == "input" and outcome.replacement is not None:
        raise ToolExecutionError(
            tool_name=tool_name,
            original_error=TypeError(
                f"Tool input guardrail '{name}' returned rewritten(); input guardrails can "
                "pass, trip or reject only."
            ),
        )
    if outcome.tripwire:
        raise GuardrailTripwireTriggered(name, f"tool {stage}", outcome.info, tool=ctx.tool_name)
    return name, outcome


async def check_tool_input(
    tool: Any, ctx: RunContext[Any], arguments: dict[str, Any]
) -> str | None:
    """Run a tool's input guardrails on a copy of ``arguments``.

    Returns the text to give the model when a guardrail rejects the call, or None to run
    it. A tripwire raises ``GuardrailTripwireTriggered``; a broken guardrail raises
    ``ToolExecutionError``. Used by the runner and by ``AgentMCPServer``.
    """
    import copy

    for fn in tool.input_guardrails:
        name, outcome = await _tool_check(fn, ctx, copy.deepcopy(arguments), "input")
        if outcome.rejection is not None:
            return f"Tool call blocked by guardrail '{name}': {outcome.rejection}"
    return None


async def check_tool_output(tool: Any, ctx: RunContext[Any], result: Any) -> tuple[Any, bool]:
    """Run a tool's output guardrails on its result: ``(result to use, rejected?)``.

    A rewrite replaces the result (later guardrails see it); a rejection replaces it with
    the blocked notice and stops checking.
    """
    for fn in tool.output_guardrails:
        name, outcome = await _tool_check(fn, ctx, result, "output")
        if outcome.rejection is not None:
            return f"Tool result blocked by guardrail '{name}': {outcome.rejection}", True
        if outcome.replacement is not None:
            result = outcome.replacement
    return result, False


_BUILTIN_NAMES = frozenset(
    {
        "PIIFinding",
        "PII_KINDS",
        "allowed_topics",
        "block_pii",
        "detect_pii",
        "json_output_only",
        "max_length",
        "no_prompt_injection",
        "redact_output_pii",
        "redact_pii",
    }
)


def __getattr__(name: str) -> Any:
    # Lazy re-export of the opt-in built-in guardrails (keeps ``import nexura_agents`` light).
    if name in _BUILTIN_NAMES:
        import importlib

        return getattr(importlib.import_module("nexura_agents.guardrails_builtin"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
