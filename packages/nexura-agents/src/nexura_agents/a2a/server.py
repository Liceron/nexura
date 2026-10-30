# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Serve a Nexura agent or team as an A2A (Agent2Agent) agent over JSON-RPC and SSE.

    server = A2AServer(support_agent, url="https://agents.example.com/support")
    app = server.http_app(token="secret")  # uvicorn module:app

Other A2A clients find the agent card at ``/.well-known/agent-card.json`` and talk to the
JSON-RPC endpoint (``POST /``). Tools that need human approval pause the run: the task
becomes ``input-required`` and the client continues the same task with an approval message.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from nexura_agents.a2a import protocol as p
from nexura_agents.a2a.protocol import A2AError
from nexura_agents.agent import Agent
from nexura_agents.attachments import Attachment
from nexura_agents.errors import ApprovalDecisionError, SDKValidationError
from nexura_agents.teams import Team, TeamInterruption
from nexura_agents.types import Message, RunInterruption

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
Interruption = RunInterruption | TeamInterruption

_FAILED_TEXT = "The agent run failed."


class _Busy(Exception):
    """Every task slot is in use by a running task."""


@dataclass
class _Outcome:
    paused: bool
    output: Any = None
    interruption: Interruption | None = None
    transcript: list[Message] | None = None  # full agent transcript (agents only)


@dataclass
class _Task:
    id: str
    context_id: str
    state: str = p.SUBMITTED
    status_message: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    interruption: Interruption | None = None
    runner_task: asyncio.Task[None] | None = None
    subscribers: list[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=list)
    updated: float = field(default_factory=time.monotonic)
    prior: list[Message] = field(default_factory=list)  # context history before this task
    user_message: Message | None = None

    @property
    def running(self) -> bool:
        return self.runner_task is not None and not self.runner_task.done()

    def snapshot(self, history_length: int | None = None) -> dict[str, Any]:
        history = self.history if history_length is None else self.history[-history_length:]
        if history_length == 0:
            history = []
        out: dict[str, Any] = {
            "id": self.id,
            "contextId": self.context_id,
            "status": p.status(self.state, self.status_message),
        }
        if self.artifacts:
            out["artifacts"] = list(self.artifacts)
        if history:
            out["history"] = list(history)
        return out

    def set_state(self, state: str, message: dict[str, Any] | None = None) -> dict[str, Any]:
        self.state = state
        self.status_message = message
        self.updated = time.monotonic()
        return {
            "statusUpdate": {
                "taskId": self.id,
                "contextId": self.context_id,
                "status": p.status(state, message),
            }
        }

    def emit(self, event: dict[str, Any]) -> None:
        for queue in self.subscribers:
            queue.put_nowait(event)

    def close_subscribers(self) -> None:
        for queue in self.subscribers:
            queue.put_nowait(None)
        self.subscribers = []


class _Store:
    """Tasks and conversation contexts, bounded by count (LRU) and idle time (TTL)."""

    def __init__(self, max_tasks: int, ttl: float) -> None:
        self.max_tasks = max_tasks
        self.ttl = ttl
        self.tasks: OrderedDict[str, _Task] = OrderedDict()
        self.contexts: OrderedDict[str, tuple[float, list[Message]]] = OrderedDict()
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def lock(self, context_id: str) -> asyncio.Lock:
        lock = self._locks.get(context_id)
        if lock is None:
            lock = self._locks[context_id] = asyncio.Lock()
        return lock

    def purge(self) -> None:
        now = time.monotonic()
        for task_id in [
            t.id for t in self.tasks.values() if not t.running and now - t.updated > self.ttl
        ]:
            del self.tasks[task_id]
        for ctx_id in [c for c, (seen, _) in self.contexts.items() if now - seen > self.ttl]:
            del self.contexts[ctx_id]

    def get(self, task_id: str) -> _Task | None:
        self.purge()
        task = self.tasks.get(task_id)
        if task is not None:
            self.tasks.move_to_end(task_id)
            task.updated = time.monotonic()
        return task

    def add(self, task: _Task) -> None:
        self.purge()
        while len(self.tasks) >= self.max_tasks:
            victim = next((t for t in self.tasks.values() if not t.running), None)
            if victim is None:
                raise _Busy
            del self.tasks[victim.id]
        self.tasks[task.id] = task

    def history(self, context_id: str) -> list[Message]:
        entry = self.contexts.get(context_id)
        if entry is None:
            return []
        self.contexts.move_to_end(context_id)
        return list(entry[1])

    def save_history(self, context_id: str, messages: list[Message]) -> None:
        self.contexts[context_id] = (time.monotonic(), messages)
        self.contexts.move_to_end(context_id)
        while len(self.contexts) > self.max_tasks:
            self.contexts.popitem(last=False)


def _version_supported(version: str) -> bool:
    """1.x is accepted (same major as ours, like the reference); 0.3.x for the legacy names."""
    try:
        nums = [int(x) for x in version.strip().split(".")]
    except ValueError:
        return False
    if not nums or len(nums) > 3:
        return False
    if nums[0] == 1:
        return True
    return nums[0] == 0 and len(nums) > 1 and nums[1] == 3


def _agent_skill_id(name: str) -> str:
    from nexura_agents.module_tools import tool_name

    return tool_name(name.lower().replace(" ", "_"))


class A2AServer:
    """Expose an ``Agent`` or ``Team`` as an A2A agent.

    Args:
        agent_or_team: What to serve. Teams run as one unit and may pause for approval.
        name: Agent card name (default: the agent's name).
        description: Agent card description (default: the agent's description).
        url: Public URL of the JSON-RPC endpoint, as clients will reach it.
        version: Your agent's version, shown in the card (not the protocol version).
        skills: Card skills as dicts (``id``, ``name``, ``description``, ``tags``); the
            default is one skill describing the agent.
        deps: Passed to every run as ``RunContext.deps``.
        runner: ``Runner`` for agent runs (usage limits, hooks, budgets) and for teams
            without their own runner.
        signing_key: Optional ``cryptography`` private key (EC P-256/P-384, Ed25519 or RSA).
            When set, the served card carries a JWS signature (A2A 1.0 section 8.4).
        signing_kid: Key id put in the signature header; required with ``signing_key``.
        signing_jku: Optional JWK Set URL advertised in the signature header.

    Security model. One bearer token guards the whole endpoint, so every holder of it can
    read, continue and cancel every task; task and context ids are random 144-bit values
    but are not per-user. Use one server per tenant if you need isolation.
    """

    def __init__(
        self,
        agent_or_team: Agent | Team,
        *,
        name: str | None = None,
        description: str | None = None,
        url: str,
        version: str = "1.0.0",
        skills: Sequence[dict[str, Any]] | None = None,
        deps: Any = None,
        runner: Any = None,
        signing_key: Any = None,
        signing_kid: str | None = None,
        signing_jku: str | None = None,
    ) -> None:
        from nexura_agents.runner import Runner

        if signing_key is not None and not signing_kid:
            raise SDKValidationError("signing_kid is required when signing_key is set.")
        self.signing_key = signing_key
        self.signing_kid = signing_kid
        self.signing_jku = signing_jku
        if not isinstance(agent_or_team, (Agent, Team)):
            raise SDKValidationError("A2AServer serves an Agent or a Team.")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise SDKValidationError("A2AServer url must be the public http(s) URL of the agent.")
        self.target = agent_or_team
        self.name = (name or agent_or_team.name).strip()
        self.description = (
            description
            or agent_or_team.description
            or f"Nexura {'team' if isinstance(agent_or_team, Team) else 'agent'} {self.name}."
        )
        self.url = url
        self.version = version
        self.deps = deps
        self.runner = runner or Runner()
        self._skills = [self._skill(s) for s in skills] if skills else [self._default_skill()]

    def _default_skill(self) -> dict[str, Any]:
        kind = "team" if isinstance(self.target, Team) else "agent"
        return {
            "id": _agent_skill_id(self.target.name),
            "name": self.target.name,
            "description": self.description,
            "tags": [kind],
        }

    @staticmethod
    def _skill(raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
            raise SDKValidationError("Each skill needs at least an 'id' and a 'name'.")
        return {
            **raw,
            "description": raw.get("description") or raw["name"],
            "tags": raw.get("tags") or ["skill"],
        }

    # ------------------------------------------------------------------ card

    def card(self, *, authenticated: bool = False) -> dict[str, Any]:
        """The agent card. ``authenticated=True`` advertises bearer auth (``http_app`` does).

        Signed when the server was given a ``signing_key``.
        """
        card: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "supportedInterfaces": [
                {
                    "url": self.url,
                    "protocolBinding": p.BINDING,
                    "protocolVersion": p.PROTOCOL_VERSION,
                }
            ],
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json"],
            "skills": [dict(s) for s in self._skills],
        }
        if authenticated:
            card["securitySchemes"] = {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}
            card["securityRequirements"] = [{"schemes": {"bearer": {"list": []}}}]
        if self.signing_key is not None:
            from nexura_agents.a2a.signing import sign_agent_card

            assert self.signing_kid is not None
            card = sign_agent_card(
                card, self.signing_key, kid=self.signing_kid, jku=self.signing_jku
            )
        return card

    # ------------------------------------------------------------- running

    def _prompt(self, task: _Task, message: dict[str, Any]) -> Message:
        texts: list[str] = []
        attachments: list[Attachment] = []
        for part in message["parts"]:
            try:
                if "text" in part:
                    texts.append(part["text"])
                elif "data" in part:
                    texts.append(json.dumps(part["data"], ensure_ascii=False))
                elif "raw" in part:
                    attachments.append(
                        Attachment.from_bytes(
                            p.decode_raw(part),
                            part.get("mediaType", "application/octet-stream"),
                            name=part.get("filename"),
                        )
                    )
                else:
                    attachments.append(
                        Attachment.from_url(part["url"], mime_type=part.get("mediaType"))
                    )
            except SDKValidationError as exc:
                raise p.invalid_params(f"Unusable file part: {exc.message}") from None
        content = "\n\n".join(texts) or None
        return Message(role="user", content=content, attachments=attachments or None)

    async def _invoke(self, task: _Task, decisions: tuple[Any, dict[str, str]] | None) -> _Outcome:
        target = self.target
        if decisions is not None:
            approve, reject = decisions
            if isinstance(target, Team):
                assert isinstance(task.interruption, TeamInterruption)
                team_result = await target._resume(
                    task.interruption,
                    approve=approve,
                    reject=reject,
                    deps=self.deps,
                    timeout=None,
                    runner=self.runner,
                )
                return _Outcome(
                    team_result.status == "paused", team_result.output, team_result.interruption
                )
            assert isinstance(task.interruption, RunInterruption)
            result = await self.runner.resume_async(
                target, task.interruption, approve=approve, reject=reject, deps=self.deps
            )
            return _Outcome(
                result.status == "paused", result.output, result.interruption, result.messages
            )
        assert task.user_message is not None
        user = task.user_message
        if isinstance(target, Team):
            prompt: Any = (
                user.content or ""
                if not task.prior and not user.attachments
                else [*task.prior, user]
            )
            team_result = await target._run(
                prompt, deps=self.deps, timeout=None, runner=self.runner
            )
            return _Outcome(
                team_result.status == "paused", team_result.output, team_result.interruption
            )
        result = await self.runner.run_async(
            target, [user], history=list(task.prior) or None, deps=self.deps
        )
        return _Outcome(
            result.status == "paused", result.output, result.interruption, result.messages
        )

    def _pending(self, interruption: Interruption) -> list[dict[str, Any]]:
        if isinstance(interruption, TeamInterruption):
            digests = interruption.pending_digests()
            return [
                {
                    "key": key,
                    "tool": call.name,
                    "arguments": p.jsonable(call.arguments),
                    "digest": digests[key],
                }
                for key, call in interruption.pending_calls()
            ]
        digests = interruption.pending_digests()
        return [
            {
                "key": call.id,
                "tool": call.name,
                "arguments": p.jsonable(call.arguments),
                "digest": digests[call.id],
            }
            for call in interruption.pending
        ]

    def _approval_message(self, task: _Task, interruption: Interruption) -> dict[str, Any]:
        pending = self._pending(interruption)
        listing = "; ".join(
            f"{c['key']}: {c['tool']}({json.dumps(c['arguments'])})" for c in pending
        )
        text = (
            "Approval needed before running: "
            f"{listing}. Reply on this task with a data part "
            '{"approve": [keys] | true, "reject": {key: reason}}; every call needs a decision.'
        )
        return p.agent_message(
            uuid.uuid4().hex,
            [p.text_part(text), p.data_part({"pendingApprovals": pending})],
            task.context_id,
            task.id,
        )

    @staticmethod
    def _output_parts(output: Any) -> list[dict[str, Any]]:
        if isinstance(output, str):
            return [p.text_part(output)]
        if output is None:
            return [p.text_part("")]
        value = p.jsonable(output)
        if isinstance(value, (dict, list)):
            return [p.data_part(value)]
        return [p.text_part(str(value))]

    async def _run_body(
        self, task: _Task, store: _Store, decisions: tuple[Any, dict[str, str]] | None
    ) -> None:
        # Runs in one context are serialized so none overwrites another's history.
        lock = store.lock(task.context_id)
        try:
            async with lock:
                if decisions is None:
                    task.prior = store.history(task.context_id)
                await self._run_locked(task, store, decisions)
        finally:
            task.close_subscribers()

    async def _run_locked(
        self, task: _Task, store: _Store, decisions: tuple[Any, dict[str, str]] | None
    ) -> None:
        try:
            if decisions is None:
                task.emit(task.set_state(p.WORKING))
            try:
                outcome = await self._invoke(task, decisions)
            except asyncio.CancelledError:
                raise
            except ApprovalDecisionError as exc:
                # The decisions did not fit the stored pause; it stays open for a retry.
                task.emit(task.set_state(p.INPUT_REQUIRED, self._reply(task, str(exc))))
                return
            except Exception:
                logger.exception("A2A run failed (task %s)", task.id)
                task.interruption = None
                task.emit(task.set_state(p.FAILED, self._reply(task, _FAILED_TEXT)))
                return
            if task.state in p.TERMINAL_STATES:
                return  # cancelled while the run finished
            if outcome.paused and outcome.interruption is not None:
                task.interruption = outcome.interruption
                message = self._approval_message(task, outcome.interruption)
                task.history.append(message)
                task.emit(task.set_state(p.INPUT_REQUIRED, message))
                return
            task.interruption = None
            artifact = {
                "artifactId": uuid.uuid4().hex,
                "name": "output",
                "parts": self._output_parts(outcome.output),
            }
            task.artifacts.append(artifact)
            task.emit(
                {
                    "artifactUpdate": {
                        "taskId": task.id,
                        "contextId": task.context_id,
                        "artifact": artifact,
                        "append": False,
                        "lastChunk": True,
                    }
                }
            )
            self._remember(store, task, outcome)
            task.emit(task.set_state(p.COMPLETED))
        finally:
            task.close_subscribers()

    def _reply(self, task: _Task, text: str) -> dict[str, Any]:
        message = p.agent_message(uuid.uuid4().hex, [p.text_part(text)], task.context_id, task.id)
        task.history.append(message)
        return message

    def _remember(self, store: _Store, task: _Task, outcome: _Outcome) -> None:
        if outcome.transcript is not None:
            store.save_history(task.context_id, outcome.transcript)
        elif task.user_message is not None:
            answer = Message(
                role="assistant",
                content=p.message_text(self._output_parts(outcome.output))
                or json.dumps(p.jsonable(outcome.output), ensure_ascii=False),
            )
            store.save_history(task.context_id, [*task.prior, task.user_message, answer])

    # ------------------------------------------------------- decisions

    @staticmethod
    def _decisions(task: _Task, message: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
        """Turn an approval message into ``({key: digest}, {key: reason})``, bound to the pause."""
        assert task.interruption is not None
        raw = next(
            (
                part["data"]
                for part in message["parts"]
                if isinstance(part.get("data"), dict)
                and ("approve" in part["data"] or "reject" in part["data"])
            ),
            None,
        )
        if raw is None:
            raise p.invalid_params(
                "This task is waiting for approval; send a data part with "
                "'approve' and/or 'reject'."
            )
        interruption = task.interruption
        digests = interruption.pending_digests()
        reject = raw.get("reject") or {}
        if not isinstance(reject, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in reject.items()
        ):
            raise p.invalid_params("'reject' must map each key to a reason string.")
        approve = raw.get("approve", [])
        if approve is True:
            chosen = {k: d for k, d in digests.items() if k not in reject}
        elif isinstance(approve, list) and all(isinstance(k, str) for k in approve):
            chosen = {k: digests.get(k, "") for k in approve}
            if any(k not in digests for k in approve):
                raise p.invalid_params("'approve' names a call that is not pending.")
        elif isinstance(approve, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in approve.items()
        ):
            chosen = dict(approve)
            for key, digest in chosen.items():
                if key not in digests or not hmac.compare_digest(
                    digest.encode(), digests[key].encode()
                ):
                    raise p.invalid_params(
                        f"Approval for '{key}' does not match the pending action; "
                        "the arguments changed after review."
                    )
        else:
            raise p.invalid_params("'approve' must be true, a list of keys or {key: digest}.")
        if any(k not in digests for k in reject):
            raise p.invalid_params("'reject' names a call that is not pending.")
        if set(chosen) | set(reject) != set(digests) or set(chosen) & set(reject):
            raise p.invalid_params(
                "Approve or reject every pending call exactly once. "
                f"Pending: {sorted(digests)}; decided: {sorted(set(chosen) | set(reject))}."
            )
        return chosen, dict(reject)

    # ---------------------------------------------------------- operations

    def _begin(
        self, store: _Store, params: Any, *, v03: bool
    ) -> tuple[_Task, tuple[dict[str, str], dict[str, str]] | None]:
        """Validate a message and create or continue a task. Raises ``A2AError``."""
        if not isinstance(params, dict):
            raise p.invalid_params("params must be an object.")
        message = p.parse_message(params.get("message"), v03=v03)
        config = params.get("configuration")
        if config is not None and not isinstance(config, dict):
            raise p.invalid_params("'configuration' must be an object.")
        task_id = message.get("taskId")
        decisions = None
        if task_id is not None:
            task = store.get(task_id)
            if task is None:
                raise A2AError(p.TASK_NOT_FOUND, "Task not found")
            if message.get("contextId") not in (None, task.context_id):
                raise p.invalid_params("contextId does not match the task.")
            if task.state != p.INPUT_REQUIRED or task.interruption is None:
                raise p.invalid_params(
                    f"Task is {p.short_state(task.state)} and cannot take new messages; "
                    "start a new task in the same contextId."
                )
            decisions = self._decisions(task, message)
            task.history.append(message)
            task.state = p.WORKING
            task.status_message = None
            task.updated = time.monotonic()
        else:
            context_id = message.get("contextId") or uuid.uuid4().hex
            task = _Task(id=secrets.token_urlsafe(18), context_id=context_id)
            task.prior = store.history(context_id)
            task.user_message = self._prompt(task, message)
            task.history.append(self._redact(message, task))
            try:
                store.add(task)
            except _Busy:
                raise A2AError(p.INTERNAL_ERROR, "Server busy: too many running tasks.") from None
        return task, decisions

    @staticmethod
    def _redact(message: dict[str, Any], task: _Task) -> dict[str, Any]:
        """Keep history small: inline file bytes are not stored."""
        parts = [
            {"text": f"[attachment {p_.get('filename') or p_.get('mediaType') or ''} omitted]"}
            if "raw" in p_
            else p_
            for p_ in message["parts"]
        ]
        return {**message, "parts": parts, "contextId": task.context_id, "taskId": task.id}

    def _launch(
        self, store: _Store, task: _Task, decisions: tuple[Any, dict[str, str]] | None
    ) -> None:
        task.runner_task = asyncio.create_task(self._run_body(task, store, decisions))

    async def _send(self, store: _Store, params: Any, *, v03: bool = False) -> dict[str, Any]:
        task, decisions = self._begin(store, params, v03=v03)
        self._launch(store, task, decisions)
        config = params.get("configuration") or {}
        history_length = config.get("historyLength")
        immediate = config.get("returnImmediately")
        if v03 and isinstance(config.get("blocking"), bool):
            immediate = not config["blocking"]  # reference compat/v0_3/conversions.py:322
        if not immediate and task.runner_task is not None:
            await asyncio.wait({task.runner_task})  # a dropped client does not cancel the run
        snapshot = task.snapshot(history_length if isinstance(history_length, int) else None)
        return snapshot

    async def _stream(
        self, store: _Store, params: Any, *, v03: bool = False
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Validate eagerly (raises ``A2AError``), then yield stream responses."""
        task, decisions = self._begin(store, params, v03=v03)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        task.subscribers.append(queue)
        first = {"task": task.snapshot()}
        self._launch(store, task, decisions)
        return self._events(task, queue, first)

    @staticmethod
    async def _events(
        task: _Task, queue: asyncio.Queue[dict[str, Any] | None], first: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        try:
            yield first
            while (event := await queue.get()) is not None:
                yield event
        finally:
            if queue in task.subscribers:
                task.subscribers.remove(queue)

    def _get_task(self, store: _Store, params: Any) -> dict[str, Any]:
        """Handle ``GetTask``; raises ``A2AError`` (-32001) for an unknown or expired task."""
        if not isinstance(params, dict):
            raise p.invalid_params("params must be an object.")
        task_id = p.check_id(params.get("id"), "id")
        length = params.get("historyLength")
        if length is not None and (
            isinstance(length, bool) or not isinstance(length, int) or length < 0
        ):
            raise p.invalid_params("historyLength must be a non-negative integer.")
        task = store.get(task_id)
        if task is None:
            raise A2AError(p.TASK_NOT_FOUND, "Task not found")
        return task.snapshot(length)

    async def _cancel_task(self, store: _Store, params: Any) -> dict[str, Any]:
        """Handle ``CancelTask``: stop an in-flight run or close a paused task."""
        if not isinstance(params, dict):
            raise p.invalid_params("params must be an object.")
        task = store.get(p.check_id(params.get("id"), "id"))
        if task is None:
            raise A2AError(p.TASK_NOT_FOUND, "Task not found")
        if task.state in p.TERMINAL_STATES:
            raise A2AError(p.TASK_NOT_CANCELABLE, "Task cannot be canceled")
        task.interruption = None
        task.emit(task.set_state(p.CANCELED))
        runner_task = task.runner_task
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
            await asyncio.wait({runner_task})
        task.close_subscribers()
        return task.snapshot()

    # ----------------------------------------------------------------- HTTP

    def http_app(
        self,
        *,
        token: str | None = None,
        allowed_origins: Sequence[str] = (),
        max_body_bytes: int = 4 * 1024 * 1024,
        max_tasks: int = 1000,
        task_ttl_seconds: float = 3600.0,
        path: str = "/",
    ) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
        """An ASGI app: ``GET /.well-known/agent-card.json`` and JSON-RPC on ``POST path``.

        ``token`` requires ``Authorization: Bearer <token>`` on the JSON-RPC endpoint (the
        card stays public and advertises the scheme); always set one unless the server is
        bound to localhost. Browser requests are refused unless their ``Origin`` is in
        ``allowed_origins``. Bodies over ``max_body_bytes`` get 413. At most ``max_tasks``
        tasks (and contexts) are kept, least recently used first; ones idle past
        ``task_ttl_seconds`` expire. Running tasks are never evicted: when every slot is
        running, new requests get an error until one finishes.

        Errors follow JSON-RPC: HTTP 200 with an ``error`` object, except 401, 403, 404, 405
        and 413 for transport problems. Internal errors never include details.
        """
        store = _Store(max_tasks, task_ttl_seconds)
        card_body = self.card(authenticated=token is not None)

        async def respond(
            send: Send, status: int, body: Any = None, headers: dict[str, str] | None = None
        ) -> None:
            raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
            hdrs = [(b"content-type", b"application/json")] if body is not None else []
            hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
            await send({"type": "http.response.start", "status": status, "headers": hdrs})
            await send({"type": "http.response.body", "body": raw})

        def error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
            err: dict[str, Any] = {"code": code, "message": message}
            if data is not None:
                err["data"] = data
            return {"jsonrpc": "2.0", "id": request_id, "error": err}

        def ok(request_id: Any, result: Any) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        async def read_body(receive: Receive, declared: str) -> bytes | None:
            if declared.isdigit() and int(declared) > max_body_bytes:
                return None
            body = b""
            while True:
                event = await receive()
                if event.get("type") == "http.disconnect":
                    return None
                body += event.get("body", b"")
                if len(body) > max_body_bytes:
                    return None
                if not event.get("more_body"):
                    return bytes(body)

        async def sse(
            send: Send, request_id: Any, events: AsyncGenerator[dict[str, Any], None], v03: bool
        ) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-cache"),
                    ],
                }
            )
            try:
                async for event in events:
                    result = p.event_to_v03(event) if v03 else event
                    chunk = f"data: {json.dumps(ok(request_id, result), ensure_ascii=False)}\n\n"
                    await send(
                        {"type": "http.response.body", "body": chunk.encode(), "more_body": True}
                    )
            except Exception:
                logger.exception("A2A stream failed")
                chunk = (
                    f"data: {json.dumps(error(request_id, p.INTERNAL_ERROR, 'Internal error'))}\n\n"
                )
                await send(
                    {"type": "http.response.body", "body": chunk.encode(), "more_body": True}
                )
            finally:
                await events.aclose()
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def rpc(send: Send, message: Any, version: str | None) -> None:
            request_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                request_id = None
            if not isinstance(message, dict):
                await respond(
                    send,
                    200,
                    error(
                        None, p.INVALID_REQUEST, "Batch and non-object requests are not supported"
                    ),
                )
                return
            method = message.get("method")
            if (
                message.get("jsonrpc") != "2.0"
                or not isinstance(method, str)
                or "id" not in message
                or ("params" in message and not isinstance(message["params"], dict))
            ):
                await respond(send, 200, error(request_id, p.INVALID_REQUEST, "Invalid request"))
                return
            if version is not None and not _version_supported(version):
                await respond(
                    send,
                    200,
                    error(
                        request_id,
                        p.VERSION_NOT_SUPPORTED,
                        "Version not supported",
                        {"supported": list(p.SUPPORTED_VERSIONS)},
                    ),
                )
                return
            v03 = method in p.METHODS_V03
            name = p.METHODS_V03.get(method, method)
            params = message.get("params", {})
            try:
                if name == p.STREAM:
                    events = await self._stream(store, params, v03=v03)
                    await sse(send, request_id, events, v03)
                    return
                if name == p.SEND:
                    task = await self._send(store, params, v03=v03)
                    result = p.task_to_v03(task) if v03 else {"task": task}
                elif name == p.GET_TASK:
                    task = self._get_task(store, params)
                    result = p.task_to_v03(task) if v03 else task
                elif name == p.CANCEL_TASK:
                    task = await self._cancel_task(store, params)
                    result = p.task_to_v03(task) if v03 else task
                elif method in p.PUSH_METHODS:
                    raise A2AError(p.PUSH_NOT_SUPPORTED, "Push Notification is not supported")
                elif method in p.UNSUPPORTED_METHODS:
                    raise A2AError(p.UNSUPPORTED_OPERATION, "This operation is not supported")
                elif method in p.EXTENDED_CARD_METHODS:
                    raise A2AError(
                        p.EXTENDED_CARD_NOT_CONFIGURED,
                        "Authenticated Extended Card is not configured",
                    )
                else:
                    raise A2AError(p.METHOD_NOT_FOUND, "Method not found")
            except A2AError as exc:
                await respond(send, 200, error(request_id, exc.code, exc.message, exc.data))
                return
            except Exception:
                logger.exception("Unhandled A2A error")
                await respond(send, 200, error(request_id, p.INTERNAL_ERROR, "Internal error"))
                return
            await respond(send, 200, ok(request_id, result))

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "lifespan":
                while True:
                    event = await receive()
                    if event["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif event["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            if scope["type"] != "http":
                return
            # HTTP header bytes are latin-1; decoding as UTF-8 can fail on hostile input.
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            if scope["path"] == p.CARD_PATH:
                if scope["method"] not in ("GET", "HEAD"):
                    await respond(send, 405, None, {"Allow": "GET"})
                else:
                    await respond(send, 200, card_body, {"Cache-Control": "no-cache"})
                return
            if scope["path"] != path:
                await respond(send, 404, {"error": "not found"})
                return
            origin = headers.get("origin")
            if origin and origin not in allowed_origins:
                await respond(send, 403, error(None, p.INVALID_REQUEST, "Origin not allowed"))
                return
            if token is not None:
                supplied = headers.get("authorization", "").encode("latin-1")
                if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
                    await respond(
                        send,
                        401,
                        error(None, p.INVALID_REQUEST, "Unauthorized"),
                        {"WWW-Authenticate": "Bearer"},
                    )
                    return
            if scope["method"] != "POST":
                await respond(send, 405, None, {"Allow": "POST"})
                return
            body = await read_body(receive, headers.get("content-length", ""))
            if body is None:
                await respond(send, 413, error(None, p.INVALID_REQUEST, "Payload too large"))
                return
            try:
                message = json.loads(body)
            except (ValueError, RecursionError):  # includes JSON/Unicode decode errors
                await respond(send, 200, error(None, p.PARSE_ERROR, "Invalid JSON payload"))
                return
            await rpc(send, message, headers.get("a2a-version"))

        return app
