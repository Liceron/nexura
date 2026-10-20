"""B08 Standalone Example: Sessions and Long-Term Memory.

Demonstrates:
1. Creating a persisted multilingual conversation session (English, Hindi, Tamil).
2. Appending turns with revision CAS tracking.
3. Storing long-term memory facts with source provenance.
4. Closing and reopening persistent SQLite storage to verify durability.
5. Performing explicit memory deletion and confirming exclusion from retrieval.
6. Building bounded LLM context with fact injection without duplicate system prompts.
7. Invoking memory operations via the B01 CLI interface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from nexura_runtime.types import Message

from nexura_runtime.memory import (
    ContextTruncationPolicy,
    MemoryModule,
    MemoryStore,
)
from nexura_runtime.modules.cli import _run_command
from nexura_runtime.modules.registry import VersionedModuleRegistry


async def main() -> None:
    print("=== B08: Sessions and Long-Term Memory Demonstration ===\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sessions_and_memory.db"

        # -------------------------------------------------------------
        # 1. Initialize Memory Store & Multilingual Session
        # -------------------------------------------------------------
        print("1. Initializing MemoryStore with SQLite persistence...")
        store = MemoryStore(db_path)
        workspace = "ws_india_retail"
        principal = "user_rajesh"
        session_id = "sess_support_001"

        print(f"  Creating session '{session_id}' for '{principal}' in '{workspace}'...")
        initial_system = Message(
            role="system",
            content="You are a trusted multilingual assistant for Nexura Bank.",
        )
        session = store.create_session(
            workspace_id=workspace,
            principal_id=principal,
            session_id=session_id,
            initial_messages=[initial_system],
            metadata={"channel": "mobile_app", "language": "hi"},
        )
        print(
            f"  Session created at revision {session.revision}. Messages: {len(session.messages)}"
        )

        # -------------------------------------------------------------
        # 2. Turn 1 (Hindi / Devanagari Script)
        # -------------------------------------------------------------
        print("\n2. Appending Turn 1 (Hindi inquiry)...")
        user_turn_1 = Message(
            role="user",
            content="नमस्ते! क्या मुझे अपने बचत खाते (Savings Account) का शेष मिल सकता है?",
        )
        asst_turn_1 = Message(
            role="assistant",
            content="नमस्ते राजेश जी! आपके बचत खाते में वर्तमान शेष ₹45,250.00 है।",
        )
        session = store.append_turn(
            workspace_id=workspace,
            session_id=session_id,
            user_message=user_turn_1,
            assistant_message=asst_turn_1,
            expected_revision=session.revision,
            principal_id=principal,
        )
        print(f"  Turn 1 committed. Session is now at revision {session.revision}.")
        assert session.revision == 2

        # -------------------------------------------------------------
        # 3. Turn 2 (Tamil Script / Language Preference)
        # -------------------------------------------------------------
        print("\n3. Appending Turn 2 (Tamil language preference)...")
        user_turn_2 = Message(
            role="user",
            content="வணக்கம், அடுத்த முறை எனக்கு அறிவிப்புகளை தமிழில் அனுப்ப முடியுமா?",
        )
        asst_turn_2 = Message(
            role="assistant",
            content="கண்டிப்பாக! உங்கள் விருப்பமான மொழியாக தமிழ் பதிவு செய்யப்பட்டுள்ளது.",
        )
        session = store.append_turn(
            workspace_id=workspace,
            session_id=session_id,
            user_message=user_turn_2,
            assistant_message=asst_turn_2,
            expected_revision=session.revision,
            principal_id=principal,
        )
        print(f"  Turn 2 committed. Session is now at revision {session.revision}.")
        assert session.revision == 3
        assert len(session.messages) == 5  # 1 system + 2 turns (4 messages)

        # -------------------------------------------------------------
        # 4. Extract and Store Long-Term Memory Facts
        # -------------------------------------------------------------
        print("\n4. Storing long-term memory facts for user...")
        fact_lang = store.store_fact(
            workspace_id=workspace,
            principal_id=principal,
            key="preferred_language",
            value="Tamil",
            category="preference",
            source_session_id=session_id,
        )
        fact_acct = store.store_fact(
            workspace_id=workspace,
            principal_id=principal,
            key="primary_account_type",
            value="Savings",
            category="financial",
            source_session_id=session_id,
        )
        fact_city = store.store_fact(
            workspace_id=workspace,
            principal_id=principal,
            key="residence_city",
            value="New Delhi",
            category="profile",
            source_session_id=session_id,
        )
        print(f"  Stored facts: {fact_lang.fact_key}, {fact_acct.fact_key}, {fact_city.fact_key}")

        # -------------------------------------------------------------
        # 5. Reopen Storage and Verify Durability
        # -------------------------------------------------------------
        print("\n5. Reopening persistent SQLite storage from disk...")
        reopened_store = MemoryStore(db_path)
        restored_session = reopened_store.get_session(workspace, session_id, principal_id=principal)
        assert restored_session is not None
        assert restored_session.revision == 3
        print(f"  Restored session messages count: {len(restored_session.messages)}")
        for i, msg in enumerate(restored_session.messages):
            print(f"    [{i}] ({msg.role}): {msg.content}")

        # -------------------------------------------------------------
        # 6. Explicit Memory Deletion
        # -------------------------------------------------------------
        print("\n6. Performing explicit memory deletion (deleting 'residence_city')...")
        deleted = reopened_store.delete_fact(workspace, principal, fact_city.fact_id)
        assert deleted is True

        # Query facts: confirm deleted fact is excluded
        active_facts = reopened_store.find_facts(workspace, principal)
        active_keys = [f.fact_key for f in active_facts]
        print(f"  Active facts after deletion: {active_keys}")
        assert "residence_city" not in active_keys
        assert "preferred_language" in active_keys
        assert "primary_account_type" in active_keys
        print("  Verification: Deleted memory is strictly excluded from retrieval.")

        # -------------------------------------------------------------
        # 7. Bounded Context Generation with Fact Injection
        # -------------------------------------------------------------
        print("\n7. Constructing bounded context with fact injection...")
        policy = ContextTruncationPolicy(max_messages=4, preserve_system_prompt=True)
        bounded_msgs = reopened_store.build_bounded_context(
            restored_session, policy=policy, facts=active_facts
        )
        print(f"  Bounded context produced {len(bounded_msgs)} messages (max 4).")
        print(f"  System prompt with facts injected:\n'''\n{bounded_msgs[0].content}\n'''")
        assert len(bounded_msgs) <= 4
        assert "Retrieved User Facts:" in (bounded_msgs[0].content or "")
        assert "Tamil" in (bounded_msgs[0].content or "")

        # -------------------------------------------------------------
        # 8. Invocation via B01 CLI
        # -------------------------------------------------------------
        print("\n8. Executing memory query via B01 CLI interface:")
        registry = VersionedModuleRegistry()
        mem_module = MemoryModule(store=reopened_store)
        registry.register(mem_module)

        cli_args = argparse.Namespace(
            module_id="nexura.runtime.memory",
            version="1.0.0",
            input=json.dumps(
                {
                    "operation": "find_facts",
                    "workspace_id": workspace,
                    "principal_id": principal,
                }
            ),
            config="{}",
            workspace=workspace,
            principal="cli_auditor",
            run_id="cli_mem_run",
            node_id="cli_mem_node",
        )
        cli_code = await _run_command(registry, cli_args)
        assert cli_code == 0, "CLI execution failed"
        print("CLI Execution: SUCCESS (code 0)")

    print("\n=== B08 Example Completed Successfully ===")


if __name__ == "__main__":
    asyncio.run(main())
