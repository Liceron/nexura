# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Client session implementation for Model Context Protocol (MCP).

The client is dual-era. It prefers the stateless 2026-07-28 revision and falls back to
the ``initialize`` handshake of the older revisions when the server only speaks those:

1. ``server/discover`` is sent with 2026-07-28 in ``params._meta`` (and, over HTTP, the
   ``MCP-Protocol-Version`` / ``Mcp-Method`` headers).
2. A ``DiscoverResult`` that lists a modern version we speak: the session stays modern
   and every later request carries the per-request ``_meta`` envelope.
3. ``UnsupportedProtocolVersionError`` (-32022): retry once with a mutual modern
   version; if the server lists only handshake versions, use the handshake; if it
   lists nothing we speak, raise ``McpProtocolVersionMismatchError``.
4. Any other error (a legacy server's -32601/-32602, a non-modern HTTP 400/404/405, or
   on stdio no answer within ``discover_timeout``): the server is legacy, so the
   ``initialize`` handshake runs and the negotiated handshake version is used.

The era is decided once per session (one stdio process or one HTTP endpoint).
Pass ``protocol_version=`` to skip detection: a modern version speaks only that version
(no fallback); a handshake version runs ``initialize`` offering it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.mcp.errors import (
    McpInputRequiredError,
    McpIntegrationError,
    McpJsonRpcError,
    McpProtocolVersionMismatchError,
    McpToolReplacementError,
    McpTransportError,
)
from nexura_agents.mcp.headers import InvalidHeaderAnnotation, header_paths
from nexura_agents.mcp.transports import McpTransport
from nexura_agents.mcp.types import (
    CACHEABLE_METHODS,
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    MODERN_ERROR_CODES,
    MODERN_PROTOCOL_VERSIONS,
    MRTR_METHODS,
    SUPPORTED_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    McpCacheHint,
    McpContentBlock,
    McpImplementation,
    McpPromptDefinition,
    McpResourceDefinition,
    McpServerCapabilities,
    McpToolCallResult,
    McpToolDefinition,
    is_modern_version,
)

logger = logging.getLogger(__name__)

# Answers one entry of a server's ``inputRequests``: ``(key, {"method", "params"})`` ->
# the result object for that request (an ElicitResult, CreateMessageResult, ...).
InputHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

DEFAULT_CLIENT_INFO = McpImplementation(name="nexura-agents", version="0.6.0")
# HTTP statuses a legacy server uses to refuse a request it does not understand.
_LEGACY_HTTP_STATUSES = frozenset({400, 404, 405})


class McpClientSession:
    """Client managing an MCP connection, protocol negotiation, and remote invocations.

    Args:
        transport: The transport to the server.
        request_timeout: Seconds to wait for one reply (default: the transport's).
        protocol_version: ``None`` (default) detects the server's era, preferring
            2026-07-28. A modern version pins it; a handshake version (such as
            ``"2025-06-18"``) runs the legacy ``initialize`` handshake offering it.
        client_info: Name and version sent to the server.
        client_capabilities: Capabilities declared on every modern request (default
            none). Declare ``elicitation`` / ``sampling`` / ``roots`` only together with
            an ``input_handler`` that can answer them.
        input_handler: Answers a 2026-07-28 server's multi round-trip ``inputRequests``.
            Without one such a result raises ``McpInputRequiredError``.
        discover_timeout: Seconds a stdio/in-memory server has to answer the
            ``server/discover`` probe before it is treated as legacy (default 10).
        max_input_rounds: Upper bound on multi round-trip retries of one request.
    """

    def __init__(
        self,
        transport: McpTransport,
        *,
        request_timeout: float | None = None,
        protocol_version: str | None = None,
        client_info: McpImplementation | None = None,
        client_capabilities: dict[str, Any] | None = None,
        input_handler: InputHandler | None = None,
        discover_timeout: float = 10.0,
        max_input_rounds: int = 8,
    ) -> None:
        if protocol_version is not None and protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ValueError(
                f"Unsupported MCP protocol version {protocol_version!r}; "
                f"choose one of {SUPPORTED_PROTOCOL_VERSIONS}."
            )
        self.transport = transport
        # Upper bound on waiting for one reply; ``None`` uses the transport's own default.
        self.request_timeout = (
            request_timeout if request_timeout is not None else transport.request_timeout
        )
        self.requested_version = protocol_version
        self.client_info = client_info or DEFAULT_CLIENT_INFO
        self.client_capabilities = dict(client_capabilities or {})
        self.input_handler = input_handler
        self.discover_timeout = discover_timeout
        self.max_input_rounds = max_input_rounds
        self._request_counter = 0
        self.server_version: str | None = None
        # "modern" (per-request envelope) or "legacy" (initialize handshake) once known.
        self.era: str | None = None
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: McpServerCapabilities = McpServerCapabilities()
        self.server_supported_versions: list[str] = []
        self.instructions: str | None = None
        # Latest freshness hint per cacheable method (2026-07-28 ``ttlMs``/``cacheScope``).
        self.cache_hints: dict[str, McpCacheHint] = {}
        self._known_tool_digests: dict[str, str] = {}
        # Pinned tool calls re-check the server's tool list when it announces a change
        # (notifications/tools/list_changed) or when the last listing is older than this
        # many seconds; 0 re-lists before every pinned call.
        self.tool_list_max_age = 30.0
        self._tools_listed_at: float | None = None
        self._tools_changed = False
        self._is_initialized = False
        # One request in flight per session: replies are matched by id, and a shared
        # transport must not interleave two callers' messages.
        self._lock = asyncio.Lock()

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def is_modern(self) -> bool:
        """True once the session speaks a stateless (2026-07-28 or later) revision."""
        return self.era == "modern"

    def _next_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    # ----------------------------------------------------------------- wire

    def _envelope(self, version: str) -> dict[str, Any]:
        return {
            META_PROTOCOL_VERSION: version,
            META_CLIENT_INFO: self.client_info.model_dump(),
            META_CLIENT_CAPABILITIES: dict(self.client_capabilities),
        }

    async def _exchange(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        version: str | None,
        timeout: float | None = None,
    ) -> Any:
        """Send one request and return its ``result`` (raising on a JSON-RPC error).

        ``version`` set means a modern request: the ``_meta`` envelope is added to a copy
        of ``params`` (the caller's dict is never changed).
        """
        body: dict[str, Any] = dict(params or {})
        if version is not None:
            meta = dict(body.get("_meta") or {})
            meta.update(self._envelope(version))
            body["_meta"] = meta
        req_id = self._next_id()
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": body}
        wait = self.request_timeout if timeout is None else timeout
        async with self._lock:
            await self.transport.send_message(payload)
            try:
                resp = await asyncio.wait_for(self._receive_response(req_id), timeout=wait)
            except TimeoutError as exc:
                raise McpTransportError(
                    f"Timed out after {wait}s waiting for reply to '{method}'.",
                    details={"timed_out": True},
                ) from exc

        if "error" in resp and resp["error"] is not None:
            err = resp["error"]
            if not isinstance(err, dict):
                raise McpJsonRpcError(-32603, "Malformed error object from server.")
            raise McpJsonRpcError(
                code=err.get("code", -32000),
                message=err.get("message", "Unknown error"),
                data=err.get("data"),
            )
        return resp.get("result")

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request in the session's era; modern results are unwrapped and checked."""
        if not self.is_modern:
            return await self._exchange(method, params, version=None)
        assert self.server_version is not None
        original = dict(params or {})
        result = await self._exchange(method, original, version=self.server_version)
        rounds = 0
        while True:
            result_type = result.get("resultType", "complete") if isinstance(result, dict) else None
            if result_type == "complete":
                break
            if result_type != "input_required" or method not in MRTR_METHODS:
                raise McpIntegrationError(
                    f"MCP server answered '{method}' with an invalid resultType {result_type!r}.",
                    details={"error_code": "MCP_INVALID_RESULT", "method": method},
                )
            rounds += 1
            if rounds > self.max_input_rounds:
                raise McpIntegrationError(
                    f"MCP server kept asking for input on '{method}' "
                    f"({self.max_input_rounds} rounds); giving up.",
                    details={"error_code": "MCP_INPUT_ROUNDS_EXCEEDED", "method": method},
                )
            result = await self._exchange(
                method,
                await self._retry_params(method, original, result),
                version=self.server_version,
            )
        if isinstance(result, dict) and method in CACHEABLE_METHODS:
            self._record_cache_hint(method, result)
        return result

    async def _retry_params(
        self, method: str, original: dict[str, Any], interim: dict[str, Any]
    ) -> dict[str, Any]:
        """Params for the retry of a multi round-trip request (SEP-2322)."""
        requests = interim.get("inputRequests")
        state = interim.get("requestState")
        if requests is None and state is None:
            raise McpIntegrationError(
                f"MCP server's input_required answer to '{method}' has neither "
                "inputRequests nor requestState.",
                details={"error_code": "MCP_INVALID_RESULT", "method": method},
            )
        retry = dict(original)
        if requests is not None:
            if not isinstance(requests, dict):
                raise McpIntegrationError(
                    "MCP inputRequests must be an object.",
                    details={"error_code": "MCP_INVALID_RESULT", "method": method},
                )
            kinds = sorted({str(r.get("method")) for r in requests.values() if isinstance(r, dict)})
            if self.input_handler is None:
                raise McpInputRequiredError(method, kinds)
            responses: dict[str, Any] = {}
            for key, request in requests.items():
                if not isinstance(request, dict):
                    raise McpIntegrationError(
                        "Each MCP input request must be an object.",
                        details={"error_code": "MCP_INVALID_RESULT", "method": method},
                    )
                responses[str(key)] = await self.input_handler(str(key), request)
            retry["inputResponses"] = responses
        if state is not None:
            if not isinstance(state, str):
                raise McpIntegrationError(
                    "MCP requestState must be a string.",
                    details={"error_code": "MCP_INVALID_RESULT", "method": method},
                )
            retry["requestState"] = state  # echoed verbatim, never inspected
        return retry

    def _record_cache_hint(self, method: str, result: dict[str, Any]) -> None:
        ttl = result.get("ttlMs")
        scope = result.get("cacheScope")
        self.cache_hints[method] = McpCacheHint(
            ttl_ms=ttl if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0 else 0,
            scope=scope if scope in ("public", "private") else "private",
            received_at=time.monotonic(),
        )

    async def _receive_response(self, req_id: int) -> dict[str, Any]:
        """Read until the reply to ``req_id``, handling whatever the server sends first.

        Servers may interleave log/progress notifications and their own requests (such
        as ``ping``) before replying; those must not be mistaken for the response.
        """
        while True:
            message = await self.transport.receive_message()
            if message.get("id") == req_id and "method" not in message:
                return message
            if "method" in message and "id" in message:
                await self._answer_server_request(message)
            elif message.get("method") == "notifications/tools/list_changed":
                self._tools_changed = True
            # Other notifications and stale replies are ignored.

    async def _answer_server_request(self, message: dict[str, Any]) -> None:
        if message["method"] == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"Method not supported: {message['method']}"},
            }
        await self.transport.send_message(reply)

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        await self.transport.send_message(payload)

    # ------------------------------------------------------------ negotiation

    async def initialize(
        self,
        client_info: McpImplementation | None = None,
    ) -> dict[str, Any]:
        """Agree on a protocol version with the server (see the module docstring).

        Returns ``protocolVersion``, ``capabilities`` and ``serverInfo`` for either era;
        a modern session also returns ``supportedVersions``.
        """
        if client_info is not None:
            self.client_info = client_info
        wanted = self.requested_version
        if wanted is None or is_modern_version(wanted):
            discovered = await self._discover(pinned=wanted)
            if discovered is not None:
                return discovered
        return await self._handshake(wanted if wanted in HANDSHAKE_PROTOCOL_VERSIONS else None)

    async def _discover(self, *, pinned: str | None) -> dict[str, Any] | None:
        """Probe ``server/discover``; the adopted result, or ``None`` for a legacy server."""
        version = pinned or MODERN_PROTOCOL_VERSIONS[0]
        probe_timeout = (
            None
            if self.transport.kind == "http"
            else min(self.discover_timeout, self.request_timeout)
        )
        for attempt in range(2):
            try:
                raw = await self._exchange(
                    "server/discover", {}, version=version, timeout=probe_timeout
                )
            except McpJsonRpcError as exc:
                if exc.code == UNSUPPORTED_PROTOCOL_VERSION:
                    supported = _supported_list(exc.details.get("rpc_data"))
                    mutual = [v for v in MODERN_PROTOCOL_VERSIONS if v in supported]
                    if mutual and attempt == 0 and pinned is None:
                        version = mutual[0]
                        continue
                    if pinned is None and any(v in HANDSHAKE_PROTOCOL_VERSIONS for v in supported):
                        return None  # a dual-era server without our modern version
                    raise McpProtocolVersionMismatchError(
                        supported[0] if supported else "<none>", SUPPORTED_PROTOCOL_VERSIONS
                    ) from exc
                if exc.code in MODERN_ERROR_CODES:
                    raise  # a modern server refused the request itself
                if pinned is not None:
                    raise _legacy_only(pinned) from exc
                return None
            except McpTransportError as exc:
                status = exc.details.get("http_status")
                legacy = status in _LEGACY_HTTP_STATUSES or (
                    exc.details.get("timed_out") and self.transport.kind != "http"
                )
                if not legacy:
                    raise
                if pinned is not None:
                    raise _legacy_only(pinned) from exc
                return None
            if not isinstance(raw, dict) or not isinstance(raw.get("supportedVersions"), list):
                if pinned is not None:
                    raise McpIntegrationError(
                        "MCP server answered server/discover with an invalid result.",
                        details={"error_code": "MCP_INVALID_RESULT"},
                    )
                return None  # not evidence of a modern server
            supported = [str(v) for v in raw["supportedVersions"]]
            chosen = version if version in supported else None
            if chosen is None:
                chosen = next((v for v in MODERN_PROTOCOL_VERSIONS if v in supported), None)
            if chosen is None or (pinned is not None and chosen != pinned):
                if pinned is None and any(v in HANDSHAKE_PROTOCOL_VERSIONS for v in supported):
                    return None  # it answers discover but advertises only handshake versions
                raise McpProtocolVersionMismatchError(
                    supported[0] if supported else "<none>", SUPPORTED_PROTOCOL_VERSIONS
                )
            return self._adopt(chosen, raw)
        raise AssertionError("unreachable")  # pragma: no cover

    def _adopt(self, version: str, raw: dict[str, Any]) -> dict[str, Any]:
        self.era = "modern"
        self.server_version = version
        self.server_supported_versions = [str(v) for v in raw.get("supportedVersions", [])]
        meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
        info = meta.get(META_SERVER_INFO) if isinstance(meta, dict) else None
        self.server_info = dict(info) if isinstance(info, dict) else {}
        caps = raw.get("capabilities")
        self.server_capabilities = McpServerCapabilities(**(caps if isinstance(caps, dict) else {}))
        instructions = raw.get("instructions")
        self.instructions = instructions if isinstance(instructions, str) else None
        self._record_cache_hint("server/discover", raw)
        self._is_initialized = True
        return {
            "protocolVersion": version,
            "supportedVersions": list(self.server_supported_versions),
            "capabilities": dict(caps) if isinstance(caps, dict) else {},
            "serverInfo": dict(self.server_info),
            **({"instructions": self.instructions} if self.instructions else {}),
        }

    async def _handshake(self, offer: str | None) -> dict[str, Any]:
        """The legacy ``initialize`` / ``notifications/initialized`` exchange."""
        init_params = {
            "protocolVersion": offer or LATEST_HANDSHAKE_VERSION,
            "capabilities": {},
            "clientInfo": self.client_info.model_dump(),
        }
        result = await self._exchange("initialize", init_params, version=None)
        if not isinstance(result, dict):
            raise McpIntegrationError(
                "MCP server answered initialize with an invalid result.",
                details={"error_code": "MCP_INVALID_RESULT"},
            )
        negotiated_version = result.get("protocolVersion", "")
        if negotiated_version not in HANDSHAKE_PROTOCOL_VERSIONS:
            raise McpProtocolVersionMismatchError(
                str(negotiated_version),
                SUPPORTED_PROTOCOL_VERSIONS,
            )

        self.era = "legacy"
        self.server_version = negotiated_version
        self.server_info = result.get("serverInfo", {})
        self.server_capabilities = McpServerCapabilities(**result.get("capabilities", {}))

        # Send initialized confirmation notification
        await self._send_notification("notifications/initialized")
        self._is_initialized = True
        return dict(result)

    # ----------------------------------------------------------------- tools

    async def list_tools(self) -> list[McpToolDefinition]:
        """Discover tools exposed by the MCP server and record their integrity digests.

        Over HTTP on a 2026-07-28 session, a tool whose ``x-mcp-header`` annotations break
        the specification is left out (with a warning), as the specification requires.
        """
        result = await self._send_request("tools/list")
        raw_tools = result.get("tools", [])
        tools: list[McpToolDefinition] = []
        digests: dict[str, str] = {}
        header_maps: dict[str, dict[tuple[str, ...], str]] = {}
        mirror = self.is_modern and self.transport.mirrors_headers

        for item in raw_tools:
            tool_def = McpToolDefinition(**item)
            if mirror:
                try:
                    paths = header_paths(tool_def.inputSchema)
                except InvalidHeaderAnnotation as exc:
                    logger.warning("MCP tool %r left out: %s", tool_def.name, exc)
                    continue
                if paths:
                    header_maps[tool_def.name] = paths
            tools.append(tool_def)
            # Record canonical digest to detect subsequent tool replacement/poisoning
            tool_digest = canonical_digest(
                {
                    "name": tool_def.name,
                    "inputSchema": tool_def.inputSchema,
                }
            )
            digests[tool_def.name] = tool_digest

        # Replace, not merge, so tools the server removed are no longer known.
        self._known_tool_digests = digests
        self._tools_listed_at = time.monotonic()
        self._tools_changed = False
        if mirror:
            self.transport.set_tool_headers(header_maps)
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        expected_digest: str | None = None,
    ) -> McpToolCallResult:
        """Invoke a remote tool, verifying schema integrity against tool replacement attacks."""
        known_digest = expected_digest or self._known_tool_digests.get(name)
        if expected_digest is not None and (
            self._tools_changed
            or self._tools_listed_at is None
            or time.monotonic() - self._tools_listed_at >= self.tool_list_max_age
        ):
            # A pinned caller must see the server's current definition, not a stale cache.
            await self.list_tools()
        if known_digest is not None:
            current_digest = self._known_tool_digests.get(name)
            if current_digest is None and expected_digest is not None:
                current_digest = "<removed>"
            if current_digest is not None and current_digest != known_digest:
                raise McpToolReplacementError(name, known_digest, current_digest)

        call_params = {
            "name": name,
            "arguments": arguments or {},
        }
        res = await self._send_request("tools/call", call_params)

        content_blocks = [McpContentBlock(**_present(b)) for b in res.get("content", [])]
        is_error = bool(res.get("isError", False))
        return McpToolCallResult(content=content_blocks, isError=is_error)

    # ------------------------------------------------------ resources/prompts

    async def list_resources(self) -> list[McpResourceDefinition]:
        """List resources available on the MCP server."""
        result = await self._send_request("resources/list")
        return [McpResourceDefinition(**_present(r)) for r in result.get("resources", [])]

    async def read_resource(self, uri: str) -> list[McpContentBlock]:
        """Read content of a specified MCP resource URI."""
        result = await self._send_request("resources/read", {"uri": uri})
        return [McpContentBlock(**_present(c)) for c in result.get("contents", [])]

    async def list_prompts(self) -> list[McpPromptDefinition]:
        """List prompt templates offered by the MCP server."""
        result = await self._send_request("prompts/list")
        return [McpPromptDefinition(**_present(p)) for p in result.get("prompts", [])]

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve a rendered prompt template from the MCP server."""
        result = await self._send_request(
            "prompts/get",
            {"name": name, "arguments": arguments or {}},
        )
        return dict(result) if isinstance(result, dict) else {}

    async def close(self) -> None:
        """Close the client session and associated transport."""
        await self.transport.close()


def _present(item: Any) -> dict[str, Any]:
    """The fields of a server object that are set (servers may send explicit nulls)."""
    if not isinstance(item, dict):
        raise McpIntegrationError(
            "MCP server sent a list entry that is not an object.",
            details={"error_code": "MCP_INVALID_RESULT"},
        )
    return {k: v for k, v in item.items() if v is not None}


def _supported_list(data: Any) -> list[str]:
    supported = data.get("supported") if isinstance(data, dict) else None
    return [str(v) for v in supported] if isinstance(supported, list) else []


def _legacy_only(pinned: str) -> McpProtocolVersionMismatchError:
    error = McpProtocolVersionMismatchError("<handshake only>", [pinned])
    error.details["hint"] = "The server does not speak the stateless revision."
    return error
