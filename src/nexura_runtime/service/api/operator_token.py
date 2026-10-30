# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Remote single-operator mode: one operator token authenticates every request (P38 C4).

The open-source service normally serves only this machine (``local-owner`` profile, loopback
peers, per-session token). ``OperatorTokenAuthProvider`` lets the same single owner use the
service from another machine, behind a TLS reverse proxy:

* **One token.** At least 43 characters that look random (:func:`validate_operator_token`
  refuses short, single-class, repeating and low-estimate tokens; it cannot measure real
  entropy), read from a file (``--token-file`` / ``NEXURA_OPERATOR_TOKEN_FILE``, preferred:
  opened once with ``O_NOFOLLOW``, owned by this user, mode 0600) or ``NEXURA_OPERATOR_TOKEN``.
  Never a command-line value.
* **Stored hashed.** Only ``sha256(token)`` is kept; digests are compared with
  ``hmac.compare_digest``.
* **Every route but the probes.** ``OperatorTokenMiddleware`` requires
  ``Authorization: Bearer <token>`` on every request except ``/healthz`` and ``/readyz``, which
  never show details in this mode. On a webhook delivery route (``POST
  /api/v1/triggers/webhooks/{id}/deliver``) it accepts only that endpoint's own delivery token
  (:class:`DeliveryTokenStore`), never the operator token. A non-loopback request that did not
  arrive over HTTPS is refused before any token is read, unless ``allow_plain_http``.
* **Uniform refusals, limited guessing.** Missing, malformed and wrong credentials get the same
  ``401``. After ``max_failures`` failures from one address within ``failure_window`` seconds
  that address gets ``429`` for wrong or missing credentials; the right token always passes.
* **Never logged.** Log lines name the peer and the reason, never a credential.

The caller is the single local owner (``local-owner``, role ``admin``). This is not multi-user
support: accounts, roles, SSO and organisations are in the Nexura enterprise edition.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from nexura_runtime.service.api.auth_providers import _local_owner
from nexura_runtime.service.api.bind_guard import is_loopback_bind
from nexura_runtime.service.api.local_session import SESSION_TOKEN_PATH, is_loopback_client
from nexura_runtime.service.api.middleware import AuthFailure, route_path
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity

#: Environment variable holding the operator token.
OPERATOR_TOKEN_ENV = "NEXURA_OPERATOR_TOKEN"
#: Environment variable naming a file that holds the operator token.
OPERATOR_TOKEN_FILE_ENV = "NEXURA_OPERATOR_TOKEN_FILE"
#: Length of ``secrets.token_urlsafe(32)``: 32 random bytes, base64url encoded.
MIN_TOKEN_LENGTH = 43
#: A token with fewer distinct characters is a pattern, not random bytes.
MIN_DISTINCT_CHARACTERS = 12
#: Minimum of :func:`estimate_token_bits` (a random 43-character token scores about 200).
MIN_ESTIMATED_BITS = 160.0
#: A token that repeats any piece of this many characters is refused.
REPEAT_WINDOW = 8
#: A run of this many equal or consecutive characters (``aaaaaaaa``, ``abcdefgh``) is refused.
MAX_MONOTONE_RUN = 8
#: A token file larger than this is not a token file.
MAX_TOKEN_FILE_BYTES = 4096
#: How to make a good token; part of every refusal.
GENERATE_HINT = 'Generate one with: python -c "import secrets;print(secrets.token_urlsafe(32))"'
#: Paths that never need the token (liveness and minimal readiness probes).
PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})
#: Failed attempts one peer may make per window before it is refused with 429.
DEFAULT_MAX_FAILURES = 10
DEFAULT_FAILURE_WINDOW = 60.0
#: Peers tracked at once; the oldest entries are dropped beyond this.
MAX_TRACKED_PEERS = 10_000
#: Longer ``Authorization`` values are refused without hashing them.
MAX_CREDENTIAL_LENGTH = 4096

UNAUTHENTICATED_MESSAGE = (
    "Authentication required: send the operator token as Authorization: Bearer <token>."
)
#: The one answer to every refused webhook delivery (token, endpoint, signature, timestamp).
DELIVERY_REFUSED_MESSAGE = (
    "Delivery refused: send this endpoint's delivery token as Authorization: Bearer <token> "
    "and a valid v2 signature."
)
#: ``POST`` on this path is a webhook delivery; group 1 is the endpoint id.
DELIVERY_ROUTE = re.compile(r"^/api/v1/triggers/webhooks/([^/]+)/deliver/?$")
#: Table of per-endpoint delivery token hashes (in the service database).
DELIVERY_TOKEN_TABLE = "nexura_webhook_delivery_tokens"

logger = logging.getLogger("nexura.service.security")


class OperatorTokenError(ValueError):
    """The operator token is missing, too weak, or its file is unsafe. Never contains it."""


class PlainHttpNetworkError(ValueError):
    """Remote mode would accept the operator token over plain HTTP on a network address."""


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


#: Compared against when an endpoint has no delivery token, so every refusal costs the same.
_NO_TOKEN_DIGEST = _sha256("nexura-no-delivery-token:" + secrets.token_urlsafe(32))


class DeliveryTokenStore:
    """Per-endpoint webhook delivery tokens, stored as SHA-256 hashes (remote mode, M3).

    A delivery token is ``secrets.token_urlsafe(32)``. It is returned once when it is issued
    (webhook registration, or rotation) and only its hash is kept, in the table
    ``nexura_webhook_delivery_tokens`` of the service database. It authorises exactly
    ``POST /api/v1/triggers/webhooks/{endpoint_id}/deliver`` for its own endpoint.
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {DELIVERY_TOKEN_TABLE} ("
                "endpoint_id TEXT PRIMARY KEY, token_sha256 BLOB NOT NULL, created_at REAL)"
            )

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def issue(self, endpoint_id: str) -> str:
        """Create (or replace) the endpoint's delivery token and return it, once."""
        token = secrets.token_urlsafe(32)
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO {DELIVERY_TOKEN_TABLE} (endpoint_id, token_sha256, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(endpoint_id) DO UPDATE SET "
                "token_sha256 = excluded.token_sha256, created_at = excluded.created_at",
                (endpoint_id, _sha256(token), time.time()),
            )
        return token

    def matches(self, endpoint_id: str, supplied: str | None) -> bool:
        """Constant-time check of ``supplied`` against the endpoint's token hash."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT token_sha256 FROM {DELIVERY_TOKEN_TABLE} WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
        stored = bytes(row[0]) if row else _NO_TOKEN_DIGEST
        if not supplied or len(supplied) > MAX_CREDENTIAL_LENGTH:
            hmac.compare_digest(_NO_TOKEN_DIGEST, stored)
            return False
        return hmac.compare_digest(_sha256(supplied), stored) and row is not None

    def has_token(self, endpoint_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                f"SELECT 1 FROM {DELIVERY_TOKEN_TABLE} WHERE endpoint_id = ?", (endpoint_id,)
            ).fetchone()
        return row is not None

    def delete(self, endpoint_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                f"DELETE FROM {DELIVERY_TOKEN_TABLE} WHERE endpoint_id = ?", (endpoint_id,)
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def generate_operator_token() -> str:
    """A new operator token: ``secrets.token_urlsafe(32)``."""
    import secrets

    return secrets.token_urlsafe(32)


def estimate_token_bits(token: str) -> float:
    """A rough upper bound on the randomness of ``token`` in bits.

    Length times the Shannon entropy of its character frequencies. A real random token scores
    about 200 (``token_urlsafe(32)``) or 256 (``token_hex(32)``); repeated words score much
    lower. It cannot see dictionary words used once, which the other checks of
    :func:`validate_operator_token` catch in the obvious cases.
    """
    if not token:
        return 0.0
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(token)
    per_char = -sum((c / n) * math.log2(c / n) for c in counts.values())
    return n * per_char


def _character_classes(token: str) -> int:
    return sum(
        (
            any(ch.islower() for ch in token),
            any(ch.isupper() for ch in token),
            any(ch.isdigit() for ch in token),
            any(not ch.isalnum() for ch in token),
        )
    )


def _repeats_a_substring(token: str, size: int = REPEAT_WINDOW) -> bool:
    seen: set[str] = set()
    for i in range(len(token) - size + 1):
        chunk = token[i : i + size]
        if chunk in seen:
            return True
        seen.add(chunk)
    return False


def _longest_monotone_run(token: str) -> int:
    """Longest run of characters that stay equal or step by one (``aaaa``, ``abcd``, ``4321``)."""
    best = run = 1
    step: int | None = None
    for prev, cur in zip(token, token[1:], strict=False):
        diff = ord(cur) - ord(prev)
        if diff in (-1, 0, 1) and (step is None or diff == step):
            run, step = run + 1, diff
        elif diff in (-1, 0, 1):
            run, step = 2, diff
        else:
            run, step = 1, None
        best = max(best, run)
    return best


def validate_operator_token(token: str) -> str:
    """Return ``token`` if it can be an operator token, else raise ``OperatorTokenError``.

    The server cannot measure real entropy; it refuses tokens that are short, use one kind of
    character, repeat an 8-character piece, contain a long ``abcd``/``aaaa`` run, or score below
    ``MIN_ESTIMATED_BITS`` in :func:`estimate_token_bits`. Every refusal says how to generate
    a good token. The token itself is never part of the message.
    """
    if not isinstance(token, str):
        raise OperatorTokenError("The operator token must be a string.")
    if len(token) < MIN_TOKEN_LENGTH:
        raise OperatorTokenError(
            f"The operator token is too short ({len(token)} characters); use at least "
            f"{MIN_TOKEN_LENGTH} random characters. {GENERATE_HINT}"
        )
    if any(ch.isspace() or not ch.isprintable() or not ch.isascii() for ch in token):
        raise OperatorTokenError(
            "The operator token may only contain printable ASCII characters without spaces. "
            + GENERATE_HINT
        )
    if (
        len(set(token)) < MIN_DISTINCT_CHARACTERS
        or _character_classes(token) < 2
        or _repeats_a_substring(token)
        or _longest_monotone_run(token) >= MAX_MONOTONE_RUN
        or estimate_token_bits(token) < MIN_ESTIMATED_BITS
    ):
        raise OperatorTokenError(
            "The operator token looks like words or a pattern, not random bytes. " + GENERATE_HINT
        )
    return token


def read_token_file(path: str | os.PathLike[str]) -> str:
    """Read an operator token file through one descriptor, refusing an unsafe file.

    The path is opened once (``O_NOFOLLOW``: a symbolic link is refused; ``O_NONBLOCK``: a named
    pipe is refused at once instead of blocking) and every check is made
    on that descriptor with ``fstat`` before reading it, so the file that was checked is the file
    that is read. On POSIX the file must be a regular file owned by the effective user of this
    process whose mode grants nothing to group or others (``chmod 600``). Windows has neither
    mode bits nor ``O_NOFOLLOW``; there the folder's ACL must keep the file private.
    A UTF-8 byte-order mark and surrounding whitespace (a trailing newline) are removed.
    """
    name = str(path)
    # O_NONBLOCK: opening a named pipe (FIFO) returns at once instead of waiting for a writer,
    # so fstat below refuses it at startup; it changes nothing for a regular file.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(name, flags)
    except FileNotFoundError:
        raise OperatorTokenError(f"Operator token file {name!r} does not exist.") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OperatorTokenError(
                f"Operator token file {name!r} is a symbolic link; name the file itself."
            ) from None
        if exc.errno == errno.EISDIR:
            raise OperatorTokenError(
                f"Operator token file {name!r} is not a regular file."
            ) from None
        raise OperatorTokenError(
            f"Operator token file {name!r} cannot be read ({type(exc).__name__})."
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OperatorTokenError(f"Operator token file {name!r} is not a regular file.")
        if sys.platform != "win32":
            if info.st_uid != os.geteuid():
                raise OperatorTokenError(
                    f"Operator token file {name!r} is not owned by the user running the "
                    "service; create it as that user and run chmod 600 on it."
                )
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise OperatorTokenError(
                    f"Operator token file {name!r} is readable by other users (mode "
                    f"{stat.S_IMODE(info.st_mode):04o}); run chmod 600 on it."
                )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_TOKEN_FILE_BYTES:
                raise OperatorTokenError(
                    f"Operator token file {name!r} is too large to be a token file."
                )
            chunks.append(chunk)
    except OSError as exc:
        raise OperatorTokenError(
            f"Operator token file {name!r} cannot be read ({type(exc).__name__})."
        ) from None
    finally:
        os.close(fd)
    try:
        text = b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise OperatorTokenError(f"Operator token file {name!r} is not UTF-8 text.") from None
    return text.strip()


def load_operator_token(
    *, token_file: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """The operator token from ``token_file``, ``NEXURA_OPERATOR_TOKEN_FILE`` or
    ``NEXURA_OPERATOR_TOKEN`` (exactly one source), validated."""
    env = os.environ if environ is None else environ
    file_path = token_file if token_file else (env.get(OPERATOR_TOKEN_FILE_ENV) or None)
    env_token = env.get(OPERATOR_TOKEN_ENV)
    if env_token is not None and not env_token.strip():
        env_token = None
    if file_path and env_token is not None:
        raise OperatorTokenError(
            f"Both a token file and {OPERATOR_TOKEN_ENV} are set; give the operator token "
            "one way only."
        )
    if file_path:
        return validate_operator_token(read_token_file(file_path))
    if env_token is not None:
        return validate_operator_token(env_token.strip())
    raise OperatorTokenError(
        f"Remote single-operator mode needs an operator token: set {OPERATOR_TOKEN_ENV}, or "
        f"pass --token-file PATH (or {OPERATOR_TOKEN_FILE_ENV}) naming a chmod 600 file. "
        "The token is never accepted on the command line."
    )


def assert_remote_transport(listen_host: str | None, *, allow_plain_http: bool = False) -> None:
    """Refuse remote mode on a non-loopback address unless plain HTTP is explicitly allowed.

    The service speaks plain HTTP. Bound to loopback it can sit behind a TLS reverse proxy on
    the same host; bound to any other address the bearer token would cross the network in
    clear text. ``allow_plain_http=True`` (``--insecure-plain-http``) accepts that, for TLS
    terminated on another host or a private encrypted network, and logs a warning.
    """
    if is_loopback_bind(listen_host):
        return
    if allow_plain_http:
        logger.warning(
            "remote single-operator mode is listening on %r over plain HTTP; the operator "
            "token crosses the network unencrypted unless TLS is terminated in front of it",
            listen_host,
        )
        return
    raise PlainHttpNetworkError(
        f"Remote single-operator mode refuses to listen on non-loopback address "
        f"{listen_host!r} over plain HTTP: the operator token would cross the network "
        "unencrypted. Bind to 127.0.0.1 and put a TLS reverse proxy (Caddy, nginx) in front, "
        "see docs/remote-single-operator.md. If TLS is terminated elsewhere (another host, a "
        "VPN), pass --insecure-plain-http (allow_plain_http=True)."
    )


class OperatorTokenFailure(AuthFailure):
    """A refused request: ``401`` (any token problem) or ``429`` (too many failures)."""

    def __init__(self, status_code: int, code: str, message: str, retry_after: float = 0.0):
        super().__init__(status_code, code, message)
        self.retry_after = retry_after

    def response(self) -> JSONResponse:
        response = super().response()
        if self.status_code == 401:
            response.headers["WWW-Authenticate"] = 'Bearer realm="nexura"'
        if self.retry_after > 0:
            response.headers["Retry-After"] = str(max(1, int(self.retry_after + 0.999)))
        return response


class FailedAttemptLimiter:
    """Counts failed authentication attempts per peer in a sliding window.

    A peer with ``max_failures`` failures in the last ``window`` seconds is blocked until the
    oldest of them leaves the window. Attempts made while blocked are not counted, so a
    blocked peer is free again after at most one window. At most ``max_peers`` peers are
    tracked; the least recently failing ones are forgotten first.
    """

    def __init__(
        self,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window: float = DEFAULT_FAILURE_WINDOW,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_peers: int = MAX_TRACKED_PEERS,
    ) -> None:
        if max_failures < 1 or window <= 0 or max_peers < 1:
            raise ValueError("max_failures, window and max_peers must be positive.")
        self.max_failures = max_failures
        self.window = window
        self.max_peers = max_peers
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, peer: str, now: float) -> deque[float] | None:
        hits = self._failures.get(peer)
        if hits is None:
            return None
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if not hits:
            del self._failures[peer]
            return None
        return hits

    def retry_after(self, peer: str) -> float:
        """Seconds until ``peer`` may try again; 0 when it is not blocked."""
        now = self._clock()
        with self._lock:
            hits = self._prune(peer, now)
            if hits is None or len(hits) < self.max_failures:
                return 0.0
            return max(0.0, self.window - (now - hits[0]))

    def record_failure(self, peer: str) -> bool:
        """Count a failure; True when it blocks the peer."""
        now = self._clock()
        with self._lock:
            hits = self._prune(peer, now) or deque()
            hits.append(now)
            self._failures[peer] = hits
            self._failures.move_to_end(peer)
            while len(self._failures) > self.max_peers:
                self._failures.popitem(last=False)
            return len(hits) >= self.max_failures

    def reset(self, peer: str) -> None:
        """Forget the failures of ``peer`` (after it authenticated)."""
        with self._lock:
            self._failures.pop(peer, None)


def peer_address(scope: Scope) -> str:
    """The ASGI peer address (behind a trusted proxy, uvicorn puts the forwarded one here)."""
    client = scope.get("client")
    if not client:
        return "unknown"
    return str(client[0])


def bearer_credential(headers: Headers) -> str | None:
    """The value of ``Authorization: Bearer <value>``, or ``None``."""
    raw = headers.get("authorization")
    if raw is None:
        return None
    scheme, _, value = raw.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


class OperatorTokenAuthProvider:
    """``AuthProvider`` of the remote single-operator mode: one bearer token, one owner."""

    name = "operator-token"
    is_authenticating = True
    #: Marks the open-source single-operator provider: ``resolve_auth_provider`` accepts it with
    #: the ``local-owner`` profile (every other authenticating provider needs the shared one).
    single_operator = True

    def __init__(
        self,
        token: str,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        failure_window: float = DEFAULT_FAILURE_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._digest = self._hash(validate_operator_token(token))
        self.limiter = FailedAttemptLimiter(max_failures, failure_window, clock=clock)

    @classmethod
    def from_environment(
        cls,
        *,
        token_file: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> OperatorTokenAuthProvider:
        """Build the provider from ``token_file`` or the environment (``load_operator_token``).

        The environment variable is read, not removed: ``nexura-runtime serve`` removes it
        from ``os.environ`` itself, and it stays in ``/proc/<pid>/environ`` either way.
        """
        return cls(load_operator_token(token_file=token_file, environ=environ), **kwargs)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"

    @staticmethod
    def _hash(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def token_matches(self, supplied: str | None) -> bool:
        """Constant-time comparison of the SHA-256 digests of ``supplied`` and the token."""
        if not supplied or len(supplied) > MAX_CREDENTIAL_LENGTH:
            return False
        return hmac.compare_digest(self._hash(supplied), self._digest)

    def record_failure(self, peer: str, reason: str) -> None:
        """Count a failed attempt of ``peer`` and log it (the reason never holds a credential)."""
        blocked = self.limiter.record_failure(peer)
        logger.warning(
            "operator token rejected for peer %s (%s)%s",
            peer,
            reason,
            "; further attempts are refused for a while" if blocked else "",
        )

    def verify(
        self,
        headers: Headers,
        peer: str,
        *,
        method: str = "GET",
        path: str = "",
        delivery_tokens: DeliveryTokenStore | None = None,
    ) -> str:
        """Check the request's bearer; return ``"operator"`` or ``"delivery:<endpoint_id>"``.

        * ``POST`` on a webhook delivery route accepts only that endpoint's delivery token;
          every other route accepts only the operator token.
        * The right credential always passes, also while the peer is blocked (m5). So the block
          does not slow guessing (a blocked peer still tells right from wrong); it only limits
          log noise and load. Resistance to guessing comes from a generated, random token.
        * ``delivery_tokens`` is the store of the app serving the request (``app.state``); one
          provider may serve several apps.
        * A wrong or missing credential is counted against the peer and refused with 401, or
          429 ``TOO_MANY_AUTH_FAILURES`` once the peer is blocked. A ``GET`` of the local
          session endpoint without any bearer (an old client's session-token probe) is refused
          without counting it: it guesses nothing.
        """
        supplied = bearer_credential(headers)
        delivery = DELIVERY_ROUTE.match(path) if method.upper() == "POST" else None
        if delivery is not None:
            endpoint_id = delivery.group(1)
            if delivery_tokens is not None and delivery_tokens.matches(endpoint_id, supplied):
                # Not a reset: holding one endpoint's delivery token must not clear the
                # failures of someone guessing the operator token from the same address.
                return f"delivery:{endpoint_id}"
            message = DELIVERY_REFUSED_MESSAGE
        else:
            if self.token_matches(supplied):
                self.limiter.reset(peer)
                return "operator"
            message = UNAUTHENTICATED_MESSAGE
            if supplied is None and path.rstrip("/") == SESSION_TOKEN_PATH:
                logger.debug("session-token probe without a bearer refused (not counted)")
                raise OperatorTokenFailure(401, "UNAUTHENTICATED", message)
        wait = self.limiter.retry_after(peer)
        if wait > 0:
            raise OperatorTokenFailure(
                429,
                "TOO_MANY_AUTH_FAILURES",
                "Too many failed authentication attempts from this address; try again later.",
                retry_after=wait,
            )
        self.record_failure(peer, "missing" if supplied is None else "invalid")
        raise OperatorTokenFailure(401, "UNAUTHENTICATED", message)

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        """The local owner (role ``admin``) for the operator token. A delivery token is a
        service principal with role ``viewer``, admitted only on its endpoint's delivery route."""
        grant = self.verify(
            request.headers,
            peer_address(request.scope),
            method=request.method,
            path=route_path(request.scope),
            delivery_tokens=app_delivery_tokens(request.app),
        )
        if grant == "operator":
            return Identity(_local_owner(), target_workspace_id or "default", "admin")
        principal = Principal(
            principal_id=f"webhook-delivery:{grant.split(':', 1)[1]}",
            name="Webhook delivery",
            is_service_account=True,
        )
        return Identity(principal, target_workspace_id or "default", "viewer")

    def is_operator(self, request: Request) -> bool:
        """True for an authenticated request carrying the operator token.

        Never on the public probes: ``/readyz`` gives no details in remote mode (M1), so it is
        not a token oracle.
        """
        if route_path(request.scope) in PUBLIC_PATHS:
            return False
        return self.token_matches(bearer_credential(request.headers))


def app_delivery_tokens(app: Any) -> DeliveryTokenStore | None:
    """``app.state.delivery_tokens`` when it is a store, else ``None``."""
    store = getattr(getattr(app, "state", None), "delivery_tokens", None)
    return store if isinstance(store, DeliveryTokenStore) else None


def delivery_token_store(app: Any) -> DeliveryTokenStore | None:
    """The delivery-token store of ``app`` in remote single-operator mode, else ``None``.

    The store lives on the app (``app.state.delivery_tokens``), never on the provider, so two
    apps that share one provider keep their own tokens (review R2-3).
    """
    if not single_operator_mode(app):
        return None
    return app_delivery_tokens(app)


def record_delivery_failure(app: Any, scope: Scope, reason: str) -> None:
    """Count a refused webhook delivery against its peer (remote single-operator mode)."""
    if single_operator_mode(app):
        provider: OperatorTokenAuthProvider = app.state.auth_provider
        provider.record_failure(peer_address(scope), f"delivery {reason}")


def single_operator_mode(app: Any) -> bool:
    """True when ``app`` runs the remote single-operator mode (local owner behind the token)."""
    state = getattr(app, "state", None)
    provider = getattr(state, "auth_provider", None)
    pm = getattr(state, "profile_manager", None)
    return isinstance(provider, OperatorTokenAuthProvider) and (
        pm is None or pm.profile == "local-owner"
    )


class OperatorTokenMiddleware:
    """Require the operator token (or, on a delivery route, the endpoint's delivery token) on
    every request except ``/healthz`` and ``/readyz``.

    Before reading any credential it refuses (403 ``PLAIN_HTTP_REFUSED``) a request from a
    non-loopback peer that did not arrive over HTTPS, unless ``allow_plain_http`` is set (m6).
    Behind a TLS reverse proxy uvicorn takes the scheme from ``X-Forwarded-Proto``.
    """

    def __init__(
        self, app: ASGIApp, *, provider: OperatorTokenAuthProvider, allow_plain_http: bool = False
    ) -> None:
        self.app = app
        self.provider = provider
        self.allow_plain_http = allow_plain_http

    async def _refuse(
        self, scope: Scope, receive: Receive, send: Send, failure: OperatorTokenFailure
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await failure.response()(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = route_path(scope)
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if path in PUBLIC_PATHS:
            # The probes stay open and never show details in this mode (M1). A wrong bearer
            # sent to them is still a guess, so it is counted (a probe sends none).
            supplied = bearer_credential(Headers(scope=scope))
            if supplied is not None and not self.provider.token_matches(supplied):
                self.provider.record_failure(peer_address(scope), f"invalid, on {path}")
            await self.app(scope, receive, send)
            return
        if (
            not self.allow_plain_http
            and str(scope.get("scheme", "http")).lower() not in ("https", "wss")
            and not is_loopback_client(scope)
        ):
            await self._refuse(
                scope,
                receive,
                send,
                OperatorTokenFailure(
                    403,
                    "PLAIN_HTTP_REFUSED",
                    "This server only accepts network requests over HTTPS. Put a TLS reverse "
                    "proxy in front that sets X-Forwarded-Proto, see "
                    "docs/remote-single-operator.md.",
                ),
            )
            return
        try:
            self.provider.verify(
                Headers(scope=scope),
                peer_address(scope),
                method=str(scope.get("method", "GET")),
                path=path,
                delivery_tokens=app_delivery_tokens(scope.get("app")),
            )
        except OperatorTokenFailure as failure:
            await self._refuse(scope, receive, send, failure)
            return
        await self.app(scope, receive, send)


__all__ = [
    "DEFAULT_FAILURE_WINDOW",
    "DEFAULT_MAX_FAILURES",
    "DELIVERY_REFUSED_MESSAGE",
    "DELIVERY_ROUTE",
    "DeliveryTokenStore",
    "app_delivery_tokens",
    "delivery_token_store",
    "estimate_token_bits",
    "record_delivery_failure",
    "MIN_TOKEN_LENGTH",
    "OPERATOR_TOKEN_ENV",
    "OPERATOR_TOKEN_FILE_ENV",
    "PUBLIC_PATHS",
    "FailedAttemptLimiter",
    "OperatorTokenAuthProvider",
    "OperatorTokenError",
    "OperatorTokenFailure",
    "OperatorTokenMiddleware",
    "PlainHttpNetworkError",
    "assert_remote_transport",
    "bearer_credential",
    "generate_operator_token",
    "load_operator_token",
    "peer_address",
    "read_token_file",
    "single_operator_mode",
    "validate_operator_token",
]
