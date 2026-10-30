# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""B00/B01 Module wrapper for Sessions and Long-Term Memory."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.types import Message

from nexura_runtime.memory.store import MemoryStore


class MemoryModule(BaseModule):
    """Module wrapper exposing session and memory management via B00/B01 contracts."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.runtime.memory",
        version="1.0.0",
        kind="memory_manager",
        title="Sessions and Long-Term Memory Manager",
        description=(
            "Workspace-scoped session management, revision CAS, and long-term memory facts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "create_session",
                        "append_turn",
                        "get_session",
                        "store_fact",
                        "find_facts",
                        "delete_fact",
                    ],
                },
                "workspace_id": {"type": "string"},
                "principal_id": {"type": "string"},
                "session_id": {"type": "string"},
                "user_message": {"type": "object"},
                "assistant_message": {"type": "object"},
                "expected_revision": {"type": "integer"},
                "fact_key": {"type": "string"},
                "fact_value": {},
                "fact_id": {"type": "string"},
                "category": {"type": "string"},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "session_id": {"type": "string"},
                "revision": {"type": "integer"},
                "messages_count": {"type": "integer"},
                "fact_id": {"type": "string"},
                "facts_count": {"type": "integer"},
            },
            "required": ["operation"],
            "additionalProperties": True,
        },
        effect_policy="idempotent_write",
    )

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or MemoryStore(":memory:")

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        op = inputs["operation"]
        ws = inputs.get("workspace_id") or context.workspace_id
        principal = inputs.get("principal_id") or context.principal_id

        if op == "create_session":
            s_id = inputs.get("session_id") or f"sess_{context.run_id}"
            created = self.store.create_session(ws, principal, s_id)
            return ModuleResult(
                outputs={
                    "operation": op,
                    "session_id": created.session_id,
                    "revision": created.revision,
                    "messages_count": len(created.messages),
                }
            )

        elif op == "get_session":
            s_id = inputs["session_id"]
            sess = self.store.get_session(ws, s_id, principal_id=principal)
            if not sess:
                raise ValueError(f"Session '{s_id}' not found.")
            return ModuleResult(
                outputs={
                    "operation": op,
                    "session_id": sess.session_id,
                    "revision": sess.revision,
                    "messages_count": len(sess.messages),
                }
            )

        elif op == "append_turn":
            s_id = inputs["session_id"]
            u_raw = inputs["user_message"]
            a_raw = inputs["assistant_message"]
            user_msg = Message(role=u_raw["role"], content=u_raw["content"])
            asst_msg = Message(role=a_raw["role"], content=a_raw["content"])
            rev = inputs.get("expected_revision", 1)
            updated = self.store.append_turn(
                ws, s_id, user_msg, asst_msg, expected_revision=rev, principal_id=principal
            )
            return ModuleResult(
                outputs={
                    "operation": op,
                    "session_id": updated.session_id,
                    "revision": updated.revision,
                    "messages_count": len(updated.messages),
                }
            )

        elif op == "store_fact":
            key = inputs["fact_key"]
            val = inputs["fact_value"]
            cat = inputs.get("category", "general")
            fact = self.store.store_fact(ws, principal, key, val, category=cat)
            return ModuleResult(
                outputs={
                    "operation": op,
                    "fact_id": fact.fact_id,
                }
            )

        elif op == "find_facts":
            key = inputs.get("fact_key")
            cat = inputs.get("category")
            facts = self.store.find_facts(ws, principal, query_key=key, category=cat)
            return ModuleResult(
                outputs={
                    "operation": op,
                    "facts_count": len(facts),
                }
            )

        elif op == "delete_fact":
            f_id = inputs["fact_id"]
            self.store.delete_fact(ws, principal, f_id)
            return ModuleResult(
                outputs={
                    "operation": op,
                    "fact_id": f_id,
                }
            )

        raise ValueError(f"Unsupported operation '{op}'.")
