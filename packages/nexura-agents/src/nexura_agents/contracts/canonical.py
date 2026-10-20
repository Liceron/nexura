# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Canonical JSON serialization and cryptographic digest utilities.

Ensures deterministic, cross-platform JSON representations conforming to RFC 8785 principles:
- Keys sorted lexicographically.
- Compact separators (no redundant whitespace).
- UTF-8 encoding without escaping non-ASCII characters.
- Strict rejection of non-finite numbers (NaN, Infinity, -Infinity).
- Preservation of distinctions between absent, null, and empty fields.
- Strict rejection of non-string mapping keys (no silent ``str()`` conversion).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel


def _normalize_value(value: Any) -> Any:
    """Recursively normalize data structures for canonical JSON serialization."""
    if isinstance(value, BaseModel):
        return _normalize_value(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize_value(asdict(value))
    if isinstance(value, Enum):
        return _normalize_value(value.value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite float value '{value}' cannot be canonically serialized.")
        # If integer equivalent, format cleanly without trailing .0 if integer
        return value
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        # JSON object keys are strings. A non-string key is refused rather than converted,
        # so ``{1: x}`` and ``{"1": y}`` can never produce the same digest.
        bad = [k for k in value if not isinstance(k, str)]
        if bad:
            raise TypeError(
                f"Mapping key {bad[0]!r} ({type(bad[0]).__name__}) cannot be canonically "
                "serialized; canonical JSON object keys must be strings."
            )
        return {k: _normalize_value(value[k]) for k in sorted(value)}
    # Fallback for custom objects with to_dict
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _normalize_value(value.to_dict())
    raise TypeError(f"Type {type(value).__name__} is not serializable to canonical JSON.")


def canonical_json(obj: Any) -> str:
    """Serialize an object into a canonical, deterministic JSON string.

    Raises:
        ValueError: If non-finite floating point numbers are encountered.
        TypeError: If an unsupported type is encountered.
    """
    normalized = _normalize_value(obj)
    return json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(obj: Any) -> str:
    """Compute the SHA-256 digest of an object's canonical JSON representation."""
    json_bytes = canonical_json(obj).encode("utf-8")
    return hashlib.sha256(json_bytes).hexdigest()
