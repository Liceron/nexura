# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""TEST ONLY: a Postgres wire proxy that counts round trips and can add network latency.

``RoundTripProxy`` listens on a loopback port and forwards every connection to a real
Postgres server. It reads the client side of the protocol to count:

* **round trips**: each time the client sends after the server has answered (pipelined
  messages in one turn count once). Connection setup turns are counted separately;
* **statements**: simple-protocol queries (``Q``, for example ``BEGIN``/``COMMIT``) and
  extended-protocol parses (``P``), with their SQL text, so a test can see what ran;
* **connections** opened through the proxy.

``delay`` seconds are added to every server-to-client chunk (order kept), so each round trip
costs ``delay`` more: 0.025 models a Cloud Run to Neon hop. The proxy needs a plain-text
connection (``proxied_url`` sets ``sslmode=disable``); it is for local test servers only.
It runs on its own thread and loop, so callers that block on the database cannot stall it.
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SSL_REQUEST = 80877103
_GSSENC_REQUEST = 80877104
_CANCEL_REQUEST = 80877102


@dataclass
class WireStats:
    """Counters since the last ``reset``."""

    round_trips: int = 0
    setup_round_trips: int = 0
    connections: int = 0
    #: SQL executed: simple queries and extended-protocol statements that were executed.
    statements: list[str] = field(default_factory=list)
    #: Parse round trips that only described a statement (asyncpg prepares, then re-parses
    #: an unnamed statement together with its execution).
    prepares: int = 0

    @property
    def query_round_trips(self) -> int:
        """Round trips after connection setup."""
        return self.round_trips - self.setup_round_trips

    def count(self, prefix: str) -> int:
        """Statements whose SQL starts with ``prefix`` (case-insensitive)."""
        p = prefix.upper()
        return sum(1 for s in self.statements if s.lstrip().upper().startswith(p))


class _Connection:
    def __init__(self, proxy: RoundTripProxy) -> None:
        self.proxy = proxy
        self.buffer = b""
        self.untyped = True  # startup-phase messages carry no type byte
        self.ready = False  # set once the first query is sent
        self.last_from_client = False
        self.parsed: list[str] = []  # Parse texts of this turn not yet executed


class RoundTripProxy:
    """TEST ONLY: loopback proxy to ``host:port``; see the module docstring."""

    def __init__(self, host: str, port: int, *, delay: float = 0.0) -> None:
        self.upstream = (host, port)
        self.delay = delay
        self._lock = threading.Lock()
        self._stats = WireStats()
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.base_events.Server | None = None
        self._thread = threading.Thread(target=self._loop.run_forever, name="pg-wire-proxy")
        self._thread.daemon = True
        self._tasks: set[asyncio.Task[Any]] = set()
        self.port = 0

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> RoundTripProxy:
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._start(), self._loop)
        future.result(10)
        return self

    async def _start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def close(self) -> None:
        async def stop() -> None:
            if self._server is not None:
                self._server.close()
            for task in list(self._tasks):
                task.cancel()
            await asyncio.sleep(0)

        if self._thread.is_alive():
            asyncio.run_coroutine_threadsafe(stop(), self._loop).result(10)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(10)

    def __enter__(self) -> RoundTripProxy:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- counters ---------------------------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._stats = WireStats()

    def snapshot(self) -> WireStats:
        with self._lock:
            s = self._stats
            return WireStats(
                s.round_trips, s.setup_round_trips, s.connections, list(s.statements), s.prepares
            )

    def proxied_url(self, url: str) -> str:
        """``url`` pointed at this proxy, with ``sslmode=disable`` (credentials kept)."""
        parts = urlsplit(url)
        netloc = parts.netloc
        userinfo = netloc.rsplit("@", 1)[0] + "@" if "@" in netloc else ""
        query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"sslmode", "ssl"}]
        query.append(("sslmode", "disable"))
        return urlunsplit(
            parts._replace(netloc=f"{userinfo}127.0.0.1:{self.port}", query=urlencode(query))
        )

    # -- forwarding -------------------------------------------------------------------------
    async def _handle(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            sreader, swriter = await asyncio.open_connection(*self.upstream)
        except OSError:
            cwriter.close()
            self._tasks.discard(task)
            return
        state = _Connection(self)
        with self._lock:
            self._stats.connections += 1
        up = asyncio.ensure_future(self._client_to_server(creader, swriter, state))
        down = asyncio.ensure_future(self._server_to_client(sreader, cwriter, state))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (up, down):
                t.cancel()
            for w in (swriter, cwriter):
                w.close()
            self._tasks.discard(task)

    async def _client_to_server(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: _Connection
    ) -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            self._observe(state, data)
            writer.write(data)
            await writer.drain()

    async def _server_to_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: _Connection
    ) -> None:
        pending: deque[tuple[float, bytes]] = deque()
        wake = asyncio.Event()
        done = False

        async def deliver() -> None:
            while True:
                while not pending:
                    if done:
                        return
                    wake.clear()
                    await wake.wait()
                due, chunk = pending.popleft()
                wait = due - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                writer.write(chunk)
                await writer.drain()

        sender = asyncio.ensure_future(deliver())
        try:
            while True:
                data = await reader.read(65536)
                state.last_from_client = False
                if not data:
                    done = True
                    wake.set()
                    await sender
                    return
                pending.append((time.monotonic() + self.delay, data))
                wake.set()
        finally:
            sender.cancel()

    def _observe(self, state: _Connection, data: bytes) -> None:
        statements: list[str] = []
        prepares = 0
        state.buffer += data
        buf = state.buffer
        while True:
            if state.untyped:
                if len(buf) < 8:
                    break
                (length,) = struct.unpack("!i", buf[:4])
                if len(buf) < length:
                    break
                (code,) = struct.unpack("!i", buf[4:8])
                buf = buf[length:]
                if code not in (_SSL_REQUEST, _GSSENC_REQUEST, _CANCEL_REQUEST):
                    state.untyped = False
                continue
            if len(buf) < 5:
                break
            kind = buf[:1]
            (length,) = struct.unpack("!i", buf[1:5])
            if len(buf) < 1 + length:
                break
            body = buf[5 : 1 + length]
            buf = buf[1 + length :]
            if kind == b"Q":
                statements.append(body.split(b"\0", 1)[0].decode("utf-8", "replace"))
            elif kind == b"P":
                _, rest = body.split(b"\0", 1)
                state.parsed.append(rest.split(b"\0", 1)[0].decode("utf-8", "replace"))
            elif kind == b"E":
                statements.append(state.parsed.pop() if state.parsed else "<prepared statement>")
            elif kind == b"S":
                prepares += len(state.parsed)
                state.parsed.clear()
        state.buffer = buf
        with self._lock:
            new_turn = not state.last_from_client
            if new_turn:
                self._stats.round_trips += 1
                if not state.ready and not statements and not prepares:
                    self._stats.setup_round_trips += 1
            if statements or prepares:
                state.ready = True
            self._stats.statements.extend(statements)
            self._stats.prepares += prepares
        state.last_from_client = True


__all__ = ["RoundTripProxy", "WireStats"]
