"""Long-term memory with one namespace per user.

Scenario: a shared assistant serves Alice and Bob. ``Memory`` derives its
namespace from ``ctx.deps.user_id`` (authenticated data, never model text), so
each user only recalls their own facts. Each user saves a fact in one run and
the fact is recalled in a later, separate run. A final run shows isolation.

Demonstrates: ``Memory(store, namespace=lambda ctx: ...)``, ``remember`` tool,
automatic recall into system instructions, ``HashingEmbedder`` (offline only).

Offline: scripted ``FakeModel``. For production use a real embedder and model:
# model="openai:gpt-4.1-mini"
"""

import asyncio
from dataclasses import dataclass

from nexura_agents import Agent, FakeModel, HashingEmbedder, InMemoryMemoryStore, Memory, ToolCall


@dataclass
class Deps:
    user_id: str


store = InMemoryMemoryStore(HashingEmbedder())
memory = Memory(store, namespace=lambda ctx: ctx.deps.user_id)


async def remember_fact(user: str, fact: str, prompt: str) -> None:
    model = FakeModel(
        responses=[[ToolCall(id="m1", name="remember", arguments={"text": fact})], "Noted."]
    )
    agent = Agent("Helper", "Be helpful.", model, memory=memory)
    await agent.run_async(prompt, deps=Deps(user))


async def ask(user: str, question: str) -> str:
    model = FakeModel(responses=["ok"])
    agent = Agent("Helper", "Be helpful.", model, memory=memory)
    await agent.run_async(question, deps=Deps(user))
    return model.calls[0]["messages"][0].content  # system prompt with recalled memories


async def main() -> None:
    await remember_fact("alice", "Alice is allergic to peanuts", "Remember my peanut allergy.")
    await remember_fact("bob", "Bob is allergic to shellfish", "Remember my shellfish allergy.")

    alice_system = await ask("alice", "What is the user allergic to?")
    bob_system = await ask("bob", "What is the user allergic to?")

    print("alice recalled peanuts:", "peanuts" in alice_system)
    print("alice sees bob's memory:", "shellfish" in alice_system)
    print("bob recalled shellfish:", "shellfish" in bob_system)
    print("bob sees alice's memory:", "peanuts" in bob_system)
    assert "peanuts" in alice_system and "shellfish" not in alice_system
    assert "shellfish" in bob_system and "peanuts" not in bob_system
    print("stored per namespace:", {u: len(await store.list(u)) for u in ("alice", "bob", "carol")})
    print("MEMORY ISOLATION OK")


if __name__ == "__main__":
    asyncio.run(main())
