# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Agent configuration with eager validation and stateless reusability."""

from __future__ import annotations

import contextlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, overload

from pydantic import BaseModel

from nexura_agents.errors import ModelRetry, SDKValidationError, ToolError
from nexura_agents.events import RunEventType
from nexura_agents.guardrails import GuardrailFn
from nexura_agents.history import HistoryProcessor, check_processors
from nexura_agents.json_schema import check_schema
from nexura_agents.limits import ModelSettings
from nexura_agents.models.base import ModelProtocol
from nexura_agents.run_context import RunContext
from nexura_agents.tools import StopAtTools, Tool
from nexura_agents.types import OutputT

ToolUseBehavior = Literal["run_llm_again", "stop_on_first_tool"] | StopAtTools
# fn(output) or fn(ctx, output) -> output, sync or async; raise ModelRetry to ask again.
OutputValidator = Callable[..., Any]
# (ctx, tools) -> tools, sync or async; see Agent(prepare_tools=...).
PrepareTools = Callable[[RunContext[Any], list[Tool]], list[Tool] | Awaitable[list[Tool]]]

if TYPE_CHECKING:
    from nexura_agents.handoffs import Handoff
    from nexura_agents.memory import Memory
    from nexura_agents.runner import Prompt, Runner
    from nexura_agents.types import RunResult

Instructions = str | Callable[[RunContext[Any]], str] | Callable[[RunContext[Any]], Awaitable[str]]


def _ask_user_tool() -> Tool:
    from nexura_agents.types import ASK_USER_TOOL

    def ask_user(question: str) -> str:
        """Ask the user a question and wait for their answer. Use it when you need
        information only the user has; ask one clear question at a time."""
        # Never runs: the call pauses the run, and resume(answers=...) supplies the result.
        raise ToolError("ask_user needs an answer from a person; resume with answers=.")

    built_in = Tool(ASK_USER_TOOL, ask_user, requires_approval=True, origin="builtin:ask_user")
    built_in.asks_user = True  # answers= may only supply results for this tool
    return built_in


# Static-typing only: the validated output type (``str`` when no ``output_type`` is given).
ModelT = TypeVar("ModelT", bound=BaseModel)


#: Name of the CUSTOM event ``Agent.as_tool`` emits with the tools its sub-agent ran.
SUBAGENT_TOOLS = "nexura.subagent_tools"


class Agent(Generic[OutputT]):  # noqa: UP046 (a TypeVar default needs typing_extensions on 3.12)
    """Stateless configuration defining an agent.

    An Agent encapsulates the name, system instructions, model adapter,
    registered tools, and execution limits.

    Agent configurations are completely immutable and stateless, allowing
    a single Agent instance to be safely reused across concurrent runs without
    state collisions or leaking conversation history between callers.
    """

    @overload
    def __init__(
        self: Agent[ModelT],
        name: str,
        instructions: Instructions,
        model: ModelProtocol | str,
        *,
        tools: Sequence[Tool] | None = None,
        max_turns: int = 10,
        max_tool_calls: int = 20,
        timeout: float | None = None,
        output_type: type[ModelT],
        output_retries: int = 1,
        input_guardrails: Sequence[GuardrailFn] = (),
        output_guardrails: Sequence[GuardrailFn] = (),
        parallel_tool_calls: bool = False,
        description: str = "",
        memory: Memory | None = None,
        handoffs: Sequence[Agent | Handoff] = (),
        max_handoffs: int = 10,
        history_processors: Sequence[HistoryProcessor] = (),
        prepare_tools: PrepareTools | None = None,
        model_settings: ModelSettings | None = None,
        tool_use_behavior: ToolUseBehavior = "run_llm_again",
        output_validators: Sequence[OutputValidator] = (),
        reset_tool_choice: bool = True,
        ask_user: bool = False,
    ) -> None: ...

    @overload
    def __init__(
        self: Agent[dict[str, Any]],
        name: str,
        instructions: Instructions,
        model: ModelProtocol | str,
        *,
        tools: Sequence[Tool] | None = None,
        max_turns: int = 10,
        max_tool_calls: int = 20,
        timeout: float | None = None,
        output_type: dict[str, Any],
        output_retries: int = 1,
        input_guardrails: Sequence[GuardrailFn] = (),
        output_guardrails: Sequence[GuardrailFn] = (),
        parallel_tool_calls: bool = False,
        description: str = "",
        memory: Memory | None = None,
        handoffs: Sequence[Agent | Handoff] = (),
        max_handoffs: int = 10,
        history_processors: Sequence[HistoryProcessor] = (),
        prepare_tools: PrepareTools | None = None,
        model_settings: ModelSettings | None = None,
        tool_use_behavior: ToolUseBehavior = "run_llm_again",
        output_validators: Sequence[OutputValidator] = (),
        reset_tool_choice: bool = True,
        ask_user: bool = False,
    ) -> None: ...

    @overload
    def __init__(
        self: Agent[str],
        name: str,
        instructions: Instructions,
        model: ModelProtocol | str,
        *,
        tools: Sequence[Tool] | None = None,
        max_turns: int = 10,
        max_tool_calls: int = 20,
        timeout: float | None = None,
        output_type: None = None,
        output_retries: int = 1,
        input_guardrails: Sequence[GuardrailFn] = (),
        output_guardrails: Sequence[GuardrailFn] = (),
        parallel_tool_calls: bool = False,
        description: str = "",
        memory: Memory | None = None,
        handoffs: Sequence[Agent | Handoff] = (),
        max_handoffs: int = 10,
        history_processors: Sequence[HistoryProcessor] = (),
        prepare_tools: PrepareTools | None = None,
        model_settings: ModelSettings | None = None,
        tool_use_behavior: ToolUseBehavior = "run_llm_again",
        output_validators: Sequence[OutputValidator] = (),
        reset_tool_choice: bool = True,
        ask_user: bool = False,
    ) -> None: ...

    def __init__(
        self,
        name: str,
        instructions: Instructions,
        model: ModelProtocol | str,
        *,
        tools: Sequence[Tool] | None = None,
        max_turns: int = 10,
        max_tool_calls: int = 20,
        timeout: float | None = None,
        output_type: type[BaseModel] | dict[str, Any] | None = None,
        output_retries: int = 1,
        input_guardrails: Sequence[GuardrailFn] = (),
        output_guardrails: Sequence[GuardrailFn] = (),
        parallel_tool_calls: bool = False,
        description: str = "",
        memory: Memory | None = None,
        handoffs: Sequence[Agent | Handoff] = (),
        max_handoffs: int = 10,
        history_processors: Sequence[HistoryProcessor] = (),
        prepare_tools: PrepareTools | None = None,
        model_settings: ModelSettings | None = None,
        tool_use_behavior: ToolUseBehavior = "run_llm_again",
        output_validators: Sequence[OutputValidator] = (),
        reset_tool_choice: bool = True,
        ask_user: bool = False,
    ) -> None:
        # The constructor arguments, so clone() can rebuild the agent with overrides.
        self._init_kwargs: dict[str, Any] = {
            key: value for key, value in locals().items() if key not in ("self", "__class__")
        }
        # Validate name
        if not isinstance(name, str) or not name.strip():
            raise SDKValidationError("Agent name must be a non-empty string.")
        self.name = name.strip()

        # Instructions: a string, or a function of the RunContext evaluated per run
        if not isinstance(instructions, str) and not callable(instructions):
            raise SDKValidationError("Agent instructions must be a string or a callable.")
        self.instructions = instructions
        self.description = description

        if isinstance(output_type, dict):
            check_schema(output_type)
        elif output_type is not None and not (
            isinstance(output_type, type) and issubclass(output_type, BaseModel)
        ):
            raise SDKValidationError(
                "output_type must be a Pydantic BaseModel subclass or a JSON-schema dict."
            )
        self.output_type = output_type
        if not isinstance(output_retries, int) or output_retries < 0:
            raise SDKValidationError("output_retries must be an integer >= 0.")
        self.output_retries = output_retries
        self.input_guardrails: tuple[GuardrailFn, ...] = tuple(input_guardrails)
        self.output_guardrails: tuple[GuardrailFn, ...] = tuple(output_guardrails)
        self.parallel_tool_calls = parallel_tool_calls
        # Shape the per-call copy of the conversation (trimming, redaction); see history.py
        self.history_processors = check_processors(history_processors)
        # (ctx, tools) -> tools: choose or adjust the tools offered before each model call
        if prepare_tools is not None and not callable(prepare_tools):
            raise SDKValidationError("prepare_tools must be a function (ctx, tools) -> tools.")
        self.prepare_tools = prepare_tools
        # Merged over Runner(model_settings=...): fields set here win for this agent.
        if model_settings is not None and not isinstance(model_settings, ModelSettings):
            raise SDKValidationError("model_settings must be a ModelSettings instance.")
        self.model_settings = model_settings
        # Whether a tool result can end the run without another model call
        if not (
            tool_use_behavior in ("run_llm_again", "stop_on_first_tool")
            or isinstance(tool_use_behavior, StopAtTools)
        ):
            raise SDKValidationError(
                "tool_use_behavior must be 'run_llm_again', 'stop_on_first_tool' or "
                f"StopAtTools([...]), got {tool_use_behavior!r}."
            )
        self.tool_use_behavior: ToolUseBehavior = tool_use_behavior
        # Checks on the parsed final output; raising ModelRetry asks the model to try again
        if callable(output_validators) or isinstance(output_validators, (str, bytes)):
            raise SDKValidationError("output_validators must be a list of functions.")
        self.output_validators: tuple[OutputValidator, ...] = tuple(output_validators)
        # After this agent has called a tool in a turn, go back to tool_choice="auto" so a
        # forced choice cannot loop. Set False to keep the choice for every model call.
        self.reset_tool_choice = bool(reset_tool_choice)
        if not all(callable(v) for v in self.output_validators):
            raise SDKValidationError("Every output validator must be a function.")

        # Validate model; "provider:model" strings build the matching provider
        if isinstance(model, str):
            from nexura_agents.models.infer import infer_model

            model = infer_model(model)
        if model is None or not hasattr(model, "generate"):
            raise SDKValidationError(
                "Agent model must provide a generate() method (ModelProtocol)."
            )
        self.model = model

        # Long-term memory adds its remember/recall tools next to the user's own
        if memory is not None and not (
            callable(getattr(memory, "build_tools", None))
            and callable(getattr(memory, "recall_block", None))
        ):
            raise SDKValidationError("Agent memory must be a nexura_agents.Memory instance.")
        self.memory = memory
        tool_seq = list(tools or [])
        if memory is not None and memory.tools:
            tool_seq.extend(memory.build_tools())
        if ask_user:
            # The model can pause the run with a question; resume with answers={id: text}.
            tool_seq.append(_ask_user_tool())

        # Validate tools and check for duplicate tool names eagerly
        seen_tool_names: set[str] = set()
        validated_tools: list[Tool] = []
        for t in tool_seq:
            if not isinstance(t, Tool):
                raise SDKValidationError(f"Expected Tool instance in tools list, got {type(t)}")
            if t.name in seen_tool_names:
                hint = (
                    " (memory tools are named 'remember' and 'recall'; rename your tool or "
                    "use Memory(tools=False))"
                    if memory is not None and memory.tools and t.name in ("remember", "recall")
                    else ""
                )
                raise SDKValidationError(
                    f"Duplicate tool name registered in agent '{self.name}': '{t.name}'{hint}"
                )
            seen_tool_names.add(t.name)
            validated_tools.append(t)

        self.tools: tuple[Tool, ...] = tuple(validated_tools)
        self.tools_by_name: dict[str, Tool] = {t.name: t for t in self.tools}
        if isinstance(self.tool_use_behavior, StopAtTools):
            missing = sorted(self.tool_use_behavior.tool_names - set(self.tools_by_name))
            if missing:
                raise SDKValidationError(
                    f"StopAtTools names tools agent '{self.name}' does not have: "
                    f"{', '.join(missing)}."
                )

        # Handoffs: each becomes a transfer_to_<name> tool, kept apart from ``tools``.
        if not isinstance(max_handoffs, int) or isinstance(max_handoffs, bool) or max_handoffs < 0:
            raise SDKValidationError(f"max_handoffs must be an integer >= 0, got {max_handoffs!r}.")
        self.max_handoffs = max_handoffs
        self.handoffs: tuple[Handoff, ...] = ()
        self.handoffs_by_name: dict[str, Handoff] = {}
        self.add_handoffs(*handoffs)

        # Validate limits eagerly
        if not isinstance(max_turns, int) or max_turns < 1:
            raise SDKValidationError(f"max_turns must be an integer >= 1, got {max_turns!r}.")
        self.max_turns = max_turns

        if not isinstance(max_tool_calls, int) or max_tool_calls < 0:
            raise SDKValidationError(
                f"max_tool_calls must be an integer >= 0, got {max_tool_calls!r}."
            )
        self.max_tool_calls = max_tool_calls

        if timeout is not None:
            if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise SDKValidationError(
                    f"timeout must be a positive number and finite, got {timeout!r}."
                )
            self.timeout: float | None = float(timeout)
        else:
            self.timeout = None

    def add_handoffs(self, *targets: Agent | Handoff) -> None:
        """Add handoff targets after construction (needed for agents that hand back to each other).

        Accepts agents and ``Handoff`` objects and applies the same checks as the constructor:
        no duplicate handoff tool names and no collision with this agent's own tools. Nothing is
        changed if any target is rejected.
        """
        from nexura_agents.handoffs import Handoff as _Handoff

        added: list[_Handoff] = []
        for item in targets:
            added.append(item if isinstance(item, _Handoff) else _Handoff(item))
        by_name = dict(self.handoffs_by_name)
        targets_by_agent_name: dict[str, Any] = {h.agent.name: h.agent for h in self.handoffs}
        for h in added:
            known = targets_by_agent_name.get(h.agent.name)
            if known is not None and known is not h.agent:
                raise SDKValidationError(
                    f"Agent '{self.name}' has two different handoff targets named "
                    f"'{h.agent.name}'. Rename one: approvals resume by agent name."
                )
            targets_by_agent_name[h.agent.name] = h.agent
            if h.tool_name in by_name:
                raise SDKValidationError(
                    f"Duplicate handoff tool name in agent '{self.name}': '{h.tool_name}'"
                )
            if h.tool_name in self.tools_by_name:
                raise SDKValidationError(
                    f"Handoff tool name '{h.tool_name}' in agent '{self.name}' collides with "
                    "one of its tools (including memory tools); pass Handoff(tool_name=...)."
                )
            by_name[h.tool_name] = h
        self.handoffs_by_name = by_name
        self.handoffs = (*self.handoffs, *added)

    @property
    def output_schema(self) -> dict[str, Any] | None:
        """JSON schema of ``output_type`` (Pydantic model schema, or the dict as given)."""
        if self.output_type is None:
            return None
        if isinstance(self.output_type, dict):
            return self.output_type
        return self.output_type.model_json_schema()

    def native_output_schema(self) -> dict[str, Any] | None:
        """The schema to hand to the model's native structured-output mode, or ``None``.

        Only models that opt in (``supports_response_schema = True``) receive it, and when the
        agent offers tools or handoffs only if the model also declares
        ``response_schema_with_tools = True``. Otherwise the schema stays in the prompt.
        """
        schema = self.output_schema
        if schema is None or not getattr(self.model, "supports_response_schema", False):
            return None
        if (self.tools or self.handoffs) and not getattr(
            self.model, "response_schema_with_tools", False
        ):
            return None
        return schema

    def definition_digest(self) -> str:
        """A stable ``sha256:<hex>`` of what a durable run depends on.

        Covers the agent name, a hash of string instructions (or the qualified name of an
        instructions function), each tool's name, argument JSON schema and effect class,
        the handoff targets and the output schema. Models, limits and descriptions are not
        included. Recovery compares it with the digest stored when the run started; a
        difference is a warning in this release and an error in a later one.
        """
        import hashlib

        instructions = self.instructions
        if isinstance(instructions, str):
            instructions_id = "sha256:" + hashlib.sha256(instructions.encode("utf-8")).hexdigest()
        else:
            module = getattr(instructions, "__module__", "") or ""
            qualname = getattr(instructions, "__qualname__", type(instructions).__qualname__)
            instructions_id = f"callable:{module}.{qualname}"
        tools = []
        for t in sorted(self.tools, key=lambda item: item.name):
            schema = t.to_schema().get("function", {}).get("parameters")
            tools.append({"name": t.name, "parameters": schema, "effect": t.effect_class().value})
        payload = {
            "name": self.name,
            "instructions": instructions_id,
            "tools": tools,
            "handoffs": sorted([h.tool_name, h.agent.name] for h in self.handoffs),
            "output_schema": self.output_schema,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def render_instructions(self, ctx: RunContext[Any]) -> str:
        """System prompt for this run, including the output schema when typed.

        With native mode active the prompt carries a short instruction only; the schema itself
        travels in the provider request. Async instruction functions need
        :meth:`render_instructions_async` (the runner uses it).
        """
        raw = self.instructions(ctx) if callable(self.instructions) else self.instructions
        if inspect.isawaitable(raw):
            if inspect.iscoroutine(raw):
                raw.close()  # never awaited: avoid the "never awaited" warning
            raise SDKValidationError(
                f"Agent '{self.name}' has async instructions; use render_instructions_async()."
            )
        return self._with_output_schema(self._check_instructions(raw))

    async def render_instructions_async(self, ctx: RunContext[Any]) -> str:
        """Like :meth:`render_instructions`, awaiting async instruction functions."""
        raw = self.instructions(ctx) if callable(self.instructions) else self.instructions
        if inspect.isawaitable(raw):
            raw = await raw
        return self._with_output_schema(self._check_instructions(raw))

    def _check_instructions(self, raw: Any) -> str:
        if not isinstance(raw, str):
            raise SDKValidationError(
                f"Instructions of agent '{self.name}' must produce a str, got {type(raw).__name__}."
            )
        return raw

    def _with_output_schema(self, text: str) -> str:
        schema = self.output_schema
        if schema is not None:
            noun = "JSON object" if schema.get("type") == "object" else "JSON value"
            if self.native_output_schema() is not None:
                text = (
                    f"{text}\n\nRespond with only a {noun} (no prose, no code fences) "
                    "that conforms to the required output schema."
                ).strip()
            else:
                text = (
                    f"{text}\n\nRespond with only a {noun} (no prose, no code fences) "
                    f"matching this JSON schema:\n{json.dumps(schema, ensure_ascii=False)}"
                ).strip()
        return text

    def run(self, prompt: Prompt, **kwargs: Any) -> RunResult[OutputT]:
        """Run this agent with a default ``Runner``. Shortcut for ``Runner().run(agent, ...)``.

        Accepts the same keyword arguments as :meth:`Runner.run` (``deps``, ``history``,
        ``timeout``, ...). Use a ``Runner`` directly for hooks, limits or budgets.
        """
        from nexura_agents.runner import Runner

        return Runner().run(self, prompt, **kwargs)

    async def run_async(self, prompt: Prompt, **kwargs: Any) -> RunResult[OutputT]:
        """Async form of :meth:`run`."""
        from nexura_agents.runner import Runner

        return await Runner().run_async(self, prompt, **kwargs)

    def as_tool(
        self,
        name: str | None = None,
        description: str | None = None,
        *,
        runner: Runner | None = None,
    ) -> Tool:
        """Expose this agent as a tool another agent can call.

        The sub-agent receives the caller's ``deps``; its token usage is added to the
        calling run so limits and accounting cover the whole tree. A sub-agent cannot wait
        for approval inside a tool call: if it needs one, the tool raises an error.
        """
        from nexura_agents.runner import Runner

        sub_runner = runner or Runner()
        agent = self

        async def call_agent(ctx: RunContext[Any], request: str) -> Any:
            from nexura_agents.usage import admission
            from nexura_agents.usage.errors import BudgetExceededError

            enclosing = admission._budgets(admission.active_budget.get())
            configured = sub_runner._configured_budget
            try:
                result = await sub_runner.run_async(agent, request, deps=ctx.deps)
            except BudgetExceededError as exc:
                if (
                    configured is not None
                    and not any(admission._descendant(held, configured) for held in enclosing)
                    and exc.run_id == getattr(configured, "run_id", None)
                    and exc.workspace_id == getattr(configured, "workspace_id", None)
                ):
                    # A local child allowance is a refused delegation, not exhaustion
                    # of the parent's allowance. Report it as a tool result so the
                    # parent can choose another action; ancestor failures still abort.
                    raise ModelRetry(f"Sub-agent '{agent.name}' budget refused: {exc}") from exc
                raise
            ctx.add_usage(result.usage)
            # Record which tools the sub-agent ran (its own sub-agents included), so audits
            # and evals of the calling run can see them.
            ran: list[str] = []
            for event in result.events:
                if event.type == RunEventType.TOOL_STARTED:
                    ran.append(str(event.data.get("tool_name", "")))
                elif event.type == RunEventType.CUSTOM and event.data.get("name") == SUBAGENT_TOOLS:
                    ran.extend(str(t) for t in (event.data.get("data") or {}).get("tools", []))
            ctx.emit(SUBAGENT_TOOLS, agent=agent.name, tools=ran)
            if result.status == "paused" and result.interruption is not None:
                names = ", ".join(call.name for call in result.interruption.pending)
                raise ToolError(
                    f"Agent '{agent.name}' stopped before {names}: these actions need human "
                    "approval, which cannot be given inside another agent's tool call. "
                    "Nothing after the pause ran."
                )
            output = result.output
            return output if not isinstance(output, BaseModel) else output.model_dump(mode="json")

        tool_name = name or self.name.lower().replace(" ", "_")
        wrapped = Tool(
            tool_name,
            call_agent,
            description=description or self.description or f"Ask the {self.name} agent.",
            origin=f"agent:{self.name}",
        )
        wrapped.nexura_agent = self  # type: ignore[attr-defined]  # for to_mermaid()
        return wrapped

    def to_mermaid(self, *, include_tools: bool = True) -> str:
        """A Mermaid flowchart of this agent, its handoffs and tools (see ``diagrams``)."""
        from nexura_agents.diagrams import agent_mermaid

        return agent_mermaid(self, include_tools=include_tools)

    def clone(self, **overrides: Any) -> Agent[OutputT]:
        """A new agent with the same configuration and ``overrides`` applied.

        Accepts any ``Agent`` constructor argument; the result is validated like a new
        agent and the original is not changed. Handoffs added with ``add_handoffs`` are
        kept unless ``handoffs=`` is given. The model object is shared (not re-created).
        """
        unknown = sorted(set(overrides) - set(self._init_kwargs))
        if unknown:
            raise SDKValidationError(f"clone() got unknown Agent arguments: {', '.join(unknown)}.")
        kwargs = dict(self._init_kwargs)
        kwargs["model"] = self.model
        kwargs["handoffs"] = self.handoffs
        kwargs.update(overrides)
        return type(self)(**kwargs)

    @contextlib.contextmanager
    def override(self, **overrides: Any) -> Iterator[Agent[OutputT]]:
        """Temporarily change this agent in place, e.g. ``with agent.override(model=fake):``.

        Meant for tests: code that holds a reference to this agent (handoffs, teams,
        ``as_tool``) sees the change while the block runs. Every run of this agent in the
        process is affected, so do not use it to vary concurrent production runs; use
        :meth:`clone` for that. The original configuration is restored on exit.
        """
        replacement = self.clone(**overrides)
        saved = dict(self.__dict__)
        self.__dict__.update(replacement.__dict__)
        try:
            yield self
        finally:
            self.__dict__.clear()
            self.__dict__.update(saved)

    def __repr__(self) -> str:
        return (
            f"Agent(name={self.name!r}, model={self.model.__class__.__name__}, "
            f"tools_count={len(self.tools)}, max_turns={self.max_turns}, "
            f"max_tool_calls={self.max_tool_calls}, timeout={self.timeout})"
        )
