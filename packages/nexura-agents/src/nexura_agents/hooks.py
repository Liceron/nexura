# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Lifecycle hooks: subclass ``AgentHooks`` and override what you need.

``Runner(hooks=[a, b])`` composes several hooks objects with :class:`ComposedHooks`; see its
docstring for the order in which they run and how their return values combine.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
from collections.abc import Iterator, Mapping, Sequence, Set
from typing import TYPE_CHECKING, Any, TypeVar

from nexura_agents.errors import SDKValidationError
from nexura_agents.run_context import RunContext
from nexura_agents.types import Message, ModelResponse, ToolCall

if TYPE_CHECKING:
    from nexura_agents.agent import Agent
    from nexura_agents.tools import Tool

__all__ = ["AgentHooks", "ComposedHooks"]


class AgentHooks:
    """Async callbacks around model and tool calls.

    Hooks observe by default. ``before_tool`` may raise ``ModelRetry`` to refuse a call
    and tell the model why, or any other exception to fail the run.

    The model and tool hooks may also take over by returning a value (``None`` changes
    nothing):

    - ``before_model`` returns a ``ModelResponse`` to skip the model call (a cache hit, a
      policy refusal). Nothing is billed: requests and usage are not counted.
    - ``after_model`` returns a ``ModelResponse`` to replace the reply. Usage still counts
      what the provider billed.
    - ``before_tool`` returns a string to skip the tool and use it as the result. The
      result is not cached; ``after_tool`` and output guardrails still run.
    - ``after_tool`` returns a string to replace the result. Files the tool returned are
      then not sent, as when a guardrail rewrites a result.

    ``tool_needs_approval`` is the one synchronous hook: return ``True`` to pause a tool
    call for human approval. It can add an approval gate, never remove one.
    """

    async def on_run_start(self, ctx: RunContext[Any]) -> None:
        """Called once when a run starts; does nothing by default."""
        return None

    async def before_model(
        self, ctx: RunContext[Any], messages: list[Message]
    ) -> ModelResponse | None:
        """Called before each model request with the outgoing messages.

        Return a ``ModelResponse`` to use it instead of calling the model.
        """
        return None

    async def after_model(
        self, ctx: RunContext[Any], response: ModelResponse
    ) -> ModelResponse | None:
        """Called after each model response. Return a ``ModelResponse`` to replace it."""
        return None

    async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> str | None:
        """Called before a tool runs; may raise to refuse the call.

        Return a string to skip the tool and use the string as its result.
        """
        return None

    async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> str | None:
        """Called after a tool returns its result. Return a string to replace the result."""
        return None

    def tool_needs_approval(
        self, ctx: RunContext[Any], tool: Tool, arguments: Mapping[str, Any]
    ) -> bool | None:
        """Sync. Return ``True`` to require human approval for this call.

        Asked only when the tool's own ``requires_approval`` does not already require it,
        so a hook can add an approval gate but never remove one: ``False`` and ``None``
        both mean "no extra gate". ``tool`` and ``arguments`` are copies: changing them
        changes neither the call nor the tool. The answer must be
        deterministic (depend only on the tool, its metadata and the arguments): it is
        asked again on resume and on durable recovery, like ``Tool.requires_approval``.
        """
        return None

    def input_guardrails(self, ctx: RunContext[Any], agent: Agent) -> Sequence[Any]:
        """Sync. Input guardrails these hooks add to ``agent``'s own (P39 G6).

        They run once per run, after the agent's own input guardrails and before any model
        call, on the user's latest message, exactly like ``Agent(input_guardrails=...)``
        (same timeout, cancellation, ``GUARDRAIL_DECISION`` events and tripwire). A policy
        installed with ``set_default_hooks`` uses this to check every run's input. They do
        not run again on resume or recovery. Default: none.
        """
        return ()

    def output_guardrails(self, ctx: RunContext[Any], agent: Agent) -> Sequence[Any]:
        """Sync. Output guardrails these hooks add to ``agent``'s own (P39 G4).

        A policy installed with ``set_default_hooks`` or inherited from an enclosing run
        uses this to check every final answer. The guardrails run after the agent's own,
        in order, exactly like ``Agent(output_guardrails=...)``, and they count for
        streaming: with them the reply is held (or checked in windows) before it is shown,
        and ``Agent(stream_guard="passthrough")`` cannot opt them out. Return the same
        guardrails for the same agent every time (it is asked per model turn). Default: none.
        """
        return ()

    async def on_run_end(self, ctx: RunContext[Any], output: Any) -> None:
        """Called when the run finishes with its output; does nothing by default."""
        return None

    async def on_agent_start(self, ctx: RunContext[Any], agent: Agent) -> None:
        """Called when an agent becomes active: at the start of a run, and for the target
        of each handoff. Does nothing by default."""
        return None

    async def on_agent_end(self, ctx: RunContext[Any], agent: Agent, output: Any) -> None:
        """Called when an agent stops being active: with the final output when the run
        ends with it, or ``None`` when it hands off. Does nothing by default."""
        return None

    async def on_handoff(self, ctx: RunContext[Any], source: Agent, target: Agent) -> None:
        """Called when control moves from ``source`` to ``target``; does nothing by default."""
        return None


def _member_name(member: Any) -> str:
    return type(member).__name__


_T = TypeVar("_T")

#: The hook method names a hooks object may define.
HOOK_NAMES = (
    "on_run_start",
    "before_model",
    "after_model",
    "before_tool",
    "after_tool",
    "tool_needs_approval",
    "on_run_end",
    "on_agent_start",
    "on_agent_end",
    "on_handoff",
    "input_guardrails",
    "output_guardrails",
)


def _checked(  # noqa: UP047 - keeps Python 3.11 support
    member: Any, hook: str, value: Any, expected: type[_T]
) -> _T | None:
    """A member's return value: ``None`` or ``expected``; anything else names the member."""
    if value is None or isinstance(value, expected):
        return value
    raise SDKValidationError(
        f"AgentHooks.{hook} of {_member_name(member)} must return None or a "
        f"{expected.__name__}, not {type(value).__name__}."
    )


def is_hooks_object(item: Any) -> bool:
    """True for a hooks *instance*: not None, a string, a collection, an iterator or a class,
    and with at least one ``AgentHooks`` method (members may leave the others out)."""
    if (
        item is None
        or isinstance(item, (type, str, bytes, Sequence, Mapping, Set, Iterator))
        or inspect.isgenerator(item)
    ):
        return False
    return any(callable(getattr(item, name, None)) for name in HOOK_NAMES)


class ComposedHooks(AgentHooks):
    """Several hooks objects run as one: ``Runner(hooks=[policy, MyHooks()])``.

    ``Runner`` builds this for a list; a single hooks object is used as it is. The order:

    - ``on_run_start``, ``on_agent_start`` and ``on_handoff``: every member, in list order.
    - ``before_model``: list order. The first member that returns a ``ModelResponse`` wins:
      the model is not called and later members' ``before_model`` is skipped.
    - ``after_model``: **reverse** list order, chained. Each member gets the current
      response (the original, or the latest replacement); the result is the last
      replacement, or ``None`` when no member replaced it.
    - ``before_tool``: list order. The first member that returns a string wins: the tool
      does not run and later members' ``before_tool`` is skipped (``after_tool`` and the
      tool's output guardrails still run, as with a single hooks object).
    - ``after_tool``: reverse list order, chained like ``after_model``.
    - ``tool_needs_approval``: list order; any ``True`` requires approval (later members
      are not asked). No member can remove a gate the tool itself declares.
    - ``on_agent_end`` and ``on_run_end``: every member, in reverse list order.
    - ``output_guardrails``: every member's guardrails, concatenated in list order.

    So the first member is the outermost: its ``before_*`` hooks take over before the
    others and its ``after_*`` hooks see the final, replaced value. Put a policy first.

    The first exception a member raises propagates at once and the remaining members are
    not called, exactly as an exception from a single hooks object fails the run
    (``ModelRetry`` from ``before_tool`` keeps its meaning). A member's invalid return
    value raises ``SDKValidationError`` naming the member's class. Members may omit
    methods (objects that do not subclass ``AgentHooks``); a missing method is skipped.
    A nested plain ``ComposedHooks`` is flattened into the member list; a subclass of
    ``ComposedHooks`` is kept as one member, so its own overrides still run. Members are
    not deduplicated. A member with none of the ``AgentHooks`` methods (for example a list
    passed as one member) raises ``SDKValidationError``.
    """

    def __init__(self, *hooks: Any) -> None:
        members: list[Any] = []
        for item in hooks:
            if not is_hooks_object(item):
                # Fail closed: a member with no hook methods (a list passed as one member,
                # a stray value) would silently do nothing.
                raise SDKValidationError(
                    "ComposedHooks members must be hooks objects with at least one AgentHooks "
                    f"method, got {type(item).__name__}. Pass hooks as separate arguments: "
                    "ComposedHooks(a, b), or Runner(hooks=[a, b])."
                )
            if type(item) is ComposedHooks:
                # Only a plain composition is flattened; a subclass keeps its own overrides.
                members.extend(item.members)
            else:
                members.append(item)
        self._members: tuple[Any, ...] = tuple(members)

    @property
    def members(self) -> tuple[AgentHooks, ...]:
        """The member hooks objects, in the order given (nested compositions flattened)."""
        return self._members

    def __repr__(self) -> str:
        return f"ComposedHooks({', '.join(_member_name(m) for m in self._members)})"

    async def _each(self, name: str, members: Any, *args: Any) -> None:
        for member in members:
            method = getattr(member, name, None)
            if method is not None:
                await method(*args)

    async def on_run_start(self, ctx: RunContext[Any]) -> None:
        """Every member's ``on_run_start``, in list order."""
        await self._each("on_run_start", self._members, ctx)

    async def on_agent_start(self, ctx: RunContext[Any], agent: Agent) -> None:
        """Every member's ``on_agent_start``, in list order."""
        await self._each("on_agent_start", self._members, ctx, agent)

    async def on_handoff(self, ctx: RunContext[Any], source: Agent, target: Agent) -> None:
        """Every member's ``on_handoff``, in list order."""
        await self._each("on_handoff", self._members, ctx, source, target)

    async def on_agent_end(self, ctx: RunContext[Any], agent: Agent, output: Any) -> None:
        """Every member's ``on_agent_end``, in reverse list order."""
        await self._each("on_agent_end", reversed(self._members), ctx, agent, output)

    async def on_run_end(self, ctx: RunContext[Any], output: Any) -> None:
        """Every member's ``on_run_end``, in reverse list order."""
        await self._each("on_run_end", reversed(self._members), ctx, output)

    async def before_model(
        self, ctx: RunContext[Any], messages: list[Message]
    ) -> ModelResponse | None:
        """The first member's ``ModelResponse`` (list order); later members are skipped."""
        for member in self._members:
            method = getattr(member, "before_model", None)
            if method is None:
                continue
            answer = _checked(member, "before_model", await method(ctx, messages), ModelResponse)
            if answer is not None:
                return answer
        return None

    async def after_model(
        self, ctx: RunContext[Any], response: ModelResponse
    ) -> ModelResponse | None:
        """Members' ``after_model`` in reverse list order, each on the current response."""
        current, replaced = response, False
        for member in reversed(self._members):
            method = getattr(member, "after_model", None)
            if method is None:
                continue
            value = _checked(member, "after_model", await method(ctx, current), ModelResponse)
            if value is not None:
                current, replaced = value, True
        return current if replaced else None

    async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> str | None:
        """The first member's string answer (list order); later members are skipped.

        Each member gets its own copy of ``call``: one cannot change what a later one (or
        the tool) sees."""
        for member in self._members:
            method = getattr(member, "before_tool", None)
            if method is None:
                continue
            answer = _checked(member, "before_tool", await method(ctx, _call_copy(call)), str)
            if answer is not None:
                return answer
        return None

    async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> str | None:
        """Members' ``after_tool`` in reverse list order, each on the current result."""
        current, replaced = result, False
        for member in reversed(self._members):
            method = getattr(member, "after_tool", None)
            if method is None:
                continue
            value = _checked(
                member, "after_tool", await method(ctx, _call_copy(call), current), str
            )
            if value is not None:
                current, replaced = value, True
        return current if replaced else None

    def tool_needs_approval(
        self, ctx: RunContext[Any], tool: Tool, arguments: Mapping[str, Any]
    ) -> bool | None:
        """``True`` when any member says so (list order, stopping at the first)."""
        for member in self._members:
            # Each member gets its own copies: one cannot change what a later one sees.
            if approval_vote(member, ctx, copy.copy(tool), copy.deepcopy(dict(arguments))):
                return True
        return None

    def input_guardrails(self, ctx: RunContext[Any], agent: Agent) -> Sequence[Any]:
        """Every member's input guardrails, in list order."""
        found: list[Any] = []
        for member in self._members:
            found.extend(hook_input_guardrails(member, ctx, agent))
        return tuple(found)

    def output_guardrails(self, ctx: RunContext[Any], agent: Agent) -> Sequence[Any]:
        """Every member's output guardrails, in list order."""
        found: list[Any] = []
        for member in self._members:
            found.extend(hook_output_guardrails(member, ctx, agent))
        return tuple(found)


def hook_input_guardrails(hooks: Any, ctx: RunContext[Any], agent: Agent) -> tuple[Any, ...]:
    """``hooks.input_guardrails(ctx, agent)`` checked like :func:`hook_output_guardrails`."""
    return _hook_guardrails(hooks, ctx, agent, "input_guardrails")


def hook_output_guardrails(hooks: Any, ctx: RunContext[Any], agent: Agent) -> tuple[Any, ...]:
    """``hooks.output_guardrails(ctx, agent)`` checked: a sync method returning a list of
    guardrail functions; a missing method contributes nothing."""
    return _hook_guardrails(hooks, ctx, agent, "output_guardrails")


def _hook_guardrails(hooks: Any, ctx: RunContext[Any], agent: Agent, name: str) -> tuple[Any, ...]:
    method = getattr(hooks, name, None)
    if method is None:
        return ()
    if not callable(method):
        # A list assigned as a class attribute (``output_guardrails = [check]``) would
        # otherwise add no guardrail and no hold: fail closed instead.
        raise SDKValidationError(
            f"AgentHooks.{name} of {_member_name(hooks)} must be a method "
            f"(def {name}(self, ctx, agent): return [...]), not "
            f"{type(method).__name__}."
        )
    value = method(ctx, agent)
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()  # never awaited: avoid the "never awaited" warning
        raise SDKValidationError(
            f"AgentHooks.{name} of {_member_name(hooks)} must be a regular (sync) "
            "method; it returned an awaitable."
        )
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise SDKValidationError(
            f"AgentHooks.{name} of {_member_name(hooks)} must return a list of "
            f"guardrail functions, not {type(value).__name__}."
        )
    if not all(callable(fn) for fn in value):
        raise SDKValidationError(
            f"AgentHooks.{name} of {_member_name(hooks)} returned a value that is not a function."
        )
    return tuple(value)


def _call_copy(call: ToolCall) -> ToolCall:
    """``call`` with deep-copied arguments (a hook's changes reach nothing else)."""
    if not isinstance(call, ToolCall):
        return call  # not a ToolCall (a test double, None): passed as given
    return dataclasses.replace(call, arguments=copy.deepcopy(call.arguments))


def approval_vote(hooks: Any, ctx: RunContext[Any], tool: Tool, arguments: Any) -> bool:
    """Ask ``hooks.tool_needs_approval`` (when it has one); ``True`` adds an approval gate.

    The method must be sync and return ``True``, ``False`` or ``None``; anything else
    raises ``SDKValidationError``. Exceptions propagate.
    """
    method = getattr(hooks, "tool_needs_approval", None)
    if method is None:
        return False
    value = method(ctx, tool, arguments)
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()  # never awaited: avoid the "never awaited" warning
        raise SDKValidationError(
            f"AgentHooks.tool_needs_approval of {_member_name(hooks)} must be a regular "
            "(sync) method; it returned an awaitable."
        )
    if value is None or isinstance(value, bool):
        return bool(value)
    raise SDKValidationError(
        f"AgentHooks.tool_needs_approval of {_member_name(hooks)} must return True, False or "
        f"None, not {type(value).__name__}."
    )
