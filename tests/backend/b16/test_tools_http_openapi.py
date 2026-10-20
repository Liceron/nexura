"""Unit and acceptance tests for B16: Typed Tools, HTTP and OpenAPI Connectors."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.errors import SchemaValidationError
from nexura_runtime.integrations.openapi.module import (
    import_openapi_operations,
)
from nexura_runtime.network.errors import DestinationPolicyDeniedError
from nexura_runtime.tools import tool

from nexura_runtime.integrations.http.errors import (
    DuplicateOperationIdError,
    HttpMaxResponseSizeExceededError,
    NonIdempotentRetryForbiddenError,
    UnsupportedOpenAPIFeatureError,
)
from nexura_runtime.integrations.http.module import HttpRequestModule
from nexura_runtime.integrations.tools.adapter import tool_to_module


@pytest.fixture
def execution_context() -> ExecutionContext:
    return ExecutionContext(run_id="run_b16_test", node_id="test_node", approval_granted=True)


class TestToolModuleAdapter:
    """Test adapting @tool into B00/B01 module contracts."""

    @pytest.mark.asyncio
    async def test_bad_args_cause_zero_effects(self, execution_context: ExecutionContext) -> None:
        """Acceptance requirement: Bad args cause zero effects."""
        side_effects: list[str] = []

        @tool(description="Process payments with side effects")
        def charge_customer(customer_id: str, amount_cents: int) -> dict[str, Any]:
            side_effects.append(f"{customer_id}:{amount_cents}")
            return {"charged": True, "amount": amount_cents}

        module = tool_to_module(charge_customer, effect_policy="non_idempotent_write")

        # Incompatible inputs (missing required fields, wrong types)
        with pytest.raises(SchemaValidationError) as excinfo:
            await module.execute(execution_context, {"invalid_key": "foo"}, {})

        assert "missing required field" in str(excinfo.value)
        # Verify 0 side effects occurred!
        assert len(side_effects) == 0

        # Successful execution
        res = await module.execute(
            execution_context,
            {"customer_id": "cust_123", "amount_cents": 5000},
            {},
        )
        assert res.status == "success"
        assert res.outputs["result"] == {"charged": True, "amount": 5000}
        assert side_effects == ["cust_123:5000"]

    @pytest.mark.asyncio
    async def test_async_tool_support(self, execution_context: ExecutionContext) -> None:
        @tool(description="Async lookup tool")
        async def async_lookup(query: str) -> str:
            return f"Found: {query}"

        module = tool_to_module(async_lookup, effect_policy="read")
        res = await module.execute(execution_context, {"query": "python"}, {})
        assert res.status == "success"
        assert res.outputs["result"] == "Found: python"
        assert module.manifest.effect_policy == "read"


class TestHttpRequestModule:
    """Test policy-bounded HTTP requests."""

    @pytest.mark.asyncio
    async def test_non_idempotent_post_retries_forbidden(
        self, execution_context: ExecutionContext
    ) -> None:
        """Acceptance requirement: POST retries forbidden unless connector declares idempotency."""
        mod = HttpRequestModule()

        # POST with retries > 0 and no idempotency key must raise NonIdempotentRetryForbiddenError
        with pytest.raises(NonIdempotentRetryForbiddenError) as exc:
            await mod.execute(
                execution_context,
                {"method": "POST", "url": "https://api.example.com/orders", "retries": 3},
                {},
            )
        assert "Automated retry forbidden for non-idempotent HTTP method 'POST'" in str(exc.value)

        # PATCH with retries > 0 and no idempotency key must also raise
        with pytest.raises(NonIdempotentRetryForbiddenError):
            await mod.execute(
                execution_context,
                {"method": "PATCH", "url": "https://api.example.com/orders/1", "retries": 2},
                {},
            )

    @pytest.mark.asyncio
    async def test_post_with_idempotency_key_allowed(
        self, execution_context: ExecutionContext
    ) -> None:
        captured_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(201, json={"status": "created"})

        transport = httpx.MockTransport(handler)
        mod = HttpRequestModule(transport=transport)

        res = await mod.execute(
            execution_context,
            {
                "method": "POST",
                "url": "https://api.example.com/orders",
                "retries": 2,
                "idempotency_key": "idem-key-999",
            },
            {},
        )
        assert res.status == "success"
        assert res.outputs["status_code"] == 201
        assert captured_headers.get("idempotency-key") == "idem-key-999"

    @pytest.mark.asyncio
    async def test_ssrf_fixtures_reject_metadata_and_private_ips(
        self, execution_context: ExecutionContext
    ) -> None:
        """Acceptance requirement: SSRF fixtures reject."""
        mod = HttpRequestModule()

        # 1. Cloud metadata IP 169.254.169.254
        with pytest.raises(DestinationPolicyDeniedError) as exc1:
            await mod.execute(
                execution_context,
                {"method": "GET", "url": "http://169.254.169.254/latest/meta-data/"},
                {},
            )
        assert "link-local / cloud metadata" in str(exc1.value)

        # 2. Private range IP 10.0.0.1
        with pytest.raises(DestinationPolicyDeniedError) as exc2:
            await mod.execute(
                execution_context,
                {"method": "GET", "url": "http://10.0.0.1/admin"},
                {},
            )
        assert "private network address" in str(exc2.value)

        # 3. Disallowed loopback 127.0.0.1
        with pytest.raises(DestinationPolicyDeniedError) as exc3:
            await mod.execute(
                execution_context,
                {"method": "GET", "url": "http://127.0.0.1:8080/metrics"},
                {"allow_loopback": False},
            )
        assert "loopback" in str(exc3.value)

    @pytest.mark.asyncio
    async def test_redirects_cannot_bypass_egress_policy(
        self, execution_context: ExecutionContext
    ) -> None:
        """Acceptance requirement: Redirects cannot bypass egress."""

        def redirect_to_metadata_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={"Location": "http://169.254.169.254/secret"},
                )
            return httpx.Response(200, text="Secret Metadata")

        transport = httpx.MockTransport(redirect_to_metadata_handler)
        mod = HttpRequestModule(transport=transport)

        with pytest.raises(DestinationPolicyDeniedError) as exc:
            await mod.execute(
                execution_context,
                {"method": "GET", "url": "https://api.example.com/start"},
                {"allowed_hosts": ["api.example.com"]},
            )
        assert "link-local / cloud metadata" in str(exc.value)

    @pytest.mark.asyncio
    async def test_header_redaction(self, execution_context: ExecutionContext) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer sensitive_token_xyz",
                    "X-Api-Key": "super_secret_key",
                    "X-Custom-Public": "public-val",
                },
                json={"ok": True},
            )

        transport = httpx.MockTransport(handler)
        mod = HttpRequestModule(transport=transport)

        res = await mod.execute(
            execution_context,
            {"method": "GET", "url": "https://api.example.com/test"},
            {},
        )
        assert res.status == "success"
        headers = res.outputs["headers"]
        assert headers["authorization"] == "[REDACTED]"
        assert headers["x-api-key"] == "[REDACTED]"
        assert headers["x-custom-public"] == "public-val"

    @pytest.mark.asyncio
    async def test_max_response_size_exceeded(self, execution_context: ExecutionContext) -> None:
        large_body = b"A" * 2000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=large_body)

        transport = httpx.MockTransport(handler)
        mod = HttpRequestModule(transport=transport)

        with pytest.raises(HttpMaxResponseSizeExceededError) as exc:
            await mod.execute(
                execution_context,
                {"method": "GET", "url": "https://api.example.com/large"},
                {"max_response_size_bytes": 1000},
            )
        assert "exceeds configured limit of 1000 bytes" in str(exc.value)


class TestOpenAPIConnector:
    """Test OpenAPI 3.x parser and module generator."""

    def test_duplicate_operation_ids_rejected(self) -> None:
        """Acceptance requirement: Duplicate operation IDs reject."""
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test Duplicate API", "version": "1.0.0"},
            "paths": {
                "/item1": {
                    "get": {"operationId": "getItem", "responses": {"200": {"description": "ok"}}}
                },
                "/item2": {
                    "get": {"operationId": "getItem", "responses": {"200": {"description": "ok"}}}
                },
            },
        }
        with pytest.raises(DuplicateOperationIdError) as exc:
            import_openapi_operations(spec)
        assert "Duplicate OpenAPI operationId 'getItem' detected" in str(exc.value)

    def test_unsupported_schema_features_surface(self) -> None:
        """Acceptance requirement: Unsupported schema features surface."""
        # External reference
        external_ref_spec = {
            "openapi": "3.0.0",
            "info": {"title": "External Ref API", "version": "1.0.0"},
            "paths": {
                "/users": {
                    "post": {
                        "operationId": "createUser",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "https://external.com/schemas/user.json"}
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        with pytest.raises(UnsupportedOpenAPIFeatureError) as exc:
            import_openapi_operations(external_ref_spec)
        assert "External schema references are not supported" in str(exc.value)

    @pytest.mark.asyncio
    async def test_openapi_invocation_and_path_interpolation(
        self, execution_context: ExecutionContext
    ) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Shop API", "version": "1.0.0"},
            "servers": [{"url": "https://shop.example.com/api/v1"}],
            "paths": {
                "/items/{item_id}": {
                    "get": {
                        "operationId": "getItemById",
                        "summary": "Retrieve item",
                        "parameters": [
                            {
                                "name": "item_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "include_details",
                                "in": "query",
                                "schema": {"type": "boolean"},
                            },
                        ],
                        "responses": {"200": {"description": "ok"}},
                    },
                    "delete": {
                        "operationId": "deleteItemById",
                        "summary": "Delete item",
                        "parameters": [
                            {
                                "name": "item_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                        ],
                        "responses": {"204": {"description": "deleted"}},
                    },
                }
            },
        }

        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(str(request.url))
            return httpx.Response(200, json={"item_id": "item_42", "title": "Widget"})

        transport = httpx.MockTransport(handler)
        modules = import_openapi_operations(spec, prefix="shop", transport=transport)

        assert len(modules) == 2
        get_mod = next(m for m in modules if m.manifest.module_id == "openapi.shop.getitembyid")
        assert get_mod.manifest.effect_policy == "read"

        del_mod = next(m for m in modules if m.manifest.module_id == "openapi.shop.deleteitembyid")
        assert del_mod.manifest.effect_policy == "idempotent_write"

        # Invoke GET
        res = await get_mod.execute(
            execution_context,
            {"item_id": "item_42", "include_details": True},
            {},
        )
        assert res.status == "success"
        assert res.outputs["status_code"] == 200
        assert res.outputs["body"] == {"item_id": "item_42", "title": "Widget"}
        assert requested_paths == [
            "https://shop.example.com/api/v1/items/item_42?include_details=true"
        ]
