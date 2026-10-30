# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""HTTP request metadata of MCP 2026-07-28 Streamable HTTP.

The transport mirrors body fields into headers so gateways can route without parsing
JSON: ``MCP-Protocol-Version``, ``Mcp-Method``, ``Mcp-Name`` (tool or prompt name,
resource URI) and ``Mcp-Param-{Name}`` for tool parameters annotated with
``x-mcp-header`` in the tool's ``inputSchema``. A server that reads the body checks
that each header matches it and answers ``HeaderMismatch`` (-32020) otherwise.

Written from the specification text
(https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http).
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterator, Mapping
from typing import Any

PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
METHOD_HEADER = "Mcp-Method"
NAME_HEADER = "Mcp-Name"
PARAM_HEADER_PREFIX = "Mcp-Param-"
X_MCP_HEADER = "x-mcp-header"

_SENTINEL = re.compile(r"^=\?base64\?(?P<payload>.*)\?=$", re.DOTALL)
_VISIBLE_ASCII = re.compile(r"^[\x20-\x7e]*$")
# RFC 9110 section 5.6.2 ``token``: the characters allowed in a header field name.
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_PRIMITIVE_TYPES = frozenset({"string", "integer", "boolean"})
_MAX_SAFE_INTEGER = 2**53 - 1
_DECIMAL = re.compile(r"^(-?[0-9]+)(?:\.0+)?$")

# JSON Schema 2020-12 keywords whose values are subschemas. Only ``properties`` keeps a
# position "statically reachable"; crossing any other one makes an annotation invalid.
_ONE_SCHEMA = frozenset(
    {
        "items",
        "contains",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
_SCHEMA_LIST = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_MAP = frozenset({"patternProperties", "dependentSchemas", "$defs", "definitions"})


class InvalidHeaderAnnotation(ValueError):
    """A tool's ``x-mcp-header`` annotation breaks the specification's constraints."""


def encode_value(value: str) -> str:
    """Header form of ``value``: verbatim when safe, else ``=?base64?...?=`` of its UTF-8.

    Safe means visible ASCII or space, no leading/trailing whitespace, and not itself
    shaped like the Base64 sentinel.
    """
    if _VISIBLE_ASCII.match(value) and value == value.strip() and not _SENTINEL.match(value):
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def decode_value(value: str | None) -> str | None:
    """Inverse of :func:`encode_value`. A malformed sentinel decodes to ``None``."""
    if value is None:
        return None
    match = _SENTINEL.match(value)
    if match is None:
        return value
    payload = match.group("payload")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if base64.b64encode(raw).decode("ascii") != payload:
        return None  # non-canonical encoding
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def header_safe(value: str) -> bool:
    """Whether ``value`` may appear in a header without encoding (no CR, LF or other controls)."""
    return bool(_VISIBLE_ASCII.match(value)) or value == ""


def _positions(schema: Any) -> Iterator[tuple[tuple[str, ...] | None, dict[str, Any]]]:
    """Every subschema with its ``properties`` path, or ``None`` once off that chain."""
    stack: list[tuple[tuple[str, ...] | None, Any]] = [((), schema)]
    while stack:
        path, node = stack.pop()
        if not isinstance(node, dict):
            continue
        yield path, node
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for name, sub in value.items():
                    stack.append(((*path, str(name)) if path is not None else None, sub))
            elif key in _ONE_SCHEMA:
                stack.append((None, value))
            elif key in _SCHEMA_LIST and isinstance(value, list):
                stack.extend((None, sub) for sub in value)
            elif key in _SCHEMA_MAP and isinstance(value, dict):
                stack.extend((None, sub) for sub in value.values())


def header_paths(input_schema: Any) -> dict[tuple[str, ...], str]:
    """Map each ``x-mcp-header`` annotated property path to its header name part.

    Raises ``InvalidHeaderAnnotation`` when an annotation is empty, not an RFC 9110
    token, duplicated (case-insensitively), on a non-primitive or ``number`` property,
    or not reachable from the root through ``properties`` keys alone.
    """
    found: dict[tuple[str, ...], str] = {}
    seen: dict[str, str] = {}
    for path, node in _positions(input_schema):
        if X_MCP_HEADER not in node:
            continue
        if not path:
            raise InvalidHeaderAnnotation(
                "x-mcp-header must sit on a property reached only through 'properties'."
            )
        where = ".".join(path)
        token = node[X_MCP_HEADER]
        if not isinstance(token, str) or not _TOKEN.match(token):
            raise InvalidHeaderAnnotation(
                f"x-mcp-header on '{where}' must be a non-empty RFC 9110 token."
            )
        if node.get("type") not in _PRIMITIVE_TYPES:
            raise InvalidHeaderAnnotation(
                f"x-mcp-header on '{where}' is only allowed on string, integer or boolean "
                "properties."
            )
        if token.lower() in seen:
            raise InvalidHeaderAnnotation(
                f"x-mcp-header '{token}' on '{where}' duplicates '{seen[token.lower()]}'."
            )
        seen[token.lower()] = where
        found[path] = token
    return found


def _value_at(arguments: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = arguments
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def render_scalar(value: Any) -> str | None:
    """String form of a primitive argument, or ``None`` when it has no header form."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            return None
        return str(value)
    if isinstance(value, str):
        return value
    return None


def param_headers(
    paths: Mapping[tuple[str, ...], str], arguments: Mapping[str, Any]
) -> dict[str, str]:
    """``Mcp-Param-*`` headers for a ``tools/call``; absent or null values send none."""
    headers: dict[str, str] = {}
    for path, token in paths.items():
        rendered = render_scalar(_value_at(arguments, path))
        if rendered is not None:
            headers[PARAM_HEADER_PREFIX + token] = encode_value(rendered)
    return headers


def check_param_headers(
    paths: Mapping[tuple[str, ...], str],
    arguments: Mapping[str, Any],
    headers: Mapping[str, str],
) -> str | None:
    """Why the ``Mcp-Param-*`` headers disagree with the body, or ``None`` if they agree.

    ``headers`` must have lower-case names. Integer parameters compare numerically.
    """
    for path, token in paths.items():
        raw = headers.get((PARAM_HEADER_PREFIX + token).lower())
        value = _value_at(arguments, path)
        rendered = render_scalar(value) if value is not None else None
        name = PARAM_HEADER_PREFIX + token
        if raw is None and rendered is None:
            continue
        if raw is None:
            return f"{name} header is missing but the body sets '{'.'.join(path)}'."
        if not header_safe(raw):
            return f"{name} header contains invalid characters."
        decoded = decode_value(raw)
        if decoded is None:
            return f"{name} header has a malformed Base64 value."
        if rendered is None:
            return f"{name} header is set but the body has no matching value."
        if isinstance(value, int) and not isinstance(value, bool):
            # Integers compare numerically: "42.0" matches 42.
            match = _DECIMAL.match(decoded)
            if match and int(match.group(1)) == value:
                continue
            return f"{name} header does not match the body."
        if decoded != rendered:
            return f"{name} header does not match the body."
    return None
