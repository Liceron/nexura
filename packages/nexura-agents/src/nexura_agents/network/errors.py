# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Network and outbound destination policy errors."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import PolicyDeniedError


class DestinationPolicyDeniedError(PolicyDeniedError):
    """Raised when an outbound URL violates security destination policy."""

    def __init__(self, url: str, reason: str, details: dict[str, Any] | None = None) -> None:
        merged = {"url": url, "reason": reason, **(details or {})}
        super().__init__(
            f"Outbound request to '{url}' denied by policy: {reason}",
            policy_name="destination_policy",
            details=merged,
        )
        self.url = url
        self.reason = reason
