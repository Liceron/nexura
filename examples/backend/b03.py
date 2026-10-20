"""Standalone executable example for B03: Credentials and outbound access policy.

Demonstrates:
- Resolving scoped CredentialReference without exposing raw secrets.
- Enforcing DestinationPolicy on outbound HTTP calls with MockTransport loopback fake.
- Preventing access to blocked metadata endpoints (169.254.169.254).
- Automatic redaction of secrets from logs and string representations.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from nexura_runtime.contracts import (
    BaseModule,
    CredentialReference,
    ExecutionContext,
    ModuleManifest,
    ModuleResult,
    PortDefinition,
)
from nexura_runtime.network import (
    DestinationPolicy,
    DestinationPolicyDeniedError,
    SafeHttpClient,
)

from nexura_runtime.credentials import (
    ScopedCredentialStore,
    redact_secrets,
)


class BankBalanceModule(BaseModule):
    """Fetches balance from an allowlisted bank endpoint using a credential reference."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self.transport = transport

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            schema_version=1,
            module_id="nexura.demo.bank_balance",
            version="1.0.0",
            kind="connector",
            title="Bank Balance Connector",
            description="Fetches account balance from banking service with token authentication.",
            effect_policy="read",
            required_credential_types=["bearer_token"],
            input_schema={
                "type": "object",
                "properties": {"account_id": {"type": "string"}},
                "required": ["account_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "currency": {"type": "string"},
                    "balance_paise": {"type": "integer"},
                },
                "required": ["account_id", "currency", "balance_paise"],
                "additionalProperties": False,
            },
            config_schema={"type": "object", "additionalProperties": False},
            named_ports=[
                PortDefinition(name="account_id", type="string"),
                PortDefinition(name="currency", type="string"),
                PortDefinition(name="balance_paise", type="integer"),
            ],
            capabilities={"network": True, "streaming": False},
        )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        resolver = context.credential_resolver
        if resolver is None:
            raise RuntimeError("CredentialResolver required in context")

        # 1. Resolve secret by reference
        ref = CredentialReference(
            credential_id="bank_api_token",
            credential_type="bearer_token",
        )
        token = resolver.resolve(ref)
        if not token:
            raise RuntimeError("Failed to resolve bank_api_token")

        # 2. Configure outbound destination policy
        policy = DestinationPolicy(
            allow_loopback=True,
            allowed_hosts=["127.0.0.1", "localhost"],
        )

        async with SafeHttpClient(policy=policy, transport=self.transport) as client:
            resp = await client.get(
                "http://127.0.0.1:8080/v1/balance",
                headers={"Authorization": f"Bearer {token}"},
                params={"account_id": inputs["account_id"]},
            )
            data = resp.json()

        return ModuleResult.success(outputs=data)


async def main() -> None:
    # 1. Initialize scoped credential store
    cred_store = ScopedCredentialStore()
    workspace_id = "ws_finance_in"
    secret_token = "secret_bank_bearer_token_xyz987"

    meta = cred_store.store(
        workspace_id=workspace_id,
        credential_id="bank_api_token",
        credential_type="bearer_token",
        secret=secret_token,
        description="RBI-compliant banking service token",
    )
    print(f"Stored credential metadata: {meta.credential_id} in {meta.workspace_id}")

    # 2. Set up loopback fake handler
    def mock_handler(request: httpx.Request) -> httpx.Response:
        auth_header = request.headers.get("Authorization")
        if auth_header != f"Bearer {secret_token}":
            return httpx.Response(401, json={"error": "Unauthorized"})
        return httpx.Response(
            200,
            json={"account_id": "ACC_MUMBAI_01", "currency": "INR", "balance_paise": 7500000},
        )

    transport = httpx.MockTransport(mock_handler)
    module = BankBalanceModule(transport=transport)

    # 3. Execute module in execution context
    ctx = ExecutionContext(
        workspace_id=workspace_id,
        principal_id="bank_operator",
        run_id="run_b03_demo",
        node_id="node_fetch_balance",
        credential_resolver=cred_store.for_workspace(workspace_id),
    )

    result = await module.execute(ctx, {"account_id": "ACC_MUMBAI_01"}, {})
    print(f"Module execution status: {result.status}")
    print(f"Balance result: {result.outputs}")

    assert result.status == "success"
    assert result.outputs["currency"] == "INR"
    assert result.outputs["balance_paise"] == 7500000

    # 4. Verify secret redaction
    log_message = f"Connecting to bank with token {secret_token} for user"
    redacted = redact_secrets(log_message, [secret_token])
    assert secret_token not in redacted
    assert "[REDACTED]" in redacted
    print(f"Redacted log output: {redacted}")

    # 5. Verify DestinationPolicy blocks metadata service
    default_policy = DestinationPolicy()
    try:
        default_policy.check_url("http://169.254.169.254/latest/meta-data/")
        raise AssertionError("Metadata check should have failed!")
    except DestinationPolicyDeniedError as e:
        print(f"Verified security block on metadata endpoint: {e.message}")

    print("B03 standalone example executed and verified successfully.")


if __name__ == "__main__":
    asyncio.run(main())
