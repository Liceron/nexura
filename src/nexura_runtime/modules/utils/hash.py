# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Hash UTF-8 text for deterministic fingerprints."""

import hashlib
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import TEXT, ComponentModule, choice, manifest


class HashModule(ComponentModule):
    manifest = manifest(
        "utils",
        "hash",
        "Compute a SHA-256, SHA-1 or MD5 hexadecimal fingerprint (not password hashing).",
        {"text": TEXT, "algorithm": choice("sha256", "sha1", "md5")},
        ["text"],
        {"digest": {"type": "string", "minLength": 32, "maxLength": 64}},
        {"text": "hello"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        return {
            "digest": hashlib.new(
                inputs.get("algorithm", "sha256"),
                inputs["text"].encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest()
        }
