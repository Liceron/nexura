# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Issuer checks of the MCP 2026-07-28 authorization profile, for callers that run their
own OAuth 2.1 authorization-code flow against an MCP server's authorization server.

Nexura's MCP client does not run that flow itself: it sends whatever ``headers`` you give
it (for example ``Authorization: Bearer ...``). These two checks are the parts of the
2026-07-28 hardening that a hand-written flow most often gets wrong:

* ``check_metadata_issuer``: the ``issuer`` in authorization server metadata must equal
  the issuer used to build the well-known URL (RFC 8414 section 3.3).
* ``check_authorization_response_issuer``: the ``iss`` parameter of the authorization
  response must equal the recorded issuer before the code is redeemed (RFC 9207
  section 2.4, as tabulated by the MCP specification). Comparison is simple string
  comparison with no normalization.

Spec: https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nexura_agents.mcp.errors import McpSecurityError


def check_metadata_issuer(expected_issuer: str, metadata: Mapping[str, Any]) -> None:
    """Refuse authorization server metadata whose ``issuer`` is not ``expected_issuer``."""
    issuer = metadata.get("issuer")
    if not isinstance(issuer, str) or issuer != expected_issuer:
        raise McpSecurityError(
            "Authorization server metadata names a different issuer; refusing to use it.",
            details={"expected_issuer": expected_issuer, "issuer": issuer},
        )


def check_authorization_response_issuer(
    recorded_issuer: str,
    response_params: Mapping[str, Any],
    *,
    iss_parameter_supported: bool,
) -> None:
    """Apply RFC 9207 to an authorization response before redeeming its ``code``.

    ``recorded_issuer`` is the ``issuer`` from the validated metadata, stored with the PKCE
    verifier before the redirect. ``iss_parameter_supported`` is the server's
    ``authorization_response_iss_parameter_supported`` metadata value. Applies to error
    responses too: on a mismatch do not act on or display ``error`` fields.
    """
    iss = response_params.get("iss")
    if iss is None:
        if iss_parameter_supported:
            raise McpSecurityError(
                "The authorization response has no 'iss' although the server advertises it.",
                details={"expected_issuer": recorded_issuer},
            )
        return
    if not isinstance(iss, str) or iss != recorded_issuer:
        raise McpSecurityError(
            "The authorization response 'iss' does not match the recorded issuer "
            "(possible mix-up attack); the authorization code must not be used.",
            details={"expected_issuer": recorded_issuer},
        )
