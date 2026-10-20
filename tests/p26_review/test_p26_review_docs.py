"""P26 review: docs/evals.md red-team section still says clients cannot choose the model and
documents only ``workflow_id``; with ``agent_id`` the client-saved spec model is used."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_evals_doc_red_team_section_covers_agent_id() -> None:
    text = (ROOT / "docs" / "evals.md").read_text(encoding="utf-8")
    section = text.split("### Red-team in Studio", 1)[1].split("\n## ", 1)[0]
    assert "agent_id" in section, "red-team doc omits the agent_id target and its model rule"
