# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Signed A2A Agent Cards (A2A 1.0, section 8.4): JWS (RFC 7515) over the JCS (RFC 8785)
canonical form of the card.

    signed = sign_agent_card(card, private_key, kid="key-1")          # serving side
    kid = verify_agent_card(signed, {"key-1": public_key})            # calling side

Canonical payload: the card without ``signatures``, with empty strings, lists and
objects removed (protobuf default-value omission, as the official ``a2a-sdk`` does), then
serialized with RFC 8785. The signature is detached: ``protected`` and ``signature`` are
the first and third parts of the compact JWS, whose payload is that canonical text.

Keys come only from the caller (a trust store keyed by ``kid``); ``jku`` URLs in a card
are never fetched. Algorithms: ES256, ES384, RS256, PS256 and EdDSA (Ed25519).
``cryptography`` must be installed (it is a core dependency of ``nexura-runtime``).

Written from the A2A 1.0 specification; checked against ``a2a-sdk`` 1.2.3 in
``experiments/interop``. No code from that package is used.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from nexura_agents.a2a.protocol import A2AError

DEFAULT_ALGORITHMS = ("ES256", "ES384", "RS256", "PS256", "EdDSA")
_MAX_DEPTH = 64
INVALID_CARD_SIGNATURE = -32006  # reported as an invalid agent response


class AgentCardSignatureError(A2AError):
    """The agent card is unsigned, or none of its signatures verifies with a trusted key."""

    def __init__(self, message: str) -> None:
        super().__init__(INVALID_CARD_SIGNATURE, message)


# ------------------------------------------------------------------ RFC 8785


def _jcs_number(value: int | float) -> str:
    if isinstance(value, bool):
        raise TypeError("bool is not a number")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("JSON has no NaN or Infinity.")
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    # ECMAScript Number::toString: shortest round-trip digits, exponent form outside
    # [1e-6, 1e21).
    text = repr(value)
    if "e" in text or "E" in text:
        mantissa, exp = text.lower().split("e")
        exponent = int(exp)
        mantissa = mantissa.rstrip("0").rstrip(".") if "." in mantissa else mantissa
        if -7 < exponent < 21:
            return format(Decimal(repr(value)), "f")
        return f"{mantissa}e{'+' if exponent > 0 else '-'}{abs(exponent)}"
    return text


def _jcs_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif code < 0x20:
            out.append(
                {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}.get(
                    ch, f"\\u{code:04x}"
                )
            )
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def canonicalize(value: Any, _depth: int = 0) -> str:
    """RFC 8785 JSON Canonicalization Scheme text of a JSON value."""
    if _depth > _MAX_DEPTH:
        raise ValueError("JSON nesting is too deep to canonicalize.")
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonicalize(v, _depth + 1) for v in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("JSON object keys must be strings.")
        # Sorted by UTF-16 code units, as RFC 8785 section 3.2.3 requires.
        keys = sorted(value, key=lambda k: k.encode("utf-16-be"))
        return (
            "{"
            + ",".join(_jcs_string(k) + ":" + canonicalize(value[k], _depth + 1) for k in keys)
            + "}"
        )
    raise TypeError(f"{type(value).__name__} is not a JSON value.")


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {k: c for k, v in value.items() if (c := _drop_empty(v)) is not None}
        return cleaned or None
    if isinstance(value, list):
        items = [c for v in value if (c := _drop_empty(v)) is not None]
        return items or None
    if value == "" and isinstance(value, str):
        return None
    return value


def canonical_card_payload(card: Mapping[str, Any]) -> bytes:
    """The bytes an Agent Card signature covers."""
    body = {k: v for k, v in card.items() if k != "signatures"}
    return canonicalize(_drop_empty(body) or {}).encode("utf-8")


# ------------------------------------------------------------------ JWS


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError("expected a base64url string")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _crypto() -> Any:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa, utils
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise A2AError(
            -32603,
            "Agent card signing needs the 'cryptography' package "
            "(pip install 'nexura-agents[signing]').",
        ) from exc
    return hashes, ec, ed25519, padding, rsa, utils


def _alg_for(key: Any) -> str:
    _, ec, ed25519, _, rsa, _ = _crypto()
    if isinstance(key, (ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey)):
        return "ES384" if key.curve.name == "secp384r1" else "ES256"
    if isinstance(key, (ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey)):
        return "EdDSA"
    if isinstance(key, (rsa.RSAPrivateKey, rsa.RSAPublicKey)):
        return "RS256"
    raise A2AError(-32602, f"Unsupported signing key type {type(key).__name__}.")


def _sign(alg: str, key: Any, data: bytes) -> bytes:
    hashes, ec, ed25519, padding, rsa, utils = _crypto()
    if alg in ("ES256", "ES384"):
        digest = hashes.SHA256() if alg == "ES256" else hashes.SHA384()
        size = 32 if alg == "ES256" else 48
        der = key.sign(data, ec.ECDSA(digest))
        r, s = utils.decode_dss_signature(der)
        return bytes(r.to_bytes(size, "big") + s.to_bytes(size, "big"))
    if alg == "EdDSA":
        return bytes(key.sign(data))
    if alg == "RS256":
        return bytes(key.sign(data, padding.PKCS1v15(), hashes.SHA256()))
    if alg == "PS256":
        pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
        return bytes(key.sign(data, pss, hashes.SHA256()))
    raise A2AError(-32602, f"Unsupported JWS algorithm {alg!r}.")


def _verify(alg: str, key: Any, data: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature

    hashes, ec, ed25519, padding, rsa, utils = _crypto()
    try:
        if alg in ("ES256", "ES384"):
            size = 32 if alg == "ES256" else 48
            curve = "secp256r1" if alg == "ES256" else "secp384r1"
            if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != curve:
                return False
            if len(signature) != 2 * size:
                return False
            der = utils.encode_dss_signature(
                int.from_bytes(signature[:size], "big"), int.from_bytes(signature[size:], "big")
            )
            digest = hashes.SHA256() if alg == "ES256" else hashes.SHA384()
            key.verify(der, data, ec.ECDSA(digest))
        elif alg == "EdDSA":
            if not isinstance(key, ed25519.Ed25519PublicKey):
                return False
            key.verify(signature, data)
        elif alg in ("RS256", "PS256"):
            if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
                return False
            pad = (
                padding.PKCS1v15()
                if alg == "RS256"
                else padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
            )
            key.verify(signature, data, pad, hashes.SHA256())
        else:
            return False
    except InvalidSignature:
        return False
    return True


def public_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    """A ``cryptography`` public key from a JWK (EC P-256/P-384, RSA or OKP Ed25519)."""
    _, ec, ed25519, _, rsa, _ = _crypto()

    def num(name: str) -> int:
        return int.from_bytes(_b64url_decode(str(jwk[name])), "big")

    kty = jwk.get("kty")
    try:
        if kty == "EC":
            curves = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1()}
            curve = curves.get(str(jwk.get("crv")))
            if curve is None:
                raise A2AError(-32602, f"Unsupported EC curve {jwk.get('crv')!r}.")
            return ec.EllipticCurvePublicNumbers(num("x"), num("y"), curve).public_key()
        if kty == "RSA":
            return rsa.RSAPublicNumbers(num("e"), num("n")).public_key()
        if kty == "OKP" and jwk.get("crv") == "Ed25519":
            return ed25519.Ed25519PublicKey.from_public_bytes(_b64url_decode(str(jwk["x"])))
    except (KeyError, ValueError, binascii.Error) as exc:
        raise A2AError(-32602, f"Invalid JWK: {exc}") from None
    raise A2AError(-32602, f"Unsupported JWK key type {kty!r}.")


def sign_agent_card(
    card: Mapping[str, Any],
    private_key: Any,
    *,
    kid: str,
    alg: str | None = None,
    jku: str | None = None,
) -> dict[str, Any]:
    """A copy of ``card`` with one more entry in ``signatures``.

    ``private_key`` is a ``cryptography`` private key (EC P-256/P-384, Ed25519 or RSA).
    """
    if not isinstance(kid, str) or not kid:
        raise A2AError(-32602, "A signing key id (kid) is required.")
    algorithm = alg or _alg_for(private_key)
    header: dict[str, Any] = {"alg": algorithm, "typ": "JOSE", "kid": kid}
    if jku:
        header["jku"] = jku
    protected = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{protected}.{_b64url(canonical_card_payload(card))}".encode("ascii")
    signature = _b64url(_sign(algorithm, private_key, signing_input))
    out = dict(card)
    out["signatures"] = [
        *[s for s in card.get("signatures") or [] if isinstance(s, dict)],
        {"protected": protected, "signature": signature},
    ]
    return out


def verify_agent_card(
    card: Mapping[str, Any],
    trusted_keys: Mapping[str, Any],
    *,
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
) -> str:
    """Check that at least one signature verifies with a trusted key; return its ``kid``.

    ``trusted_keys`` maps key ids to ``cryptography`` public keys or JWK dicts. A
    signature whose ``kid`` is not in the map, whose ``alg`` is not in ``algorithms`` or
    which carries ``crit`` header parameters is skipped. Raises
    ``AgentCardSignatureError`` when the card is unsigned or nothing verifies.
    """
    signatures = card.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        raise AgentCardSignatureError("The agent card is not signed.")
    payload = _b64url(canonical_card_payload(card))
    for entry in signatures:
        if not isinstance(entry, dict):
            continue
        try:
            protected = entry["protected"]
            header = json.loads(_b64url_decode(protected))
            signature = _b64url_decode(entry["signature"])
        except (KeyError, ValueError, TypeError, binascii.Error):
            continue
        if not isinstance(header, dict) or "crit" in header:
            continue
        kid, alg = header.get("kid"), header.get("alg")
        if not isinstance(kid, str) or kid not in trusted_keys or alg not in algorithms:
            continue
        key = trusted_keys[kid]
        if isinstance(key, Mapping):
            key = public_key_from_jwk(key)
        if _verify(str(alg), key, f"{protected}.{payload}".encode("ascii"), signature):
            return kid
    raise AgentCardSignatureError("No signature on the agent card verifies with a trusted key.")
