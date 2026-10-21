"""Nexura A2A against the official ``a2a-sdk`` 1.2.3, both directions, offline.

Servers run with uvicorn on 127.0.0.1. Both agent cards are signed with fresh EC P-256
keys: Nexura's by ``A2AServer(signing_key=...)``, the reference server's by the SDK's own
``create_agent_card_signer``. Each side verifies the other's signature.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from a2a.client import ClientConfig, ClientFactory, create_client
from a2a.client.card_resolver import A2ACardResolver
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
)
from a2a.utils.signing import InvalidSignaturesError, create_signature_verifier
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from nexura_agents.a2a import A2AClient, AgentCardSignatureError, verify_agent_card


def _pub(keys: dict[str, Path], name: str) -> Any:
    return load_pem_public_key(keys[name + ".pub"].read_bytes())


def _request(text: str) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(role=Role.ROLE_USER, message_id=uuid.uuid4().hex, parts=[Part(text=text)])
    )


# ------------------------------------------------- a2a-sdk client -> Nexura server


async def test_reference_resolver_parses_and_verifies_nexura_card(
    nexura_a2a: str, signing_keys: dict[str, Path]
) -> None:
    pem = signing_keys["nexura.pub"].read_bytes()
    verifier = create_signature_verifier(lambda kid, jku: pem, ["ES256"])
    async with httpx.AsyncClient() as http:
        card = await A2ACardResolver(http, nexura_a2a).get_agent_card(signature_verifier=verifier)
    assert card.name == "nexura_helper"
    (interface,) = card.supported_interfaces
    assert interface.protocol_binding == "JSONRPC" and interface.protocol_version == "1.0"
    assert interface.url == nexura_a2a + "/"
    assert len(card.signatures) == 1


async def test_reference_verifier_rejects_nexura_card_with_wrong_key(
    nexura_a2a: str, signing_keys: dict[str, Path]
) -> None:
    other = signing_keys["stranger.pub"].read_bytes()
    verifier = create_signature_verifier(lambda kid, jku: other, ["ES256"])
    async with httpx.AsyncClient() as http:
        with pytest.raises(InvalidSignaturesError):
            await A2ACardResolver(http, nexura_a2a).get_agent_card(signature_verifier=verifier)


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
async def test_reference_client_talks_to_nexura(nexura_a2a: str, streaming: bool) -> None:
    async with httpx.AsyncClient() as http:
        client = await create_client(
            nexura_a2a, client_config=ClientConfig(streaming=streaming, httpx_client=http)
        )
        events = [event async for event in client.send_message(_request("hello"))]
        final_states = []
        texts = []
        task_id = None
        for event in events:
            if event.HasField("task"):
                task_id = event.task.id
                final_states.append(event.task.status.state)
                texts += [p.text for a in event.task.artifacts for p in a.parts]
            elif event.HasField("status_update"):
                final_states.append(event.status_update.status.state)
            elif event.HasField("artifact_update"):
                texts += [p.text for p in event.artifact_update.artifact.parts]
        assert texts == ["fake-model answer"]
        assert final_states[-1] == 3  # TASK_STATE_COMPLETED
        assert task_id
        fetched = await client.get_task(GetTaskRequest(id=task_id))
        assert fetched.status.state == 3
        await client.close()


async def test_reference_v03_client_talks_to_nexura(nexura_a2a: str) -> None:
    """The SDK's v0.3 compatibility transport against Nexura's 0.3 method names."""
    legacy_card = AgentCard(
        name="nexura_helper",
        description="0.3 view",
        version="1",
        supported_interfaces=[
            AgentInterface(url=nexura_a2a + "/", protocol_binding="JSONRPC", protocol_version="0.3")
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    async with httpx.AsyncClient() as http:
        client = ClientFactory(ClientConfig(streaming=False, httpx_client=http)).create(legacy_card)
        assert type(client._transport).__name__ == "CompatJsonRpcTransport"  # type: ignore[attr-defined]
        events = [event async for event in client.send_message(_request("old client"))]
        task = events[-1].task
        assert task.status.state == 3
        assert [p.text for a in task.artifacts for p in a.parts] == ["fake-model answer"]
        await client.close()


# ------------------------------------------------- Nexura client -> a2a-sdk server


async def test_nexura_client_verifies_reference_signed_card_and_sends(
    official_a2a: str, signing_keys: dict[str, Path]
) -> None:
    client = A2AClient(official_a2a, card_keys={"official-key-1": _pub(signing_keys, "official")})
    task = await client.send("hi there")
    assert client.card_verified_kid == "official-key-1"
    assert task.state == "completed"
    assert task.text == "echo: hi there"
    events = [event async for event in client.stream("streamed")]
    assert events[-1].text == "echo: streamed"


async def test_nexura_client_refuses_reference_card_with_untrusted_key(
    official_a2a: str, signing_keys: dict[str, Path]
) -> None:
    client = A2AClient(official_a2a, card_keys={"official-key-1": _pub(signing_keys, "stranger")})
    with pytest.raises(AgentCardSignatureError):
        await client.send("never sent")
    assert client.card is None  # an unverified card is never cached


async def test_nexura_client_refuses_tampered_reference_card(
    official_a2a: str, signing_keys: dict[str, Path]
) -> None:
    async with httpx.AsyncClient() as http:
        card = (await http.get(official_a2a + "/.well-known/agent-card.json")).json()
    trusted = {"official-key-1": _pub(signing_keys, "official")}
    assert verify_agent_card(card, trusted) == "official-key-1"
    card["supportedInterfaces"][0]["url"] = "http://127.0.0.1:1/evil"
    with pytest.raises(AgentCardSignatureError):
        verify_agent_card(card, trusted)


async def test_nexura_v03_wire_against_reference_compat_endpoint(official_a2a: str) -> None:
    """Nexura's 0.3 fallback (chosen for a 0.3-only card) against the SDK's v0.3 compat."""
    client = A2AClient(official_a2a)
    client.card = {
        "name": "legacy-view",
        "url": official_a2a + "/",
        "preferredTransport": "JSONRPC",
        "protocolVersion": "0.3.0",
    }
    task = await client.send("old wire")
    assert client._endpoint is not None and client._endpoint[1] == "0.3"
    assert task.text == "echo: old wire"
