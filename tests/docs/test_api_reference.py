"""``docs/api-reference.md`` is generated from docstrings and must stay current."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "gen_api_reference", ROOT / "scripts/gen_api_reference.py"
)
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_every_public_name_and_method_has_a_docstring() -> None:
    assert gen.missing_docstrings() == []


def test_api_reference_page_is_up_to_date() -> None:
    page = ROOT / "docs" / "api-reference.md"
    assert page.read_text(encoding="utf-8") == gen.render(), (
        "docs/api-reference.md is stale; run: uv run python scripts/gen_api_reference.py"
    )


@pytest.mark.parametrize("authenticating", [False, True])
def test_advertised_profiles_require_an_authenticating_provider(authenticating: bool) -> None:
    """An edition name alone must not advertise unavailable shared authentication."""
    from nexura_runtime.service.api.descriptors import get_capabilities

    app = SimpleNamespace(
        state=SimpleNamespace(
            profile_manager=SimpleNamespace(profile="local-owner"),
            auth_provider=SimpleNamespace(is_authenticating=authenticating),
        )
    )
    request = Request({"type": "http", "app": app})
    capabilities = asyncio.run(get_capabilities(request))
    expected = ["local-owner"]
    if authenticating:
        expected.append("authenticated-shared-service")
    assert capabilities.supported_profiles == expected


def test_public_catalog_and_reference_do_not_claim_closed_features() -> None:
    from nexura_runtime.release import get_matrix, get_module

    local = get_module("B24")
    assert local is not None and local.profile == "core"
    assert local.capabilities == ["local_owner", "workspace_ownership", "resource_scoping"]
    page = (ROOT / "docs/testing.md").read_text(encoding="utf-8")
    for entry in get_matrix():
        assert entry["profile"] != "auth"
        assert not any(part.endswith("blueprint") for part in Path(entry["report_path"]).parts)
        assert f"| {entry['code']} | {entry['name']} | {entry['status']} |" in page
    assert all(not section[0].startswith("nexura_enterprise") for section in gen.SECTIONS)
    # The shipped catalog is JSON data, without requiring any private planning files.
    catalog = ROOT / "src/nexura_runtime/release/capabilities.json"
    assert len(json.loads(catalog.read_text())) == 33
