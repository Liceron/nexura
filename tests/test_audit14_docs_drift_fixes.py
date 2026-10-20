"""Audit 14 regressions, promoted from .autopilot/audit14/repros.

Audit 14 (docs drift): each test asserts the documented (or correct) behaviour; a failure
is a finding.

Offline only: local-owner profile, tmp databases, the built-in offline coordinator module.
Doc tests read the doc text and assert the statement that matches the code.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nexura_runtime.secrets import generate_master_key

os.environ.setdefault("NEXURA_MASTER_KEY", generate_master_key())

from nexura_agents.skills import Skill, SkillLibrary  # noqa: E402

from nexura_runtime.service.api.app import create_api_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1"
BASE = f"{API}/evaluations/studio"
REFUNDS_A = "---\nname: refunds\ndescription: How to handle refunds.\n---\nRefund within 30 days."
REFUNDS_B = "---\nname: refunds\ndescription: How to handle refunds.\n---\nRefund within 60 days."


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_api_app(db_path=str(tmp_path / "s.db"), profile="local-owner"))


# ------------------------------------------------------------------------- skills


def test_default_agent_module_reads_workspace_skills(tmp_path: Path) -> None:
    """docs/skills.md:132-136 says the service connects agent modules to the skills store at
    start and that "No agent module is registered by default". Actual: the default app lists
    ``nexura.agents.coordinator`` in GET /api/v1/modules with the ``skills`` config field
    (so Studio's inspector shows the skills picker on it), but ``mount_skills``
    (service/api/skills.py:143-146) binds only ``registry._handlers``, never the versioned
    default registry the node resolves through. A saved workflow using it with skills fails
    every run with "This node lists skills but no skill store is connected"."""
    c = _client(tmp_path)
    modules = c.get(f"{API}/modules").json()
    items = modules if isinstance(modules, list) else modules.get("modules") or []
    assert any((m.get("module_id") or m.get("id")) == "nexura.agents.coordinator" for m in items)
    made = c.post(f"{API}/skills", json={"markdown": REFUNDS_A, "label": "production"})
    assert made.status_code == 201, made.text
    spec = {
        "spec_version": 1,
        "name": "skilled",
        "entry_node": "ask",
        "nodes": {
            "ask": {
                "id": "ask",
                "module_id": "nexura.agents.coordinator",
                "transitions": {},
                "config": {"skills": ["refunds"]},
            }
        },
        "terminal_nodes": ["ask"],
    }
    saved = c.post(f"{API}/workflows", json={"workflow_id": "wf", "spec": spec})
    assert saved.status_code == 201, saved.text
    run: dict[str, Any] = c.post(
        f"{API}/runs", json={"workflow_id": "wf", "initial_state": {"prompt": "refund?"}}
    ).json()
    assert run["status"] == "completed", run.get("error") or run


def test_docs_do_not_claim_there_is_no_default_agent_module(tmp_path: Path) -> None:
    """docs/skills.md:135 "No agent module is registered by default" and
    studio-web/tests/e2e/README.md:24 "no agent module in the e2e backend" (the e2e harness is
    ``create_api_app(profile='local-owner')``). Both are false: that app lists
    ``nexura.agents.coordinator``, whose config schema has ``skills``."""
    c = _client(tmp_path)
    modules = c.get(f"{API}/modules").json()
    items = modules if isinstance(modules, list) else modules.get("modules") or []
    assert any((m.get("module_id") or m.get("id")) == "nexura.agents.coordinator" for m in items)
    assert "No agent module is registered" not in _read("docs/skills.md")
    assert "no agent module in the e2e backend" not in _read("studio-web/tests/e2e/README.md")


def test_service_does_not_reuse_version_numbers_after_a_skill_is_removed(tmp_path: Path) -> None:
    """docs/skills.md:99-105 says the service keeps the library's own export "so versions and
    labels behave exactly as above", and above: "version numbers are not reused". The SDK
    library numbers a re-created skill 4 after removing versions 1-3; the service deletes the
    row (skills_store.py:78-83) and numbers it 1 again, so ``?version=1`` now names different
    text than it did before."""
    lib = SkillLibrary()
    for md in (REFUNDS_A, REFUNDS_B, REFUNDS_A):
        lib.add(Skill.from_markdown(md))
    lib.remove("refunds")
    sdk_version = lib.add(Skill.from_markdown(REFUNDS_B)).version
    assert sdk_version == 4

    c = _client(tmp_path)
    for md in (REFUNDS_A, REFUNDS_B, REFUNDS_A):
        assert c.post(f"{API}/skills", json={"markdown": md}).status_code == 201
    assert c.delete(f"{API}/skills/refunds").json()["removed"] == 3
    again = c.post(f"{API}/skills", json={"markdown": REFUNDS_B}).json()
    assert again["version"] == sdk_version, again


def test_skills_doc_says_dedup_is_against_the_newest_version_only() -> None:
    """docs/skills.md:89-90 "Adding the same content again returns the existing version" (and
    :112 "identical text is not a new version"). Actual (skills.py:333): only content equal to
    the *newest* version is deduplicated; re-adding v1's text after v2 stores v3."""
    lib = SkillLibrary()
    lib.add(Skill.from_markdown(REFUNDS_A))
    lib.add(Skill.from_markdown(REFUNDS_B))
    assert lib.add(Skill.from_markdown(REFUNDS_A)).version == 3
    doc = _read("docs/skills.md")
    sentence = next(s for s in re.split(r"(?<=\.)\s+", doc) if "same content again" in s)
    assert "newest" in sentence or "latest" in sentence, sentence


def test_changelog_skills_entry_does_not_say_there_is_no_studio_page() -> None:
    """CHANGELOG.md:30 (Unreleased, Skills) "There is no Studio page yet." contradicts
    CHANGELOG.md:63 and studio-web/src/features/skills/SkillsView.tsx (P20b Skills page)."""
    assert (ROOT / "studio-web/src/features/skills/SkillsView.tsx").is_file()
    unreleased = _read("CHANGELOG.md").split("\n## ", 2)[1]
    assert "There is no Studio page yet" not in unreleased


# --------------------------------------------------------------- workflow tests


def test_evals_doc_states_the_expected_trajectory_limit(tmp_path: Path) -> None:
    """docs/evals.md:403-404 "Redaction keeps at most 100 items of any single list inside one
    case (a longer expected tool trajectory is cut)". Actual (studio_engine.py:137-146): an
    ``expected_trajectory`` over 50 steps is refused with 422 DATASET_VALIDATION_ERROR; it is
    never cut. (Other lists, e.g. ``expected_agent_path``, are cut at 100.)"""
    c = _client(tmp_path)
    r = c.put(
        f"{BASE}/datasets/d",
        json={"cases": [{"name": "a", "input": "x", "expected_trajectory": ["t"] * 60}]},
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "DATASET_VALIDATION_ERROR"
    doc = " ".join(_read("docs/evals.md").split())
    assert "a longer expected tool trajectory is cut" not in doc


def test_e2e_readme_does_not_mark_workflow_tests_spec_as_unrun() -> None:
    """studio-web/tests/e2e/README.md:25 says workflow_tests.spec.ts is "Written but not yet run
    in this change"; LOG.md (P22 DONE, P23 DONE) records browser 89/89 including it, and both
    of its tests pass in .gate-p23-e2e.log."""
    assert "Written but not yet run" not in _read("studio-web/tests/e2e/README.md")


# ----------------------------------------------------------------- README / reports


def _guide_links() -> set[str]:
    index = _read("docs/README.md")
    return {m for m in re.findall(r"\]\(([a-z0-9-]+\.md)\)", index) if m != "api-reference.md"}


def test_readme_guide_count_matches_the_docs_index() -> None:
    """README.md:13 and packages/nexura-agents/README.md:38 say "20 guides"; docs/README.md
    links 31 guides (skills.md among them) plus the API reference."""
    count = len(_guide_links())
    assert count != 20
    for rel in ("README.md", "packages/nexura-agents/README.md"):
        claimed = re.search(r"(\d+) guides", _read(rel))
        assert claimed is not None, rel
        assert int(claimed.group(1)) == count, f"{rel} claims {claimed.group(1)}, index has {count}"


def test_gap_update_does_not_say_sub_runs_tab_shows_on_every_run() -> None:
    """The public guide must match Studio's ``has_subworkflows`` gate for Sub-runs."""
    durable = _read("studio-web/src/features/runs/durable.ts")
    assert "has_subworkflows === true" in durable
    guide = _read("docs/studio-and-service.md")
    assert "Sub-runs tab shows on every run" not in guide
    assert "Sub-runs tab appears only for runs whose workflow has sub-workflow steps" in guide
