# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Portable, data-only recordings and offline failure replay.

Record with :class:`ReproRecorder` before exporting. Ordinary checkpoints do not
contain historical model responses or tool outputs and cannot invent them.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import hashlib
import inspect
import json
import os
import re
import socket
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from functools import wraps
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import quote, quote_plus, unquote_plus

from nexura_agents.network.redaction import redact_secret_text
from nexura_agents.tools import StopAtTools

__all__ = [
    "ReproBundle",
    "ReplayResult",
    "ReproRecorder",
    "export_repro_bundle",
    "replay_repro_bundle",
    "offline_guard",
    "register_workflow_replay",
]


def _json(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    # Fail closed on functions, handles, credentials objects, and arbitrary reprs.
    return json.loads(json.dumps(value, allow_nan=False))


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _error(exc: BaseException) -> dict[str, Any]:
    data: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    original = getattr(exc, "original_error", None)
    if original is not None:
        data["original"] = _error(original)
    return data


def _portable(value: Any) -> Any:
    """Canonical JSON form of a completed output, or a marker for values JSON cannot hold."""
    try:
        return _json(value)
    except (TypeError, ValueError, RecursionError):
        return {"$unserializable": type(value).__name__}


def _outcome(
    result: Any = None, exc: BaseException | None = None, kind: str = "agent"
) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "status": "failed" if exc else getattr(result, "status", "completed"),
        "error_type": type(exc).__name__ if exc else None,
        "error_message": str(exc) if exc else None,
        "failing_step": (getattr(exc, "node_id", None) or getattr(exc, "tool_name", None))
        if exc
        else None,
    }
    # The completed output is part of the outcome: a replay that finishes with a different
    # answer (agents) or final state (workflows) did not reproduce the run.
    # A run that raised has neither (recorded as null).
    if kind == "workflow":
        outcome["state"] = None if result is None else _portable(result.state)
    else:
        outcome["output"] = None if result is None else _portable(getattr(result, "output", None))
    return outcome


# Outcome fields compared beyond status/error/step, each stored with a canonical digest.
_COMPARED_RESULTS = ("output", "state")


def _seal_outcome(outcome: dict[str, Any]) -> None:
    """Store the canonical-JSON digest of each recorded result (after redaction)."""
    for key in _COMPARED_RESULTS:
        if key in outcome:
            outcome[f"{key}_digest"] = _digest(outcome[key])


def _show(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "..."


def _first_difference(actual: Any, expected: Any, path: str = "$") -> str | None:
    """Path and values (replayed != recorded) of the first difference, in key order."""
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key in sorted(set(actual) | set(expected)):
            step = (
                f"{path}.{key}"
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                else f"{path}[{json.dumps(key, ensure_ascii=False)}]"
            )
            if key not in actual:
                return f"{step}: missing != {_show(expected[key])}"
            if key not in expected:
                return f"{step}: {_show(actual[key])} != missing"
            found = _first_difference(actual[key], expected[key], step)
            if found:
                return found
        return None
    if isinstance(actual, list) and isinstance(expected, list):
        for index, (left, right) in enumerate(zip(actual, expected, strict=False)):
            found = _first_difference(left, right, f"{path}[{index}]")
            if found:
                return found
        if len(actual) != len(expected):
            return f"{path}: length {len(actual)} != {len(expected)}"
        return None
    # bool is an int in Python; true != 1 in JSON.
    if actual != expected or type(actual) is not type(expected):
        return f"{path}: {_show(actual)} != {_show(expected)}"
    return None


def _scrub(data: Any, credential_values: dict[str, str]) -> Any:
    """Strip refs before resolution, like WorkflowExporter's credential-ref export.

    Discover sensitive fields first so copies in prompts/errors are scrubbed too. Every
    string kept (error messages, tool outputs, model text) also goes through
    ``redact_secret_text``, the shared free-text redaction.
    Values resolved from refs must be supplied by the recorder's owner; environment
    refs are discovered without enumerating unrelated environment variables.
    """
    known: set[str] = {v for v in credential_values.values() if v}
    sensitive = re.compile(
        r"(?i)(api.?key|api.?token|auth.?token|authorization|cookie|password|passwd|"
        r"secret|credential|access.?token|refresh.?token|private.?key|private.?token|"
        r"bearer.?token|session.?token|id.?token|ocp.apim.subscription.key)"
    )
    header = re.compile(
        r"(?i)^(authorization|proxy-authorization|cookie|set-cookie|x-auth-token|"
        r"x-api-key|x-api-token|private-token|ocp-apim-subscription-key)$"
    )

    def secret_shape(item: Any) -> bool:
        if isinstance(item, dict):
            return any(
                sensitive.search(k) or k.lower() == "pass" or secret_shape(v)
                for k, v in item.items()
            )
        if isinstance(item, list):
            return any(secret_shape(v) for v in item)
        return isinstance(item, str) and bool(
            re.search(r"(?i)\b(?:Bearer|Basic|Token)\s+\S+|\bsk-\S+", item)
        )

    def secret_field(k: str, item: Any) -> bool:
        return bool(sensitive.search(k)) or (
            k.lower() in {"key", "token", "auth"} and secret_shape(item)
        )

    def parsed(v: str) -> Any:
        if v.lstrip().startswith(("{", "[", '"')):
            try:
                return json.loads(v)
            except (ValueError, RecursionError):
                pass
        return None

    def learn(v: Any) -> None:
        if isinstance(v, dict):
            for item in v.values():
                learn(item)
        elif isinstance(v, list):
            for item in v:
                learn(item)
        elif v is not None:
            known.add(str(v))
            known.add(json.dumps(v, ensure_ascii=False))

    def discover(v: Any, *, properties: bool = False) -> None:
        if isinstance(v, dict):
            if isinstance(v.get("ref"), str):
                ref = v["ref"]
                if os.environ.get(ref):
                    learn(os.environ[ref])
                if "value" in v:
                    learn(v["value"])
            for k, item in v.items():
                if not properties and secret_field(k, item) and item != {"$redacted": k}:
                    learn(item)
                discover(item, properties=k == "properties")
        elif isinstance(v, list):
            if (
                len(v) == 2
                and isinstance(v[0], str)
                and header.fullmatch(v[0])
                and v[1] != {"$redacted": v[0]}
            ):
                learn(v[1])
            for item in v:
                discover(item)
        elif isinstance(v, str):
            nested = parsed(v)
            if nested is not None:
                discover(nested)

    discover(data)

    copies = set(known)
    for secret in known:
        copies.update(
            (
                quote(secret, safe=""),
                quote_plus(secret),
                base64.b64encode(secret.encode()).decode(),
                base64.urlsafe_b64encode(secret.encode()).decode(),
            )
        )

    def clean(v: Any, *, properties: bool = False) -> Any:
        if isinstance(v, dict):
            if isinstance(v.get("ref"), str):
                return {"$redacted": clean(v["ref"])}
            return {
                redact_secret_text(k): {"$redacted": redact_secret_text(k)}
                if not properties and secret_field(k, item) and item is not None
                else clean(item, properties=k == "properties")
                for k, item in v.items()
            }
        if isinstance(v, list):
            if len(v) == 2 and isinstance(v[0], str) and header.fullmatch(v[0]):
                return [v[0], {"$redacted": v[0]}]
            return [clean(item) for item in v]
        if isinstance(v, str):
            nested = parsed(v)
            if nested is not None:
                return redact_secret_text(json.dumps(clean(nested), ensure_ascii=False))

            # Decode base64 tokens as well, including Basic's arbitrary user:secret prefix.
            def encoded(match: re.Match[str]) -> str:
                token = match.group()
                try:
                    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
                except (ValueError, UnicodeError):
                    return token
                return "[REDACTED]" if any(s in decoded for s in known) else token

            v = re.sub(r"[A-Za-z0-9_+/-]{8,}={0,2}", encoded, v)
            for secret in sorted(copies, key=len, reverse=True):
                if secret:
                    v = v.replace(secret, "[REDACTED]")
            # Percent-encoding is case insensitive.
            if any(s in unquote_plus(v) for s in known):
                v = "[REDACTED]"
            v = re.sub(
                r"(?i)\b(?:[\w-]*authorization|(?:set-)?cookie|x-auth-token|x-api-key|"
                r"x-api-token|private-token|ocp-apim-subscription-key)\s*:\s*[^\r\n]+",
                "[REDACTED]",
                v,
            )
            v = re.sub(r"(?i)\b(?:Bearer|Basic)\s+\S+|\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", v)
            # The product's free-text redaction (run events, exports): labelled pairs in
            # JSON / Python-repr / key=value form, credential query parameters, token formats.
            return redact_secret_text(v)
        if v is not None and str(v) in known:
            return "[REDACTED]"
        return v

    return clean(data)


@dataclasses.dataclass
class ReproBundle:
    """One versioned JSON document. Loading never imports or executes bundle code."""

    data: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.data, dict):
            raise ValueError("Repro bundle must be a JSON object")
        if type(self.data.get("schema_version")) is not int or self.data["schema_version"] != 1:
            raise ValueError("Unsupported repro bundle schema_version")
        if self.data.get("run_kind") not in ("agent", "workflow"):
            raise ValueError("Unsupported repro run_kind")
        for key in (
            "inputs",
            "spec",
            "model_responses",
            "tool_fixtures",
            "approvals",
            "budget_config",
            "expected_outcome",
            "operations",
        ):
            if key not in self.data:
                raise ValueError(f"Incomplete repro recording: missing {key}")
        if not isinstance(self.data["expected_outcome"], dict) or not {
            "status",
            "error_type",
            "error_message",
            "failing_step",
        }.issubset(self.data["expected_outcome"]):
            raise ValueError("Incomplete repro recording: missing expected outcome")

    def save(self, path: str | Path) -> None:
        """Use best-effort redaction of known secret shapes; review a bundle before sharing it."""
        data = _scrub(_json(self.data), {})
        # Hash the portable arguments, never the pre-redaction secrets.
        for fixture in data["tool_fixtures"]:
            fixture["args_digest"] = _digest(fixture["arguments"])
        _seal_outcome(data["expected_outcome"])
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> ReproBundle:
        """Read a supported bundle; reject schema mismatches before execution."""
        return cls(json.loads(Path(path).read_text()))


@dataclasses.dataclass(frozen=True)
class ReplayResult:
    """Outcome comparison and the first observed divergence, if any.

    A different completed output or workflow final state is reported as
    ``"output differs at $.amount: 500 != 5000"`` (replayed value first, then the
    recorded one). ``notes`` says what could not be compared, for example an older
    bundle that did not record the output.
    """

    reproduced: bool
    outcome: dict[str, Any]
    first_divergence: str | None = None
    notes: tuple[str, ...] = ()


class ReproRecorder:
    """Opt-in recorder for Runner and WorkflowEngine, including approval resumes.

    Use one recorder per run. The original agent, model and tools are not mutated.
    Persist ``export_repro_bundle(recorder).data`` in RunRecord.metadata['repro']
    to make it available to the runtime CLI's stored-run lookup.
    """

    def __init__(self, *, credential_values: dict[str, str] | None = None) -> None:
        self.credential_values = credential_values or {}
        self.data: dict[str, Any] = {}
        self._target: Any = None
        self._executor: Any = None

    def _start(self, kind: str, inputs: Any, spec: Any, budget: Any) -> None:
        if self.data:
            raise ValueError("Use a fresh ReproRecorder for each run")
        self.data = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "framework_version": version("nexura-agents"),
            "run_kind": kind,
            "inputs": _json(inputs),
            "spec": _json(spec),
            "model_responses": [],
            "tool_fixtures": [],
            "approvals": [],
            "operations": ["run"],
            "budget_config": _json(budget.config) if budget is not None else None,
            "expected_outcome": {},
        }

    async def _observe(self, name: str, arguments: Any, call: Any) -> Any:
        fixture = {"name": name, "arguments": _json(arguments)}
        self.data["tool_fixtures"].append(fixture)
        try:
            result = await call()
            fixture["output"] = _json(result)
            return result
        except BaseException as exc:
            fixture["error"] = _error(exc)
            raise

    async def _finish(self, call: Any) -> Any:
        try:
            result = await call()
            self.data["expected_outcome"] = _outcome(result, kind=self.data["run_kind"])
            return result
        except BaseException as exc:
            self.data["expected_outcome"] = _outcome(exc=exc, kind=self.data["run_kind"])
            raise

    async def _observe_tagged(self, agent_name: str, name: str, arguments: Any, call: Any) -> Any:
        fixture = {"agent": agent_name, "name": name, "arguments": _json(arguments)}
        self.data["tool_fixtures"].append(fixture)
        try:
            result = call()
            if inspect.isawaitable(result):
                result = await result
            fixture["output"] = _json(result)
            return result
        except BaseException as exc:
            fixture["error"] = _error(exc)
            raise

    async def run_agent(self, runner: Any, agent: Any, prompt: str) -> Any:
        """Record a Runner run, including handoffs; exceptions keep their normal behavior.

        Every agent reachable through handoffs is recorded: its spec, its model responses
        and its tool fixtures are tagged with the agent's name, so replay gives each agent
        its own scripted replies and fixtures. Agent names must be unique in the graph.
        """
        graph = _agent_graph(agent)
        for item in graph:
            if not isinstance(item.instructions, str):
                raise ValueError(
                    f"Repro recording needs static instructions; agent '{item.name}' "
                    "has an instructions function"
                )
        specs = {item.name: _agent_spec(item) for item in graph}
        spec = dict(specs[agent.name])
        if len(graph) > 1:
            spec["agents"] = {name: s for name, s in specs.items() if name != agent.name}
        self._start("agent", prompt, spec, runner.budget)
        self.data["usage_limits"] = _json(runner.usage_limits)
        if getattr(runner, "max_handoffs", None) is not None:
            self.data["runner_max_handoffs"] = runner.max_handoffs
        clones: dict[str, Any] = {}
        for item in graph:
            tools = []
            for original in item.tools:

                async def execute(
                    arguments: Any, ctx: Any = None, tool: Any = original, owner: Any = item
                ) -> Any:
                    return await self._observe_tagged(
                        owner.name, tool.name, arguments, lambda: tool.execute(arguments, ctx)
                    )

                # Preserve the tool's validation/approval contract; execute records output.
                wrapped = copy.copy(original)
                wrapped.execute = execute
                tools.append(wrapped)
            clones[item.name] = item.clone(
                model=_RecordingModel(self, item), tools=tools, handoffs=()
            )
        for item in graph:
            handoffs = []
            for original_handoff in item.handoffs:
                handoffs.append(self._recording_handoff(item, original_handoff, clones))
            if handoffs:
                clones[item.name].add_handoffs(*handoffs)
        self._target = clones[agent.name]
        self._executor = runner
        return await self._finish(lambda: runner.run_async(self._target, prompt))

    def _recording_handoff(self, source: Any, original: Any, clones: dict[str, Any]) -> Any:
        from nexura_agents.handoffs import Handoff

        callback = original.on_handoff
        on_handoff = None
        if callback is not None:

            async def on_handoff(ctx: Any, value: Any) -> Any:
                # Keyed by the tool call id, which the scripted replies reproduce exactly.
                return await self._observe_tagged(
                    source.name,
                    f"on_handoff:{original.tool_name}",
                    {"tool_call_id": ctx.tool_call_id},
                    lambda: callback(ctx, value),
                )

        wrapped = Handoff(
            clones[original.agent.name],
            tool_name=original.tool_name,
            description=original.description,
            input_filter=original.input_filter,
            on_handoff=on_handoff,
            input_type=original.input_type,
        )
        wrapped.tool.enabled = original.tool.enabled
        wrapped.enabled = original.enabled
        return wrapped

    def _wrap_engine(self, engine: Any) -> Any:
        executor = copy.copy(engine)
        original = engine._run_step

        def gate(node: Any, state: Any) -> bool:
            needed = engine._write_operation_gated(node, state)
            self.data["workflow_gates"].append({"node": node.id, "needed": needed})
            return bool(needed)

        async def step(node: Any, handler: Any, state: Any, *args: Any) -> Any:
            return await self._observe(
                node.id, state, lambda: original(node, handler, state, *args)
            )

        executor._run_step = step
        executor._write_operation_gated = gate
        return executor

    async def run_workflow(
        self, engine: Any, initial_state: dict[str, Any], **run_kwargs: Any
    ) -> Any:
        """Record node boundary results, including composite-node fixtures.

        ``run_kwargs`` (for example ``workflow_run_id``) pass through to ``engine.run``.
        """
        self._start("workflow", initial_state, engine.spec.to_dict(), engine.budget)
        self.data["max_steps"] = engine.max_steps
        self.data["approval_required_handlers"] = sorted(engine.approval_required_handlers)
        self.data["workflow_gates"] = []
        self._executor = self._wrap_engine(engine)
        return await self._finish(lambda: self._executor.run(initial_state, **run_kwargs))

    async def resume_agent(
        self, interruption: Any, *, approve: bool = False, reject: dict[str, str] | None = None
    ) -> Any:
        """Record an approval or rejection; normal Runner approval checks still apply."""
        self.data["operations"].append("resume")
        self.data["approvals"].append({"approve": approve, "reject": reject})
        return await self._finish(
            lambda: self._executor.resume_async(
                self._target, interruption, approve=approve, reject=reject
            )
        )

    async def resume_workflow(
        self,
        run_id: str,
        approval_id: str,
        *,
        decision: str = "approve",
        state_update: dict[str, Any] | None = None,
        engine: Any = None,
    ) -> Any:
        """Record a workflow approval decision without retaining authority to act.

        ``engine`` continues the recording on a fresh engine for the same run (a
        service builds one per request); by default the recording engine is reused.
        """
        self.data["operations"].append("resume")
        recorded: dict[str, Any] = {"decision": decision}
        if state_update is not None:
            recorded["state_update"] = _json(state_update)
        self.data["approvals"].append(recorded)
        if engine is not None:
            self._executor = self._wrap_engine(engine)
        extra = {"state_update": state_update} if state_update is not None else {}
        return await self._finish(
            lambda: self._executor.resume(run_id, approval_id, decision=decision, **extra)
        )


def _agent_graph(start: Any) -> list[Any]:
    """Every agent reachable through handoffs, start first; names must be unique."""
    graph = [start]
    seen = {id(start): start}
    names = {start.name: start}
    index = 0
    while index < len(graph):
        for item in graph[index].handoffs:
            target = item.agent
            if id(target) in seen:
                continue
            if target.name in names:
                raise ValueError(
                    f"Repro recording needs unique agent names; two agents are named "
                    f"'{target.name}'"
                )
            seen[id(target)] = target
            names[target.name] = target
            graph.append(target)
        index += 1
    return graph


def _agent_spec(agent: Any) -> dict[str, Any]:
    output_schema: Any = agent.output_type
    if isinstance(output_schema, type):
        output_schema = cast(Any, output_schema).model_json_schema()
    spec: dict[str, Any] = {
        "name": agent.name,
        "instructions": agent.instructions,
        "max_turns": agent.max_turns,
        "max_tool_calls": agent.max_tool_calls,
        "output_type": output_schema,
        "output_retries": agent.output_retries,
        "tool_use_behavior": agent.tool_use_behavior
        if isinstance(agent.tool_use_behavior, str)
        else {"stop_at_tools": sorted(agent.tool_use_behavior.tool_names)},
        "tools": [
            dict(
                schema=t.to_schema(),
                requires_approval=bool(t.requires_approval),
                max_retries=t.max_retries,
                timeout=t.timeout,
                on_timeout=t.on_timeout,
                cache=t.cache,
                idempotent=t.idempotent,
                is_async=t.is_async,
                retry=None
                if t.retry is None
                else dict(
                    max_attempts=t.retry.max_attempts,
                    initial_delay=t.retry.initial_delay,
                    backoff=t.retry.backoff,
                    max_delay=t.retry.max_delay,
                    retry_on=[c.__name__ for c in t.retry.retry_on],
                ),
            )
            for t in agent.tools
        ],
    }
    if agent.handoffs:
        spec["max_handoffs"] = agent.max_handoffs
        spec["handoffs"] = [
            {
                "agent": h.agent.name,
                "tool_name": h.tool_name,
                "description": h.description,
                "input_schema": h.input_type.model_json_schema() if h.input_type else None,
                "on_handoff": h.on_handoff is not None,
                "input_filter": h.input_filter is not None,
            }
            for h in agent.handoffs
        ]
    return spec


class _RecordingModel:
    """Delegates to an agent's model and records each response, tagged with the agent."""

    def __init__(self, recorder: ReproRecorder, agent: Any) -> None:
        from nexura_agents.models.resilience import _accepts_run_context

        self._model = agent.model
        self.accepts_run_context = _accepts_run_context(agent.model)

        @wraps(agent.model.generate)
        async def generate(*args: Any, **kwargs: Any) -> Any:
            try:
                response = await agent.model.generate(*args, **kwargs)
            except BaseException as exc:
                recorder.data["model_responses"].append({"agent": agent.name, "error": _error(exc)})
                raise
            recorded = _json(response)
            recorded["agent"] = agent.name
            recorder.data["model_responses"].append(recorded)
            return response

        self.generate = generate

    def __getattr__(self, name: str) -> Any:
        if name == "_model":  # not yet set (copy/pickle): never recurse
            raise AttributeError(name)
        return getattr(self._model, name)


def export_repro_bundle(
    run_record_or_checkpoint: Any, *, credential_values: dict[str, str] | None = None
) -> ReproBundle:
    """Export a complete recording or RunRecord with metadata['repro'].

    Incomplete historical checkpoints are refused rather than fabricating fixtures.
    Supply resolved credential values by reference name to scrub copies in free text.
    """
    source = run_record_or_checkpoint
    known = dict(credential_values or {})
    if isinstance(source, ReproRecorder):
        known.update(source.credential_values)
        source = source.data
    elif isinstance(source, ReproBundle):
        source = source.data
    elif hasattr(source, "repro"):
        source = source.repro
    elif hasattr(source, "metadata"):
        source = source.metadata.get("repro", {})
    elif isinstance(source, dict) and "metadata" in source:
        source = source["metadata"].get("repro", {})
    elif isinstance(source, dict) and "repro" in source:
        source = source["repro"]
    data = _scrub(_json(source), known)
    bundle = ReproBundle(data)
    for fixture in data["tool_fixtures"]:
        fixture["args_digest"] = _digest(fixture["arguments"])
    _seal_outcome(data["expected_outcome"])
    # A separate fingerprint detects accidental fixture edits without trusting outcome alone.
    data["recording_digest"] = _digest({k: v for k, v in data.items() if k != "recording_digest"})
    return bundle


@contextmanager
def offline_guard() -> Any:
    """Block connect/connect_ex/sendto/sendmsg and socket resolver entry points.

    Process-wide for the duration: run replay in a dedicated process when other
    application tasks need network access. Blocks socket create_connection,
    gethostbyname/gethostbyname_ex/gethostbyaddr/getaddrinfo/getnameinfo and the
    active asyncio loop's getaddrinfo/getnameinfo. sendmsg is patched if present.
    Direct _socket APIs, send/sendall, subprocesses and other loops are not guarded.
    No subprocess or plugin code is loaded from a bundle.
    """

    attempts: list[str] = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        attempts.append("socket/DNS operation")
        raise RuntimeError("Network access blocked during repro replay")

    with ExitStack() as stack:
        for name in ("connect", "connect_ex", "sendto", "sendmsg"):
            if hasattr(socket.socket, name):
                stack.enter_context(patch.object(socket.socket, name, blocked))
        for name in (
            "gethostbyname",
            "gethostbyname_ex",
            "gethostbyaddr",
            "getaddrinfo",
            "getnameinfo",
            "create_connection",
        ):
            stack.enter_context(patch.object(socket, name, blocked))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            for name in ("getaddrinfo", "getnameinfo"):
                stack.enter_context(patch.object(loop, name, blocked))
        yield attempts


WorkflowReplay = Callable[..., Awaitable[Any]]
_workflow_replay: WorkflowReplay | None = None


def register_workflow_replay(replay: WorkflowReplay) -> None:
    """Install the workflow replayer (the runtime package registers it on import).

    The SDK does not depend on the workflow runtime; workflow bundles replay only
    when ``nexura_runtime`` is installed and imported.
    """
    global _workflow_replay
    _workflow_replay = replay


class _Divergence(BaseException):
    pass


_ERROR_TYPES: dict[str, type[BaseException]] = {
    "TimeoutError": TimeoutError,
    "ConnectionError": ConnectionError,
    "CancelledError": asyncio.CancelledError,
}


def _error_type(name: str) -> type[BaseException]:
    # Synthesized names share identity for retry matching, never imported from JSON.
    if name not in _ERROR_TYPES:
        _ERROR_TYPES[name] = type(name, (Exception,), {})
    return _ERROR_TYPES[name]


def _recorded_error(data: dict[str, Any], *, tool: bool = False, name: str = "") -> BaseException:
    from nexura_agents.errors import ToolError, ToolExecutionError

    # A name and message only: never import a class named by untrusted JSON.
    if tool and data["type"] == "ToolExecutionError" and "original" in data:
        return ToolExecutionError(
            tool_name=name, original_error=cast(Exception, _recorded_error(data["original"]))
        )
    cls = type(data["type"], (ToolError,), {}) if tool else _error_type(data["type"])
    return cast(BaseException, cls(data["message"]))


def _replay_agents(
    data: dict[str, Any], consume: Callable[..., Any], next_response: Callable[[str], Any]
) -> Any:
    """Rebuild the recorded agent graph with scripted models and inert fixture tools."""
    from pydantic import BaseModel, ConfigDict

    from nexura_agents import Agent, FakeModel
    from nexura_agents.handoffs import Handoff
    from nexura_agents.tools import SchemaTool, ToolRetry

    root = data["spec"]
    specs = {root["name"]: root, **root.get("agents", {})}
    agents: dict[str, Any] = {}
    for agent_name, spec in specs.items():
        tools = []
        for item in spec["tools"]:
            schema = item["schema"]["function"]
            name = schema["name"]

            async def execute(
                arguments: Any,
                ctx: Any = None,
                tool_name: str = name,
                timeout: float | None = item.get("timeout"),
                owner: str = agent_name,
            ) -> Any:
                return consume(tool_name, arguments, tool=True, timeout=timeout, agent=owner)

            async def inert(ctx: Any, args: Any) -> Any:
                raise _Divergence("Fixture stub was bypassed")

            retry = item.get("retry")
            policy = (
                None
                if retry is None
                else ToolRetry(
                    **{k: v for k, v in retry.items() if k != "retry_on"},
                    retry_on=tuple(_error_type(n) for n in retry["retry_on"]),
                )
            )
            stub = SchemaTool(
                name,
                schema["parameters"],
                inert,
                description=schema["description"],
                requires_approval=item["requires_approval"],
                max_retries=item["max_retries"],
                timeout=item.get("timeout"),
                on_timeout=item.get("on_timeout", "tell_model"),
                retry=policy,
                cache=item.get("cache", False),
                idempotent=item.get("idempotent", False),
            )
            stub.is_async = item.get("is_async", True)
            stub.execute = execute  # type: ignore[method-assign]
            tools.append(stub)
        behavior = spec.get("tool_use_behavior", "run_llm_again")
        if isinstance(behavior, dict):
            behavior = StopAtTools(behavior["stop_at_tools"])

        def handler(*args: Any, owner: str = agent_name) -> Any:
            return next_response(owner)

        agents[agent_name] = Agent(
            spec["name"],
            spec["instructions"],
            FakeModel(handler=handler),
            tools=tools,
            max_turns=spec["max_turns"],
            max_tool_calls=spec["max_tool_calls"],
            output_type=spec.get("output_type"),
            output_retries=spec.get("output_retries", 1),
            tool_use_behavior=behavior,
            max_handoffs=spec.get("max_handoffs", 10),
        )

    class RecordedHandoffInput(BaseModel):
        # Structured handoff input is replayed as given; its validation is not recreated.
        model_config = ConfigDict(extra="allow")

    for agent_name, spec in specs.items():
        handoffs = []
        for item in spec.get("handoffs", []):
            target = agents.get(item["agent"])
            if target is None:
                raise _Divergence(f"Handoff target '{item['agent']}' is not in the recording")
            on_handoff = None
            if item.get("on_handoff"):

                def on_handoff(
                    ctx: Any, value: Any, owner: str = agent_name, tool: str = item["tool_name"]
                ) -> None:
                    consume(f"on_handoff:{tool}", {"tool_call_id": ctx.tool_call_id}, agent=owner)

            handoffs.append(
                Handoff(
                    target,
                    tool_name=item["tool_name"],
                    description=item.get("description"),
                    on_handoff=on_handoff,
                    input_type=RecordedHandoffInput if item.get("input_schema") else None,
                )
            )
        if handoffs:
            agents[agent_name].add_handoffs(*handoffs)
    return agents[root["name"]]


async def replay_repro_bundle(bundle: ReproBundle) -> ReplayResult:
    """Run a bundle offline through Runner/WorkflowEngine using only inert fixtures."""
    from nexura_agents import Runner
    from nexura_agents.types import Message, ModelResponse, ToolCall, Usage

    ReproBundle(bundle.data)
    data = copy.deepcopy(bundle.data)
    expected_digest = data.pop("recording_digest", None)
    if expected_digest != _digest(data):
        return ReplayResult(False, {}, "Recording digest mismatch (bundle was edited)")
    fixtures = list(data["tool_fixtures"])
    position = 0

    def consume(
        name: str,
        arguments: Any,
        *,
        tool: bool = False,
        timeout: float | None = None,
        agent: str | None = None,
    ) -> Any:
        nonlocal position
        if position >= len(fixtures):
            raise _Divergence(f"Unexpected fixture call: {name}")
        fixture = fixtures[position]
        if name != fixture["name"] or _digest(arguments) != fixture["args_digest"]:
            raise _Divergence(f"Fixture {position}: name or arguments diverged at {name}")
        if agent is not None and fixture.get("agent", agent) != agent:
            raise _Divergence(
                f"Fixture {position}: recorded for agent '{fixture['agent']}', "
                f"called by '{agent}' at {name}"
            )
        position += 1
        if "error" in fixture:
            if fixture["error"]["type"] == "CancelledError":
                # wait_for cancels execute before converting cancellation into timeout.
                raise TimeoutError() if timeout is not None else asyncio.CancelledError()
            exc = _recorded_error(fixture["error"], tool=tool, name=name)
            if tool:
                exc.tool_name = name  # type: ignore[attr-defined]
            raise exc
        return copy.deepcopy(fixture.get("output"))

    result: Any = None
    executor: Any
    failure = None
    model_position = 0
    network_attempts: list[str] = []
    gates: list[dict[str, Any]] = []
    budget = None
    if data["budget_config"] is not None:
        from nexura_agents.usage import BudgetConfig, BudgetManager

        budget = BudgetManager().create_run_budget(
            "repro", "offline", config=BudgetConfig.model_validate(data["budget_config"])
        )
    workflow_replay = _workflow_replay
    if data["run_kind"] == "workflow":
        if workflow_replay is None:
            return ReplayResult(
                False,
                {},
                "Workflow bundles replay through the runtime; import nexura_runtime first",
            )
        gates = list(data.get("workflow_gates", []))
    try:
        with offline_guard() as network_attempts:
            if data["run_kind"] == "agent":
                responses = data["model_responses"]
                root_name = data["spec"]["name"]

                def next_response(agent_name: str) -> ModelResponse:
                    nonlocal model_position
                    if model_position >= len(responses):
                        raise _Divergence("Model script exhausted")
                    raw = responses[model_position]
                    # Bundles without handoffs predate per-agent tags: the root answered.
                    recorded_agent = raw.get("agent", root_name)
                    if recorded_agent != agent_name:
                        raise _Divergence(
                            f"Model response {model_position}: recorded for agent "
                            f"'{recorded_agent}', requested by '{agent_name}'"
                        )
                    model_position += 1
                    if "error" in raw:
                        raise _recorded_error(raw["error"])
                    msg = dict(raw["message"])
                    msg["tool_calls"] = [ToolCall(**t) for t in msg.get("tool_calls") or []]
                    return ModelResponse(
                        message=Message(**msg),
                        usage=Usage(**raw["usage"]) if raw.get("usage") else None,
                        finish_reason=raw.get("finish_reason"),
                    )

                agent = _replay_agents(data, consume, next_response)
                from nexura_agents.limits import UsageLimits

                limits = UsageLimits(**data["usage_limits"]) if data.get("usage_limits") else None
                executor = Runner(
                    budget=budget,
                    usage_limits=limits,
                    max_handoffs=data.get("runner_max_handoffs"),
                )
                result = await executor.run_async(agent, data["inputs"])
                for decision in data["approvals"]:
                    result = await executor.resume_async(agent, result.interruption, **decision)
            else:
                # Workflow replay lives in the runtime package, which registers it.
                assert workflow_replay is not None
                result = await workflow_replay(data, consume, budget, gates, _Divergence)
    except _Divergence as exc:
        return ReplayResult(False, {}, str(exc))
    except BaseException as exc:
        failure = exc
    actual = _scrub(_outcome(result, failure, data["run_kind"]), {})
    if network_attempts:
        return ReplayResult(False, actual, "Network access attempted during repro replay")
    expected = _scrub(data["expected_outcome"], {})
    for key in ("status", "error_type", "failing_step", "error_message"):
        if actual[key] != expected[key]:
            return ReplayResult(
                False, actual, f"{key}: expected {expected[key]!r}, got {actual[key]!r}"
            )
    compared = "state" if data["run_kind"] == "workflow" else "output"
    notes: tuple[str, ...] = ()
    if compared in expected:
        stored = data["expected_outcome"]
        if stored.get(f"{compared}_digest", _digest(stored[compared])) != _digest(stored[compared]):
            return ReplayResult(False, actual, f"Recorded {compared} digest mismatch")
        recorded = expected[compared]
        replayed = actual.get(compared)
        if _digest(replayed) != _digest(recorded):
            where = _first_difference(replayed, recorded) or "$: digest differs"
            return ReplayResult(False, actual, f"{compared} differs at {where}")
    else:
        label = "final state" if compared == "state" else "output"
        notes = (
            f"The bundle did not record the {label} (made by an older version); "
            "only status, error and failing step were compared.",
        )
    if gates:
        return ReplayResult(False, actual, "Recording has unconsumed workflow gates", notes)
    if position != len(fixtures) or (
        data["run_kind"] == "agent" and model_position != len(data["model_responses"])
    ):
        return ReplayResult(False, actual, "Recording has unconsumed responses or fixtures", notes)
    return ReplayResult(True, actual, notes=notes)
