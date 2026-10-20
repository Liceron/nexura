"""B34: service-internal modules carry an explicit ``internal`` descriptor flag.

Studio's module palette hides remaining OSS service modules whose descriptor says
``internal: true`` (for example ``nexura.service.triggers``). The modules stay registered,
so existing saved workflows still validate. The flag does not change manifest digests.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from nexura_agents.contracts.manifest import ModuleManifest

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.validation import validate_workflow_graph
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.studio.store import StudioStore

INTERNAL_MODULES = {
    "nexura.service.triggers",
    "nexura.observability",
    "nexura.evaluation.runner",
    "nexura.runtime.approvals",
    # B37: hidden until the P27 stage 2 sandbox exists; still registered for saved workflows.
    "nexura.tools.sandbox.execute",
}

# Workflow building blocks that must stay in the palette.
OFFERED_MODULES = {
    "nexura.prompt.template",
    "nexura.data.transform",
    "nexura.channel.email",
    "nexura.channel.whatsapp",
    "nexura.connectors.slack",
    "nexura.connectors.postgres",
    "nexura.knowledge.retrieval",
    "nexura.models.provider",
    "nexura.http.request",
    "nexura.usage.budget",
}


@pytest.fixture
def app(tmp_path: Path) -> Any:
    db_file = tmp_path / "b34.sqlite3"
    return create_api_app(
        registry=get_default_registry(),
        store=StudioStore(db_file),
        db_path=db_file,
        profile="local-owner",
    )


async def _list(app: Any) -> dict[str, dict[str, Any]]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/v1/modules")
    assert res.status_code == 200
    return {item["module_id"]: item for item in res.json()["items"]}


@pytest.mark.asyncio
async def test_list_marks_service_internal_modules_and_keeps_them_listed(app: Any) -> None:
    items = await _list(app)
    # Still registered and listed: hiding is the builder's job, not unregistering.
    assert "nexura.service.auth" not in items
    assert "nexura.service.workspaces" not in items
    assert set(items) >= INTERNAL_MODULES
    for module_id in INTERNAL_MODULES:
        assert items[module_id]["internal"] is True, module_id
    for module_id in OFFERED_MODULES:
        assert items[module_id]["internal"] is False, module_id
    # Every descriptor carries the flag, including legacy handlers.
    assert all(isinstance(item["internal"], bool) for item in items.values())
    assert items["synthetic_order_lookup"]["internal"] is False
    # The only internal modules are the ones listed in INTERNAL_MODULES.
    assert {mid for mid, item in items.items() if item["internal"]} == INTERNAL_MODULES


@pytest.mark.asyncio
async def test_palette_categories_are_not_empty_once_internal_modules_are_hidden(
    app: Any,
) -> None:
    items = await _list(app)
    offered = [item for item in items.values() if not item["internal"]]
    channels = [i for i in offered if i["module_id"].startswith("nexura.channel.")]
    connectors = [
        i
        for i in offered
        if i["kind"] == "integration" and (i.get("display") or {}).get("group") == "connectors"
    ]
    assert len(channels) >= 2
    assert len(connectors) == 15
    assert all(i["module_id"].startswith("nexura.connectors.") for i in connectors)
    # No module declares a "channel" kind: Studio must categorise channels by module id.
    assert not any("channel" in i["kind"] for i in items.values())


@pytest.mark.asyncio
async def test_single_descriptor_reports_flag(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        auth = await client.get("/api/v1/modules/nexura.service.triggers")
        prompt = await client.get("/api/v1/modules/nexura.prompt.template")
    assert auth.status_code == 200
    assert auth.json()["internal"] is True
    assert prompt.status_code == 200
    assert prompt.json()["internal"] is False


@pytest.mark.asyncio
async def test_saved_workflow_using_internal_module_still_validates(app: Any) -> None:
    spec = {
        "name": "ExistingWorkspaceLookup",
        "entry_node": "ws",
        "spec_version": 1,
        "nodes": {
            "ws": {
                "id": "ws",
                "handler": "nexura.service.triggers",
                "module_id": "nexura.service.triggers",
                "module_version": "1.0.0",
                "config": {},
                "input_mapping": {},
                "output_mapping": {},
                "transitions": {},
                "default_transition": None,
                "requires_approval": False,
            }
        },
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/workflows/validate", json={"spec": spec})
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is True, body["errors"]
    assert default_registry.has("nexura.service.triggers")


def test_internal_flag_defaults_false_and_does_not_change_digest() -> None:
    base = ModuleManifest(
        module_id="b34.example.module",
        version="1.0.0",
        kind="tool",
        title="Example",
        description="Example module.",
    )
    assert base.internal is False
    flagged = base.model_copy(update={"internal": True})
    assert flagged.internal is True
    assert flagged.canonical_hash() == base.canonical_hash()
    assert flagged.digest == base.digest
    # Other fields still change the digest.
    assert base.model_copy(update={"title": "Other"}).canonical_hash() != base.canonical_hash()


def test_registered_internal_manifests_carry_flag() -> None:
    for module_id in INTERNAL_MODULES:
        manifest = default_registry.get_entry(module_id).manifest
        assert manifest.internal is True, module_id
        unflagged = manifest.model_copy(update={"internal": False})
        assert unflagged.canonical_hash() == manifest.canonical_hash()


def test_registered_modules_require_only_declared_oss_extras() -> None:
    declared = set(importlib.metadata.metadata("nexura-runtime").get_all("Provides-Extra") or [])
    assert "auth" not in declared
    for descriptor in default_registry.list_modules():
        manifest = default_registry.get_entry(
            descriptor["module_id"], descriptor["version"]
        ).manifest
        assert manifest.dependency_extra is None or manifest.dependency_extra in declared, (
            manifest.module_id,
            manifest.dependency_extra,
        )


def test_trigger_module_is_available_and_validates_without_auth_extra() -> None:
    entry = default_registry.get_entry("nexura.service.triggers")
    assert entry.is_available, entry.missing_reason
    assert entry.get_instance().manifest.module_id == "nexura.service.triggers"
    spec = {
        "name": "Local trigger workflow",
        "entry_node": "trigger",
        "nodes": {
            "trigger": {
                "id": "trigger",
                "handler": "nexura.service.triggers",
                "module_id": "nexura.service.triggers",
                "module_version": "1.0.0",
            }
        },
        "terminal_nodes": ["trigger"],
    }
    report = validate_workflow_graph(spec, registry=get_default_registry())
    assert report.valid, report.errors
