"""Regression tests for B2 OpenAPI import fixes (SSRF, path encoding, ref cycles, body keys)."""

from __future__ import annotations

import httpx
import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.network.errors import DestinationPolicyDeniedError
from nexura_agents.openapi.module import import_openapi_operations
from nexura_agents.openapi.parser import OpenAPISpecParser

CTX = ExecutionContext(run_id="r", node_id="n", approval_granted=True)


@pytest.mark.parametrize("host", ["169.254.169.254", "127.0.0.1", "10.0.0.5"])
async def test_spec_server_private_hosts_are_not_auto_allowed(host: str) -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, json={})

    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": f"http://{host}"}],
        "paths": {"/m": {"get": {"operationId": "meta"}}},
    }
    module = import_openapi_operations(spec, transport=httpx.MockTransport(handler))[0]
    with pytest.raises(DestinationPolicyDeniedError):
        await module.execute(CTX, {}, {})
    assert hits == []


async def test_path_parameters_are_url_encoded() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={})

    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/users/{id}": {
                "get": {
                    "operationId": "u",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                }
            }
        },
    }
    module = import_openapi_operations(
        spec, base_url="https://api.example.com/v1", transport=httpx.MockTransport(handler)
    )[0]
    await module.invoke(CTX, {"id": "../admin/keys?x=1#"})
    assert seen[0].raw_path.split(b"?")[0] == b"/v1/users/..%2Fadmin%2Fkeys%3Fx%3D1%23"
    assert seen[0].query == b""


def test_self_referential_schema_ref_stops_expanding_at_cycle() -> None:
    node = {"$ref": "#/components/schemas/Node"}
    spec = {
        "openapi": "3.0.0",
        "components": {"schemas": {"Node": {"type": "object", "properties": {"child": node}}}},
        "paths": {
            "/n": {
                "post": {
                    "operationId": "n",
                    "requestBody": {"content": {"application/json": {"schema": node}}},
                }
            }
        },
    }
    op = OpenAPISpecParser(spec).parse_operations()[0]
    assert op.body_schema == {"type": "object", "properties": {"child": {}}}


async def test_idempotency_key_is_not_sent_in_request_body() -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={})

    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/x": {
                "post": {
                    "operationId": "p",
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    },
                }
            }
        },
    }
    module = import_openapi_operations(
        spec, base_url="https://api.example.com", transport=httpx.MockTransport(handler)
    )[0]
    await module.invoke(CTX, {"name": "a", "idempotency_key": "k1"})
    assert b"idempotency_key" not in seen[0]
    assert b'"name"' in seen[0]
