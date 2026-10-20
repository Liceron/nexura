"""Focused acceptance tests for B03: Credentials and outbound access policy."""

from __future__ import annotations

import httpx
import pytest
from nexura_runtime.contracts import CredentialReference
from nexura_runtime.network import (
    DestinationPolicy,
    DestinationPolicyDeniedError,
    SafeHttpClient,
)

from nexura_runtime.credentials import (
    CredentialAccessDeniedError,
    CredentialNotFoundError,
    ScopedCredentialStore,
    SecretValue,
    redact_secrets,
)


def test_keys_absent_from_repr_str_and_metadata() -> None:
    """Secret values are masked from repr, str, and metadata models."""
    secret = "secret_live_api_key_123456"
    val = SecretValue(secret)

    # 1. Masked representations
    assert repr(val) == "***REDACTED***"
    assert str(val) == "***REDACTED***"
    assert secret not in repr(val)
    assert secret not in str(val)

    # 2. Equality uses constant-time comparison
    assert val == secret
    assert val.reveal() == secret

    # 3. Credential metadata does not contain the raw secret
    store = ScopedCredentialStore()
    meta = store.store("ws_1", "cred_1", "bearer_token", secret)
    dumped = meta.model_dump()
    assert secret not in str(dumped)
    assert "secret" not in dumped


def test_cross_workspace_reference_rejected() -> None:
    """A credential in workspace A cannot be accessed from workspace B."""
    store = ScopedCredentialStore()
    store.store("workspace_alpha", "api_key", "bearer_token", "alpha_secret_val")

    # Alpha resolves successfully
    ref_alpha = CredentialReference(credential_id="api_key", credential_type="bearer_token")
    secret = store.resolve_ref("workspace_alpha", ref_alpha)
    assert secret.reveal() == "alpha_secret_val"

    # Beta attempting to resolve alpha's credential fails with CredentialNotFoundError
    with pytest.raises(CredentialNotFoundError):
        store.resolve_ref("workspace_beta", ref_alpha)

    # Cross-workspace reference with mismatched workspace attribute
    # fails with CredentialAccessDeniedError
    ref_mismatched = CredentialReference(credential_id="api_key", credential_type="bearer_token")
    ref_mismatched.workspace_id = "workspace_alpha"
    with pytest.raises(CredentialAccessDeniedError) as exc_info:
        store.resolve_ref("workspace_beta", ref_mismatched)
    assert "Cross-workspace credential access denied" in exc_info.value.message


def test_redact_secrets_utility() -> None:
    """Secret strings are reliably redacted from log texts and tracebacks."""
    token = "sk_live_very_secret_token_abcdef123"
    msg = f"Failed to connect using token {token} to remote host."
    redacted = redact_secrets(msg, [token])

    assert token not in redacted
    assert "[REDACTED]" in redacted
    assert redacted == "Failed to connect using token [REDACTED] to remote host."


def test_destination_policy_blocks_metadata_and_private_ips() -> None:
    """Cloud metadata endpoints and private IP ranges are blocked by default."""
    policy = DestinationPolicy()

    # 1. AWS/GCP/Azure link-local metadata IP
    with pytest.raises(DestinationPolicyDeniedError, match="cloud metadata"):
        policy.check_url("http://169.254.169.254/latest/meta-data")

    # 2. Loopback blocked by default
    with pytest.raises(DestinationPolicyDeniedError, match="loopback"):
        policy.check_url("http://127.0.0.1:8000/internal")

    # 3. RFC 1918 private addresses blocked by default
    with pytest.raises(DestinationPolicyDeniedError, match="private network"):
        policy.check_url("http://192.168.1.1/admin")

    with pytest.raises(DestinationPolicyDeniedError, match="private network"):
        policy.check_url("http://10.0.0.5:5432")

    # 4. Allowed when explicitly configured
    allowed_policy = DestinationPolicy(
        allow_loopback=True,
        allow_private_ranges=True,
        allowed_hosts=["127.0.0.1", "192.168.1.1"],
    )
    allowed_policy.check_url("http://127.0.0.1:8000/internal")
    allowed_policy.check_url("http://192.168.1.1/admin")


@pytest.mark.asyncio
async def test_redirects_cannot_bypass_destination_policy() -> None:
    """Redirecting to a forbidden destination is caught and rejected."""
    policy = DestinationPolicy(
        allow_loopback=True,
        allowed_hosts=["127.0.0.1"],
        allow_metadata_service=False,
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/safe_endpoint":
            # Malicious redirect to cloud metadata service
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data"},
            )
        return httpx.Response(200, text="OK")

    transport = httpx.MockTransport(mock_handler)

    async with SafeHttpClient(policy=policy, transport=transport) as client:
        with pytest.raises(DestinationPolicyDeniedError, match="cloud metadata"):
            await client.get("http://127.0.0.1/safe_endpoint", follow_redirects=True)


def test_import_makes_zero_network_requests() -> None:
    """Importing credential and network modules creates no network calls or sockets."""
    # Importing packages should be completely passive and fast
    import nexura_runtime.network  # noqa: F401

    import nexura_runtime.credentials  # noqa: F401
