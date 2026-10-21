"""An A2A agent built with the official ``a2a-sdk`` 1.2.3, used by the interop tests.

    python official_a2a_server.py http PORT [SIGNING_KEY_PEM]

The executor answers ``echo: <text>`` with one agent message (no model, no network).
JSON-RPC is served at ``/`` with the SDK's v0.3 compatibility switched on. With a PEM
private key the card is signed by the SDK's own signer (ES256, kid ``official-key-1``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn
from a2a.helpers.proto_helpers import get_message_text, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette


class EchoExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = get_message_text(context.message) if context.message else ""
        await event_queue.enqueue_event(
            new_text_message(f"echo: {text}", context_id=context.context_id)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return None


def card(port: int) -> AgentCard:
    return AgentCard(
        name="official-echo",
        description="Echoes the request (a2a-sdk reference server).",
        version="1.2.3",
        supported_interfaces=[
            AgentInterface(
                url=f"http://127.0.0.1:{port}/", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(id="echo", name="Echo", description="Echoes text.", tags=["echo"])],
    )


if __name__ == "__main__":
    port = int(sys.argv[2])
    agent_card = card(port)
    if len(sys.argv) > 3:
        from a2a.utils.signing import create_agent_card_signer

        signer = create_agent_card_signer(
            Path(sys.argv[3]).read_bytes(),
            {"alg": "ES256", "kid": "official-key-1", "typ": "JOSE", "jku": None},
        )
        agent_card = signer(agent_card)
    handler = DefaultRequestHandler(
        agent_executor=EchoExecutor(), task_store=InMemoryTaskStore(), agent_card=agent_card
    )
    routes = [
        *create_agent_card_routes(agent_card),
        *create_jsonrpc_routes(handler, "/", enable_v0_3_compat=True),
    ]
    uvicorn.run(Starlette(routes=routes), host="127.0.0.1", port=port, log_level="warning")
