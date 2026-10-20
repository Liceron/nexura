# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Transport implementations for Model Context Protocol communication."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlsplit

import httpx

from nexura_agents.mcp.errors import (
    McpCommandNotAllowedError,
    McpTransportError,
)
from nexura_agents.network.client import SafeHttpClient
from nexura_agents.network.policy import DestinationPolicy


class McpTransport(ABC):
    """Abstract base transport for bi-directional JSON-RPC messaging."""

    # How long a client session waits for one reply unless told otherwise.
    request_timeout: float = 60.0

    @abstractmethod
    async def send_message(self, message: dict[str, Any]) -> None:
        """Send a JSON-RPC message."""
        ...

    @abstractmethod
    async def receive_message(self, timeout: float | None = None) -> dict[str, Any]:
        """Receive the next incoming JSON-RPC message."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the transport and release all associated resources."""
        ...


class McpInMemoryTransport(McpTransport):
    """Fast, safe in-memory transport using paired asynchronous queues."""

    def __init__(
        self,
        incoming: asyncio.Queue[dict[str, Any]],
        outgoing: asyncio.Queue[dict[str, Any]],
    ) -> None:
        self._incoming = incoming
        self._outgoing = outgoing
        self._is_closed = False

    async def send_message(self, message: dict[str, Any]) -> None:
        if self._is_closed:
            raise McpTransportError("Cannot send message on closed in-memory transport.")
        await self._outgoing.put(message)

    async def receive_message(self, timeout: float | None = None) -> dict[str, Any]:
        if self._is_closed:
            raise McpTransportError("Cannot receive message on closed in-memory transport.")
        try:
            if timeout is not None:
                return await asyncio.wait_for(self._incoming.get(), timeout=timeout)
            return await self._incoming.get()
        except TimeoutError as exc:
            raise McpTransportError("Timed out waiting for message.") from exc

    async def close(self) -> None:
        self._is_closed = True

    @classmethod
    def create_pair(cls) -> tuple[McpInMemoryTransport, McpInMemoryTransport]:
        """Create a paired client and server in-memory transport."""
        q1: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        q2: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        client_transport = cls(incoming=q2, outgoing=q1)
        server_transport = cls(incoming=q1, outgoing=q2)
        return client_transport, server_transport


class McpStdioTransport(McpTransport):
    """Subprocess stdio transport with strict command allowlisting and process isolation."""

    DEFAULT_ALLOWED_COMMANDS = frozenset({"python", "python3", "uv", "node", "npx"})
    # asyncio's own 64 KiB line limit is too small for real tool results.
    DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024
    STDERR_TAIL_BYTES = 8192
    # Flags that make an allowed interpreter run code taken from the command line instead of a
    # server module or script. They turn "allowlisted launcher" into "arbitrary code".
    INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-p", "--eval", "--print", "--exec"})

    def __init__(
        self,
        command: list[str],
        *,
        allowed_commands: set[str] | frozenset[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        allow_inline_code: bool = False,
        enforce_inline_code_check: bool | None = None,
    ) -> None:
        if not command:
            raise McpTransportError("Subprocess command cannot be empty.")

        self.command = command
        self.allowed_commands = (
            allowed_commands if allowed_commands is not None else self.DEFAULT_ALLOWED_COMMANDS
        )
        self.env = env
        self.cwd = cwd
        self.max_line_bytes = max_line_bytes
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._is_closed = False
        self._lock = asyncio.Lock()

        # Enforce command allowlist. A path is accepted only when it IS the allowed launcher
        # (this interpreter, or what PATH resolves the name to) or is listed verbatim; a
        # binary merely *named* "python" elsewhere on disk is not.
        cmd_base = os.path.basename(command[0])
        explicit = command[0] in self.allowed_commands
        by_name = cmd_base in self.allowed_commands and self._is_trusted_launcher(command[0])
        if not explicit and not by_name:
            raise McpCommandNotAllowedError(command[0], sorted(list(self.allowed_commands)))
        # An explicit allowed_commands set is the operator's own opt-in for those launchers;
        # inline-code flags are blocked only under the platform default allowlist.
        # ``enforce_inline_code_check=True`` applies the check to a caller-built default set too.
        enforce = (
            allowed_commands is None
            if enforce_inline_code_check is None
            else (enforce_inline_code_check)
        )
        bad = None
        if not allow_inline_code and enforce:
            bad = _inline_code_argument(command)
        if bad is None and not allow_inline_code:
            # Environment overrides are checked under any allowlist: choosing which launchers
            # may run says nothing about letting the caller change what they load.
            bad = _code_loading_env_key(env)
        if bad is not None:
            raise McpCommandNotAllowedError(
                f"{command[0]} {bad}",
                sorted(list(self.allowed_commands)),
            )

    @staticmethod
    def _is_trusted_launcher(command: str) -> bool:
        """A bare name, or a path that resolves to the same file PATH or this interpreter uses."""
        if os.sep not in command and (os.altsep is None or os.altsep not in command):
            return True
        target = os.path.realpath(command)
        trusted = {os.path.realpath(sys.executable)}
        found = shutil.which(os.path.basename(command))
        if found:
            trusted.add(os.path.realpath(found))
        return target in trusted

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            clean_env = dict(os.environ)
            if self.env:
                clean_env.update(self.env)
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_env,
                cwd=self.cwd,
                limit=self.max_line_bytes,
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._process))
        return self._process

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Keep reading stderr so a chatty server never blocks on a full pipe."""
        if proc.stderr is None:
            return
        while chunk := await proc.stderr.read(4096):
            self._stderr_tail += chunk
            del self._stderr_tail[: -self.STDERR_TAIL_BYTES]

    def _stderr_suffix(self) -> str:
        tail = self._stderr_tail.decode("utf-8", errors="replace").strip()
        return f" Server stderr (tail): {tail}" if tail else ""

    async def send_message(self, message: dict[str, Any]) -> None:
        if self._is_closed:
            raise McpTransportError("Cannot send message on closed stdio transport.")
        proc = await self._ensure_process()
        if proc.stdin is None or proc.returncode is not None:
            raise McpTransportError(f"Subprocess terminated with code {proc.returncode}.")
        line = json.dumps(message, ensure_ascii=False) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()

    async def receive_message(self, timeout: float | None = None) -> dict[str, Any]:
        if self._is_closed:
            raise McpTransportError("Cannot receive message on closed stdio transport.")
        proc = await self._ensure_process()
        if proc.stdout is None or proc.returncode is not None:
            raise McpTransportError(f"Subprocess terminated with code {proc.returncode}.")

        async def _read_line() -> str:
            assert proc.stdout is not None
            try:
                raw = await proc.stdout.readline()
            except ValueError as exc:  # line exceeded the stream limit
                raise McpTransportError(
                    f"Stdio message exceeded {self.max_line_bytes} bytes."
                ) from exc
            if not raw:
                raise McpTransportError(
                    "Subprocess closed stdout stream unexpectedly." + self._stderr_suffix()
                )
            return raw.decode("utf-8").strip()

        try:
            if timeout is not None:
                line_str = await asyncio.wait_for(_read_line(), timeout=timeout)
            else:
                line_str = await _read_line()
            parsed = json.loads(line_str)
            if not isinstance(parsed, dict):
                raise McpTransportError("Expected JSON object from stdio.")
            return parsed
        except TimeoutError as exc:
            raise McpTransportError("Timed out reading line from stdio.") from exc
        except json.JSONDecodeError as exc:
            raise McpTransportError(f"Malformed JSON from stdio: {exc}") from exc

    async def close(self) -> None:
        async with self._lock:
            self._is_closed = True
            if self._process is not None:
                proc = self._process
                self._process = None
                if self._stderr_task is not None:
                    self._stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._stderr_task
                    self._stderr_task = None

                # Close stdin
                if proc.stdin and not proc.stdin.is_closing():
                    with contextlib.suppress(Exception):
                        proc.stdin.close()

                # Terminate process if still active
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        with contextlib.suppress(Exception):
                            await proc.wait()

                # Close the pipe transport while the loop is running. A pipe whose reader
                # was paused (an over-limit line left unread data) never sees EOF, so the
                # transport stays open after the process exits; it would otherwise be closed
                # by its finalizer after the loop has closed ("Event loop is closed").
                # asyncio.subprocess.Process has no public close().
                transport = getattr(proc, "_transport", None)
                if transport is not None:
                    with contextlib.suppress(Exception):
                        transport.close()


_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)*)?(\.exe)?$")
_NODE_RE = re.compile(r"^node(\.exe)?$")
_NODE_LONG_CODE = (
    "--eval",
    "--print",
    "--require",
    "--import",
    "--loader",
    "--experimental-loader",
)
_NODE_SHORT_CODE = frozenset("epr")


def _python_inline_code(args: list[str]) -> str | None:
    """The offending python argument, or None. Scans interpreter options up to the script/-m."""
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "-":
            return arg  # program read from stdin
        if arg.startswith("--"):
            if arg.split("=", 1)[0] in ("--command", "--exec", "--eval"):
                return arg
            continue
        if not arg.startswith("-"):
            return None  # the script path; everything after belongs to the script
        for pos, ch in enumerate(arg[1:], start=1):
            if ch in "ci":  # -c runs inline code; -i opens a REPL on stdin (the JSON-RPC pipe)
                return arg
            if ch == "m":
                return None  # module name follows; the rest are the module's arguments
            if ch in "WX":  # takes a value: attached or the next argument
                if pos == len(arg) - 1:
                    i += 1
                break
    return "(no script: program read from stdin)"


def _node_inline_code(args: list[str]) -> str | None:
    """The offending node argument, or None. Scans options up to the script path."""
    for arg in args:
        if arg == "-":
            return arg  # program read from stdin
        if not arg.startswith("-"):
            return None
        if arg.startswith("--"):
            if arg.split("=", 1)[0] in (*_NODE_LONG_CODE, "--interactive"):
                return arg
            continue
        if (_NODE_SHORT_CODE | {"i"}) & set(arg[1:]):
            return arg
    return "(no script: program read from stdin)"


# Environment variables that change which program or code an allowed launcher runs: the
# env-var equivalent of ``python -c`` / ``node -e``. Whole families are refused by prefix so a
# variable added to a runtime later is covered; a few harmless, common ones are allowed.
CODE_LOADING_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "GCONV_PATH",
        "BASH_ENV",
        "ENV",
        "SHELL",
        "COMSPEC",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)
CODE_LOADING_ENV_PREFIXES = (
    "LD_",
    "DYLD_",
    "PYTHON",
    "NODE_",
    "NPM_CONFIG_",
    "YARN_",
    "PNPM_",
    "UV_",
    "PIP_",
    "XDG_",
    "PERL",
    "RUBY",
    "GEM_",
    "BUNDLE_",
    "CONDA",
    "VIRTUAL_ENV",
    "DENO_",
    "BUN_",
)
SAFE_ENV_KEYS = frozenset(
    {
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUTF8",
        "PYTHONHASHSEED",
        "NODE_ENV",
        "NODE_NO_WARNINGS",
    }
)


def _code_loading_env_key(env: dict[str, str] | None) -> str | None:
    """Return the first caller-supplied variable that could change what the launcher runs.

    Covers the search path and home/config directories, the dynamic loader (``LD_*``,
    ``DYLD_*``), interpreter start-up hooks and module paths (``PYTHON*``, ``NODE_*``), package
    managers' settings and indexes (``NPM_CONFIG_*``, ``UV_*``, ``PIP_*``), and trust stores.
    Names are compared case-insensitively. ``SAFE_ENV_KEYS`` are allowed.
    """
    for key in env or {}:
        upper = key.upper()
        if upper in SAFE_ENV_KEYS:
            continue
        if upper in CODE_LOADING_ENV_KEYS or upper.startswith(CODE_LOADING_ENV_PREFIXES):
            return f"env {key}"
    return None


def _inline_code_argument(command: list[str]) -> str | None:
    """Return the argument that makes the launcher run command-line code, if any.

    Only the options before the script or module are examined: after it, arguments belong
    to the server. ``uv``/``uvx`` are followed into the interpreter they launch, and
    ``npx -c/--call`` (a shell command) is refused.
    """
    base = os.path.basename(command[0]).lower()
    args = command[1:]
    if base in ("uv", "uvx", "uv.exe"):
        for index, arg in enumerate(args):
            name = os.path.basename(arg).lower()
            if _PYTHON_RE.match(name) or _NODE_RE.match(name):
                return _inline_code_argument([arg, *args[index + 1 :]])
        return None
    if _PYTHON_RE.match(base):
        return _python_inline_code(args)
    if _NODE_RE.match(base):
        return _node_inline_code(args)
    if base in ("npx", "npx.cmd"):
        for arg in args:
            if not arg.startswith("-"):
                return None
            if arg.split("=", 1)[0] in ("--call", "-c") or arg.startswith("-c"):
                return arg
    return None


class McpHttpTransport(McpTransport):
    """MCP "Streamable HTTP" transport, bounded by a DestinationPolicy.

    Each JSON-RPC message is POSTed to one endpoint. Replies come back as JSON or as a
    server-sent event stream; the ``Mcp-Session-Id`` the server assigns is sent on
    every later request, and the session is closed with ``DELETE`` on ``close()``.
    """

    def __init__(
        self,
        endpoint_url: str,
        *,
        headers: dict[str, str] | None = None,
        policy: DestinationPolicy | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.headers = dict(headers or {})
        base = policy or DestinationPolicy(allow_loopback=True, allow_private_ranges=True)
        # The endpoint is the operator's explicit choice; trust its host. Work on a copy so a
        # policy shared with other tools is never widened. No redirect is ever followed (see
        # ``send_message``), so the trust never extends to another destination.
        host = urlsplit(endpoint_url).hostname
        self.policy = base.model_copy(deep=True)
        if host and host not in self.policy.allowed_hosts:
            self.policy.allowed_hosts = [*self.policy.allowed_hosts, host]
        self.timeout = timeout
        self.request_timeout = timeout
        self._client = SafeHttpClient(policy=self.policy, timeout=self.timeout, transport=transport)
        self._response_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._is_closed = False
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    def _request_headers(self) -> dict[str, str]:
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def send_message(self, message: dict[str, Any]) -> None:
        if self._is_closed:
            raise McpTransportError("Cannot send message on closed HTTP transport.")
        resp = await self._client.post(
            self.endpoint_url,
            headers=self._request_headers(),
            json=message,
            follow_redirects=False,
        )
        if resp.is_redirect:
            raise McpTransportError(
                f"MCP endpoint answered with a redirect (HTTP {resp.status_code}); "
                "redirects are not followed."
            )
        if resp.status_code == 404 and self.session_id:
            raise McpTransportError("MCP session expired or unknown to the server (HTTP 404).")
        if resp.status_code in (202, 204):
            return  # notification or response accepted; nothing to read
        if resp.status_code != 200:
            raise McpTransportError(f"MCP HTTP request failed with status code {resp.status_code}.")
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id

        content_type = resp.headers.get("content-type", "")
        try:
            if "text/event-stream" in content_type:
                messages = _parse_sse_messages(resp.text)
            else:
                body = resp.json()
                messages = body if isinstance(body, list) else [body]
        except ValueError as exc:
            raise McpTransportError("Failed to parse HTTP JSON-RPC response.") from exc
        for item in messages:
            if isinstance(item, dict):
                result = item.get("result")
                if isinstance(result, dict) and "protocolVersion" in result:
                    self.protocol_version = str(result["protocolVersion"])
                await self._response_queue.put(item)

    async def receive_message(self, timeout: float | None = None) -> dict[str, Any]:
        if self._is_closed:
            raise McpTransportError("Cannot receive message on closed HTTP transport.")
        try:
            if timeout is not None:
                return await asyncio.wait_for(self._response_queue.get(), timeout=timeout)
            return await self._response_queue.get()
        except TimeoutError as exc:
            raise McpTransportError("Timed out waiting for HTTP response.") from exc

    async def close(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        if self.session_id:
            with contextlib.suppress(Exception):
                await self._client.request(
                    "DELETE",
                    self.endpoint_url,
                    headers=self._request_headers(),
                    follow_redirects=False,
                )
        await self._client.close()


def _parse_sse_messages(text: str) -> list[Any]:
    """JSON payloads of each ``data:`` event in a server-sent event body."""
    messages: list[Any] = []
    data: list[str] = []
    # The SSE spec splits only on CRLF, CR and LF; str.splitlines() would also break on
    # U+2028, U+0085 and friends that may appear inside JSON strings.
    for line in [*re.split(r"\r\n|\r|\n", text), ""]:
        if line == "":
            if data:
                messages.append(json.loads("\n".join(data)))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    return messages
