# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""OpenAPI 3.x specification parser with security boundaries and schema resolution."""

from __future__ import annotations

import json
import re
from typing import Any

from nexura_agents.contracts.manifest import EffectPolicy
from nexura_agents.openapi.errors import (
    DuplicateOperationIdError,
    UnsupportedOpenAPIFeatureError,
)


def _resolve_internal_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve an internal '#/components/schemas/...' reference in the spec."""
    if not ref.startswith("#/"):
        raise UnsupportedOpenAPIFeatureError(
            "$ref",
            f"External or non-root schema references are prohibited: '{ref}'",
        )

    parts = ref[2:].split("/")
    current: Any = spec
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise UnsupportedOpenAPIFeatureError(
                "$ref",
                f"Could not resolve internal reference '{ref}' (segment '{part}' missing)",
            )
        current = current[part]

    if not isinstance(current, dict):
        raise UnsupportedOpenAPIFeatureError(
            "$ref",
            f"Reference '{ref}' did not resolve to an object schema.",
        )
    return current


def resolve_schema_refs(
    spec: dict[str, Any],
    schema: dict[str, Any],
    _chain: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Recursively resolve all internal $refs and reject external ones; cycles stop expanding."""
    if "$ref" in schema:
        ref_val = str(schema["$ref"])
        if not ref_val.startswith("#/"):
            raise UnsupportedOpenAPIFeatureError(
                "$ref",
                f"External schema references are not supported: '{ref_val}'",
            )
        if ref_val in _chain:
            # Recursive schema: stop expanding at the cycle with a permissive schema.
            return {}
        resolved = _resolve_internal_ref(spec, ref_val)
        return resolve_schema_refs(spec, resolved, (*_chain, ref_val))

    resolved_schema: dict[str, Any] = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            resolved_schema[k] = resolve_schema_refs(spec, v, _chain)
        elif isinstance(v, list):
            resolved_schema[k] = [
                resolve_schema_refs(spec, item, _chain) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            resolved_schema[k] = v
    return resolved_schema


class OpenAPIOperationSpec:
    """Parsed representation of a single OpenAPI operation."""

    def __init__(
        self,
        *,
        operation_id: str,
        path: str,
        method: str,
        summary: str,
        description: str,
        path_params: list[str],
        query_params: list[str],
        header_params: list[str],
        body_schema: dict[str, Any] | None,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        effect_policy: EffectPolicy,
        is_idempotent: bool,
        is_gated: bool,
    ) -> None:
        self.operation_id = operation_id
        self.path = path
        self.method = method.upper()
        self.summary = summary
        self.description = description
        self.path_params = path_params
        self.query_params = query_params
        self.header_params = header_params
        self.body_schema = body_schema
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.effect_policy = effect_policy
        self.is_idempotent = is_idempotent
        self.is_gated = is_gated


class OpenAPISpecParser:
    """Parses and validates OpenAPI 3.x specifications into typed operations."""

    def __init__(self, spec: dict[str, Any] | str) -> None:
        if isinstance(spec, str):
            try:
                self.spec: dict[str, Any] = json.loads(spec)
            except json.JSONDecodeError as exc:
                raise UnsupportedOpenAPIFeatureError("syntax", f"Malformed JSON: {exc}") from exc
        else:
            self.spec = dict(spec)

        self.title: str = self.spec.get("info", {}).get("title", "api")
        self.servers: list[str] = [
            s.get("url", "") for s in self.spec.get("servers", []) if s.get("url")
        ]

    def parse_operations(self) -> list[OpenAPIOperationSpec]:
        """Extract and validate all operations, enforcing uniqueness and security."""
        seen_operation_ids: set[str] = set()
        operations: list[OpenAPIOperationSpec] = []

        paths = self.spec.get("paths", {})
        if not isinstance(paths, dict):
            return []

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            for method in ("get", "post", "put", "delete", "patch", "head", "options"):
                if method not in path_item:
                    continue

                op_data = path_item[method]
                if not isinstance(op_data, dict):
                    continue

                op_id = op_data.get("operationId")
                if not op_id:
                    # Generate deterministic operation ID
                    clean_path = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
                    op_id = f"{method}_{clean_path}"

                # Enforce Operation ID uniqueness across the spec
                if op_id in seen_operation_ids:
                    raise DuplicateOperationIdError(op_id, path, method)
                seen_operation_ids.add(op_id)

                summary = op_data.get("summary") or f"{method.upper()} {path}"
                description = op_data.get("description") or summary

                # Parse parameters (path, query, header)
                path_params: list[str] = []
                query_params: list[str] = []
                header_params: list[str] = []
                props: dict[str, Any] = {}
                required: list[str] = []

                all_params = list(path_item.get("parameters", [])) + list(
                    op_data.get("parameters", [])
                )
                for param in all_params:
                    if "$ref" in param:
                        param = resolve_schema_refs(self.spec, param)

                    p_name = param.get("name")
                    p_in = param.get("in")
                    p_req = param.get("required", False)
                    p_schema = param.get("schema", {"type": "string"})
                    p_schema = resolve_schema_refs(self.spec, p_schema)

                    if not p_name or not p_in:
                        continue

                    if p_in == "path":
                        path_params.append(p_name)
                        props[p_name] = p_schema
                        required.append(p_name)
                    elif p_in == "query":
                        query_params.append(p_name)
                        props[p_name] = p_schema
                        if p_req:
                            required.append(p_name)
                    elif p_in == "header":
                        header_params.append(p_name)
                        props[p_name] = p_schema
                        if p_req:
                            required.append(p_name)

                # Parse requestBody
                body_schema: dict[str, Any] | None = None
                req_body = op_data.get("requestBody")
                if req_body:
                    if "$ref" in req_body:
                        req_body = resolve_schema_refs(self.spec, req_body)

                    content = req_body.get("content", {})
                    json_media = content.get("application/json")
                    if json_media and "schema" in json_media:
                        body_schema = resolve_schema_refs(self.spec, json_media["schema"])
                        if req_body.get("required", False):
                            required.append("body")
                        props["body"] = body_schema

                input_schema = {
                    "type": "object",
                    "properties": props,
                    "required": sorted(list(set(required))),
                }

                # Output schema from 200 or 201 response
                output_schema: dict[str, Any] = {
                    "type": "object",
                    "properties": {
                        "status_code": {"type": "integer"},
                        "body": {},
                        "headers": {"type": "object"},
                    },
                }
                responses = op_data.get("responses", {})
                success_resp = responses.get("200") or responses.get("201")
                if isinstance(success_resp, dict):
                    resp_content = success_resp.get("content", {})
                    resp_json = resp_content.get("application/json", {})
                    if "schema" in resp_json:
                        resolved_resp = resolve_schema_refs(self.spec, resp_json["schema"])
                        output_schema["properties"]["body"] = resolved_resp

                # Effect policy & idempotency
                method_upper = method.upper()
                is_idempotent = method_upper in ("GET", "HEAD", "PUT", "DELETE")
                if op_data.get("x-idempotent") is True:
                    is_idempotent = True

                is_gated = op_data.get("x-gated") is True

                effect_policy: EffectPolicy
                if method_upper in ("GET", "HEAD"):
                    effect_policy = "read"
                elif is_idempotent:
                    effect_policy = "idempotent_write"
                else:
                    effect_policy = "non_idempotent_write"

                operations.append(
                    OpenAPIOperationSpec(
                        operation_id=op_id,
                        path=path,
                        method=method,
                        summary=summary,
                        description=description,
                        path_params=path_params,
                        query_params=query_params,
                        header_params=header_params,
                        body_schema=body_schema,
                        input_schema=input_schema,
                        output_schema=output_schema,
                        effect_policy=effect_policy,
                        is_idempotent=is_idempotent,
                        is_gated=is_gated,
                    )
                )

        return operations
