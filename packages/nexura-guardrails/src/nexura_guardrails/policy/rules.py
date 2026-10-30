# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Policy rules: ``When`` (what a rule matches) and ``Rule`` (what it does).

Matching is deterministic: stage, agent name, tool name, tool origin, toolset, effect class
and argument values. Every non-empty field of ``When`` must match (AND); within a field any
pattern may match (OR). Name, origin, toolset and agent patterns are ``fnmatch`` globs,
matched case-sensitively.

Actions, from most to least severe: ``fail`` (stop the run), ``block`` (refuse the item and
tell the model, or trip for agent input and output), ``require_approval`` (a person approves
the tool call first), ``scrub`` (replace the text with a check's redaction) and ``allow``
(an allowlist entry; it never cancels another rule).
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from nexura_agents.tools import ToolInfo

__all__ = [
    "ACTIONS",
    "AGENT_STAGES",
    "EFFECTS",
    "ENFORCEMENT_MODES",
    "LOCAL_ORIGINS",
    "POLICY_STAGES",
    "SEVERITY",
    "TOOL_STAGES",
    "ArgCondition",
    "PolicyError",
    "Rule",
    "When",
]

PolicyStage = Literal["input", "model_input", "model_output", "tool_input", "tool_output", "output"]
Action = Literal["allow", "block", "scrub", "require_approval", "fail"]
Enforcement = Literal["enforce", "shadow"]

POLICY_STAGES: tuple[str, ...] = (
    "input",
    "model_input",
    "model_output",
    "tool_input",
    "tool_output",
    "output",
)
TOOL_STAGES = frozenset({"tool_input", "tool_output"})
AGENT_STAGES = frozenset({"input", "output"})
SCRUB_STAGES = frozenset({"model_output", "tool_output", "output"})
ACTIONS: tuple[str, ...] = ("allow", "block", "scrub", "require_approval", "fail")
#: ``enforce`` applies a rule's action; ``shadow`` evaluates and reports it, never applies it.
ENFORCEMENT_MODES: tuple[str, ...] = ("enforce", "shadow")
SEVERITY: Mapping[str, int] = {
    "allow": 0,
    "scrub": 1,
    "require_approval": 2,
    "block": 3,
    "fail": 4,
}
#: ``read`` is an alias of ``pure`` (a read-only module's effect policy).
EFFECTS: tuple[str, ...] = ("pure", "read", "idempotent", "non_idempotent", "reconcilable")
#: Origins whose tools a bare ``tools`` pattern of an ``allow`` rule may admit.
LOCAL_ORIGINS: tuple[str, ...] = ("python:", "builtin:", "agent:")
_RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_COMPARISONS = frozenset({"gt", "gte", "lt", "lte"})
_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "matches", "exists")
_SEGMENT = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)\]|\[\"([^\"]*)\"\]|\['([^']*)'\]")


class PolicyError(ValueError):
    """A policy that cannot be loaded or built; ``path`` names the offending field."""

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


def _strings(value: Any, what: str, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    items = (value,) if isinstance(value, str) else value
    if isinstance(items, (bytes, Mapping)) or not isinstance(items, Iterable):
        raise PolicyError(f"{what} must be a string or a list of strings.", path)
    out = tuple(items)
    if not all(isinstance(item, str) and item for item in out):
        raise PolicyError(f"every {what} must be a non-empty string.", path)
    return out


def _parse_path(raw: str, path: str) -> tuple[str | int, ...]:
    text = raw.strip()
    if text.startswith("$"):
        text = text[1:]
    if text and not text.startswith((".", "[")):
        text = "." + text
    segments: list[str | int] = []
    pos = 0
    while pos < len(text):
        match = _SEGMENT.match(text, pos)
        if match is None:
            raise PolicyError(
                f"argument path {raw!r} is not of the form $.name, $.name[0] or $['key'].", path
            )
        name, index, dq, sq = match.groups()
        if index is not None:
            segments.append(int(index))
        else:
            segments.append(name if name is not None else (dq if dq is not None else sq))
        pos = match.end()
    if not segments:
        raise PolicyError(f"argument path {raw!r} names no argument.", path)
    return tuple(segments)


_MISSING = object()


def _resolve(value: Any, segments: tuple[str | int, ...]) -> Any:
    for segment in segments:
        if isinstance(segment, int):
            if not isinstance(value, (list, tuple)) or segment >= len(value):
                return _MISSING
            value = value[segment]
        else:
            if not isinstance(value, Mapping) or segment not in value:
                return _MISSING
            value = value[segment]
    return value


Real = int | float | Decimal


def _number(value: Any) -> Real | None:
    """``value`` itself when it is a finite real number (``int``, ``float`` or ``Decimal``,
    never ``bool``), else None. Kept in its own type: ``Decimal`` compares exactly with ints
    and floats in Python, so nothing is rounded through ``float``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return value  # always finite; math.isfinite would overflow on a huge int
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class ArgCondition:
    """One argument test: ``ArgCondition("$.amount", {"gt": 100})``.

    Operators: ``eq``, ``ne``, ``gt``, ``gte``, ``lt``, ``lte`` (numbers only), ``in`` and
    ``not_in`` (a list), ``contains`` (a substring of a string or an item of a list),
    ``matches`` (a regular expression searched in a string) and ``exists`` (true or false).
    All operators of one condition must hold. A missing argument fails every operator
    except ``exists: false``, so a ``block`` rule on ``$.amount`` does not match a call
    without an amount: name such calls with another rule if they must be blocked too.
    """

    path: str
    ops: Mapping[str, Any]
    _segments: tuple[str | int, ...] = field(default=(), repr=False, compare=False)
    _pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        where = f"when.args[{self.path!r}]"
        if not isinstance(self.path, str) or not self.path.strip():
            raise PolicyError("an argument path must be a non-empty string.", where)
        if not isinstance(self.ops, Mapping) or not self.ops:
            raise PolicyError("an argument condition needs at least one operator.", where)
        unknown = sorted(set(self.ops) - set(_OPS))
        if unknown:
            raise PolicyError(f"unknown operators {unknown}; use {list(_OPS)}.", where)
        for op in ("gt", "gte", "lt", "lte"):
            if op in self.ops and _number(self.ops[op]) is None:
                raise PolicyError(f"'{op}' needs a number.", where)
        for op in ("in", "not_in"):
            if op in self.ops and not isinstance(self.ops[op], (list, tuple)):
                raise PolicyError(f"'{op}' needs a list.", where)
        if "exists" in self.ops and not isinstance(self.ops["exists"], bool):
            raise PolicyError("'exists' needs true or false.", where)
        pattern = None
        if "matches" in self.ops:
            if not isinstance(self.ops["matches"], str):
                raise PolicyError("'matches' needs a regular expression string.", where)
            try:
                pattern = re.compile(self.ops["matches"])
            except re.error as exc:
                raise PolicyError(
                    f"'matches' is not a valid regular expression: {exc}.", where
                ) from None
        object.__setattr__(self, "ops", dict(self.ops))
        object.__setattr__(self, "_segments", _parse_path(self.path, where))
        object.__setattr__(self, "_pattern", pattern)

    def holds(self, arguments: Any, *, restrictive: bool = True) -> bool:
        """True when every operator holds for the argument at ``path``.

        A comparison (``gt``, ``gte``, ``lt``, ``lte``) on a value that is present but is not
        a finite real number (a string, ``NaN``, an infinity, a boolean, ``null``, an object)
        cannot be decided: it counts as met for a ``restrictive`` rule (``block``, ``fail``,
        ``scrub``, ``require_approval``: fail closed) and as unmet for an ``allow`` rule.
        """
        value = _resolve(arguments, self._segments)
        for op, expected in self.ops.items():
            if op == "exists":
                if (value is not _MISSING) != expected:
                    return False
                continue
            if value is _MISSING:
                return False
            if op in _COMPARISONS and _number(value) is None:
                if restrictive:
                    continue
                return False
            if not _apply(op, value, expected, self._pattern):
                return False
        return True


def _apply(op: str, value: Any, expected: Any, pattern: re.Pattern[str] | None) -> bool:
    if op == "eq":
        if isinstance(value, bool) or isinstance(expected, bool):
            # True is not 1: booleans equal only booleans.
            return isinstance(value, bool) and isinstance(expected, bool) and value is expected
        return bool(value == expected)
    if op == "ne":
        return not _apply("eq", value, expected, pattern)
    if op in ("gt", "gte", "lt", "lte"):
        left, right = _number(value), _number(expected)
        if left is None or right is None:
            return False
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[op]
    if op == "in":
        return any(_apply("eq", value, item, None) for item in expected)
    if op == "not_in":
        return not any(_apply("eq", value, item, None) for item in expected)
    if op == "contains":
        if isinstance(value, str):
            return isinstance(expected, str) and expected in value
        if isinstance(value, (list, tuple)):
            return any(_apply("eq", item, expected, None) for item in value)
        return False
    if op == "matches":
        return isinstance(value, str) and pattern is not None and pattern.search(value) is not None
    return False  # pragma: no cover - operators are validated


def _conditions(value: Any, path: str) -> tuple[ArgCondition, ...]:
    if value is None:
        return ()
    if isinstance(value, ArgCondition):
        return (value,)
    if isinstance(value, Mapping):
        out = []
        for key, ops in value.items():
            if not isinstance(key, str):
                raise PolicyError("argument paths must be strings.", path)
            if not isinstance(ops, Mapping):
                raise PolicyError(
                    f"the condition for {key!r} must be an object such as {{gt: 100}}.", path
                )
            out.append(ArgCondition(key, ops))
        return tuple(out)
    if isinstance(value, (list, tuple)) and all(isinstance(c, ArgCondition) for c in value):
        return tuple(value)
    raise PolicyError("args must map argument paths to conditions, e.g. {$.amount: {gt: 100}}.")


@dataclass(frozen=True, init=False)
class When:
    """What a rule matches. Every given field must match; see the module docstring.

    ``stage``: one or more of ``input``, ``model_input``, ``model_output``, ``tool_input``,
    ``tool_output`` and ``output`` (default: all). ``tools``, ``origin``, ``toolset``,
    ``effect`` and ``args`` filter tool calls, so a rule that uses them may only name tool
    stages. ``tools`` globs match the tool name; ``origin`` globs match ``ToolInfo.origin``
    (``mcp:<server>:<tool>``, ``openapi:<METHOD>:<path>``, ``python:...``); ``toolset``
    globs match the names of ``toolset:<name>`` tags; ``effect`` lists effect classes
    (``read`` means ``pure``); ``agents`` globs match the agent name; ``args`` maps argument
    paths to conditions (``{"$.amount": {"gt": 100}}``).
    """

    stage: tuple[str, ...]
    tools: tuple[str, ...]
    origin: tuple[str, ...]
    toolset: tuple[str, ...]
    effect: tuple[str, ...]
    agents: tuple[str, ...]
    args: tuple[ArgCondition, ...]

    def __init__(
        self,
        stage: str | Iterable[str] | None = None,
        *,
        tools: str | Iterable[str] | None = None,
        origin: str | Iterable[str] | None = None,
        toolset: str | Iterable[str] | None = None,
        effect: str | Iterable[str] | None = None,
        agents: str | Iterable[str] | None = None,
        args: Mapping[str, Mapping[str, Any]] | Iterable[ArgCondition] | None = None,
    ) -> None:
        stages = _strings(stage, "stage", "when.stage") or POLICY_STAGES
        bad = sorted(set(stages) - set(POLICY_STAGES))
        if bad:
            raise PolicyError(f"unknown stages {bad}; use {list(POLICY_STAGES)}.", "when.stage")
        effects = _strings(effect, "effect", "when.effect")
        bad = sorted(set(effects) - set(EFFECTS))
        if bad:
            raise PolicyError(f"unknown effect classes {bad}; use {list(EFFECTS)}.", "when.effect")
        values = {
            "stage": tuple(dict.fromkeys(stages)),
            "tools": _strings(tools, "tool pattern", "when.tools"),
            "origin": _strings(origin, "origin pattern", "when.origin"),
            "toolset": _strings(toolset, "toolset pattern", "when.toolset"),
            "effect": tuple(dict.fromkeys("pure" if e == "read" else e for e in effects)),
            "agents": _strings(agents, "agent pattern", "when.agents"),
            "args": _conditions(args, "when.args"),
        }
        for key, value in values.items():
            object.__setattr__(self, key, value)
        if self.filters_tools and not set(self.stage) <= TOOL_STAGES:
            raise PolicyError(
                "tools, origin, toolset, effect and args only apply to tool calls; name only "
                "tool_input and tool_output in stage.",
                "when",
            )

    @property
    def filters_tools(self) -> bool:
        """True when the rule filters on tool facts (so it only matches tool stages)."""
        return bool(self.tools or self.origin or self.toolset or self.effect or self.args)

    def matches(
        self,
        stage: str,
        *,
        agent: str | None = None,
        tool: ToolInfo | None = None,
        arguments: Any = None,
        allow_rule: bool = False,
        ignore_args: bool = False,
    ) -> bool:
        """True when this matches ``stage`` for ``agent`` and (on tool stages) ``tool``.

        For an ``allow`` rule (``allow_rule``), ``tools`` patterns without an ``origin``
        filter admit only local tools (no origin, ``python:``, ``builtin:`` or ``agent:``):
        an allowlist entry ``lookup`` never admits an MCP or OpenAPI tool of that name.
        """
        if stage not in self.stage:
            return False
        if self.agents and not any(fnmatch.fnmatchcase(agent or "", p) for p in self.agents):
            return False
        if not self.filters_tools:
            return True
        if tool is None:
            return False
        origin = tool.origin
        if self.tools:
            if not any(fnmatch.fnmatchcase(tool.name, p) for p in self.tools):
                return False
            local = origin is None or origin.startswith(LOCAL_ORIGINS)
            if allow_rule and not self.origin and not local:
                return False
        if self.origin and (
            origin is None or not any(fnmatch.fnmatchcase(origin, p) for p in self.origin)
        ):
            return False
        if self.toolset and not any(
            fnmatch.fnmatchcase(name, p) for name in tool.toolsets for p in self.toolset
        ):
            return False
        effect = "pure" if tool.effect == "read" else tool.effect
        if self.effect and effect not in self.effect:
            return False
        return ignore_args or all(
            condition.holds(arguments, restrictive=not allow_rule) for condition in self.args
        )

    def describe(self) -> dict[str, Any]:
        """A JSON-safe summary (for the service API and the digest)."""
        out: dict[str, Any] = {"stage": list(self.stage)}
        for key in ("tools", "origin", "toolset", "effect", "agents"):
            value = getattr(self, key)
            if value:
                out[key] = list(value)
        if self.args:
            out["args"] = {c.path: dict(c.ops) for c in self.args}
        return out


@dataclass(frozen=True, init=False)
class Rule:
    """One policy rule: ``Rule("gate-writes", When(stage="tool_input", effect="non_idempotent"),
    action="require_approval")``.

    ``check``/``checks`` name checks of the policy. Without checks the action applies to
    every matching item; with checks it applies when they do not allow the text (``mode``
    ``"tiered"``, the default, runs them in order and stops at the first finding;
    ``"parallel"`` runs them together). ``on_error`` (``"block"`` or ``"allow"``) overrides
    the policy's for this rule's checks. ``enforcement`` (``"enforce"`` or ``"shadow"``)
    overrides the policy's ``mode`` for this rule: a shadow rule is evaluated and reported
    (audit, decision events) but its action is never applied. ``None`` (the default) follows
    the policy. (``mode`` is taken: it says how a rule's checks run.)

    Constraints, checked here: ``scrub`` needs checks and only applies to
    ``model_output``, ``tool_output`` and ``output``; ``require_approval`` applies only to
    ``tool_input`` and takes no checks (an approval gate must be deterministic, it is
    evaluated again on resume and recovery); ``allow`` takes no checks.
    """

    id: str
    when: When
    action: str
    checks: tuple[str, ...]
    mode: str
    on_error: str | None
    enforcement: str | None

    def __init__(
        self,
        id: str,  # noqa: A002 - the rule's id, as in the policy file
        when: When | None = None,
        *,
        action: Action = "block",
        check: str | Iterable[str] | None = None,
        checks: str | Iterable[str] | None = None,
        mode: Literal["tiered", "parallel"] = "tiered",
        on_error: Literal["block", "allow"] | None = None,
        enforcement: Enforcement | None = None,
    ) -> None:
        if not isinstance(id, str) or not _RULE_ID.fullmatch(id):
            raise PolicyError(
                "a rule id must be 1-64 letters, digits, '_', '.', ':' or '-', starting with a "
                "letter or digit.",
                "id",
            )
        where = f"rules[{id}]"
        if when is None:
            when = When()
        if not isinstance(when, When):
            raise PolicyError("when must be a When.", where)
        if action not in ACTIONS:
            raise PolicyError(f"unknown action {action!r}; use {list(ACTIONS)}.", where)
        if check is not None and checks is not None:
            raise PolicyError("give check or checks, not both.", where)
        names = _strings(checks if checks is not None else check, "check name", where)
        if len(set(names)) != len(names):
            raise PolicyError("a check is named twice.", where)
        if mode in ENFORCEMENT_MODES:
            raise PolicyError(
                f"a rule's mode says how its checks run ('tiered' or 'parallel'); for a "
                f"{mode} rule use enforcement: {mode} (Rule(..., enforcement={mode!r})).",
                where,
            )
        if mode not in ("tiered", "parallel"):
            raise PolicyError("mode must be 'tiered' or 'parallel'.", where)
        if enforcement is not None and enforcement not in ENFORCEMENT_MODES:
            raise PolicyError(
                f"unknown enforcement {enforcement!r}; use 'enforce' or 'shadow'.", where
            )
        if on_error not in (None, "block", "allow"):
            raise PolicyError("on_error must be 'block' or 'allow'.", where)
        stages = set(when.stage)
        if action == "scrub":
            if not names:
                raise PolicyError(
                    "a scrub rule needs a check that redacts (for example pii).", where
                )
            if not stages <= SCRUB_STAGES:
                raise PolicyError(
                    "scrub applies only to model_output, tool_output and output (input and "
                    "tool arguments cannot be rewritten).",
                    where,
                )
        if action == "require_approval":
            if stages != {"tool_input"}:
                raise PolicyError("require_approval applies only to stage tool_input.", where)
            if names:
                raise PolicyError(
                    "require_approval takes no checks: an approval gate must be deterministic "
                    "(it is evaluated again on resume and recovery). Use when.tools, origin, "
                    "effect or args instead.",
                    where,
                )
        if action == "allow" and names:
            raise PolicyError(
                "allow takes no checks: it is an allowlist entry and never cancels another rule.",
                where,
            )
        for key, value in (
            ("id", id),
            ("when", when),
            ("action", action),
            ("checks", names),
            ("mode", mode),
            ("on_error", on_error),
            ("enforcement", enforcement),
        ):
            object.__setattr__(self, key, value)

    @property
    def severity(self) -> int:
        """The action's rank (``fail`` 4 ... ``allow`` 0)."""
        return SEVERITY[self.action]

    def describe(self) -> dict[str, Any]:
        """A JSON-safe summary (for the service API and the digest)."""
        out: dict[str, Any] = {
            "id": self.id,
            "when": self.when.describe(),
            "action": self.action,
            "checks": list(self.checks),
        }
        if len(self.checks) > 1:
            out["mode"] = self.mode
        if self.on_error is not None:
            out["on_error"] = self.on_error
        if self.enforcement is not None:
            out["enforcement"] = self.enforcement  # only when set: older digests stay the same
        return out
