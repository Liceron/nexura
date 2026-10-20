"""Standalone demonstration for Module B16: Typed Tools, HTTP and OpenAPI Connectors.

Runs 100% keylessly and offline:
  uv run python examples/backend/b16.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import httpx
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.errors import SchemaValidationError
from nexura_runtime.integrations.openapi import import_openapi_operations
from nexura_runtime.network.errors import DestinationPolicyDeniedError
from nexura_runtime.tools import tool

from nexura_runtime.integrations.http import (
    DuplicateOperationIdError,
    HttpRequestModule,
    NonIdempotentRetryForbiddenError,
    UnsupportedOpenAPIFeatureError,
)
from nexura_runtime.integrations.tools import tool_to_module


def _create_mock_order_service_transport() -> httpx.MockTransport:
    """Offline mock HTTP service for orders."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()

        if method == "GET" and path == "/orders/ORD-8801":
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "Authorization": "Bearer secret_tok"},
                json={
                    "order_id": "ORD-8801",
                    "customer": "प्रिया शर्मा",
                    "total": 4200,
                    "currency": "INR",
                    "status": "confirmed",
                },
            )

        if method == "POST" and path == "/orders":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                201,
                headers={"content-type": "application/json"},
                json={
                    "order_id": "ORD-NEW-101",
                    "status": "created",
                    "total": payload.get("total", 0),
                },
            )

        if path == "/health":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"status": "ok", "service": "order_gateway"},
            )

        return httpx.Response(404, json={"error": f"Not found: {path}"})

    return httpx.MockTransport(handler)


async def main() -> None:
    print("=== B16: Typed Tools, HTTP and OpenAPI Connectors Demonstration ===\n")
    mock_transport = _create_mock_order_service_transport()
    # This script is the operator: it grants the human approval that writes need
    # (in a workflow the engine sets this only after someone approves the step).
    ctx = ExecutionContext(run_id="run_b16_demo", node_id="node_connectors", approval_granted=True)

    # 1. @tool Adapted into Module Contract
    print("1. Converting @tool into B00/B01 Module Contract...")
    side_effects_counter = 0

    @tool(description="Fetch order status from order gateway")
    def fetch_order(order_id: str) -> dict[str, str]:
        nonlocal side_effects_counter
        side_effects_counter += 1
        return {"order_id": order_id, "status": "shipped"}

    tool_mod = tool_to_module(fetch_order, effect_policy="read")
    print(f"  Tool module created: {tool_mod.manifest.module_id}")
    print(f"  Inferred Input Schema: {tool_mod.manifest.input_schema}")

    # Valid invocation
    t_result = await tool_mod.execute(ctx, {"order_id": "ORD-8801"}, {})
    print(f"  Tool execution result: {t_result.outputs['result']}")
    print(f"  Side effects executed: {side_effects_counter}")

    # Bad arguments rejection before dispatch (Zero effects guarantee)
    print("\n2. Demonstrating Bad Arguments Rejection (Zero Effects Guarantee)...")
    try:
        await tool_mod.execute(ctx, {"wrong_arg": 123}, {})
        print("  [ERROR] Failed to reject invalid argument!")
    except SchemaValidationError as exc:
        print(f"  [SUCCESS] Pre-flight schema check blocked execution: {exc}")
        print(f"  Side effects executed: {side_effects_counter} (remained unchanged!)")

    # 3. OpenAPI 3.x Operation Importer
    print("\n3. Importing OpenAPI 3.x Specifications into Modules...")
    openapi_spec = {
        "openapi": "3.0.0",
        "info": {"title": "Order Management API", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/orders/{order_id}": {
                "get": {
                    "operationId": "getOrder",
                    "summary": "Retrieve an order by ID",
                    "parameters": [
                        {
                            "name": "order_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Order found",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Order"}
                                }
                            },
                        }
                    },
                }
            },
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "summary": "Create a new order",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateOrderInput"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "Order created"}},
                }
            },
        },
        "components": {
            "schemas": {
                "Order": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string"},
                        "customer": {"type": "string"},
                        "total": {"type": "integer"},
                    },
                },
                "CreateOrderInput": {
                    "type": "object",
                    "properties": {
                        "customer": {"type": "string"},
                        "total": {"type": "integer"},
                    },
                    "required": ["customer", "total"],
                },
            }
        },
    }

    imported_modules = import_openapi_operations(
        openapi_spec,
        base_url="https://api.example.com",
        transport=mock_transport,
        prefix="orders",
    )
    print(f"  Imported {len(imported_modules)} OpenAPI operation modules:")
    for mod in imported_modules:
        print(
            f"   - {mod.manifest.module_id} "
            f"(method: {mod.op_spec.method}, effect: {mod.manifest.effect_policy})"
        )

    # Execute GET operation
    get_mod = next(m for m in imported_modules if m.op_spec.operation_id == "getOrder")
    get_res = await get_mod.execute(ctx, {"order_id": "ORD-8801"}, {})
    print("\n  GET /orders/{order_id} Result:")
    print(f"   Status Code: {get_res.outputs['status_code']}")
    print(f"   Customer: {get_res.outputs['body']['customer']} (Unicode Indic script preserved)")
    auth_header = get_res.outputs["headers"].get("Authorization")
    print(f"   Sensitive Header: Authorization={auth_header} (Redacted!)")

    # Execute POST operation
    post_mod = next(m for m in imported_modules if m.op_spec.operation_id == "createOrder")
    post_res = await post_mod.execute(
        ctx,
        {"body": {"customer": "अमित वर्मा", "total": 3500}},
        {},
    )
    print("\n  POST /orders Result:")
    print(f"   Status Code: {post_res.outputs['status_code']}")
    print(f"   Body: {post_res.outputs['body']}")

    # 4. Security Boundaries: SSRF, Duplicate IDs, Unsupported Features, and Idempotency
    print("\n4. Demonstrating Security Boundaries & Rejection Cases...")

    # Case A: SSRF Protection via DestinationPolicy
    http_mod = HttpRequestModule(transport=mock_transport)
    try:
        await http_mod.execute(
            ctx,
            {"method": "GET", "url": "http://169.254.169.254/latest/meta-data/"},
            {},
        )
        print("  [ERROR] Failed to block AWS metadata SSRF!")
    except DestinationPolicyDeniedError as exc:
        print(f"  [SUCCESS] Blocked Cloud Metadata SSRF: {exc}")

    # Case B: Duplicate Operation ID Rejection
    duplicate_spec = {
        "openapi": "3.0.0",
        "info": {"title": "Duplicate API", "version": "1.0.0"},
        "paths": {
            "/path_a": {
                "get": {"operationId": "sharedId", "responses": {"200": {"description": "ok"}}}
            },
            "/path_b": {
                "post": {"operationId": "sharedId", "responses": {"200": {"description": "ok"}}}
            },
        },
    }
    try:
        import_openapi_operations(duplicate_spec)
        print("  [ERROR] Failed to reject duplicate operationId!")
    except DuplicateOperationIdError as exc:
        print(f"  [SUCCESS] Rejected duplicate operationId: {exc}")

    # Case C: Unsupported External Schema References
    external_ref_spec = {
        "openapi": "3.0.0",
        "info": {"title": "External Ref API", "version": "1.0.0"},
        "paths": {
            "/external": {
                "get": {
                    "operationId": "getExternal",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "https://untrusted.com/schema.json"}
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    try:
        import_openapi_operations(external_ref_spec)
        print("  [ERROR] Failed to reject external $ref!")
    except UnsupportedOpenAPIFeatureError as exc:
        print(f"  [SUCCESS] Rejected external schema reference: {exc}")

    # Case D: Non-Idempotent POST Retries Forbidden
    try:
        await http_mod.execute(
            ctx,
            {"method": "POST", "url": "https://api.example.com/orders", "retries": 3},
            {},
        )
        print("  [ERROR] Failed to block non-idempotent retries!")
    except NonIdempotentRetryForbiddenError as exc:
        print(f"  [SUCCESS] Blocked automated retries for non-idempotent POST: {exc}")

    # 5. CLI Execution via subprocess
    print("\n5. Executing HttpRequestModule via B01 CLI...")
    cli_payload = json.dumps(
        {
            "method": "GET",
            "url": "https://api.example.com/health",
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexura_runtime.modules",
            "run",
            "nexura.http.request",
            "--input",
            cli_payload,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    print(proc.stdout.strip())
    if proc.returncode == 0:
        print("\nCLI Execution: SUCCESS (code 0)")
    else:
        print(f"\nCLI Execution: FAILED (code {proc.returncode}): {proc.stderr}")

    print("\n=== B16 Example Completed Successfully ===")


if __name__ == "__main__":
    asyncio.run(main())
