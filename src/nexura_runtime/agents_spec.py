# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Declarative agent specs saved per workspace and run by the ``nexura.agents.agent`` node.

An :class:`AgentSpec` is plain JSON: it names a chat model from the server's allowlist,
tools by module id, knowledge collections by id and workspace skills by name. It holds no
code, no provider strings and no keys, and nothing in it is evaluated. This module checks
the shape (names, sizes, the output JSON schema); whether the names exist in a workspace is
checked by the service when the spec is saved and again every time it runs.
"""

from __future__ import annotations

import json
import re
from typing import Any

from nexura_agents.errors import SDKValidationError
from nexura_agents.json_schema import check_schema
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

#: Same rule as skill names: lowercase letters, digits, ``_`` and ``-``; 1-64 characters.
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
#: The offline fixture every server offers (``service.api.chat_models.DEFAULT_CHAT_MODEL``).
DEFAULT_AGENT_MODEL = "fake-deterministic"
MAX_DESCRIPTION_CHARS = 500
MAX_INSTRUCTIONS_CHARS = 20_000
MAX_TOOLS = 20
MAX_KNOWLEDGE_COLLECTIONS = 10
MAX_SKILLS = 20
#: Output JSON schema size (JSON characters).
MAX_OUTPUT_SCHEMA_CHARS = 16_000
#: Whole spec size (JSON characters).
MAX_SPEC_CHARS = 64_000
#: Upper bounds for ``limits``.
MAX_TURNS_CAP = 20
MAX_TOOL_CALLS_CAP = 50
MAX_TIMEOUT_SECONDS = 300.0

_TOOL_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class AgentSpecError(ValueError):
    """A spec that is malformed, or names something that does not exist where it runs.

    ``field_errors`` lists ``{"field", "message"}`` items for forms; ``code`` is the stable
    API error code (``AGENT_INVALID`` unless a more specific one applies).
    """

    def __init__(
        self,
        message: str,
        field_errors: list[dict[str, str]] | None = None,
        *,
        code: str = "AGENT_INVALID",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.field_errors = list(field_errors or [])


class AgentLimits(BaseModel):
    """Per-run caps; every value is bounded by a server-side maximum."""

    model_config = ConfigDict(extra="forbid")

    max_turns: int = Field(default=6, ge=1, le=MAX_TURNS_CAP)
    max_tool_calls: int = Field(default=10, ge=0, le=MAX_TOOL_CALLS_CAP)
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=MAX_TIMEOUT_SECONDS)


def _unique(values: list[str], what: str) -> list[str]:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{what} {value!r} is listed twice.")
        seen.add(value)
    return values


class AgentSpec(BaseModel):
    """A saved agent: references and text only (see the module docstring)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=AGENT_NAME_PATTERN)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS_CHARS)
    #: A name from the server's chat model allowlist (``GET /chat/models``).
    model: str = Field(default=DEFAULT_AGENT_MODEL, min_length=1, max_length=128)
    #: Module ids from the app registry, exposed to the model as tools.
    tools: list[str] = Field(default_factory=list, max_length=MAX_TOOLS)
    #: Knowledge collection ids of the workspace, searched with ``knowledge_search``.
    knowledge: list[str] = Field(default_factory=list, max_length=MAX_KNOWLEDGE_COLLECTIONS)
    #: Workspace skill names, read at ``skills_label`` when the agent runs.
    skills: list[str] = Field(default_factory=list, max_length=MAX_SKILLS)
    skills_label: str = Field(default="production", pattern=_LABEL.pattern)
    #: Optional JSON schema the final answer must match (the supported subset only).
    output_schema: dict[str, Any] | None = None
    limits: AgentLimits = Field(default_factory=AgentLimits)

    @field_validator("description")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if _CONTROL.search(value.replace("\n", " ").replace("\t", " ")):
            raise ValueError("description must not contain control characters.")
        return value

    @field_validator("model")
    @classmethod
    def _model_name(cls, value: str) -> str:
        if not value.strip() or _CONTROL.search(value):
            raise ValueError("model must be a configured chat model name.")
        return value

    @field_validator("tools")
    @classmethod
    def _tool_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _TOOL_ID.fullmatch(item):
                raise ValueError(f"{item!r} is not a module id.")
        return _unique(value, "Tool")

    @field_validator("knowledge")
    @classmethod
    def _collection_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip() or len(item) > 128 or _CONTROL.search(item):
                raise ValueError(f"{item!r} is not a knowledge collection id.")
        return _unique(value, "Knowledge collection")

    @field_validator("skills")
    @classmethod
    def _skill_names(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _SKILL_NAME.fullmatch(item):
                raise ValueError(f"{item!r} is not a skill name.")
        return _unique(value, "Skill")

    @field_validator("output_schema")
    @classmethod
    def _schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        size = len(json.dumps(value, ensure_ascii=False))
        if size > MAX_OUTPUT_SCHEMA_CHARS:
            raise ValueError(
                f"output_schema is {size} characters; the limit is {MAX_OUTPUT_SCHEMA_CHARS}."
            )
        try:
            check_schema(value)
        except SDKValidationError as exc:
            raise ValueError(str(exc).replace("output_type", "output_schema")) from None
        _check_schema_bounds(value)
        return value

    @model_validator(mode="after")
    def _size(self) -> AgentSpec:
        size = len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False))
        if size > MAX_SPEC_CHARS:
            raise ValueError(f"The agent spec is {size} characters; the limit is {MAX_SPEC_CHARS}.")
        return self


def parse_agent_spec(data: Any) -> AgentSpec:
    """Validate ``data`` as an :class:`AgentSpec`; raises :class:`AgentSpecError`."""
    if not isinstance(data, dict):
        raise AgentSpecError("An agent spec must be a JSON object.")
    try:
        return AgentSpec.model_validate(data)
    except ValidationError as exc:
        errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ())) or "spec",
                "message": str(err.get("msg", "Invalid value")).removeprefix("Value error, "),
            }
            for err in exc.errors()
        ]
        shown = "; ".join(f"{e['field']}: {e['message']}" for e in errors[:5])
        raise AgentSpecError(f"Invalid agent spec: {shown}", errors) from None


#: Largest ``minLength`` / ``minItems`` an output schema may ask for: an agent's answer is a
#: chat reply, and the offline fixture builds a value of at least that size.
MAX_SCHEMA_MIN_LENGTH = 2_000
MAX_SCHEMA_MIN_ITEMS = 50
#: Budget for one offline fixture answer, in values plus string characters, so a recursive
#: ``$ref`` or many long strings stay small (the answer is roughly this many characters).
MAX_EXAMPLE_BUDGET = 16_000


def _check_schema_bounds(node: Any, depth: int = 0) -> None:
    """Refuse output schemas whose minimum sizes no reasonable answer meets."""
    if depth > 64:
        return
    if isinstance(node, dict):
        for key, limit in (
            ("minLength", MAX_SCHEMA_MIN_LENGTH),
            ("minItems", MAX_SCHEMA_MIN_ITEMS),
        ):
            value = node.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > limit:
                raise ValueError(f"output_schema {key} {value} is over the limit of {limit}.")
        for child in node.values():
            _check_schema_bounds(child, depth + 1)
    elif isinstance(node, list):
        for child in node:
            _check_schema_bounds(child, depth + 1)


def schema_example(schema: dict[str, Any]) -> Any:
    """A minimal value matching ``schema`` (offline fixtures answer with it).

    Bounded by ``MAX_EXAMPLE_BUDGET`` (values plus string characters), so a recursive ``$ref``
    cannot fan out; once spent, remaining values are ``None``.
    """
    return _example(schema, schema, 0, [MAX_EXAMPLE_BUDGET])


def _example(root: dict[str, Any], node: Any, depth: int, budget: list[int]) -> Any:
    if not isinstance(node, dict) or depth > 8 or budget[0] <= 0:
        return None
    budget[0] -= 1
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = (root.get("$defs") or {}).get(ref[len("#/$defs/") :])
        return _example(root, target, depth + 1, budget)
    if "const" in node:
        return node["const"]
    if isinstance(node.get("enum"), list) and node["enum"]:
        return node["enum"][0]
    declared = node.get("type")
    if isinstance(declared, list):
        declared = next((t for t in declared if t != "null"), "null")
    if declared is None:
        declared = "object" if "properties" in node else "null"
    if declared == "object":
        props = node.get("properties") or {}
        wanted = node.get("required") or list(props)
        out: dict[str, Any] = {}
        for key in wanted:
            if budget[0] <= 0:
                break  # spent: stop adding keys rather than adding nulls forever
            budget[0] -= len(str(key)) + 4  # every key costs, even one whose value is cut off
            out[key] = _example(root, props.get(key, {}), depth + 1, budget)
        return out
    if declared == "array":
        count = min(int(node.get("minItems", 0) or 0), MAX_SCHEMA_MIN_ITEMS)
        items: list[Any] = []
        for _ in range(count):
            if budget[0] <= 0:
                break
            budget[0] -= 2
            items.append(_example(root, node.get("items", {}), depth + 1, budget))
        return items
    if declared == "string":
        length = min(int(node.get("minLength", 0) or 0), MAX_SCHEMA_MIN_LENGTH, max(budget[0], 0))
        budget[0] -= length
        return "a" * length
    if declared in ("integer", "number"):
        low, high = node.get("minimum"), node.get("maximum")
        value = low if low is not None else (min(0, high) if high is not None else 0)
        return int(value) if declared == "integer" else value
    if declared == "boolean":
        return False
    return None


__all__ = [
    "AGENT_NAME_PATTERN",
    "DEFAULT_AGENT_MODEL",
    "MAX_DESCRIPTION_CHARS",
    "MAX_INSTRUCTIONS_CHARS",
    "MAX_KNOWLEDGE_COLLECTIONS",
    "MAX_OUTPUT_SCHEMA_CHARS",
    "MAX_SKILLS",
    "MAX_SPEC_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "MAX_TOOLS",
    "MAX_TOOL_CALLS_CAP",
    "MAX_TURNS_CAP",
    "AgentLimits",
    "AgentSpec",
    "AgentSpecError",
    "parse_agent_spec",
    "schema_example",
]
