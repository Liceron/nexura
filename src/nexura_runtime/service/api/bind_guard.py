# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Startup guard: refuse to listen on a network address without authentication.

Both the REST service (``local-owner`` profile has ambient admin authority) and Studio
(token optional on loopback) call :func:`assert_safe_bind` before they serve. Binding to
anything but loopback without authentication fails with a clear message. The open-source
edition authenticates only the remote single-operator mode (one operator token,
``nexura-runtime serve --auth token``, ``operator_token``); multi-user authentication is in the
Nexura enterprise edition. The only opt-out is the loudly named flag
``allow_unauthenticated_network=True`` or the environment variable
``NEXURA_INSECURE_ALLOW_UNAUTHENTICATED_NETWORK=I_UNDERSTAND_THIS_IS_UNSAFE``.
"""

from __future__ import annotations

import ipaddress
import os

from nexura_runtime.service.auth.errors import UnauthenticatedNetworkError

INSECURE_ENV_VAR = "NEXURA_INSECURE_ALLOW_UNAUTHENTICATED_NETWORK"
INSECURE_ENV_VALUE = "I_UNDERSTAND_THIS_IS_UNSAFE"


def is_loopback_bind(host: str | None) -> bool:
    """True when ``host`` only accepts connections from this machine.

    Wildcards (``0.0.0.0``, ``::``, empty) and any other name or address are not loopback.
    """
    if host is None:
        return True
    value = host.strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def insecure_network_opt_in(explicit: bool = False) -> bool:
    """True when the operator explicitly chose to run unauthenticated on a network."""
    return explicit or os.environ.get(INSECURE_ENV_VAR) == INSECURE_ENV_VALUE


def assert_safe_bind(
    host: str | None,
    *,
    authenticated: bool,
    allow_unauthenticated_network: bool = False,
    component: str = "Nexura",
) -> None:
    """Raise :class:`UnauthenticatedNetworkError` for a non-loopback bind without auth."""
    if is_loopback_bind(host) or authenticated:
        return
    if insecure_network_opt_in(allow_unauthenticated_network):
        return
    raise UnauthenticatedNetworkError(
        f"{component} refuses to listen on non-loopback address {host!r} without "
        "authentication: anyone who can reach it could run workflows and read data. "
        "Bind to 127.0.0.1, or run the remote single-operator mode, where one operator token "
        "authenticates every request (nexura-runtime serve --auth token, see "
        "docs/remote-single-operator.md). Multi-user logins, roles and SSO are in the Nexura "
        "enterprise edition (Studio alone can use NEXURA_STUDIO_TOKEN). For throwaway local "
        f"development only, set allow_unauthenticated_network=True or {INSECURE_ENV_VAR}="
        f"{INSECURE_ENV_VALUE}."
    )
