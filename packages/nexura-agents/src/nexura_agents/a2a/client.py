# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Call remote A2A (Agent2Agent) agents, and use them as tools of a Nexura agent.

    client = A2AClient("https://agents.example.com/support", headers={"Authorization": "Bearer x"})
    task = await client.send("Where is order A1?")
    print(task.text)

    agent = Agent(name="Concierge", ..., tools=[remote_agent_tool(client)])

Requests go through the SSRF-safe ``SafeHttpClient`` and a ``DestinationPolicy``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from nexura_agents.a2a import protocol as p
from nexura_agents.a2a.protocol import A2AError
from nexura_agents.errors import SDKValidationError, ToolError
from nexura_agents.network.client import SafeHttpClient
from nexura_agents.network.errors import DestinationPolicyDeniedError
from nexura_agents.network.policy import DestinationPolicy
from nexura_agents.tools import Tool

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_ERROR_TEXT_LIMIT = 2000

PartInput = str | dict[str, Any]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _values(parts: Any) -> tuple[list[str], list[Any]]:
    """Text and data values of parts in either wire format; files are not interpreted."""
    texts: list[str] = []
    datas: list[Any] = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif "data" in part:
            datas.append(part["data"])
    return texts, datas


@dataclass(frozen=True)
class A2ATask:
    """A remote task (or a direct message reply) in a protocol-version-neutral form.

    ``state`` is lowercase: ``submitted``, ``working``, ``completed``, ``failed``,
    ``canceled``, ``input-required``, ``rejected`` or ``auth-required``. ``text`` joins the
    text parts of the artifacts, ``data`` is the first structured (data) part, and ``output``
    is ``data`` when there is no text, else ``text``. A plain message reply has no ``id``.
    """

    id: str | None
    context_id: str | None
    state: str
    text: str = ""
    data: Any = None
    status_text: str = ""
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def output(self) -> Any:
        return self.data if self.data is not None and not self.text else self.text

    @property
    def is_input_required(self) -> bool:
        return self.state == "input-required"

    @property
    def is_completed(self) -> bool:
        return self.state == "completed"

    @classmethod
    def from_wire(cls, obj: dict[str, Any]) -> A2ATask:
        """Parse a Task or Message object in the 1.0 or 0.3 JSON shape."""
        is_message = "status" not in obj and ("parts" in obj or obj.get("kind") == "message")
        if is_message:
            texts, datas = _values(obj.get("parts"))
            return cls(
                id=obj.get("taskId"),
                context_id=obj.get("contextId"),
                state="completed",
                text="\n".join(texts),
                data=datas[0] if datas else None,
                raw=obj,
            )
        status = obj.get("status")
        if not isinstance(status, dict) or not isinstance(obj.get("id"), str):
            raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote agent returned an invalid task.")
        state = p.short_state(p.normalize_state(status.get("state")))
        artifacts = [a for a in obj.get("artifacts") or [] if isinstance(a, dict)]
        texts, datas = [], []
        for artifact in artifacts:
            t, d = _values(artifact.get("parts"))
            texts.extend(t)
            datas.extend(d)
        message: dict[str, Any] = _as_dict(status.get("message"))
        status_texts, status_datas = _values(message.get("parts"))
        pending: list[dict[str, Any]] = []
        for value in status_datas:
            if isinstance(value, dict) and isinstance(value.get("pendingApprovals"), list):
                pending = [c for c in value["pendingApprovals"] if isinstance(c, dict)]
        return cls(
            id=obj["id"],
            context_id=obj.get("contextId"),
            state=state,
            text="\n".join(texts),
            data=datas[0] if datas else None,
            status_text="\n".join(status_texts),
            pending_approvals=pending,
            artifacts=artifacts,
            raw=obj,
        )


@dataclass(frozen=True)
class A2AEvent:
    """One streamed update. ``kind`` is ``task``, ``message``, ``status`` or ``artifact``."""

    kind: str
    task_id: str | None = None
    context_id: str | None = None
    state: str | None = None  # lowercase state for task and status events
    text: str = ""
    data: Any = None
    task: A2ATask | None = None  # set for ``task`` and ``message`` events
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_input_required(self) -> bool:
        return self.state == "input-required"


def _parse_event(result: Any) -> A2AEvent:
    if not isinstance(result, dict):
        raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote agent sent an invalid stream event.")
    if "task" in result or result.get("kind") == "task":
        task = A2ATask.from_wire(result.get("task", result))
        return A2AEvent(
            "task", task.id, task.context_id, task.state, task.text, task.data, task, result
        )
    if "message" in result or result.get("kind") == "message":
        task = A2ATask.from_wire(result.get("message", result))
        return A2AEvent(
            "message", task.id, task.context_id, None, task.text, task.data, task, result
        )
    update = result.get("statusUpdate", result if result.get("kind") == "status-update" else None)
    if isinstance(update, dict) and isinstance(update.get("status"), dict):
        status = update["status"]
        message: dict[str, Any] = _as_dict(status.get("message"))
        texts, datas = _values(message.get("parts"))
        state = p.short_state(p.normalize_state(status.get("state")))
        return A2AEvent(
            "status", update.get("taskId"), update.get("contextId"), state,
            "\n".join(texts), datas[0] if datas else None, None, result,
        )  # fmt: skip
    update = result.get(
        "artifactUpdate", result if result.get("kind") == "artifact-update" else None
    )
    if isinstance(update, dict) and isinstance(update.get("artifact"), dict):
        texts, datas = _values(update["artifact"].get("parts"))
        return A2AEvent(
            "artifact", update.get("taskId"), update.get("contextId"), None,
            "\n".join(texts), datas[0] if datas else None, None, result,
        )  # fmt: skip
    raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote agent sent an unknown stream event.")


class A2AClient:
    """Talk to one remote A2A agent over JSON-RPC.

    Args:
        url: The agent's base URL; its card is at ``<url>/.well-known/agent-card.json``.
        headers: Sent with every request, e.g. ``{"Authorization": "Bearer ..."}``.
        timeout: Seconds per HTTP request (a stream may run longer than this in total).
        transport: An ``httpx`` transport, e.g. ``httpx.ASGITransport(app=...)`` for tests.
        policy: Outbound ``DestinationPolicy``. As with ``MCPServer.http``, the default
            permits loopback and private ranges and trusts the host you configured; other
            hosts (a card pointing elsewhere) must pass the policy. Pass your own policy to
            tighten this. Cloud metadata addresses are always blocked.
        card_keys: Trusted public keys for signed agent cards, by key id (``cryptography``
            public keys or JWK dicts). When given, the card must carry a JWS signature that
            verifies with one of them, or every call fails with ``AgentCardSignatureError``.
            ``jku`` URLs in the card are never fetched. Without ``card_keys`` signatures
            are not checked.
        card_algorithms: JWS algorithms accepted for card signatures.

    The client speaks 1.0 when the card offers a 1.0 JSON-RPC interface and falls back to
    the 0.3 method names and shapes for older cards. No connection is kept between calls,
    so one client is safe to use across event loops.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 60,
        transport: httpx.AsyncBaseTransport | None = None,
        policy: DestinationPolicy | None = None,
        card_keys: Mapping[str, Any] | None = None,
        card_algorithms: Sequence[str] | None = None,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SDKValidationError("A2AClient url must be an http(s) URL.")
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._transport = transport
        base = policy or DestinationPolicy(allow_loopback=True, allow_private_ranges=True)
        self._operator_hosts = {h.lower() for h in base.allowed_hosts}
        # Never mutate a policy the caller shares: add only our own host to a copy.
        self.policy = base.model_copy(
            update={"allowed_hosts": [*base.allowed_hosts, parts.hostname]}
        )
        self.card: dict[str, Any] | None = None
        self._endpoint: tuple[str, str] | None = None  # (JSON-RPC url, "1.0" | "0.3")
        self.card_keys = dict(card_keys) if card_keys else None
        self.card_algorithms = tuple(card_algorithms) if card_algorithms else None
        # Key id of the signature that verified the cached card (None when unchecked).
        self.card_verified_kid: str | None = None

    # ---------------------------------------------------------------- plumbing

    def _http(self) -> SafeHttpClient:
        return SafeHttpClient(policy=self.policy, timeout=self.timeout, transport=self._transport)

    def _card_url(self) -> str:
        base = self.url.split("?", 1)[0].rstrip("/")
        return base if base.endswith(p.CARD_PATH) else base + p.CARD_PATH

    @staticmethod
    def _raise_for_body(body: Any, status: int) -> None:
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            raise A2AError(
                code if isinstance(code, int) else p.INTERNAL_ERROR,
                str(error.get("message", "Remote error")),
                error.get("data"),
            )
        if status != 200:
            raise A2AError(-32000, f"The A2A server answered HTTP {status}.", {"status": status})

    def _headers(self, wire: str, *, stream: bool = False) -> dict[str, str]:
        return {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            p.VERSION_HEADER: wire,
        }

    async def _fetch(
        self, method: str, url: str, headers: dict[str, str], payload: Any
    ) -> tuple[int, bytes]:
        """One request whose body is read at most ``MAX_RESPONSE_BYTES`` (never followed)."""
        async with (
            self._http() as http,
            http.stream(
                method, url, headers=headers, json=payload, refuse_redirect=False
            ) as response,
        ):
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote response is too large.")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote response is too large.")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks)

    async def get_card(self, *, refresh: bool = False) -> dict[str, Any]:
        """Fetch (and cache) the agent card."""
        if self.card is not None and not refresh:
            return self.card
        status_code, raw = await self._fetch(
            "GET", self._card_url(), {**self.headers, "Accept": "application/json"}, None
        )
        if status_code != 200:
            raise A2AError(-32000, f"Agent card request answered HTTP {status_code}.")
        try:
            card = json.loads(raw)
        except (ValueError, RecursionError):
            card = None
        if not isinstance(card, dict) or not isinstance(card.get("name"), str):
            raise A2AError(p.INVALID_AGENT_RESPONSE, "The URL did not return an A2A agent card.")
        if self.card_keys is not None:
            from nexura_agents.a2a.signing import DEFAULT_ALGORITHMS, verify_agent_card

            # Raises AgentCardSignatureError; an unverified card is never cached or used.
            self.card_verified_kid = verify_agent_card(
                card, self.card_keys, algorithms=self.card_algorithms or DEFAULT_ALGORITHMS
            )
        self.card = card
        self._endpoint = None
        return card

    async def _resolve(self) -> tuple[str, str]:
        if self._endpoint is not None:
            return self._endpoint
        card = await self.get_card()
        candidates: list[tuple[str, str]] = []
        for iface in card.get("supportedInterfaces") or []:
            if (
                isinstance(iface, dict)
                and str(iface.get("protocolBinding", "")).upper() == p.BINDING
            ):
                version = str(iface.get("protocolVersion", ""))
                wire = p.PROTOCOL_VERSION if version.startswith("1.") else p.LEGACY_PROTOCOL_VERSION
                if isinstance(iface.get("url"), str):
                    candidates.append((iface["url"], wire))
        legacy = str(card.get("preferredTransport", p.BINDING)).upper() == p.BINDING
        if not candidates and isinstance(card.get("url"), str) and legacy:  # 0.3 card
            candidates.append((card["url"], p.LEGACY_PROTOCOL_VERSION))
        candidates.sort(key=lambda c: c[1] != p.PROTOCOL_VERSION)
        if not candidates:
            raise A2AError(p.UNSUPPORTED_OPERATION, "The agent offers no JSON-RPC interface.")
        endpoint, wire = candidates[0]
        scheme, host, _port = origin = _origin(endpoint)
        base = _origin(self.url)
        if scheme not in ("http", "https") or not host:
            raise A2AError(p.UNSUPPORTED_OPERATION, "The agent card has an invalid endpoint URL.")
        if base[0] == "https" and scheme != "https":
            raise A2AError(
                p.UNSUPPORTED_OPERATION,
                "The agent card points at an insecure http endpoint; refusing to downgrade.",
            )
        if origin != base and host not in self._operator_hosts:
            # A remote card must not be able to point us at another origin (another host,
            # another port on the same host, an internal service) unless the operator listed
            # the host in policy.allowed_hosts.
            raise A2AError(
                p.UNSUPPORTED_OPERATION,
                f"The agent card points at '{scheme}://{host}:{_port}', which is not the "
                "configured origin; add the host to policy.allowed_hosts to allow it.",
            )
        self._endpoint = (endpoint, wire)
        return self._endpoint

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        endpoint, wire = await self._resolve()
        name = _method(method, wire)
        payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": name, "params": params}
        status_code, raw = await self._fetch("POST", endpoint, self._headers(wire), payload)
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            raise A2AError(
                p.INVALID_AGENT_RESPONSE,
                f"The A2A server returned invalid JSON (HTTP {status_code}).",
            ) from None
        self._raise_for_body(body, status_code)
        if not isinstance(body, dict) or "result" not in body:
            raise A2AError(p.INVALID_AGENT_RESPONSE, "The A2A server sent no result.")
        return body["result"]

    def _message(
        self,
        content: str | Sequence[PartInput],
        wire: str,
        task_id: str | None,
        context_id: str | None,
    ) -> dict[str, Any]:
        items = [content] if isinstance(content, str) else list(content)
        if not items:
            raise SDKValidationError("A message needs at least one part.")
        parts: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                parts.append(p.text_part(item))
            elif isinstance(item, dict):
                parts.append(item)
            else:
                raise SDKValidationError("Message parts are strings or part dicts.")
        message: dict[str, Any] = {
            "messageId": uuid.uuid4().hex,
            "role": p.ROLE_USER,
            "parts": parts,
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        return message if wire == p.PROTOCOL_VERSION else p.message_to_v03(message)

    async def _send_params(
        self,
        content: str | Sequence[PartInput],
        task_id: str | None,
        context_id: str | None,
        history_length: int | None,
        return_immediately: bool,
    ) -> dict[str, Any]:
        _, wire = await self._resolve()
        params: dict[str, Any] = {"message": self._message(content, wire, task_id, context_id)}
        config: dict[str, Any] = {}
        if history_length is not None:
            config["historyLength"] = history_length
        if return_immediately:
            config["returnImmediately"] = True
        if config:
            params["configuration"] = config
        return params

    # -------------------------------------------------------------- operations

    async def send(
        self,
        content: str | Sequence[PartInput],
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        history_length: int | None = None,
        return_immediately: bool = False,
    ) -> A2ATask:
        """Send a message and wait for the task to finish or pause.

        ``content`` is text or a list of parts (strings, or dicts like ``{"data": {...}}``).
        Pass ``task_id`` to continue a task that is ``input-required``, and ``context_id``
        to continue a conversation. A remote agent may answer with a plain message instead
        of a task; it is returned as a completed ``A2ATask`` without an ``id``.
        """
        params = await self._send_params(
            content, task_id, context_id, history_length, return_immediately
        )
        result = await self._rpc(p.SEND, params)
        if not isinstance(result, dict):
            raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote agent returned an invalid result.")
        if "task" in result or "message" in result:
            return A2ATask.from_wire(result.get("task") or result["message"])
        return A2ATask.from_wire(result)  # 0.3: the result is the task or message

    async def stream(
        self,
        content: str | Sequence[PartInput],
        *,
        task_id: str | None = None,
        context_id: str | None = None,
    ) -> AsyncGenerator[A2AEvent, None]:
        """Send a message and yield each update (server-sent events) until the stream ends."""
        params = await self._send_params(content, task_id, context_id, None, False)
        endpoint, wire = await self._resolve()
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": _method(p.STREAM, wire),
            "params": params,
        }
        async with (
            self._http() as http,
            http.stream(
                "POST", endpoint, headers=self._headers(wire, stream=True), json=payload
            ) as response,
        ):
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raw = b""
                async for chunk in response.aiter_bytes():
                    raw += chunk
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote response is too large.")
                try:
                    body = json.loads(raw)
                except (ValueError, RecursionError):
                    body = None
                self._raise_for_body(body, response.status_code)
                raise A2AError(p.INVALID_AGENT_RESPONSE, "The A2A server did not stream.")
            data: list[str] = []
            size = 0
            async for line in _lines(response):
                if line:
                    if line.startswith("data:"):
                        piece = line[5:].removeprefix(" ")
                        size += len(piece)
                        if size > MAX_RESPONSE_BYTES:
                            raise A2AError(p.INVALID_AGENT_RESPONSE, "Stream event is too large.")
                        data.append(piece)
                    continue
                if not data:
                    continue
                payload_text, data, size = "\n".join(data), [], 0
                try:
                    message = json.loads(payload_text)
                except (ValueError, RecursionError):
                    raise A2AError(p.INVALID_AGENT_RESPONSE, "Invalid stream event.") from None
                self._raise_for_body(message, 200)
                yield _parse_event(message.get("result") if isinstance(message, dict) else None)

    async def get_task(self, task_id: str, *, history_length: int | None = None) -> A2ATask:
        """Fetch a task's current state, artifacts and (optionally limited) history."""
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        result = await self._rpc(p.GET_TASK, params)
        if not isinstance(result, dict):
            raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote agent returned an invalid task.")
        return A2ATask.from_wire(result)

    async def cancel(self, task_id: str) -> A2ATask:
        """Cancel a running or paused task; returns the task in its ``canceled`` state."""
        result = await self._rpc(p.CANCEL_TASK, {"id": task_id})
        if not isinstance(result, dict):
            raise A2AError(p.INVALID_AGENT_RESPONSE, "Remote agent returned an invalid task.")
        return A2ATask.from_wire(result)

    async def decide(
        self,
        task_id: str,
        *,
        approve: bool | Sequence[str] | dict[str, str] = (),
        reject: dict[str, str] | None = None,
    ) -> A2ATask:
        """Answer an ``input-required`` approval pause on a Nexura A2A server.

        ``approve`` is ``True``, a list of keys, or ``{key: digest}`` copied from
        ``task.pending_approvals`` so approval is bound to the exact action you reviewed.
        """
        decision: dict[str, Any] = {
            "approve": approve if isinstance(approve, (bool, dict)) else list(approve)
        }
        if reject:
            decision["reject"] = dict(reject)
        return await self.send([p.data_part(decision)], task_id=task_id)

    def as_tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        requires_approval: bool = False,
    ) -> Tool:
        """This remote agent as a tool; see :func:`remote_agent_tool`."""
        return remote_agent_tool(
            self, name=name, description=description, requires_approval=requires_approval
        )


async def _lines(response: httpx.Response) -> AsyncGenerator[str, None]:
    """Decoded lines of a stream; a line longer than ``MAX_RESPONSE_BYTES`` is an error."""
    buffer = b""
    async for chunk in response.aiter_bytes():
        buffer += chunk
        while (index := buffer.find(b"\n")) != -1:
            line, buffer = buffer[:index], buffer[index + 1 :]
            yield line.rstrip(b"\r").decode("utf-8", "replace")
        if len(buffer) > MAX_RESPONSE_BYTES:
            raise A2AError(p.INVALID_AGENT_RESPONSE, "Stream event is too large.")
    if buffer:
        yield buffer.rstrip(b"\r").decode("utf-8", "replace")


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower(), port


def _method(method: str, wire: str) -> str:
    if wire == p.PROTOCOL_VERSION:
        return method
    return next(old for old, new in p.METHODS_V03.items() if new == method)


def _clip(text: str) -> str:
    return text if len(text) <= _ERROR_TEXT_LIMIT else text[:_ERROR_TEXT_LIMIT] + "..."


def remote_agent_tool(
    url_or_client: str | A2AClient,
    *,
    name: str | None = None,
    description: str | None = None,
    requires_approval: bool = False,
    **client_options: Any,
) -> Tool:
    """A tool that asks a remote A2A agent and returns its output.

    The tool takes one ``request`` string. The description comes from the agent card's
    skills when the card is already cached on the client (``await client.get_card()``
    first); otherwise pass ``description=`` so the model knows what the agent is for.
    ``client_options`` (``headers``, ``timeout``, ``transport``, ``policy``) build the
    client when you pass a URL.

    If the remote task needs input or approval, or fails, the tool raises ``ToolError``
    so the calling model sees a clear message. Remote text is untrusted content.
    """
    from nexura_agents.module_tools import tool_name

    client = (
        url_or_client
        if isinstance(url_or_client, A2AClient)
        else A2AClient(url_or_client, **client_options)
    )
    card = client.card or {}
    host = urlsplit(client.url).hostname or "remote"
    remote_name = str(card.get("name") or host)
    skills = [s for s in card.get("skills") or [] if isinstance(s, dict)]
    if description is None:
        if card:
            lines = "; ".join(
                f"{s.get('name', s.get('id'))}: {s.get('description', '')}" for s in skills
            )
            description = f"{card.get('description', '')} Skills: {lines}".strip()
        else:
            description = f"Ask the remote A2A agent at {host}."
    tool_label = tool_name(name or (remote_name if card else "remote_agent"))

    async def call_remote(request: str) -> Any:
        try:
            task = await client.send(request)
        except A2AError as exc:
            raise ToolError(
                f"Remote agent '{remote_name}' returned an error: {_clip(exc.message)}"
            ) from exc
        except (httpx.HTTPError, DestinationPolicyDeniedError) as exc:
            raise ToolError(
                f"Remote agent '{remote_name}' could not be reached ({type(exc).__name__})."
            ) from exc
        if task.is_completed:
            return task.output
        detail = _clip(task.status_text or task.text)
        if task.is_input_required:
            raise ToolError(
                f"Remote agent '{remote_name}' needs input or approval before it can continue "
                f"(task {task.id}). It cannot be given from inside a tool call. "
                f"Remote message: {detail}"
            )
        raise ToolError(
            f"Remote agent '{remote_name}' ended in state '{task.state}' without a result. "
            f"Remote message: {detail}"
        )

    return Tool(
        tool_label,
        call_remote,
        description=description,
        requires_approval=requires_approval,
        origin=f"a2a:{host}",  # the URL the operator chose, not the remote card
    )
