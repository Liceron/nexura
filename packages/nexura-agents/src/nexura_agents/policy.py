# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Default hooks for every run, and the hooks a run passes on to the runs it starts.

An operator installs a policy once with :func:`set_default_hooks`; every ``Runner`` then
uses it, before its own hooks. While a run is in progress its hooks are the *active*
hooks (:data:`active_hooks`): a ``Runner`` started inside it (``Agent.as_tool``, a team
member, a module or template that runs an agent, an ``AgentMCPServer`` tool) uses them
too, the way a nested run stays inside the enclosing budget. So the hooks a run gets are

    the inherited active hooks, then the process default, then the runner's own hooks,

with each hooks object used once (deduplicated by identity). The first is the outermost
(see :class:`~nexura_agents.hooks.ComposedHooks`): a policy's ``before_*`` hooks take
over before the developer's, and its ``after_*`` hooks see the final value.

A runner's own ``hooks=`` add to the default; they never replace it. Only
``Runner(default_hooks=False)`` leaves the process default out, and it exists for tests:
it cannot drop hooks inherited from an enclosing run. An operator who installs the default
with ``set_default_hooks(policy, locked=True)`` makes it a boundary against code in the
same process: while it is locked, ``default_hooks=False`` and replacing or removing the
default raise ``SDKValidationError`` (the service will install its policy this way, G6).

Context variables are copied into tasks, ``asyncio.to_thread`` and the runner's own
threads, but not into a plain ``threading.Thread``: start such threads with
``contextvars.copy_context().run`` so a run started there inherits the hooks.

Code that runs tools outside a ``Runner`` (``AgentMCPServer``, the runtime's agent
coordinator) uses :func:`tool_call_needs_approval` and :func:`run_tool_call`, so a tool
call gets the same guardrails, hooks and approval gates there.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from nexura_agents.errors import SDKValidationError
from nexura_agents.hooks import AgentHooks, ComposedHooks, approval_vote, is_hooks_object

if TYPE_CHECKING:
    from nexura_agents.run_context import RunContext
    from nexura_agents.tools import Tool
    from nexura_agents.types import ToolCall

__all__ = [
    "active_hooks",
    "check_model_stages",
    "default_hooks",
    "default_hooks_locked",
    "effective_hooks",
    "run_tool_call",
    "set_default_hooks",
    "tool_call_needs_approval",
]

#: The hooks of the run in progress (``None`` outside a run, or a run without hooks).
#: Set by the runner for each step of its event stream, so it follows SDK-created child
#: tasks and nested runners but not the code consuming the stream.
active_hooks: ContextVar[AgentHooks | None] = ContextVar("nexura_active_hooks", default=None)

_default: AgentHooks | None = None
_locked = False
_UNSET: Any = object()


def compose_hooks(hooks: Any) -> AgentHooks | None:
    """One hooks object as is, a list or tuple as ``ComposedHooks``, ``None``/``[]`` as None.

    Anything else (a set, dict, generator, class, string or an object without any
    ``AgentHooks`` method) raises ``SDKValidationError``.
    """
    if hooks is None:
        return None
    if isinstance(hooks, Sequence) and not isinstance(hooks, (str, bytes)):
        return ComposedHooks(*hooks) if len(hooks) else None
    if not is_hooks_object(hooks):
        raise SDKValidationError(
            "hooks must be a hooks object (an AgentHooks instance or an object with its "
            f"methods) or a list of them, got {type(hooks).__name__}."
        )
    return hooks  # type: ignore[no-any-return]


def set_default_hooks(
    hooks: AgentHooks | Sequence[AgentHooks] | None, *, locked: bool = False
) -> None:
    """Install hooks (a policy) that every ``Runner`` in this process uses.

    They run before the runner's own hooks, which cannot remove them. ``None`` removes
    the default. A list is composed in order. Runs already in progress keep the hooks
    they started with. Code is never loaded from the environment: the operator's
    process calls this (``nexura_guardrails.GuardrailPolicy.install()`` does).

    ``locked=True`` makes the default binding for the rest of the process: later calls
    that remove or replace it, and ``Runner(default_hooks=False)`` /
    ``AgentCoordinator(default_hooks=False)``, raise ``SDKValidationError``. Installing
    the same object again is allowed. Use it when the default is an operator's policy
    that application code must not be able to drop.
    """
    global _default, _locked
    if not isinstance(locked, bool):
        raise SDKValidationError("locked must be True or False.")
    composed = compose_hooks(hooks)
    if _locked and composed is not _default:
        raise SDKValidationError(
            "The default hooks are locked by the operator (set_default_hooks(..., "
            "locked=True)); they cannot be replaced or removed in this process."
        )
    _default = composed
    _locked = _locked or (locked and composed is not None)


def default_hooks() -> AgentHooks | None:
    """The process default hooks set with :func:`set_default_hooks` (``None`` when unset)."""
    return _default


def default_hooks_locked() -> bool:
    """True when the process default was installed with ``locked=True``."""
    return _locked


def check_opt_out(use_default: bool) -> None:
    """Refuse ``default_hooks=False`` while the operator's default is locked."""
    if not use_default and _locked:
        raise SDKValidationError(
            "default_hooks=False is not allowed: the default hooks are locked by the "
            "operator (set_default_hooks(..., locked=True))."
        )


def _unlock_for_tests() -> None:
    """Remove a locked default. For test fixtures only; never call it in application code."""
    global _default, _locked
    _default, _locked = None, False


def _members(hooks: AgentHooks | None) -> tuple[Any, ...]:
    if hooks is None:
        return ()
    if type(hooks) is ComposedHooks:
        return hooks.members
    return (hooks,)


def _contained(member: Any) -> set[int]:
    """Identities of the hooks objects inside a ``ComposedHooks`` subclass (recursively)."""
    found: set[int] = set()
    if isinstance(member, ComposedHooks):
        for inner in member.members:
            found.add(id(inner))
            found |= _contained(inner)
    return found


def _without(member: Any, seen: set[int]) -> Any:
    """A ``ComposedHooks`` subclass instance without the members already used (a shallow
    copy, so its own overrides keep running); anything else as it is."""
    if not isinstance(member, ComposedHooks) or not (_contained(member) & seen):
        return member
    clone = copy.copy(member)
    clone._members = tuple(
        _without(inner, seen) for inner in member.members if id(inner) not in seen
    )
    return clone


def requires_model_stages(hooks: Any) -> bool:
    """True when ``hooks`` (or a member of a composition) says it must see every model
    call and the agent's input and output (``requires_model_stages()``, P39 G6): a
    guardrail policy with ``input``, ``model_input``, ``model_output`` or ``output`` rules."""
    if hooks is None:
        return False
    method = getattr(hooks, "requires_model_stages", None)
    if callable(method) and method() is True:
        return True
    if isinstance(hooks, ComposedHooks):
        return any(requires_model_stages(member) for member in hooks.members)
    return False


def check_model_stages(applies_model_stages: bool, hooks: Any = _UNSET) -> None:
    """Refuse a caller that runs models itself without the hooks' model stages (an
    ``AgentCoordinator``, an ``AgentDefinition`` subagent) when the hooks it would use need
    them (P39 G6): ``hooks``, or by default the inherited active hooks and the process
    default (locked or not). Whatever the source: the caller's own ``hooks=policy``, an
    unlocked ``install()``, a locked operator policy or an enclosing run's hooks."""
    if applies_model_stages:
        return
    sources = (active_hooks.get(), _default) if hooks is _UNSET else (hooks,)
    if not any(requires_model_stages(source) for source in sources):
        return
    raise SDKValidationError(
        "A guardrail policy in effect has input, model or "
        "output rules, which this code path cannot apply: it calls models without the "
        "before_model/after_model hooks and the agent input/output guardrails (for example "
        "AgentCoordinator and AgentDefinition subagents). It is refused instead of running "
        "unchecked; use Agent and Runner (handoffs, Agent.as_tool, teams), or a policy "
        "without those stages."
    )


def effective_hooks(
    own: AgentHooks | None = None,
    *,
    use_default: bool = True,
    applies_model_stages: bool = False,
) -> AgentHooks | None:
    """The hooks a run started now uses: the inherited active hooks, the process default
    (unless ``use_default`` is False and the default is not locked) and ``own``, in that
    order, each object once.

    ``applies_model_stages``: the caller runs every model call through the hooks'
    ``before_model``/``after_model`` and adds their input and output guardrails (the
    ``Runner``), or makes no model calls of its own (tools served over MCP). Other callers
    are refused with ``SDKValidationError`` when any of the returned hooks requires those
    stages (:func:`check_model_stages`).

    A hooks object that also sits inside a ``ComposedHooks`` subclass met later (for
    example the policy wrapped by a developer's composition) runs only in its first
    position: the subclass gets a copy without it. A single hooks object is returned as
    it is; several become a ``ComposedHooks``.
    """
    members: list[Any] = []
    seen: set[int] = set()
    use = use_default or _locked
    for source in (active_hooks.get(), _default if use else None, own):
        for member in _members(source):
            if id(member) in seen:
                continue
            seen.add(id(member))
            member = _without(member, seen)
            seen |= _contained(member)
            members.append(member)
    for member in members:
        check_model_stages(applies_model_stages, member)
    if not members:
        return None
    if len(members) == 1:
        return members[0]  # type: ignore[no-any-return]
    return ComposedHooks(*members)


@contextlib.contextmanager
def scoped(hooks: AgentHooks | None) -> Iterator[None]:
    """Make ``hooks`` the active hooks for the block (runs started inside inherit them)."""
    token = active_hooks.set(hooks)
    try:
        yield
    finally:
        active_hooks.reset(token)


async def hooks_stream(stream: Any, hooks: AgentHooks | None) -> AsyncGenerator[Any, None]:
    """Make ``hooks`` active for each advancement of ``stream``, without leaking them
    into the consumer (mirrors ``usage.admission.budget_stream``)."""
    try:
        while True:
            token = active_hooks.set(hooks)
            try:
                event = await anext(stream)
            except StopAsyncIteration:
                return
            finally:
                active_hooks.reset(token)
            yield event
    finally:
        token = active_hooks.set(hooks)
        try:
            await stream.aclose()
        finally:
            active_hooks.reset(token)


def hook_copy(call: ToolCall) -> ToolCall:
    """A copy of ``call`` for hooks: changing its arguments changes nothing that runs."""
    return dataclasses.replace(call, arguments=copy.deepcopy(call.arguments))


def _override(hook: str, value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise SDKValidationError(
        f"AgentHooks.{hook} must return None or a str, not {type(value).__name__}."
    )


def tool_call_needs_approval(
    tool: Tool, ctx: RunContext[Any], arguments: dict[str, Any], hooks: AgentHooks | None
) -> bool:
    """The runner's approval rule for one call: the tool's ``requires_approval``, or else
    any hook's ``tool_needs_approval`` (it gets copies of the tool and the arguments)."""
    if tool.needs_approval(ctx, arguments):
        return True
    if hooks is None:
        return False
    from nexura_agents.errors import ToolValidationError

    try:
        tool.dispatch_arguments(arguments)
    except ToolValidationError:
        return False  # refused as invalid by run_tool_call before anything runs
    return approval_vote(hooks, ctx, copy.copy(tool), copy.deepcopy(arguments))


async def run_tool_call(
    tool: Tool,
    ctx: RunContext[Any],
    call: ToolCall,
    hooks: AgentHooks | None,
    execute: Callable[[], Awaitable[str]],
) -> tuple[str, bool]:
    """Run one tool call the way the runner does, outside a ``Runner``.

    In order: the tool's input guardrails, ``before_tool`` (a string answer skips the
    tool), ``execute()``, ``after_tool`` (a string replaces the result) and the tool's
    output guardrails. ``hooks`` are active while the tool runs, so a run it starts
    inherits them. Hooks get copies of ``call``: ``execute`` runs what was checked and
    approved. Returns ``(result text, rejected)``: ``rejected`` is True when a
    guardrail blocked the call or withheld its result (the text says which). Approval is
    the caller's to check first (:func:`tool_call_needs_approval`). Exceptions propagate:
    a tripwire, a broken guardrail or a hook error fails the call.
    """
    from nexura_agents.guardrails import check_tool_input, check_tool_output

    if getattr(ctx, "_tool_object", None) is None:
        ctx._tool_object = tool  # hooks validate the arguments the tool will receive (G6)
    # Invalid arguments are refused before guardrails, hooks or the tool run, as execute()
    # would refuse them (unknown names of a plain @tool are dropped, not refused); hooks and
    # the policy see the raw and the dispatched arguments (G6). Raises ToolValidationError.
    tool.dispatch_arguments(call.arguments)
    blocked = await check_tool_input(tool, ctx, call.arguments)
    if blocked is not None:
        return blocked, True
    with scoped(hooks):
        answered = (
            _override("before_tool", await hooks.before_tool(ctx, hook_copy(call)))
            if hooks is not None
            else None
        )
        result = answered if answered is not None else await execute()
        if hooks is not None:
            rewritten = _override(
                "after_tool", await hooks.after_tool(ctx, hook_copy(call), result)
            )
            if rewritten is not None:
                result = rewritten
    return await check_tool_output(tool, ctx, result)
