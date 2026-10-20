"""Keyless two-turn persistent conversation example using SQLiteSessionStore and FakeModel.

Demonstrates:
1. Multi-turn conversation persistence across separate Runner invocations.
2. Deduplication of system instructions across turns.
3. Durable session snapshot revisions and message history.
4. Using task temporary directories by default to avoid cluttering the repository.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from nexura_runtime.session import SQLiteSessionStore

from nexura_runtime import Agent, FakeModel, Runner


async def main() -> None:
    print("=" * 65)
    print("Nexura AgentKit - Two-Turn Persistent Conversation Example")
    print("=" * 65)

    # Use a temporary directory for local SQLite database
    with tempfile.TemporaryDirectory(prefix="nexura_session_") as tmp_dir:
        db_path = Path(tmp_dir) / "persistent_chat.db"
        print(f"Using temporary SQLite database: {db_path}")

        store = SQLiteSessionStore(db_path)
        session_id = "user_chat_session_42"

        # Pre-configured responses for FakeModel simulating 2 turns
        fake_model = FakeModel(
            responses=[
                "नमस्ते! मैं आपकी सहायता के लिए तैयार हूँ। कृपया अपना ऑर्डर आईडी साझा करें।",
                "धन्यवाद! आपका ऑर्डर #ORD-DELHI-101 प्रगति पर है (In Progress)।",
            ]
        )

        agent = Agent(
            name="HindiSupportBot",
            instructions="आप एक विनम्र ग्राहक सहायता सहायक हैं। (You are a helpful customer assistant)",
            model=fake_model,
        )

        runner = Runner(session_store=store)

        # Turn 1
        print("\n--- Turn 1 ---")
        prompt_1 = "नमस्ते, मुझे सहायता चाहिए।"
        print(f"User: {prompt_1}")
        res_1 = await runner.run_async(agent, prompt_1, session_id=session_id)
        print(f"Assistant: {res_1.final_output}")

        snap_1 = await store.get(session_id)
        assert snap_1 is not None
        print(f"Persisted Revision: {snap_1.revision} (Messages: {len(snap_1.messages)})")

        # Turn 2: Reusing the same session_id on the persistent runner
        print("\n--- Turn 2 ---")
        prompt_2 = "मेरा ऑर्डर नंबर ORD-DELHI-101 है।"
        print(f"User: {prompt_2}")
        res_2 = await runner.run_async(agent, prompt_2, session_id=session_id)
        print(f"Assistant: {res_2.final_output}")

        snap_2 = await store.get(session_id)
        assert snap_2 is not None
        print(f"Persisted Revision: {snap_2.revision} (Messages: {len(snap_2.messages)})")

        # Verify system prompt was not duplicated
        system_prompts = [m for m in snap_2.messages if m.role == "system"]
        print(f"\nSystem prompt count: {len(system_prompts)} (Verified deduplicated)")
        assert len(system_prompts) == 1

        print("\nConversation History in Store:")
        for idx, m in enumerate(snap_2.messages, start=1):
            role_tag = f"[{m.role.upper()}]".ljust(12)
            content_preview = (m.content or "")[:60]
            print(f"  {idx}. {role_tag} {content_preview}")

        await store.close()
        print("\nSession store closed cleanly.")

    print("\nTemporary database cleaned up.")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
