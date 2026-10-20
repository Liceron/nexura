"""P28 step 5: organisations and the hosted secrets features are not in the open-source edition.

Organisations (``/api/v1/organisations``: members, invites, org workspaces, org audit), the
``/api/v1/secrets`` admin routes and master-key rotation moved to the closed edition
(EDITIONS_PLAN 2026-10-18 step 5). The open-source service must not serve any of it and no
open-source module may import it. The local encrypted secrets file stays (founder decision
2026-10-18); ``tests/test_p28s5_oss_credentials.py`` checks that it still works.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from nexura_runtime.secrets import EncryptedSecretStore, generate_master_key
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.builtin_extensions import BUILTIN_EXTENSION_NAMES

ROOT = Path(__file__).resolve().parents[1]

# Every route the organisations extension and the secrets router served, with a request body
# that would have reached it.
MOVED_REQUESTS: list[tuple[str, str, Any]] = [
    ("GET", "/api/v1/organisations", None),
    ("POST", "/api/v1/organisations", {"org_id": "acme", "name": "Acme"}),
    ("POST", "/api/v1/organisations/invites/accept", {"token": "nxi_0123456789abcdef"}),
    ("GET", "/api/v1/organisations/acme", None),
    ("GET", "/api/v1/organisations/acme/members", None),
    ("PATCH", "/api/v1/organisations/acme/members/bob", {"role": "admin"}),
    ("DELETE", "/api/v1/organisations/acme/members/bob", None),
    ("GET", "/api/v1/organisations/acme/invites", None),
    ("POST", "/api/v1/organisations/acme/invites", {"role": "member"}),
    ("DELETE", "/api/v1/organisations/acme/invites/inv_1", None),
    ("POST", "/api/v1/organisations/acme/workspaces", {"workspace_id": "w1", "name": "W1"}),
    ("GET", "/api/v1/organisations/acme/audit", None),
    ("GET", "/api/v1/secrets", None),
    ("POST", "/api/v1/secrets", {"name": "k", "value": "value-value-value-value"}),
    ("GET", "/api/v1/secrets/k", None),
    ("PUT", "/api/v1/secrets/k", {"value": "value-value-value-value"}),
    ("DELETE", "/api/v1/secrets/k", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    MOVED_REQUESTS,
    ids=[f"{m} {p}" for m, p, _ in MOVED_REQUESTS],
)
async def test_oss_service_does_not_serve_organisations_or_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any
) -> None:
    # With a master key the local encrypted store is installed; the routes are still absent.
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    app = create_api_app(db_path=str(tmp_path / "oss.sqlite3"), profile="local-owner")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.request(method, path, json=body)
    if method in ("GET", "POST"):
        assert response.status_code == 404, (method, path, response.status_code)
    else:
        assert response.status_code in (404, 405), (method, path, response.status_code)


def test_oss_app_has_no_organisation_state_and_keeps_the_local_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    app = create_api_app(db_path=str(tmp_path / "oss.sqlite3"))
    assert not hasattr(app.state, "org_repo")
    assert frozenset() == BUILTIN_EXTENSION_NAMES
    assert "nexura.organisations" not in app.state.extensions.configured
    assert "nexura.secrets" not in app.state.extensions.configured
    # The open-source local encrypted secrets file is still installed by create_api_app.
    assert isinstance(app.state.secret_store, EncryptedSecretStore)
    assert app.state.credential_store._backend is app.state.secret_store
    paths = set(app.openapi()["paths"])
    assert not any(p.startswith(("/api/v1/organisations", "/api/v1/secrets")) for p in paths)
    assert "/api/v1/credentials" in paths


def test_rotation_is_not_in_the_oss_store() -> None:
    assert not hasattr(EncryptedSecretStore, "rotate_master_key")


def test_organisation_models_are_not_in_the_oss_package() -> None:
    from nexura_runtime.service import workspaces
    from nexura_runtime.service.workspaces import models

    for name in ("OrgRole", "Organisation", "OrgMember", "OrgInvite", "OrgAuditEvent"):
        assert not hasattr(models, name), name
    for name in ("OrganisationRepository", "OrganisationError", "ORG_ROLE_TO_WORKSPACE_ROLE"):
        assert not hasattr(workspaces, name), name


@pytest.mark.parametrize(
    "module",
    [
        "nexura_runtime.secrets.api",
        "nexura_runtime.service.api.organisations",
        "nexura_runtime.service.workspaces.organisations",
    ],
)
def test_moved_modules_are_not_in_the_oss_package(module: str) -> None:
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:
        spec = None
    assert spec is None, module


_MOVED_IMPORT = re.compile(
    r"nexura_runtime\.secrets\.api|nexura_runtime\.secrets import api"
    r"|nexura_runtime\.service\.api\.organisations"
    r"|nexura_runtime\.service\.api import organisations"
    r"|nexura_runtime\.service\.workspaces\.organisations|features/organisation/"
    r"|OrganisationRepository|OrganisationsExtension|mount_organisations|rotate_master_key"
)


@pytest.mark.parametrize(
    ("base", "suffixes"),
    [
        ("src", (".py",)),
        ("packages", (".py",)),
        ("clients/typescript/src", (".ts", ".tsx", ".js", ".mjs")),
        ("studio-web/src", (".ts", ".tsx", ".js", ".jsx")),
    ],
)
def test_no_oss_source_references_a_moved_module(base: str, suffixes: tuple[str, ...]) -> None:
    hits = []
    for path in sorted((ROOT / base).rglob("*")):
        if path.suffix not in suffixes or not path.is_file():
            continue
        if {"node_modules", "dist", "__pycache__", ".venv"} & set(path.parts):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _MOVED_IMPORT.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not hits, hits


def test_moved_files_are_gone_from_the_oss_tree() -> None:
    moved = [
        "src/nexura_runtime/secrets/api.py",
        "src/nexura_runtime/service/api/organisations.py",
        "src/nexura_runtime/service/workspaces/organisations.py",
        "studio-web/src/features/organisation",
        "studio-web/tests/e2e/organisation.spec.ts",
        "docs/organisations-and-secrets.md",
        "tests/test_orgs_and_secrets.py",
    ]
    present = [p for p in moved if (ROOT / p).exists()]
    assert not present, present
    # The local encrypted secrets file and its guide stay in the open-source edition.
    for kept in ("__init__.py", "store.py", "keys.py", "errors.py"):
        assert (ROOT / "src" / "nexura_runtime" / "secrets" / kept).is_file(), kept
    assert (ROOT / "docs" / "local-secrets.md").is_file()


def test_studio_has_no_organisation_strings() -> None:
    for lang in ("en", "hi"):
        data = json.loads((ROOT / "studio-web" / "src" / "i18n" / f"{lang}.json").read_text())
        assert "organisation" not in data, lang
        assert "organisation" not in data["nav"], lang
