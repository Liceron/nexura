# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""OpenAPI specification connector package."""

from nexura_agents.openapi.module import (
    OpenAPIOperationModule,
    import_openapi_operations,
)
from nexura_agents.openapi.parser import (
    OpenAPIOperationSpec,
    OpenAPISpecParser,
    resolve_schema_refs,
)
from nexura_agents.openapi.toolset import openapi_tools

__all__ = [
    "openapi_tools",
    "OpenAPISpecParser",
    "OpenAPIOperationSpec",
    "OpenAPIOperationModule",
    "import_openapi_operations",
    "resolve_schema_refs",
]
