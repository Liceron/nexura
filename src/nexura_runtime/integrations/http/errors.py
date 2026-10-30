# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""HTTP integration errors; defined in the agent SDK and re-exported here."""

from nexura_agents.openapi.errors import (
    DuplicateOperationIdError,
    HttpIntegrationError,
    HttpMaxResponseSizeExceededError,
    NonIdempotentRetryForbiddenError,
    UnsupportedOpenAPIFeatureError,
)

__all__ = [
    "HttpIntegrationError",
    "DuplicateOperationIdError",
    "UnsupportedOpenAPIFeatureError",
    "HttpMaxResponseSizeExceededError",
    "NonIdempotentRetryForbiddenError",
]
