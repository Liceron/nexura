"""Signed A2A Agent Cards (A2A 1.0 section 8.4): RFC 8785 canonical form plus JWS.

The canonicalization example is the one in the specification
(https://a2a-protocol.org/latest/specification/, section 8.4.1, fetched 2026-10-21).
Cross-checks against the official a2a-sdk signer and verifier are in
experiments/interop/tests/test_a2a_interop.py.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from nexura_agents import Agent, FakeModel
from nexura_agents.a2a import A2AClient, A2AServer, AgentCardSignatureError
from nexura_agents.a2a.signing import (
    canonical_card_payload,
    canonicalize,
    sign_agent_card,
    verify_agent_card,
)
from nexura_agents.errors import SDKValidationError

SPEC_CARD = {
    "name": "Example Agent",
    "description": "An example agent that echoes messages.",
    "supportedInterfaces": [
        {
            "url": "https://example.com/a2a/v1",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ],
    "version": "1.0.0",
    "capabilities": {"streaming": False, "pushNotifications": False, "extensions": []},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {"id": "echo", "name": "Echo", "description": "Echoes the input.", "tags": ["echo"]}
    ],
}
SPEC_CANONICAL = (
    '{"capabilities":{"pushNotifications":false,"streaming":false},"defaultInputModes":'
    '["text/plain"],"defaultOutputModes":["text/plain"],"description":"An example agent '
    'that echoes messages.","name":"Example Agent","skills":[{"description":"Echoes the '
    'input.","id":"echo","name":"Echo","tags":["echo"]}],"supportedInterfaces":[{"protocol'
    'Binding":"JSONRPC","protocolVersion":"1.0","url":"https://example.com/a2a/v1"}],'
    '"version":"1.0.0"}'
)


def test_canonical_payload_matches_the_specification_example() -> None:
    signed_already = {**SPEC_CARD, "signatures": [{"protected": "x", "signature": "y"}]}
    assert canonical_card_payload(signed_already).decode() == SPEC_CANONICAL


def test_rfc_8785_ordering_numbers_and_escapes() -> None:
    # Property sorting by UTF-16 code units (RFC 8785 section 3.2.3 example keys).
    value = {"€": 1, "\r": 2, "דּ": 3, "1": 4, "\U0001f600": 5, "\u0080": 6, "ö": 7}
    keys = list(json.loads(canonicalize(value)))
    assert keys == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]
    assert canonicalize([1e21, 1e-7, 0.5, -0.0, 10.0]) == "[1e+21,1e-7,0.5,0,10]"
    assert canonicalize(' \x07"\\') == '" \\u0007\\"\\\\"'
    with pytest.raises(ValueError):
        canonicalize(float("nan"))


@pytest.mark.parametrize(
    "key",
    [
        ec.generate_private_key(ec.SECP256R1()),
        ec.generate_private_key(ec.SECP384R1()),
        ed25519.Ed25519PrivateKey.generate(),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    ],
    ids=["ES256", "ES384", "EdDSA", "RS256"],
)
def test_sign_and_verify_round_trip(key: Any) -> None:
    signed = sign_agent_card(SPEC_CARD, key, kid="k1", jku="https://example.com/jwks.json")
    header = json.loads(base64.urlsafe_b64decode(signed["signatures"][0]["protected"] + "=="))
    assert header["kid"] == "k1" and header["typ"] == "JOSE"
    assert header["jku"] == "https://example.com/jwks.json"
    assert verify_agent_card(signed, {"k1": key.public_key()}) == "k1"
    assert "signatures" not in SPEC_CARD  # the input card is not modified


def test_verification_failures() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    signed = sign_agent_card(SPEC_CARD, key, kid="k1")
    with pytest.raises(AgentCardSignatureError, match="not signed"):
        verify_agent_card(SPEC_CARD, {"k1": key.public_key()})
    with pytest.raises(AgentCardSignatureError):
        verify_agent_card(signed, {"k1": other.public_key()})  # wrong key
    with pytest.raises(AgentCardSignatureError):
        verify_agent_card(signed, {"k2": key.public_key()})  # unknown kid
    with pytest.raises(AgentCardSignatureError):
        verify_agent_card(signed, {"k1": key.public_key()}, algorithms=["RS256"])
    tampered = {**signed, "name": "Evil Agent"}
    with pytest.raises(AgentCardSignatureError):
        verify_agent_card(tampered, {"k1": key.public_key()})
    # A second signature with an untrusted key does not stop a trusted one verifying.
    rotated = sign_agent_card(signed, other, kid="old")
    assert verify_agent_card(rotated, {"k1": key.public_key()}) == "k1"


def test_jwk_public_keys_are_accepted() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()

    def b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()

    jwk = {"kty": "EC", "crv": "P-256", "x": b64(numbers.x), "y": b64(numbers.y)}
    assert verify_agent_card(sign_agent_card(SPEC_CARD, key, kid="j"), {"j": jwk}) == "j"


def _server(**kwargs: Any) -> A2AServer:
    agent = Agent(name="helper", instructions="", model=FakeModel(responses=["fake answer"] * 5))
    return A2AServer(agent, url="http://agent.test/", **kwargs)


async def test_served_card_is_signed_and_client_verifies_it() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    app = _server(signing_key=key, signing_kid="srv-1").http_app(token="t")
    transport = httpx.ASGITransport(app=app)
    good = A2AClient(
        "http://agent.test",
        headers={"Authorization": "Bearer t"},
        transport=transport,
        card_keys={"srv-1": key.public_key()},
    )
    card = await good.get_card()
    assert "securitySchemes" in card  # the bearer scheme is covered by the signature
    assert good.card_verified_kid == "srv-1"
    assert (await good.send("hi")).text == "fake answer"

    stranger = ec.generate_private_key(ec.SECP256R1()).public_key()
    bad = A2AClient("http://agent.test", transport=transport, card_keys={"srv-1": stranger})
    with pytest.raises(AgentCardSignatureError):
        await bad.send("hi")
    assert bad.card is None


async def test_client_with_keys_refuses_an_unsigned_card_and_without_keys_accepts_it() -> None:
    transport = httpx.ASGITransport(app=_server().http_app())
    strict = A2AClient(
        "http://agent.test",
        transport=transport,
        card_keys={"k": ec.generate_private_key(ec.SECP256R1()).public_key()},
    )
    with pytest.raises(AgentCardSignatureError, match="not signed"):
        await strict.get_card()
    lenient = A2AClient("http://agent.test", transport=transport)
    assert "signatures" not in await lenient.get_card()
    assert lenient.card_verified_kid is None


def test_signing_key_needs_a_kid() -> None:
    with pytest.raises(SDKValidationError):
        _server(signing_key=ec.generate_private_key(ec.SECP256R1()))
