# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Deterministic UUID v5 identifiers."""

import uuid
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import TEXT, ComponentModule, manifest


class UuidModule(ComponentModule):
    manifest = manifest(
        "utils",
        "uuid",
        "Derive UUID5 from a namespace UUID (or dns/url/oid/x500) and a UTF-8 name.",
        {"namespace": {"type": "string", "maxLength": 36}, "name": TEXT},
        ["namespace", "name"],
        {"uuid": {"type": "string", "minLength": 36, "maxLength": 36}},
        {"namespace": "dns", "name": "example.test"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        namespaces = {
            "dns": uuid.NAMESPACE_DNS,
            "url": uuid.NAMESPACE_URL,
            "oid": uuid.NAMESPACE_OID,
            "x500": uuid.NAMESPACE_X500,
        }
        raw = inputs["namespace"]
        namespace = namespaces[raw] if raw in namespaces else uuid.UUID(raw)
        return {"uuid": str(uuid.uuid5(namespace, inputs["name"]))}
