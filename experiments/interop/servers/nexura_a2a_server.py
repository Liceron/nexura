"""Nexura A2AServer used by the interop tests.

    python nexura_a2a_server.py http PORT [SIGNING_KEY_PEM]

Serves an agent driven by a labelled local fake model (no network, no paid calls). With a
PEM private key the agent card is signed (kid ``nexura-key-1``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from nexura_agents import Agent, FakeModel
from nexura_agents.a2a import A2AServer

if __name__ == "__main__":
    port = int(sys.argv[2])
    key = (
        load_pem_private_key(Path(sys.argv[3]).read_bytes(), password=None)
        if len(sys.argv) > 3
        else None
    )
    agent = Agent(
        name="nexura_helper",
        description="Answers with a fixed sentence (local fake model).",
        instructions="",
        model=FakeModel(responses=["fake-model answer"] * 50),
    )
    server = A2AServer(
        agent,
        url=f"http://127.0.0.1:{port}/",
        signing_key=key,
        signing_kid="nexura-key-1" if key is not None else None,
    )
    uvicorn.run(server.http_app(), host="127.0.0.1", port=port, log_level="warning")
