# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Agent-to-agent handoffs: control moves to another agent and the first one stops.

``Agent(handoffs=[specialist])`` gives the agent a ``transfer_to_<name>`` tool. When the model
calls it, the runner makes the target the active agent for the rest of the run: the target's
model, instructions, tools, handoffs, ``output_type`` and output guardrails take over, and the
conversation carries on in the same transcript. Compare ``Agent.as_tool()``, where the caller
keeps control and only receives an answer.

Rules, all deterministic:

- One handoff per model turn. If a turn has several handoff calls the first wins; the others get
  a tool result saying they were ignored. Other (non-handoff) tool calls in the same turn still
  run, with the agent that made them, and the switch happens after the whole batch.
- Run-wide limits (``max_turns``, ``max_tool_calls``, ``timeout``) come from the agent that
  started the run; ``max_handoffs`` (default 10) bounds the number of switches.
- Input guardrails run only for the starting agent. Output guardrails and ``output_type`` belong
  to the agent that produces the final answer.
- Memory is recalled once, for the starting agent, and is not repeated after a handoff.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from nexura_agents.errors import SDKValidationError
from nexura_agents.tools import EnabledRule, Tool, _check_enabled_rule

if TYPE_CHECKING:
    from nexura_agents.agent import Agent
    from nexura_agents.run_context import RunContext
    from nexura_agents.types import Message

__all__ = ["Handoff", "handoff"]

InputFilter = Callable[["list[Message]"], "list[Message]"]
OnHandoff = Callable[["RunContext[Any]", str], "Awaitable[None] | None"]


def _snake(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class Handoff:
    """A way for one agent to pass the conversation to another agent.

    Args:
        agent: The agent that takes over.
        tool_name: Name of the tool the model calls. Defaults to ``transfer_to_<agent name>``
            in lower snake case.
        description: Tool description shown to the model. Defaults to a generic sentence.
        input_filter: Receives a copy of the conversation (without the system message) and
            returns the messages the target sees, for example to drop earlier tool noise. The
            result must keep every tool call paired with its result.
        on_handoff: Called with the run context and the model's reason (or, with
            ``input_type``, the validated input object) when the handoff happens. May be async.
        input_type: A Pydantic model for structured handoff arguments (for example a ticket
            with a priority). The model fills it in when it calls the handoff tool; the target
            agent finds it in ``ctx.metadata["handoff_input"]`` (as a dict).
        enabled: ``True``/``False`` or a function of the ``RunContext`` (sync or async),
            checked before each model call, like ``@tool(enabled=...)``. A disabled handoff
            is not offered, and a call to it fails like an unknown tool.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        tool_name: str | None = None,
        description: str | None = None,
        input_filter: InputFilter | None = None,
        on_handoff: OnHandoff | None = None,
        input_type: type[BaseModel] | None = None,
        enabled: EnabledRule = True,
    ) -> None:
        from nexura_agents.agent import Agent as _Agent

        if not isinstance(agent, _Agent):
            raise SDKValidationError(f"Handoff target must be an Agent, got {type(agent)}.")
        if tool_name is not None and (not isinstance(tool_name, str) or not tool_name.strip()):
            raise SDKValidationError("Handoff tool_name must be a non-empty string.")
        if input_filter is not None and not callable(input_filter):
            raise SDKValidationError("Handoff input_filter must be callable.")
        if on_handoff is not None and not callable(on_handoff):
            raise SDKValidationError("Handoff on_handoff must be callable.")
        if input_type is not None and not (
            isinstance(input_type, type) and issubclass(input_type, BaseModel)
        ):
            raise SDKValidationError("Handoff input_type must be a Pydantic BaseModel subclass.")
        self.input_type = input_type
        self.agent = agent
        self.tool_name = tool_name.strip() if tool_name else f"transfer_to_{_snake(agent.name)}"
        self.description = (
            description
            or agent.description
            and f"Transfer the conversation to {agent.name}: {agent.description}"
            or f"Transfer the conversation to the {agent.name} agent."
        )
        self.input_filter = input_filter
        self.on_handoff = on_handoff
        self.tool = self._build_tool()
        self.tool.enabled = _check_enabled_rule(self.tool_name, enabled)
        self.enabled = self.tool.enabled

    def _build_tool(self) -> Tool:
        target = self.agent.name

        async def transfer(reason: str = "") -> str:
            """Placeholder body; the runner performs the switch."""
            return f"Transferred to {target}."

        if self.input_type is not None:
            return Tool(
                self.tool_name,
                transfer,
                description=self.description,
                args_schema=self.input_type,
                origin="handoff",
                tags={f"handoff:{target}"},
            )
        return Tool(
            self.tool_name,
            transfer,
            description=self.description,
            origin="handoff",
            tags={f"handoff:{target}"},
        )

    def reason(self, arguments: dict[str, Any]) -> str:
        """Validated ``reason`` from the model's tool arguments.

        With ``input_type`` this is the validated input as JSON (what is checkpointed).
        """
        if self.input_type is not None:
            return self.input_type.model_validate(arguments).model_dump_json()
        return str(self.tool.args_schema.model_validate(arguments).reason or "")  # type: ignore[attr-defined]

    def parsed_input(self, reason: str) -> Any:
        """The ``input_type`` object for a stored ``reason`` (or the reason text itself)."""
        if self.input_type is None:
            return reason
        return self.input_type.model_validate_json(reason)

    def __repr__(self) -> str:
        return f"Handoff(agent={self.agent.name!r}, tool_name={self.tool_name!r})"


def handoff(agent: Agent, **kwargs: Any) -> Handoff:
    """Shorthand for ``Handoff(agent, **kwargs)``."""
    return Handoff(agent, **kwargs)
