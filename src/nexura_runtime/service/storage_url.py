# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Postgres URL normalization without importing an optional database driver."""

import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def normalize_database_url(url: str | None, *, strict: bool = True) -> str:
    """Return ``url`` with the ``postgresql`` scheme and without ``channel_binding``.

    asyncpg would pass ``channel_binding`` (found in Neon console URLs) to the server as a
    setting, so it is dropped with a credential-free log line; ``sslmode`` is kept.

    ``strict`` (the service ``database_url``) requires a ``postgres``/``postgresql`` URL with
    a host, in the authority or as a ``host=`` query option (a Unix socket directory).
    ``strict=False`` serves the DSNs of the stores that predate the shared database: any
    input that is not a ``postgres``/``postgresql`` URL (for example a keyword DSN) is
    returned unchanged, so the inputs those stores accept do not narrow.

    Strict mode logs a warning (never the URL) when a remote host has no ``sslmode`` of
    ``require``/``verify-ca``/``verify-full``. asyncpg verifies the server certificate only
    with ``verify-ca``/``verify-full``; use ``sslmode=verify-full`` for Neon.
    """
    if not isinstance(url, str) or not url.strip():
        if strict:
            raise ValueError("database_url must be a PostgreSQL URL")
        return url  # type: ignore[return-value]
    parts = urlsplit(url)
    is_postgres = parts.scheme in {"postgres", "postgresql"}
    if not is_postgres and not strict:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    has_host = bool(parts.hostname) or any(key == "host" and value for key, value in query)
    if strict and (not is_postgres or not has_host):
        raise ValueError("database_url must be a PostgreSQL URL")
    if strict and parts.hostname and parts.hostname not in _LOCAL_HOSTS:
        sslmode = dict(query).get("sslmode", "")
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            logging.getLogger(__name__).warning(
                "database_url names a remote host without sslmode=require/verify-full; "
                "use sslmode=verify-full for managed Postgres such as Neon"
            )
    binding = any(key == "channel_binding" for key, _ in query)
    if not binding and parts.scheme == "postgresql":
        return url  # nothing to change: keep the operator's exact spelling
    if binding:
        logging.getLogger(__name__).info("Dropping unsupported channel_binding URL option")
    return urlunsplit(
        parts._replace(
            scheme="postgresql",
            query=urlencode([(key, value) for key, value in query if key != "channel_binding"]),
        )
    )
