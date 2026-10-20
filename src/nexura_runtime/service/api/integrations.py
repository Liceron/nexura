# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""API endpoints and scoped backend adapters for MCP and OpenAPI tools discovery, configuration,
and test-runs."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.mcp.client import McpClientSession
from nexura_agents.mcp.module import McpToolModule
from nexura_agents.mcp.server import McpServerSession
from nexura_agents.mcp.transports import (
    McpInMemoryTransport,
)
from nexura_agents.openapi.module import (
    OpenAPIOperationModule,
    import_openapi_operations,
)
from nexura_agents.openapi.parser import (
    OpenAPISpecParser,
)
from pydantic import BaseModel, Field

from nexura_runtime.integrations.http.errors import (
    DuplicateOperationIdError,
    UnsupportedOpenAPIFeatureError,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])

# ---------------------------------------------------------------------------
# Built-in Local Fixtures
# ---------------------------------------------------------------------------

PETSTORE_FIXTURE_SPEC = {
    "openapi": "3.0.0",
    "info": {
        "title": "Petstore Sample API",
        "version": "1.0.0",
        "description": "Local offline fixture for Petstore endpoints",
    },
    "servers": [{"url": "https://petstore.example.com/api/v1"}],
    "paths": {
        "/pets/{petId}": {
            "get": {
                "operationId": "getPetById",
                "summary": "Retrieve pet by ID",
                "description": "Fetch pet record using string petId",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "Pet found"}},
            },
            "delete": {
                "operationId": "deletePet",
                "summary": "Delete pet by ID",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"204": {"description": "Pet deleted"}},
            },
        },
        "/pets": {
            "post": {
                "operationId": "addPet",
                "summary": "Create new pet",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "tag": {"type": "string"},
                                },
                                "required": ["name"],
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Pet created"}},
            }
        },
    },
}

WEATHER_FIXTURE_SPEC = {
    "openapi": "3.0.0",
    "info": {
        "title": "Weather Sample API",
        "version": "1.0.0",
        "description": "Local offline fixture for Weather forecasts",
    },
    "servers": [{"url": "https://weather.example.com/api"}],
    "paths": {
        "/current/{city}": {
            "get": {
                "operationId": "getCurrentWeather",
                "summary": "Get current weather condition",
                "parameters": [
                    {
                        "name": "city",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "Weather report"}},
            }
        }
    },
}

McpSessionCleanup = Callable[[], Awaitable[None]]
McpSessionFactory = Callable[[], Awaitable[tuple[McpClientSession, McpSessionCleanup]]]

# Dynamic owner tag read via getattr by WorkflowEngine and the descriptors API.
_WORKSPACE_TAG_ATTR = "_workspace_id"


def tag_module_workspace(module: BaseModule, workspace_id: str) -> None:
    """Tag a registered module instance with the workspace that owns it."""
    setattr(module, _WORKSPACE_TAG_ATTR, workspace_id)


def _create_mcp_fixture_server(fixture_id: str, s_trans: McpInMemoryTransport) -> McpServerSession:
    """Build and register tools on an in-memory MCP server session for known fixtures."""
    server = McpServerSession(s_trans)

    if fixture_id == "echo_fixture":

        async def echo_handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"echo": args.get("message", ""), "timestamp": 1700000000.0}

        async def reverse_handler(args: dict[str, Any]) -> dict[str, Any]:
            text = args.get("text", "")
            return {"reversed": text[::-1]}

        server.register_tool(
            name="echo",
            description="Echoes back the supplied message string with a timestamp.",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            handler=echo_handler,
        )
        server.register_tool(
            name="reverse_text",
            description="Reverses the provided text string character by character.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=reverse_handler,
        )
    else:
        # Default: math_fixture
        async def add_handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"sum": float(args.get("x", 0)) + float(args.get("y", 0))}

        async def multiply_handler(args: dict[str, Any]) -> dict[str, Any]:
            return {"product": float(args.get("a", 0)) * float(args.get("b", 0))}

        server.register_tool(
            name="math_add",
            description="Add two numbers together and return the numeric sum.",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                "required": ["x", "y"],
            },
            handler=add_handler,
        )
        server.register_tool(
            name="math_multiply",
            description="Multiply two numbers together and return the numeric product.",
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
            handler=multiply_handler,
        )

    return server


# ---------------------------------------------------------------------------
# Request & Response Models
# ---------------------------------------------------------------------------


class FixtureMetadata(BaseModel):
    id: str
    name: str
    description: str


class SubsystemStatus(BaseModel):
    available: bool
    missing_reason: str | None = None
    fixtures: list[FixtureMetadata] = Field(default_factory=list)


class IntegrationStatusResponse(BaseModel):
    mcp: SubsystemStatus
    openapi: SubsystemStatus


class McpDiscoverRequest(BaseModel):
    server_name: str = "local_mcp"
    transport_type: Literal["in_memory", "stdio"] = "in_memory"
    fixture_id: str | None = "math_fixture"
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None


class McpDiscoveredTool(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    digest: str
    effect_policy: str = "idempotent_write"


class McpDiscoverResponse(BaseModel):
    server_name: str
    tools: list[McpDiscoveredTool]
    is_fixture: bool = True


class McpTestRunRequest(BaseModel):
    server_name: str = "local_mcp"
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    fixture_id: str | None = "math_fixture"
    transport_type: Literal["in_memory", "stdio"] = "in_memory"
    command: str | None = None
    args: list[str] | None = None


class McpTestRunResponse(BaseModel):
    status: Literal["success", "error"]
    output: Any = None
    content: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    is_fixture: bool = True
    execution_label: str = "Fixture Dry-Run (Local Mock Transport)"
    duration_ms: float = 0.0


class OpenAPIDiscoverRequest(BaseModel):
    spec_content: str | None = None
    spec_dict: dict[str, Any] | None = None
    url: str | None = None
    fixture_id: str | None = None
    prefix: str = "api"


class OpenAPIDiscoveredOperation(BaseModel):
    operation_id: str
    module_id: str
    method: str
    path: str
    summary: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effect_policy: str
    is_idempotent: bool


class OpenAPIDiscoverResponse(BaseModel):
    prefix: str
    operations: list[OpenAPIDiscoveredOperation]
    is_fixture: bool = True


class OpenAPITestRunRequest(BaseModel):
    operation_id: str
    prefix: str = "api"
    inputs: dict[str, Any] = Field(default_factory=dict)
    fixture_id: str | None = None
    spec_content: str | None = None
    spec_dict: dict[str, Any] | None = None
    base_url: str | None = None
    mock_response: dict[str, Any] | None = None


class OpenAPITestRunResponse(BaseModel):
    status: Literal["success", "error"]
    status_code: int = 200
    body: Any = None
    headers: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    is_fixture: bool = True
    execution_label: str = "Fixture Dry-Run (Local Mock Transport)"
    duration_ms: float = 0.0


class IntegrationRegisterRequest(BaseModel):
    kind: Literal["mcp", "openapi"]
    server_name_or_prefix: str
    fixture_id: str | None = None
    tools: list[str] | None = None


class IntegrationRegisterResponse(BaseModel):
    registered_modules: list[str]
    count: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status", response_model=IntegrationStatusResponse)
async def get_integration_status() -> IntegrationStatusResponse:
    """Return status of MCP and OpenAPI integration adapters and local fixtures."""
    return IntegrationStatusResponse(
        mcp=SubsystemStatus(
            available=True,
            missing_reason=None,
            fixtures=[
                FixtureMetadata(
                    id="math_fixture",
                    name="Math Calculator",
                    description="Offline fixture providing math_add and math_multiply tools.",
                ),
                FixtureMetadata(
                    id="echo_fixture",
                    name="Echo & String Transformer",
                    description="Offline fixture providing echo and reverse_text tools.",
                ),
            ],
        ),
        openapi=SubsystemStatus(
            available=True,
            missing_reason=None,
            fixtures=[
                FixtureMetadata(
                    id="petstore_fixture",
                    name="Petstore Sample API",
                    description="Offline OpenAPI 3.0 fixture for pet CRUD operations.",
                ),
                FixtureMetadata(
                    id="weather_fixture",
                    name="Weather Sample API",
                    description="Offline OpenAPI 3.0 fixture for weather lookup.",
                ),
            ],
        ),
    )


@router.post("/mcp/discover", response_model=McpDiscoverResponse)
async def discover_mcp_tools(payload: McpDiscoverRequest) -> McpDiscoverResponse:
    """Discover tools exposed by a local MCP server fixture or authorized stdio command."""
    if payload.transport_type == "stdio":
        if not payload.command:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "MISSING_COMMAND",
                    "message": "Subprocess command must be specified.",
                },
            )
        # Validate against path traversal
        if ".." in payload.command or "/" in payload.command or "\\" in payload.command:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "PATH_TRAVERSAL_DETECTED",
                    "message": f"Command contains forbidden path characters: '{payload.command}'",
                },
            )
        # Reject caller-controlled stdio execution for browser/API security
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "STDIO_EXECUTION_DISABLED",
                "message": (
                    "Caller-supplied stdio subprocess execution is disabled for browser/API "
                    "clients. "
                    "Use registered operator connector definitions or in-memory fixtures."
                ),
            },
        )

    # In-memory fixture
    c_trans, s_trans = McpInMemoryTransport.create_pair()
    fixture_id = payload.fixture_id or "math_fixture"
    server = _create_mcp_fixture_server(fixture_id, s_trans)
    server_task = asyncio.create_task(server.serve_loop())

    client = McpClientSession(c_trans)
    try:
        await client.initialize()
        raw_tools = await client.list_tools()
    finally:
        await client.close()
        await server.close()
        server_task.cancel()

    discovered = [
        McpDiscoveredTool(
            name=t.name,
            description=t.description,
            input_schema=t.inputSchema,
            digest=canonical_digest({"name": t.name, "inputSchema": t.inputSchema}),
            effect_policy="idempotent_write",
        )
        for t in raw_tools
    ]

    return McpDiscoverResponse(
        server_name=payload.server_name,
        tools=discovered,
        is_fixture=True,
    )


@router.post("/mcp/test-run", response_model=McpTestRunResponse)
async def test_run_mcp_tool(payload: McpTestRunRequest) -> McpTestRunResponse:
    """Execute a dry-run invocation of a local MCP tool with labeled fixture transport."""
    start_time = time.perf_counter()

    if payload.transport_type == "stdio":
        if not payload.command:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "MISSING_COMMAND",
                    "message": "Command required for stdio transport.",
                },
            )
        if ".." in payload.command or "/" in payload.command or "\\" in payload.command:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "PATH_TRAVERSAL_DETECTED",
                    "message": "Command contains path traversal.",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "STDIO_EXECUTION_DISABLED",
                "message": (
                    "Caller-supplied stdio subprocess execution is disabled for browser/API "
                    "clients. "
                    "Use registered operator connector definitions or in-memory fixtures."
                ),
            },
        )

    # In-memory fixture execution
    c_trans, s_trans = McpInMemoryTransport.create_pair()
    fixture_id = payload.fixture_id or "math_fixture"
    server = _create_mcp_fixture_server(fixture_id, s_trans)
    server_task = asyncio.create_task(server.serve_loop())

    client = McpClientSession(c_trans)
    try:
        await client.initialize()
        tools = await client.list_tools()
        target_tool = next((t for t in tools if t.name == payload.tool_name), None)
        if target_tool is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "TOOL_NOT_FOUND",
                    "message": f"Tool '{payload.tool_name}' not found on fixture '{fixture_id}'.",
                },
            )

        res = await client.call_tool(payload.tool_name, payload.arguments)
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        content_list = [c.model_dump() for c in res.content]

        return McpTestRunResponse(
            status="error" if res.isError else "success",
            output=content_list[0].get("text") if content_list else None,
            content=content_list,
            is_fixture=True,
            execution_label="Fixture Dry-Run (Local Mock Transport)",
            duration_ms=round(elapsed_ms, 2),
        )
    finally:
        await client.close()
        await server.close()
        server_task.cancel()


def _validate_safe_url(url: str) -> None:
    """Validate untrusted URL against SSRF, cloud metadata, and path traversal."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNSAFE_URL_SCHEME",
                "message": f"URL scheme '{parsed.scheme}' forbidden. Only HTTP/HTTPS allowed.",
            },
        )

    host = (parsed.hostname or "").lower()
    # Reject cloud metadata and link-local addresses
    if (
        host == "169.254.169.254"
        or host.startswith("169.254.")
        or "metadata.google.internal" in host
        or host == "instance-data"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNTRUSTED_DESTINATION_DENIED",
                "message": (
                    f"Access to cloud metadata or link-local host '{host}' is strictly forbidden."
                ),
            },
        )

    # Reject path traversal in URL path
    if ".." in parsed.path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PATH_TRAVERSAL_DETECTED",
                "message": "Path traversal detected in URL path.",
            },
        )


@router.post("/openapi/discover", response_model=OpenAPIDiscoverResponse)
async def discover_openapi_operations(payload: OpenAPIDiscoverRequest) -> OpenAPIDiscoverResponse:
    """Parse and discover operations from an OpenAPI 3.x spec with SSRF & schema validation."""
    spec_data: dict[str, Any]

    if payload.fixture_id:
        if payload.fixture_id == "weather_fixture":
            spec_data = WEATHER_FIXTURE_SPEC
        else:
            spec_data = PETSTORE_FIXTURE_SPEC
    elif payload.spec_dict:
        spec_data = payload.spec_dict
    elif payload.spec_content:
        import json

        try:
            spec_data = json.loads(payload.spec_content)
        except Exception:
            try:
                import yaml  # type: ignore[import-untyped]

                spec_data = yaml.safe_load(payload.spec_content)
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "INVALID_SPEC_SYNTAX",
                        "message": f"Malformed OpenAPI spec: {exc}",
                    },
                ) from exc
    elif payload.url:
        _validate_safe_url(payload.url)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "REMOTE_FETCH_DISABLED",
                "message": (
                    "External network fetch disabled in offline evaluation mode. Use local "
                    "fixture or paste spec."
                ),
            },
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MISSING_SPEC",
                "message": "Must provide fixture_id, spec_dict, or spec_content.",
            },
        )

    # Parse and validate spec
    try:
        parser = OpenAPISpecParser(spec_data)
        operations = parser.parse_operations()
    except DuplicateOperationIdError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "DUPLICATE_OPERATION_ID", "message": str(exc)},
        ) from exc
    except UnsupportedOpenAPIFeatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UNSUPPORTED_OPENAPI_FEATURE", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "SPEC_PARSING_ERROR",
                "message": f"Failed to parse OpenAPI spec: {exc}",
            },
        ) from exc

    prefix = payload.prefix or "api"
    discovered = []
    for op in operations:
        clean_op_id = re.sub(r"[^a-z0-9_-]", "_", op.operation_id.lower()).strip("_")
        clean_prefix = re.sub(r"[^a-z0-9_-]", "_", prefix.lower()).strip("_")
        mod_id = f"openapi.{clean_prefix}.{clean_op_id}"
        discovered.append(
            OpenAPIDiscoveredOperation(
                operation_id=op.operation_id,
                module_id=mod_id,
                method=op.method.upper(),
                path=op.path,
                summary=op.summary,
                description=op.description,
                input_schema=op.input_schema,
                output_schema=op.output_schema,
                effect_policy=op.effect_policy,
                is_idempotent=op.is_idempotent,
            )
        )

    return OpenAPIDiscoverResponse(
        prefix=prefix,
        operations=discovered,
        is_fixture=bool(payload.fixture_id),
    )


@router.post("/openapi/test-run", response_model=OpenAPITestRunResponse)
async def test_run_openapi_operation(payload: OpenAPITestRunRequest) -> OpenAPITestRunResponse:
    """Execute a dry-run invocation of an OpenAPI operation using safe local mock transport."""
    start_time = time.perf_counter()

    spec_data: dict[str, Any]
    if payload.fixture_id:
        spec_data = (
            WEATHER_FIXTURE_SPEC
            if payload.fixture_id == "weather_fixture"
            else PETSTORE_FIXTURE_SPEC
        )
    elif payload.spec_dict:
        spec_data = payload.spec_dict
    elif payload.spec_content:
        import json

        try:
            spec_data = json.loads(payload.spec_content)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_SPEC_SYNTAX",
                    "message": "Failed to parse spec content as JSON.",
                },
            ) from exc
    else:
        spec_data = PETSTORE_FIXTURE_SPEC

    # Find operation spec
    try:
        parser = OpenAPISpecParser(spec_data)
        operations = parser.parse_operations()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "SPEC_PARSING_ERROR", "message": str(exc)},
        ) from exc

    op_spec = next((op for op in operations if op.operation_id == payload.operation_id), None)
    if op_spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "OPERATION_NOT_FOUND",
                "message": f"Operation '{payload.operation_id}' not found in specification.",
            },
        )

    # Prepare mock handler
    mock_body = (
        payload.mock_response.get("json")
        if payload.mock_response and "json" in payload.mock_response
        else {
            "status": "ok",
            "operation": payload.operation_id,
            "inputs": payload.inputs,
            "fixture": True,
        }
    )
    mock_status = payload.mock_response.get("status_code", 200) if payload.mock_response else 200

    def mock_transport_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            mock_status,
            json=mock_body,
            headers={"Content-Type": "application/json", "X-Fixture": "true"},
        )

    transport = httpx.MockTransport(mock_transport_handler)
    module = OpenAPIOperationModule(
        op_spec=op_spec,
        base_url=payload.base_url or "https://api.example.com",
        transport=transport,
        prefix=payload.prefix,
    )

    ctx = ExecutionContext(run_id="dry_run_test", node_id="openapi_node")
    res = await module.execute(ctx, payload.inputs, {})
    elapsed_ms = (time.perf_counter() - start_time) * 1000

    return OpenAPITestRunResponse(
        status="success" if res.status == "success" else "error",
        status_code=res.outputs.get("status_code", mock_status),
        body=res.outputs.get("body", mock_body),
        headers=res.outputs.get("headers", {}),
        error=res.error_message,
        is_fixture=True,
        execution_label="Fixture Dry-Run (Local Mock Transport)",
        duration_ms=round(elapsed_ms, 2),
    )


def _assert_tool_ids_available(
    request: Request,
    ws_repo: Any,
    app_registry: Any,
    v_reg: Any,
    caller_ws: str,
    mod_ids: list[str],
) -> None:
    """Reject (409) ids owned by another workspace or by an operator/global module.

    Ids without an ownership row that already exist in the shared registry belong to the
    operator and must never be replaced by a tenant registration. Only local-owner mode
    names the owning workspace in the message.
    """
    principal = getattr(request.state, "principal", None)
    local = principal is not None and getattr(principal, "profile", "") == "local-owner"
    for mod_id in mod_ids:
        owner = ws_repo.get_resource_workspace("integration_tool", mod_id) if ws_repo else None
        if owner is None:
            # Tenant tools restored from the database at startup carry a workspace tag
            # even before the in-memory ownership table is repopulated.
            existing = getattr(app_registry, "_handlers", {}).get(mod_id)
            if existing is None and v_reg is not None and v_reg.has(mod_id):
                with contextlib.suppress(Exception):
                    existing = v_reg.get(mod_id)
                # The versioned registry can outlive the app; a tenant-tagged entry there
                # without an ownership row is stale tenant state, not an operator module.
                if getattr(existing, _WORKSPACE_TAG_ATTR, None) is not None:
                    continue
            owner = getattr(existing, _WORKSPACE_TAG_ATTR, None)
        if owner == caller_ws:
            continue
        taken = owner is not None
        if not taken:
            taken = bool(app_registry.has_handler(mod_id)) or (
                v_reg is not None and bool(v_reg.has(mod_id))
            )
        if not taken:
            continue
        if local and owner is not None:
            # Local-owner mode: one person owns every workspace, so naming it is fine.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "CROSS_WORKSPACE_DENIED",
                    "message": (
                        f"Integration tool '{mod_id}' is owned by workspace "
                        f"'{owner}', not '{caller_ws}'."
                    ),
                },
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INTEGRATION_TOOL_ID_CONFLICT",
                "message": f"Integration tool id '{mod_id}' is already in use.",
            },
        )


@router.post("/register", response_model=IntegrationRegisterResponse)
async def register_integration_tools(
    request: Request,
    payload: IntegrationRegisterRequest,
) -> IntegrationRegisterResponse:
    """Register discovered MCP tools or OpenAPI operations into the workspace module registry."""
    caller_ws = getattr(request.state, "workspace_id", "default")
    principal = getattr(request.state, "principal", None)
    principal_id: str = (
        principal.principal_id
        if principal is not None and hasattr(principal, "principal_id")
        else (str(principal) if principal else "local-owner")
    )
    raw_role = getattr(request.state, "role", None)
    if principal:
        from nexura_runtime.service.auth.errors import PermissionDeniedError
        from nexura_runtime.service.auth.models import WorkspaceRole
        from nexura_runtime.service.auth.permissions import check_permission

        # Missing or unrecognised roles narrow to "viewer", which grants no permissions.
        role: WorkspaceRole = (
            "admin" if raw_role == "admin" else ("editor" if raw_role == "editor" else "viewer")
        )
        try:
            check_permission(principal, role, "edit")
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    app_registry = getattr(request.app.state, "registry", None)
    if app_registry is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "NO_REGISTRY", "message": "Application registry not initialized."},
        )

    ws_repo = getattr(request.app.state, "workspace_repo", None)
    v_reg = getattr(app_registry, "versioned_registry", None)
    registered_ids: list[str] = []
    pending: list[tuple[str, Any]] = []

    if payload.kind == "mcp":
        c_trans, s_trans = McpInMemoryTransport.create_pair()
        fixture_id = payload.fixture_id or "math_fixture"
        server = _create_mcp_fixture_server(fixture_id, s_trans)
        server_task = asyncio.create_task(server.serve_loop())

        client = McpClientSession(c_trans)
        try:
            await client.initialize()
            tools = await client.list_tools()
        finally:
            await client.close()
            await server.close()
            server_task.cancel()

        def make_fixture_session_factory(fid: str) -> McpSessionFactory:
            async def _session_factory() -> tuple[McpClientSession, McpSessionCleanup]:
                c_pair, s_pair = McpInMemoryTransport.create_pair()
                srv = _create_mcp_fixture_server(fid, s_pair)
                srv_loop_task = asyncio.create_task(srv.serve_loop())
                cli = McpClientSession(c_pair)
                await cli.initialize()

                async def _cleanup() -> None:
                    await cli.close()
                    await srv.close()
                    srv_loop_task.cancel()

                return cli, _cleanup

            return _session_factory

        for t in tools:
            if payload.tools and t.name not in payload.tools:
                continue

            clean_srv = (
                re.sub(r"[^a-z0-9_-]", "_", payload.server_name_or_prefix.lower()).strip("_")
                or "srv"
            )
            clean_name = re.sub(r"[^a-z0-9_-]", "_", t.name.lower()).strip("_") or "tool"
            mod_id = f"mcp.{clean_srv}.{clean_name}"

            # Create module instance with active session factory
            mod = McpToolModule(
                client=None,
                tool_def=t,
                server_name=payload.server_name_or_prefix,
                session_factory=make_fixture_session_factory(fixture_id),
            )
            tag_module_workspace(mod, caller_ws)
            pending.append((mod_id, mod))

    elif payload.kind == "openapi":
        spec_data = (
            WEATHER_FIXTURE_SPEC
            if payload.fixture_id == "weather_fixture"
            else PETSTORE_FIXTURE_SPEC
        )
        modules = import_openapi_operations(
            spec_data,
            prefix=payload.server_name_or_prefix,
        )
        for m in modules:
            if payload.tools and m.op_spec.operation_id not in payload.tools:
                continue
            mod_id = m.manifest.module_id

            tag_module_workspace(m, caller_ws)
            pending.append((mod_id, m))

    # Check every id before registering any so a collision leaves no partial state.
    _assert_tool_ids_available(
        request, ws_repo, app_registry, v_reg, caller_ws, [mid for mid, _ in pending]
    )
    for mod_id, mod_obj in pending:
        if ws_repo is not None:
            ws_repo.record_resource_ownership("integration_tool", mod_id, caller_ws, principal_id)
        app_registry.register(mod_id, mod_obj)
        if v_reg is not None and not v_reg.has(mod_id, mod_obj.manifest.version):
            with contextlib.suppress(Exception):
                v_reg.register(mod_obj)
        registered_ids.append(mod_id)

    # Durably persist registration in SQLite if db exists
    store = getattr(request.app.state, "store", None)
    if store and hasattr(store, "db_path") and store.db_path and store.db_path != ":memory:":
        import sqlite3

        with (
            contextlib.suppress(Exception),
            sqlite3.connect(str(store.db_path), timeout=5.0) as conn,
        ):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_service_integration_registrations (
                    workspace_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    fixture_id TEXT,
                    server_name_or_prefix TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (workspace_id, module_id)
                )
                """
            )
            for mid in registered_ids:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO nexura_service_integration_registrations
                    (workspace_id, module_id, kind, fixture_id,
                     server_name_or_prefix, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        caller_ws,
                        mid,
                        payload.kind,
                        payload.fixture_id,
                        payload.server_name_or_prefix,
                        time.time(),
                    ),
                )
            conn.commit()

    return IntegrationRegisterResponse(
        registered_modules=registered_ids,
        count=len(registered_ids),
    )
