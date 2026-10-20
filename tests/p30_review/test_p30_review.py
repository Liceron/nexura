"""P30 review regressions (OpenAI Evals importer). Promoted from .autopilot/p30/repros.

Run: uv run --frozen python -m pytest -q tests/p30_review
Offline only: the agent is a labelled local FakeModel; the service is an in-process TestClient.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nexura_agents import Agent, FakeModel

from nexura_runtime.cli.main import main
from nexura_runtime.evaluation import studio_engine as engine
from nexura_runtime.evaluation.openai_import import OpenAIImportError, convert_openai_evals
from nexura_runtime.service.api.app import create_api_app

ROUTE = "/api/v1/evaluations/studio/datasets/import-openai"
OUTPUT_CHECK = "{{ sample.output_text }}"
# Built at runtime so no secret-shaped literal sits in the file.
SECRET = "sk-proj-" + "REVIEW" + "0123456789abcdefghijklmn"


def _line(item: dict[str, Any]) -> str:
    return json.dumps({"item": item})


def _fake_agent(reply: Any) -> Any:
    """Local fake model (labelled synthetic): replies ``reply(user_text)``."""

    def factory(_version: str | None = None) -> Agent:
        def handler(messages: Any, tools: Any) -> str:
            last = next((m.content for m in reversed(messages) if m.role == "user"), "")
            return str(reply(last))

        return Agent(
            name="local-fake-review", instructions="fake", model=FakeModel(handler=handler)
        )

    return factory


async def _run(cases: list[dict[str, Any]], scorers: list[dict[str, Any]], reply: Any) -> Any:
    return await engine.run_dataset(
        cases=cases,
        scorer_specs=scorers,
        target={"kind": "agent"},
        repeats=1,
        judge_model=None,
        agent_factory=_fake_agent(reply),
    )


# ------------------------------------------------------------------ file robustness


def test_utf8_bom_on_first_line_is_accepted() -> None:
    """Windows editors / Excel save JSONL with a BOM; line 1 must not be rejected."""
    text = "﻿" + _line({"input": "a", "expected": "b"}) + "\n" + _line({"input": "c"})
    result = convert_openai_evals(text, "bom")
    assert len(result.dataset["cases"]) == 2


def test_cli_reads_a_bom_file(tmp_path: Path) -> None:
    src = tmp_path / "bom.jsonl"
    src.write_text(
        _line({"input": "a"}) + "\n" + _line({"input": "b"}) + "\n", encoding="utf-8-sig"
    )
    out = tmp_path / "out.json"
    assert main(["evals", "import-openai", str(src), "--dataset-id", "bom", "--out", str(out)]) == 0


def test_raw_unicode_line_separator_inside_a_string_does_not_split_the_line() -> None:
    """JSON allows raw U+2028 / U+0085 in strings; str.splitlines() splits on them."""
    first = json.dumps(
        {"item": {"input": "para one para two", "expected": "x"}}, ensure_ascii=False
    )
    nel = json.dumps({"item": {"input": "a\u0085b", "expected": "y"}}, ensure_ascii=False)
    result = convert_openai_evals(first + "\n" + nel + "\n", "lsep")
    assert [c["input"] for c in result.dataset["cases"]] == ["para one para two", "a\u0085b"]


def test_deeply_nested_item_is_a_line_error_not_a_crash() -> None:
    deep = '{"item":{"input":"x","expected":' + "[" * 100_000 + "]" * 100_000 + "}}"
    with pytest.raises(OpenAIImportError):
        convert_openai_evals(deep + "\n" + _line({"input": "y"}), "deep")


def test_import_route_answers_4xx_for_deep_nesting(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "s.db"), profile="local-owner")
    deep = '{"item":{"input":"x","expected":' + "[" * 100_000 + "]" * 100_000 + "}}"
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as c:
        resp = c.post(ROUTE, json={"content": deep, "dataset_id": "deep"})
    assert 400 <= resp.status_code < 500, resp.status_code


def test_import_route_answers_4xx_for_a_lone_surrogate_escape(tmp_path: Path) -> None:
    """``"\\ud800"`` is valid JSON text but not encodable UTF-8; the route returns 500."""
    app = create_api_app(db_path=str(tmp_path / "s.db"), profile="local-owner")
    content = '{"item":{"input":"hi \\ud800 there","expected":"y"}}'
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as c:
        resp = c.post(ROUTE, json={"content": content, "dataset_id": "sur"})
    assert 400 <= resp.status_code < 500, resp.status_code


def test_cli_lone_surrogate_escape_exits_1_not_traceback(tmp_path: Path) -> None:
    src = tmp_path / "sur.jsonl"
    src.write_text('{"item":{"input":"hi \\ud800 there","expected":"y"}}\n', encoding="utf-8")
    out = tmp_path / "out.json"
    # Today: UnicodeEncodeError traceback from write_text (or sys.stdout.write without --out).
    assert main(["evals", "import-openai", str(src), "--dataset-id", "sur", "--out", str(out)]) == 1


# ------------------------------------------------------------------ criteria mapping


async def test_eq_against_a_numeric_item_field_scores_like_openai() -> None:
    """OpenAI renders ``{{ item.answer }}`` into the reference string, so output "4" passes
    ``eq``. The import maps it to exact_match with expected_output 4 (an int), which
    exact_match compares structurally against the text output and fails."""
    criteria = [
        {
            "type": "string_check",
            "name": "eq",
            "input": OUTPUT_CHECK,
            "operation": "eq",
            "reference": "{{ item.answer }}",
        }
    ]
    result = convert_openai_evals(
        _line({"question": "2+2?", "answer": 4}), "num", testing_criteria=criteria
    )
    assert result.scorers == [{"type": "exact_match"}]
    report = await _run(result.dataset["cases"], result.scorers, lambda _q: "4")
    assert [c.passed for c in report.cases] == [True]


def test_items_missing_the_expected_field_are_reported() -> None:
    """A case without expected_output scores 1.0 "n/a" under exact_match, so an item that
    simply lacks the field silently passes. The import must name the line."""
    text = _line({"input": "a", "expected": "A"}) + "\n" + _line({"input": "b"})
    result = convert_openai_evals(text, "miss")
    report = result.report()
    blob = json.dumps(report["warnings"] + report["skipped_lines"])
    assert "line 2" in blob or "1 item" in blob, report


async def test_items_missing_the_expected_field_do_not_pass_silently() -> None:
    text = _line({"input": "a", "expected": "A"}) + "\n" + _line({"input": "b"})
    result = convert_openai_evals(text, "miss")
    report = await _run(result.dataset["cases"], [{"type": "exact_match"}], lambda _q: "wrong")
    # OpenAI string_check against an empty / missing reference never passes "wrong".
    assert [c.passed for c in report.cases] == [False, False]


def test_long_literal_reference_maps_to_a_usable_scorer_or_is_unmapped() -> None:
    criteria = [
        {
            "type": "string_check",
            "name": "policy text",
            "input": OUTPUT_CHECK,
            "operation": "like",
            "reference": "Refunds are processed within five business days. " * 5,
        }
    ]
    result = convert_openai_evals(_line({"input": "q"}), "long", testing_criteria=criteria)
    if result.scorers:
        engine.build_scorers(result.scorers, None)  # raises INVALID_SCORER (pattern > 200)
    else:
        assert result.unmapped_criteria


async def test_ilike_literal_regex_is_the_same_check_on_long_outputs() -> None:
    """Report says "same check", but regex_match fails any output over 2048 chars."""
    criteria = [
        {
            "type": "string_check",
            "name": "mentions refund",
            "input": OUTPUT_CHECK,
            "operation": "ilike",
            "reference": "refund",
        }
    ]
    result = convert_openai_evals(_line({"input": "q"}), "ilike", testing_criteria=criteria)
    note = result.mapped_criteria[0]["note"] if result.mapped_criteria else ""
    long_reply = "x" * 3000 + " REFUND issued"
    report = await _run(result.dataset["cases"], result.scorers, lambda _q: long_reply)
    assert report.cases[0].passed or "same check" not in note


# ------------------------------------------------------------------ secrets


def test_duplicate_name_error_does_not_echo_a_secret(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "s.db"), profile="local-owner")
    content = _line({"input": "a", "id": SECRET}) + "\n" + _line({"input": "b", "id": SECRET})
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        resp = c.post(ROUTE, json={"content": content, "dataset_id": "d", "name_field": "id"})
    assert resp.status_code == 422
    assert SECRET not in resp.text


def test_report_does_not_carry_an_unredacted_secret_from_criteria() -> None:
    """Dataset is redacted, but a literal reference is copied verbatim into the regex
    scorer the report suggests sending to POST /runs (and the CLI prints it)."""
    criteria = [
        {
            "type": "string_check",
            "name": "no leak",
            "input": OUTPUT_CHECK,
            "operation": "eq",
            "reference": SECRET,
        }
    ]
    result = convert_openai_evals(_line({"input": "q"}), "sec", testing_criteria=criteria)
    # re.escape only inserts backslashes; undoing them recovers the key verbatim.
    unescaped = re.sub(r"\\(.)", r"\1", json.dumps(result.report()).replace("\\\\", "\\"))
    assert SECRET not in unescaped
