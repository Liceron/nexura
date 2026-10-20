"""Tests for credentials and workspace membership API with strict role and secrecy enforcement."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from nexura_runtime.service.api.app import create_api_app


@pytest.mark.asyncio
async def test_credentials_lifecycle_and_secrecy(tmp_path) -> None:
    db_file = tmp_path / "test_cred.sqlite3"
    app = create_api_app(db_path=db_file, profile="local-owner")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        sentinel_secret = "sk_live_sentinel_xyz987654321"

        # 1. Register credential in workspace "default"
        create_res = await client.post(
            "/api/v1/credentials",
            json={
                "credential_id": "openai_prod",
                "credential_type": "api_key",
                "provider": "openai",
                "description": "Production OpenAI LLM key",
                "secret": sentinel_secret,
                "environment_variable": "OPENAI_API_KEY",
            },
            headers={"X-Workspace-ID": "default"},
        )
        assert create_res.status_code == 201
        created_data = create_res.json()
        assert created_data["credential_id"] == "openai_prod"
        assert created_data["provider"] == "openai"
        assert created_data["status"] == "active"
        # CRITICAL NEGATIVE TEST: Secret MUST NOT be in response payload
        assert "secret" not in created_data
        assert sentinel_secret not in str(create_res.text)

        # 2. List credentials
        list_res = await client.get(
            "/api/v1/credentials",
            headers={"X-Workspace-ID": "default"},
        )
        assert list_res.status_code == 200
        list_data = list_res.json()
        assert list_data["total"] == 1
        item = list_data["items"][0]
        assert item["credential_id"] == "openai_prod"
        assert "secret" not in item
        assert sentinel_secret not in str(list_res.text)

        # 3. Connection check / verify
        verify_res = await client.post(
            "/api/v1/credentials/openai_prod/verify",
            headers={"X-Workspace-ID": "default"},
        )
        assert verify_res.status_code == 200
        verify_data = verify_res.json()
        assert verify_data["status"] == "valid"
        assert "verified" in verify_data["message"].lower()

        # 4. Revoke credential
        revoke_res = await client.post(
            "/api/v1/credentials/openai_prod/revoke",
            headers={"X-Workspace-ID": "default"},
        )
        assert revoke_res.status_code == 200
        assert revoke_res.json()["status"] == "revoked"

        # 5. Subsequent verify fails closed as revoked
        verify_after_revoke = await client.post(
            "/api/v1/credentials/openai_prod/verify",
            headers={"X-Workspace-ID": "default"},
        )
        assert verify_after_revoke.status_code == 200
        assert verify_after_revoke.json()["status"] == "revoked"

        # 6. Delete credential
        del_res = await client.delete(
            "/api/v1/credentials/openai_prod",
            headers={"X-Workspace-ID": "default"},
        )
        assert del_res.status_code == 200
        assert del_res.json()["deleted"] is True

        # 7. Subsequent list is empty
        list_empty = await client.get(
            "/api/v1/credentials",
            headers={"X-Workspace-ID": "default"},
        )
        assert list_empty.json()["total"] == 0


@pytest.mark.asyncio
async def test_cross_workspace_credential_isolation(tmp_path) -> None:
    db_file = tmp_path / "test_cred_iso.sqlite3"
    app = create_api_app(db_path=db_file, profile="local-owner")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create workspace-a and workspace-b
        await client.post("/api/v1/workspaces", json={"workspace_id": "ws-alpha", "name": "Alpha"})
        await client.post("/api/v1/workspaces", json={"workspace_id": "ws-beta", "name": "Beta"})

        # Register secret in ws-alpha
        await client.post(
            "/api/v1/credentials",
            json={
                "credential_id": "alpha_secret",
                "credential_type": "bearer_token",
                "provider": "custom",
                "description": "Alpha vault item",
                "secret": "alpha_secret_value_123",
            },
            headers={"X-Workspace-ID": "ws-alpha"},
        )

        # Listing in ws-beta MUST return empty
        beta_list = await client.get(
            "/api/v1/credentials",
            headers={"X-Workspace-ID": "ws-beta"},
        )
        assert beta_list.status_code == 200
        assert beta_list.json()["total"] == 0

        # Direct access to alpha_secret from ws-beta MUST fail with 403 or 404
        beta_get = await client.get(
            "/api/v1/credentials/alpha_secret",
            headers={"X-Workspace-ID": "ws-beta"},
        )
        assert beta_get.status_code in (403, 404)

        # Direct verify from ws-beta MUST fail with 403 or return invalid
        beta_verify = await client.post(
            "/api/v1/credentials/alpha_secret/verify",
            headers={"X-Workspace-ID": "ws-beta"},
        )
        assert (
            beta_verify.status_code in (403, 404) or beta_verify.json().get("status") == "invalid"
        )


@pytest.mark.asyncio
async def test_local_owner_admin_actions_ignore_forged_identity_and_members_are_absent(
    tmp_path: Any,
) -> None:
    app = create_api_app(db_path=tmp_path / "roles.db")
    headers = {
        "X-Workspace-ID": "corp-team",
        "X-Principal-ID": "analyst",
        "X-Principal-Role": "viewer",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.post(
            "/api/v1/credentials",
            headers=headers,
            json={
                "credential_id": "local",
                "credential_type": "api_key",
                "secret": "local-secret-012345",
            },
        )
        assert r.status_code == 201, r.text
        assert (
            await c.post("/api/v1/credentials/local/verify", headers=headers)
        ).status_code == 200
        assert (
            await c.post("/api/v1/credentials/local/revoke", headers=headers)
        ).status_code == 200
        assert (await c.delete("/api/v1/credentials/local", headers=headers)).status_code in (
            200,
            204,
        )
        for suffix in ("members", "my-membership"):
            assert (
                await c.get(f"/api/v1/workspaces/corp-team/{suffix}", headers=headers)
            ).status_code == 404
        assert (
            await c.post(
                "/api/v1/workspaces/corp-team/members",
                headers=headers,
                json={"principal_id": "other", "role": "admin"},
            )
        ).status_code == 404
