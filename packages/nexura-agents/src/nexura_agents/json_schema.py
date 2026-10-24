# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Small JSON-schema toolkit for structured output (no third-party dependency).

Covers three jobs:

* ``check_schema`` rejects schemas outside the supported subset (used for dict ``output_type``).
* ``validate_instance`` validates a parsed JSON value and returns readable error lines that the
  runner sends back to the model on an output retry.
* ``to_openai_schema`` / ``to_gemini_schema`` adapt a schema to a provider's native
  structured-output dialect.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from nexura_agents.errors import SDKValidationError

# Keywords a dict ``output_type`` may use. Everything else is rejected at Agent construction.
SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "description",
        "title",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "$defs",
        "$ref",
        "$schema",
        "$comment",
    }
)
_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})
_MAX_ERRORS = 8


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _resolve(root: dict[str, Any], ref: str) -> dict[str, Any] | None:
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        return None
    target = root.get("$defs", {})
    node = target.get(ref[len(prefix) :]) if isinstance(target, dict) else None
    return node if isinstance(node, dict) else None


def check_schema(schema: Any) -> None:
    """Raise ``SDKValidationError`` naming unsupported keywords or malformed parts."""
    if not isinstance(schema, dict):
        raise SDKValidationError("output_type schema must be a JSON-schema dict.")
    problems: list[str] = []
    _check_node(schema, schema, "$", problems)
    if problems:
        shown = "; ".join(problems[:_MAX_ERRORS])
        raise SDKValidationError(
            f"Invalid output_type JSON schema: {shown}. Supported keywords: "
            f"{', '.join(sorted(SUPPORTED_KEYWORDS))}."
        )


def _check_node(root: dict[str, Any], node: Any, path: str, problems: list[str]) -> None:
    if not isinstance(node, dict):
        problems.append(f"{path} must be an object")
        return
    unknown = sorted(set(node) - SUPPORTED_KEYWORDS)
    if unknown:
        problems.append(f"unsupported keyword(s) {', '.join(repr(k) for k in unknown)} at {path}")
    declared = node.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if (
            not names
            or not all(isinstance(n, str) and n in _TYPES for n in names)
            or len(set(map(str, names))) != len(names)
        ):
            problems.append(f"'type' at {path} must be one of {sorted(_TYPES)} or a list of them")
    if "required" in node:
        req = node["required"]
        if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
            problems.append(f"'required' at {path} must be a list of strings")
    if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
        problems.append(f"'enum' at {path} must be a non-empty list")
    for key in ("minimum", "maximum"):
        if key in node and not _is_number(node[key]):
            problems.append(f"'{key}' at {path} must be a number")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in node and (
            not isinstance(node[key], int) or isinstance(node[key], bool) or node[key] < 0
        ):
            problems.append(f"'{key}' at {path} must be an integer >= 0")
    for key in ("description", "title"):
        if key in node and not isinstance(node[key], str):
            problems.append(f"'{key}' at {path} must be a string")
    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or _resolve(root, ref) is None:
            problems.append(
                f"'$ref' at {path} must point to a local '#/$defs/<name>' that exists, got {ref!r}"
            )
    props = node.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            problems.append(f"'properties' at {path} must be an object")
        else:
            for name, sub in props.items():
                _check_node(root, sub, f"{path}.{name}", problems)
    if "items" in node:
        _check_node(root, node["items"], f"{path}[]", problems)
    extra = node.get("additionalProperties")
    if extra is not None and not isinstance(extra, bool):
        _check_node(root, extra, f"{path}.*", problems)
    defs = node.get("$defs")
    if defs is not None:
        if not isinstance(defs, dict):
            problems.append(f"'$defs' at {path} must be an object")
        else:
            for name, sub in defs.items():
                _check_node(root, sub, f"$defs.{name}", problems)


def validate_instance(schema: dict[str, Any], value: Any, *, show_values: bool = True) -> list[str]:
    """Return human-readable errors (empty when ``value`` matches ``schema``).

    ``show_values=False`` leaves the checked values out of the messages (only locations,
    expected types and limits), for text that has not passed output guardrails yet.
    """
    errors: list[str] = []
    _validate(schema, schema, value, "$", errors, 0, show_values)
    return errors


def _validate(
    root: dict[str, Any],
    node: dict[str, Any],
    value: Any,
    path: str,
    errors: list[str],
    depth: int,
    show: bool = True,
) -> None:
    if len(errors) >= _MAX_ERRORS:
        return
    if depth > 64:
        errors.append(f"{path}: schema recursion too deep")
        return
    if "$ref" in node:
        target = _resolve(root, node["$ref"])
        if target is not None:
            _validate(root, target, value, path, errors, depth + 1, show)
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        if not siblings:
            return
        node = siblings
    declared = node.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_matches_type(n, value) for n in names):
            errors.append(f"{path}: expected {' or '.join(names)}, got {_type_name(value)}")
            return
    if "enum" in node and not any(_same(value, option) for option in node["enum"]):
        shown = repr(value) if show else "the value"
        errors.append(f"{path}: {shown} is not one of {node['enum']!r}")
    if "const" in node and not _same(value, node["const"]):
        got = f", got {value!r}" if show else ""
        errors.append(f"{path}: expected the constant {node['const']!r}{got}")
    if _is_number(value):
        number = str(value) if show else "the value"
        if "minimum" in node and value < node["minimum"]:
            errors.append(f"{path}: {number} is less than the minimum {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            errors.append(f"{path}: {number} is greater than the maximum {node['maximum']}")
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            errors.append(f"{path}: text is shorter than minLength {node['minLength']}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.append(f"{path}: text is longer than maxLength {node['maxLength']}")
    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            errors.append(f"{path}: has {len(value)} items, minItems is {node['minItems']}")
        if "maxItems" in node and len(value) > node["maxItems"]:
            errors.append(f"{path}: has {len(value)} items, maxItems is {node['maxItems']}")
        items = node.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(value):
                _validate(root, items, element, f"{path}[{index}]", errors, depth + 1, show)
    if isinstance(value, dict):
        for name in node.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required property '{name}'")
        props = node.get("properties", {})
        extra = node.get("additionalProperties")
        for name, member in value.items():
            if name in props:
                _validate(root, props[name], member, f"{path}.{name}", errors, depth + 1, show)
            elif extra is False:
                errors.append(f"{path}: unexpected property" + (f" '{name}'" if show else ""))
            elif isinstance(extra, dict):
                where = f"{path}.{name}" if show else f"{path}.*"
                _validate(root, extra, member, where, errors, depth + 1, show)


def _matches_type(name: str, value: Any) -> bool:
    if name == "number":
        return _is_number(value) and not (isinstance(value, float) and not math.isfinite(value))
    if name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer()
        )
    return _type_name(value) == name


def _same(a: Any, b: Any) -> bool:
    # JSON equality: True must not equal 1.
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return bool(a == b)


# --- provider dialects ---------------------------------------------------------------------


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``schema`` with local ``$ref`` replaced by their definitions and ``$defs`` removed.

    A reference that would recurse forever is replaced by a bare ``{"type": "object"}``.
    """
    root = copy.deepcopy(schema)

    def walk(node: Any, stack: tuple[str, ...]) -> Any:
        """Walk one schema node (never a properties map or a literal value)."""
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in stack:
                return {"type": "object"}
            target: Any = root
            for part in ref[2:].split("/"):
                target = target.get(part) if isinstance(target, dict) else None
            if isinstance(target, dict):
                merged = walk(target, (*stack, ref))
                siblings = walk({k: v for k, v in node.items() if k != "$ref"}, stack)
                return {**merged, **siblings}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$defs", "definitions"):
                continue
            if key in ("properties", "patternProperties") and isinstance(value, dict):
                out[key] = {name: walk(sub, stack) for name, sub in value.items()}
            elif key in ("items", "additionalProperties", "not", "if", "then", "else"):
                out[key] = walk(value, stack)
            elif key in ("anyOf", "oneOf", "allOf", "prefixItems") and isinstance(value, list):
                out[key] = [walk(sub, stack) for sub in value]
            else:
                out[key] = value
        return out

    out = walk(root, ())
    return out if isinstance(out, dict) else {}


_STRICT_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "description",
        "title",
        "$defs",
        "$ref",
        "anyOf",
    }
)


def _strict_safe(node: Any, is_root: bool) -> bool:
    if isinstance(node, list):
        return all(_strict_safe(n, False) for n in node)
    if not isinstance(node, dict):
        return True
    if not set(node) <= _STRICT_KEYWORDS:
        return False
    declared = node.get("type")
    names = declared if isinstance(declared, list) else [declared]
    if is_root and "object" not in names:
        return False
    if "object" in names or "properties" in node:
        props = node.get("properties")
        if not isinstance(props, dict) or not props:
            return False  # free-form object: strict mode cannot express it
        if set(node.get("required", [])) != set(props):
            return False  # optional properties would change meaning under strict
        if node.get("additionalProperties", False) is not False:
            return False
    members: list[Any] = []
    for key in ("properties", "$defs"):
        if isinstance(node.get(key), dict):
            members.extend(node[key].values())
    for key in ("items", "anyOf"):
        if key in node:
            members.append(node[key])
    return all(_strict_safe(m, False) for m in members)


def _close_objects(node: Any) -> Any:
    """Add ``additionalProperties: false`` to every object schema node (schema-aware)."""
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            out[key] = {name: _close_objects(sub) for name, sub in value.items()}
        elif key in ("items", "additionalProperties"):
            out[key] = _close_objects(value)
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            out[key] = [_close_objects(sub) for sub in value]
        else:
            out[key] = value
    if "properties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def to_openai_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return ``(schema, strict)`` for OpenAI ``response_format``.

    Strict mode is used only when it cannot change what a valid answer is: the root is an
    object, every object lists all its properties as required, no free-form objects, and only
    keywords strict mode documents are present. Then ``additionalProperties: false`` is added to
    each object. Otherwise the schema is sent unchanged with ``strict: false``.
    """
    if _strict_safe(schema, True):
        return _close_objects(copy.deepcopy(schema)), True
    return copy.deepcopy(schema), False


def openai_response_format(schema: dict[str, Any]) -> dict[str, Any]:
    """``response_format`` for a JSON schema; ``strict`` only when it is safe (see json_schema)."""
    converted, strict = to_openai_schema(schema)
    return {
        "type": "json_schema",
        "json_schema": {"name": "output_schema", "schema": converted, "strict": strict},
    }


_GEMINI_KEYWORDS = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "propertyOrdering",
    }
)


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a JSON schema to Gemini's ``responseSchema`` dialect.

    ``$ref`` are inlined, ``$defs``/``additionalProperties``/``title``/``default``/length
    keywords and other unsupported keywords are dropped, ``type: [X, "null"]`` becomes
    ``type: X`` with ``nullable: true`` and ``const`` becomes a one-value ``enum``. Dropped
    constraints are still enforced by the runner's output validation and retry loop.
    """

    def convert(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        if "const" in node and "enum" not in node:
            out["enum"] = [node["const"]]
        for key, value in node.items():
            if key not in _GEMINI_KEYWORDS:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: convert(sub) for name, sub in value.items()}
            elif key == "items":
                out[key] = convert(value)
            elif key == "anyOf" and isinstance(value, list):
                variants = [convert(v) for v in value]
                plain = [v for v in variants if v != {"type": "null"}]
                if len(plain) == 1 and len(variants) == 2:
                    out.update(plain[0])
                    out["nullable"] = True
                else:
                    out[key] = plain or variants
                    if len(plain) != len(variants):
                        out["nullable"] = True
            elif key == "type" and isinstance(value, list):
                kinds = [t for t in value if t != "null"]
                if len(kinds) != len(value):
                    out["nullable"] = True
                if len(kinds) == 1:
                    out["type"] = kinds[0]
                elif kinds:
                    out["anyOf"] = [{"type": t} for t in kinds]
            elif key == "enum" and isinstance(value, list):
                out[key] = [v for v in value if v is not None] or value
                if None in value:
                    out["nullable"] = True
            else:
                out[key] = value
        return out

    converted = convert(inline_refs(schema))
    return converted if isinstance(converted, dict) else {}
