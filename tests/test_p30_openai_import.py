"""P30: OpenAI Evals JSONL -> Studio eval dataset (converter, CLI and the Studio route).

Offline. Fixtures under ``tests/fixtures/openai_evals/`` are synthetic. The CLI's ``--url``
upload runs against an in-process app; without ``--url`` nothing opens a connection.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexura_runtime.cli.main import main
from nexura_runtime.evaluation import studio_engine as engine
from nexura_runtime.evaluation.openai_import import (
    IMPORT_TAG,
    MAX_IMPORT_BYTES,
    OpenAIImportError,
    convert_openai_evals,
    dataset_to_cases,
)
from nexura_runtime.service.api.app import create_api_app

# ``nexura_runtime.cli.main`` the attribute is the function; this is the module.
cli_main = importlib.import_module("nexura_runtime.cli.main")
FIXTURES = Path(__file__).parent / "fixtures" / "openai_evals"
# Built at runtime so no secret-shaped literal sits in a fixture file.
FAKE_KEY = "sk-" + "fixture" + "0123456789abcdef"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _criteria() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_read("tickets_eval.json"))
    return data


# ------------------------------------------------------------------ converter: shapes


def test_documented_item_rows_convert_and_validate() -> None:
    result = convert_openai_evals(_read("tickets.jsonl"), "tickets", input_field="ticket_text")
    cases = result.dataset["cases"]
    assert [c["name"] for c in cases] == ["item-0001", "item-0002", "item-0003"]
    assert cases[0]["input"] == "My monitor won't turn on!"
    # No criteria: expected comes from the documented candidate list (correct_label).
    assert result.expected_field == "correct_label"
    assert [c["expected_output"] for c in cases] == ["Hardware", "Software", "Other"]
    assert all(c["tags"] == [IMPORT_TAG] for c in cases)
    assert cases[1]["metadata"] == {"openai_line": 2}
    assert result.dataset["format"] == engine.DATASET_FORMAT
    # The document is exactly what the Studio PUT validates.
    validated, _, redactions = engine.validate_dataset("tickets", result.dataset)
    assert validated == cases and redactions == 0


def test_input_field_is_required_when_not_detectable() -> None:
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(_read("tickets.jsonl"), "tickets")
    assert err.value.code == "INPUT_FIELD_REQUIRED"
    assert "ticket_text" in err.value.message  # lists the fields it saw


def test_item_prefix_and_template_names_are_accepted() -> None:
    for spelling in ("item.ticket_text", "{{ item.ticket_text }}"):
        result = convert_openai_evals(_read("tickets.jsonl"), "t", input_field=spelling)
        assert result.input_field == "ticket_text"


def test_registry_rows_with_chat_messages_and_several_ideals() -> None:
    result = convert_openai_evals(_read("registry_samples.jsonl"), "registry")
    first, second, third = result.dataset["cases"]
    assert result.input_field == "input" and result.expected_field == "ideal"
    assert first["input"] == "What colour is the sky on a clear day?"
    assert first["metadata"]["openai_input_messages"][0]["role"] == "system"
    # A lone user message is the input itself; nothing extra is stored.
    assert "openai_input_messages" not in second["metadata"]
    assert second["expected_output"] == "4"
    assert second["metadata"]["openai_acceptable_outputs"] == ["4", "four"]
    assert third == {**third, "input": "Capital of France?", "expected_output": "Paris"}
    text = " ".join(result.warnings)
    assert "chat messages" in text and "several acceptable answers" in text


def test_inline_run_source_ignores_samples_and_says_so() -> None:
    result = convert_openai_evals(_read("run_content.json"), "news")
    assert len(result.dataset["cases"]) == 2
    assert result.expected_field == "ground_truth"
    assert result.samples_ignored == 1
    assert any("sample" in w for w in result.warnings)
    assert all("sample" not in json.dumps(c["metadata"]) for c in result.dataset["cases"])


def test_json_array_of_rows_is_accepted() -> None:
    rows = [{"item": {"question": "a?", "answer": "A"}}, {"item": {"question": "b?"}}]
    result = convert_openai_evals(json.dumps(rows), "arr")
    assert [c["input"] for c in result.dataset["cases"]] == ["a?", "b?"]
    # A missing expected field renders as empty text on OpenAI, so it is "" (not an "n/a"
    # pass) and the line is named in a warning.
    assert result.dataset["cases"][1]["expected_output"] == ""
    assert any("line 2" in w and "'answer'" in w for w in result.warnings)


def test_a_nexura_export_is_pointed_at_the_dataset_import() -> None:
    doc = {"format": engine.DATASET_FORMAT, "cases": [{"name": "a", "input": "x"}]}
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(json.dumps(doc), "d")
    assert err.value.code == "ALREADY_NEXURA_DATASET"


# ------------------------------------------------------------------ converter: custom fields


def test_custom_field_names_and_nested_paths() -> None:
    result = convert_openai_evals(
        _read("custom_fields.jsonl"),
        "refunds",
        input_field="q",
        expected_field="item.gold",
        name_field="case_id",
    )
    cases = result.dataset["cases"]
    assert [c["name"] for c in cases] == ["refund-1", "refund-2"]
    assert cases[0]["expected_output"] == "No, returns close after 30 days."
    # Unused fields are kept, not dropped.
    assert cases[0]["metadata"]["openai_item"] == {"meta": {"locale": "en-IN"}}
    nested = convert_openai_evals(_read("custom_fields.jsonl"), "n", input_field="meta.locale")
    assert nested.dataset["cases"][0]["input"] == "en-IN"


def test_duplicate_or_missing_name_field_is_a_line_error() -> None:
    text = (
        "\n".join(json.dumps({"item": {"id": "same", "input": f"q{i}"}}) for i in range(2))
        + "\n"
        + json.dumps({"item": {"input": "q3"}})
    )
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(text, "d", name_field="id")
    fields = {fe["field"]: fe["error"] for fe in err.value.field_errors}
    assert "duplicate case name 'same'" in fields["line 2"]
    assert "missing name field" in fields["line 3"]


# ------------------------------------------------------------------ converter: malformed lines


def test_malformed_lines_are_reported_with_line_numbers() -> None:
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(_read("malformed.jsonl"), "bad")
    assert err.value.status_code == 422 and err.value.code == "OPENAI_IMPORT_INVALID"
    by_line = {fe["field"]: fe["error"] for fe in err.value.field_errors}
    assert set(by_line) == {"line 2", "line 3", "line 4"}
    assert by_line["line 2"].startswith("not valid JSON")
    assert "expected a JSON object" in by_line["line 3"]
    assert "missing input field 'question'" in by_line["line 4"]


def test_skip_invalid_converts_the_rest_and_lists_the_skipped_lines() -> None:
    result = convert_openai_evals(_read("malformed.jsonl"), "bad", skip_invalid=True)
    assert [c["name"] for c in result.dataset["cases"]] == ["item-0001", "item-0005"]
    assert [s["line"] for s in result.skipped_lines] == [2, 3, 4]
    assert result.report()["skipped_lines"] == result.skipped_lines


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ({"item": "text"}, "'item' must be an object"),
        ({"item": {"input": "x"}, "extra": 1}, "unexpected keys next to 'item'"),
        ({"item": {"input": 3}}, "input must be text"),
        ({"item": {"input": [{"role": "system", "content": "s"}]}}, "no user message"),
        (
            {"item": {"input": [{"role": "user", "content": [{"type": "input_image"}]}]}},
            "images and files are not converted",
        ),
    ],
)
def test_bad_rows_are_explained(row: dict[str, Any], fragment: str) -> None:
    good = json.dumps({"item": {"input": "fine"}})
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(good + "\n" + json.dumps(row), "d")
    assert err.value.field_errors[0]["field"] == "line 2"
    assert fragment in err.value.field_errors[0]["error"]


def test_engine_validation_errors_are_mapped_back_to_lines() -> None:
    text = (
        json.dumps({"item": {"input": "ok"}})
        + "\n\n"
        + json.dumps({"item": {"input": "x" * (engine.MAX_TEXT_CHARS + 1)}})
    )
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(text, "d")
    assert err.value.code == "DATASET_VALIDATION_ERROR"
    assert err.value.field_errors[0]["field"] == "line 3 (input)"


def test_empty_file_and_bad_id() -> None:
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals("\n\n", "d")
    assert err.value.code == "EMPTY_DATASET"
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(_read("tickets.jsonl"), "bad id!", input_field="ticket_text")
    assert err.value.code == "INVALID_DATASET_ID"


# ------------------------------------------------------------------ converter: caps


def test_too_many_items_is_refused_not_truncated() -> None:
    rows = "\n".join(json.dumps({"item": {"input": f"q{i}"}}) for i in range(engine.MAX_CASES + 1))
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(rows, "big")
    assert err.value.status_code == 413 and err.value.code == "DATASET_TOO_LARGE"


def test_raw_file_size_cap() -> None:
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(" " * (MAX_IMPORT_BYTES + 1), "big")
    assert err.value.status_code == 413 and err.value.code == "IMPORT_TOO_LARGE"


def test_converted_dataset_size_cap_applies() -> None:
    # 120 cases of ~7.9k chars each is far over the 512 KiB dataset cap.
    rows = "\n".join(json.dumps({"item": {"input": f"{i} " + "y" * 7900}}) for i in range(120))
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(rows, "big")
    assert err.value.status_code == 413 and err.value.code == "DATASET_TOO_LARGE"


# ------------------------------------------------------------------ converter: secrets


def test_secrets_are_redacted_like_any_saved_dataset() -> None:
    text = "\n".join(
        [
            json.dumps({"item": {"input": f"my key is {FAKE_KEY}", "answer": "rotate it"}}),
            json.dumps({"item": {"input": "plain", "answer": "x", "api_key": "hunter2-value"}}),
        ]
    )
    result = convert_openai_evals(text, "secrets")
    dumped = json.dumps(result.dataset)
    assert FAKE_KEY not in dumped and "hunter2-value" not in dumped
    assert "[REDACTED]" in result.dataset["cases"][0]["input"]
    assert result.redactions == 2 and result.report()["redactions"] == 2


# ------------------------------------------------------------------ converter: criteria


def test_criteria_map_where_a_scorer_corresponds_and_the_rest_are_listed() -> None:
    result = convert_openai_evals(
        _read("tickets.jsonl"), "tickets", input_field="ticket_text", testing_criteria=_criteria()
    )
    assert result.expected_field == "correct_label"  # from the eq criterion's reference
    assert result.scorers == [{"type": "exact_match"}, {"type": "regex", "pattern": "(?i)ware"}]
    unmapped = {u["name"]: u["reason"] for u in result.unmapped_criteria}
    assert set(unmapped) == {"Not the placeholder", "Close to label", "Helpfulness", "Custom code"}
    assert "differs" in unmapped["Not the placeholder"]
    assert "fuzzy_match" in unmapped["Close to label"]
    assert "judge model" in unmapped["Helpfulness"]
    assert "never runs uploaded code" in unmapped["Custom code"]
    eq = next(m for m in result.mapped_criteria if m["scorer"]["type"] == "exact_match")
    assert "case-sensitive" in eq["note"]  # the difference is stated, not hidden
    # Every mapped scorer is accepted by the Studio run engine.
    assert len(engine.build_scorers(result.scorers, judge_model=None)) == 2


@pytest.mark.parametrize(
    ("criterion", "expected"),
    [
        ({"operation": "like", "reference": "{{ item.correct_label }}"}, {"type": "contains"}),
        ({"operation": "ilike", "reference": "{{item.correct_label}}"}, {"type": "contains"}),
        (
            {"operation": "eq", "reference": "Hardware"},
            {"type": "regex", "pattern": r"\AHardware\Z"},
        ),
        ({"operation": "like", "reference": "a.b"}, {"type": "regex", "pattern": r"a\.b"}),
    ],
)
def test_string_check_operations(criterion: dict[str, Any], expected: dict[str, Any]) -> None:
    c = {"type": "string_check", "name": "c", "input": "{{ sample.output_text }}", **criterion}
    result = convert_openai_evals(
        _read("tickets.jsonl"), "t", input_field="ticket_text", testing_criteria=[c]
    )
    assert result.scorers == [expected] and result.unmapped_criteria == []


def test_criteria_that_do_not_fit_are_not_guessed() -> None:
    criteria = [
        {
            "type": "string_check",
            "name": "tool name",
            "input": "{{ sample.output_tools[0].function.name }}",
            "operation": "eq",
            "reference": "lookup",
        },
        {
            "type": "string_check",
            "name": "other field",
            "input": "{{ sample.output_text }}",
            "operation": "eq",
            "reference": "{{ item.ticket_text }}",
        },
        {"type": "multi", "name": "m"},
        {"type": "label_model", "name": "l"},
        {"type": "something_new", "name": "x"},
    ]
    result = convert_openai_evals(
        _read("tickets.jsonl"),
        "t",
        input_field="ticket_text",
        expected_field="correct_label",
        testing_criteria=criteria,
    )
    assert result.scorers == []
    reasons = [u["reason"] for u in result.unmapped_criteria]
    assert "sample.output_text" in reasons[0]
    assert "item.ticket_text" in reasons[1] and "item.correct_label" in reasons[1]
    assert len(reasons) == 5


def test_invalid_criteria_shape() -> None:
    with pytest.raises(OpenAIImportError) as err:
        convert_openai_evals(
            _read("tickets.jsonl"), "t", input_field="ticket_text", testing_criteria="x"
        )
    assert err.value.code == "INVALID_CRITERIA"


def test_dataset_to_cases_runs_with_the_sdk() -> None:
    from nexura_agents import Agent, FakeModel, evaluate
    from nexura_agents.evals import exact_match

    result = convert_openai_evals(_read("tickets.jsonl"), "t", input_field="ticket_text")
    labels = {"My monitor won't turn on!": "hardware", "I'm in vim and I can't quit!": "Software"}
    agent = Agent(
        name="labeller",
        instructions="Label the ticket.",
        model=FakeModel(handler=lambda messages, tools: labels.get(messages[-1].content, "?")),
    )
    report = evaluate(agent, dataset_to_cases(result.dataset), [exact_match()])
    assert [c.passed for c in report.cases] == [True, True, False]


# ------------------------------------------------------------------ CLI


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the CLI opened an HTTP client without --url")

    monkeypatch.setattr(cli_main, "_http_client", refuse)


def test_cli_writes_dataset_and_report_offline(
    tmp_path: Path, no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    out, report = tmp_path / "ds.json", tmp_path / "report.json"
    code = main(
        [
            "evals",
            "import-openai",
            str(FIXTURES / "tickets.jsonl"),
            "--dataset-id",
            "tickets",
            "--input-field",
            "ticket_text",
            "--criteria",
            str(FIXTURES / "tickets_eval.json"),
            "--out",
            str(out),
            "--report",
            str(report),
        ]
    )
    assert code == 0
    doc = json.loads(out.read_text())
    assert doc["format"] == engine.DATASET_FORMAT and len(doc["cases"]) == 3
    rep = json.loads(report.read_text())
    assert rep["scorers"][0] == {"type": "exact_match"} and len(rep["unmapped_criteria"]) == 4
    err = capsys.readouterr().err
    assert "Converted 3 item(s)" in err and "not mapped: Custom code" in err


def test_cli_prints_to_stdout_without_out(
    no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["evals", "import-openai", str(FIXTURES / "registry_samples.jsonl"), "--dataset-id", "r"]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["dataset_id"] == "r"


def test_cli_reports_line_errors_and_exits_1(
    no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["evals", "import-openai", str(FIXTURES / "malformed.jsonl"), "--dataset-id", "b"])
    assert code == 1
    err = capsys.readouterr().err
    assert "line 2: not valid JSON" in err and "line 4: missing input field" in err
    code = main(
        [
            "evals",
            "import-openai",
            str(FIXTURES / "malformed.jsonl"),
            "--dataset-id",
            "b",
            "--skip-invalid",
        ]
    )
    assert code == 0
    assert "skipped line 3" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--criteria", "/nonexistent/criteria.json"],
        ["--url", "ftp://example.test"],
    ],
)
def test_cli_usage_errors_exit_2(no_network: None, extra: list[str]) -> None:
    args = ["evals", "import-openai", str(FIXTURES / "registry_samples.jsonl"), "--dataset-id", "r"]
    assert main(args + extra) == 2
    assert main(["evals", "import-openai", "/nonexistent.jsonl", "--dataset-id", "r"]) == 2


def test_cli_refuses_to_send_a_key_over_plain_http(
    no_network: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEXURA_API_KEY", "nxk_fixture_not_real")
    args = ["evals", "import-openai", str(FIXTURES / "registry_samples.jsonl"), "--dataset-id", "r"]
    assert main(args + ["--url", "http://nexura.example.test"]) == 2
    assert "plain http" in capsys.readouterr().err


def _in_process(app: Any, seen: list[dict[str, str]]) -> Any:
    def factory(base_url: str, headers: dict[str, str]) -> Any:
        seen.append(dict(headers))
        return TestClient(app, base_url=base_url, headers=headers)

    return factory


def test_cli_url_puts_the_dataset_to_an_in_process_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_api_app(db_path=str(tmp_path / "s.db"), profile="local-owner")
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(cli_main, "_http_client", _in_process(app, seen))
    monkeypatch.setenv("NEXURA_API_KEY", "nxk_fixture_not_real")
    code = main(
        [
            "evals",
            "import-openai",
            str(FIXTURES / "custom_fields.jsonl"),
            "--dataset-id",
            "refunds",
            "--input-field",
            "q",
            "--expected-field",
            "gold",
            "--name-field",
            "case_id",
            "--url",
            "http://127.0.0.1:8765",
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert seen[0]["X-API-Key"] == "nxk_fixture_not_real"
    assert "Saved dataset 'refunds' revision 1" in capsys.readouterr().err
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        stored = client.get("/api/v1/evaluations/studio/datasets/refunds").json()
    assert [c["name"] for c in stored["cases"]] == ["refund-1", "refund-2"]
    assert stored["cases"][0]["expected_output"] == "No, returns close after 30 days."


def test_cli_url_reports_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_api_app(db_path=str(tmp_path / "s.db"), profile="local-owner")
    monkeypatch.setattr(cli_main, "_http_client", _in_process(app, []))
    monkeypatch.delenv("NEXURA_API_KEY", raising=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        first = client.put(
            "/api/v1/evaluations/studio/datasets/r", json={"cases": [{"name": "a", "input": "x"}]}
        )
        assert first.status_code == 200
    # A stale revision is refused by the service; the CLI says so and exits 2.
    original = cli_main._http_client

    class Stale:
        def __init__(self, base_url: str, headers: dict[str, str]) -> None:
            self.inner = original(base_url, headers)

        def __enter__(self) -> Any:
            client = self.inner.__enter__()
            put = client.put
            client.put = lambda path, json: put(path, json={**json, "expected_revision": 0})
            return client

        def __exit__(self, *exc: Any) -> None:
            self.inner.__exit__(*exc)

    monkeypatch.setattr(cli_main, "_http_client", Stale)
    args = ["evals", "import-openai", str(FIXTURES / "registry_samples.jsonl"), "--dataset-id", "r"]
    assert main(args + ["--url", "http://127.0.0.1:8765"]) == 2
    assert "REVISION_CONFLICT" in capsys.readouterr().err


# ------------------------------------------------------------------ Studio route


def test_import_route_converts_without_saving(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "s.db"), profile="local-owner")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        resp = client.post(
            "/api/v1/evaluations/studio/datasets/import-openai",
            json={
                "content": _read("tickets.jsonl"),
                "dataset_id": "tickets",
                "input_field": "ticket_text",
                "testing_criteria": _criteria(),
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dataset"]["format"] == engine.DATASET_FORMAT
        assert body["report"]["scorers"][0] == {"type": "exact_match"}
        assert client.get("/api/v1/evaluations/studio/datasets").json()["total"] == 0
        # The returned document saves unchanged through the existing import path.
        saved = client.put("/api/v1/evaluations/studio/datasets/tickets", json=body["dataset"])
        assert saved.status_code == 200 and saved.json()["case_count"] == 3


def test_import_route_returns_line_errors(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "s.db"), profile="local-owner")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        resp = client.post(
            "/api/v1/evaluations/studio/datasets/import-openai",
            json={"content": _read("malformed.jsonl"), "dataset_id": "bad"},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "OPENAI_IMPORT_INVALID"
    assert [fe["field"] for fe in detail["field_errors"]] == ["line 2", "line 3", "line 4"]


def test_local_import_ignores_viewer_header(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from nexura_runtime.service.api.app import create_api_app

    with TestClient(create_api_app(db_path=tmp_path / "import.db")) as c:
        r = c.post(
            "/api/v1/evaluations/studio/datasets/import-openai",
            headers={"X-Principal-ID": "forged", "X-Principal-Role": "viewer"},
            json={
                "content": _read("tickets.jsonl"),
                "dataset_id": "local",
                "input_field": "ticket_text",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["dataset"]["cases"]
