# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""HTTP integration package."""

from nexura_runtime.integrations.http.errors import (
    DuplicateOperationIdError,
    HttpIntegrationError,
    HttpMaxResponseSizeExceededError,
    NonIdempotentRetryForbiddenError,
    UnsupportedOpenAPIFeatureError,
)
from nexura_runtime.integrations.http.module import (
    HttpRequestModule,
    _default_http_mock_transport,
    redact_headers,
)
from nexura_runtime.modules.registry import default_registry

# Register with default module registry using safe offline mock transport
default_registry.register(HttpRequestModule(transport=_default_http_mock_transport()))

__all__ = [
    "HttpRequestModule",
    "redact_headers",
    "HttpIntegrationError",
    "DuplicateOperationIdError",
    "UnsupportedOpenAPIFeatureError",
    "HttpMaxResponseSizeExceededError",
    "NonIdempotentRetryForbiddenError",
]
