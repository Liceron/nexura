"""Loader, validation and import-report tests (no model calls)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexura_runtime.integrations.langflow import (
    FlowFormatError,
    FlowTooLargeError,
    UnsupportedFlowError,
    load,
)

FIX = Path(__file__).parent / "fixtures"


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def test_minimal_flow_report() -> None:
    imported = load(fx("f1_minimal_chat"))
    assert [n.class_name for n in imported.report.supported] == ["ChatInput", "Agent", "ChatOutput"]
    assert imported.report.unsupported == []
    assert imported.report.required_variables == ["OPENAI_API_KEY"]
    assert len(imported.edges) == 2
    # the code field is not retained
    assert all("code" not in n.fields for n in imported.nodes.values())


def test_accepts_path_text_and_dict(tmp_path: Path) -> None:
    path = FIX / "f1_minimal_chat.json"
    a, b, c = load(path), load(str(path)), load(path.read_text(encoding="utf-8"))
    assert a.name == b.name == c.name == "minimal chat"


def test_envelopes_agree() -> None:
    db, bare, data_only = (
        load(fx("f8_db_export")),
        load(fx("f1_minimal_chat")),
        load(fx("f8_data_only")),
    )
    assert len(db.nodes) == len(bare.nodes) == len(data_only.nodes) == 3
    assert data_only.name == "flow"


def test_component_file_rejected() -> None:
    with pytest.raises(FlowFormatError, match="custom component"):
        load({"is_component": True, "data": {}})


def test_notes_skipped_and_reported() -> None:
    report = load(fx("f3_chained_agents")).report
    assert [n.text for n in report.notes] == ["Two agents in a chain."]
    assert "note-n1" not in {n.id for n in report.supported}


def test_handle_forms_all_parse() -> None:
    imported = load(fx("f5_handle_variants"))
    assert [(e.source, e.target_field) for e in imported.edges] == [
        ("ChatInput-a1", "input_value"),
        ("Agent-b2", "input_value"),
        ("Agent-c3", "input_value"),
    ]


def test_parsed_handle_preferred_over_string() -> None:
    doc = fx("f1_minimal_chat")
    edge = doc["data"]["edges"][0]
    edge["sourceHandle"] = "garbage"  # string is wrong, parsed copy is right
    assert load(doc).edges[0].source_output == "message"


def test_bad_handles_report_every_problem() -> None:
    with pytest.raises(FlowFormatError) as err:
        load(fx("f6_bad_handles"))
    assert len(err.value.problems) >= 2


def test_dangling_edge_and_duplicate_id() -> None:
    with pytest.raises(FlowFormatError, match="Ghost-99"):
        load(fx("f6_dangling_edge"))
    with pytest.raises(FlowFormatError, match="duplicate node id"):
        load(fx("f6_duplicate_id"))


def test_cycle_that_is_not_a_loop_is_rejected() -> None:
    with pytest.raises(FlowFormatError, match="cycle that is not a Loop"):
        load(fx("f6_cycle"))


def test_loop_is_unsupported_not_a_cycle_error() -> None:
    with pytest.raises(UnsupportedFlowError, match="LoopComponent"):
        load(fx("f6_loop"))
    report = load(fx("f6_loop"), strict=False).report
    assert [u.class_name for u in report.unsupported] == ["LoopComponent"]


def test_unsupported_node_in_main_path_fails_by_default() -> None:
    with pytest.raises(UnsupportedFlowError) as err:
        load(fx("f6_unsupported_file"))
    assert "File-f1" in str(err.value)
    assert err.value.report is not None and err.value.report.unsupported[0].in_main_path


def test_unsupported_node_non_strict_is_reported() -> None:
    imported = load(fx("f6_unsupported_file"), strict=False)
    (bad,) = imported.report.unsupported
    assert (bad.id, bad.class_name) == ("File-f1", "File")
    assert "file" in bad.reason
    assert imported.report.unsupported_edges


def test_unsupported_node_off_path_is_only_a_warning() -> None:
    imported = load(fx("f6_unsupported_offpath"))
    assert imported.report.unsupported[0].in_main_path is False
    assert any("off the main path" in w for w in imported.report.warnings)


def test_missing_type_is_unsupported() -> None:
    imported = load(fx("f6_missing_type"), strict=False)
    assert imported.report.unsupported[0].reason == "node has no data.type"


def test_edited_code_node_is_unsupported() -> None:
    with pytest.raises(UnsupportedFlowError, match="code was edited"):
        load(fx("f6_code_edited"))
    imported = load(fx("f6_code_edited"), strict=False)
    assert imported.report.unsupported[0].id == "Agent-b2"


def test_code_without_hash_warns() -> None:
    doc = fx("f1_minimal_chat")
    for n in doc["data"]["nodes"]:
        n["data"]["node"]["metadata"] = {}
    warnings = load(doc).report.warnings
    assert any("cannot be verified" in w for w in warnings)


def test_literal_secret_not_copied() -> None:
    doc = fx("f1_minimal_chat")
    key = doc["data"]["nodes"][1]["data"]["node"]["template"]["api_key"]
    key["load_from_db"] = False
    key["value"] = "sk-literal-secret"
    imported = load(doc)
    assert imported.nodes["Agent-b2"].fields["api_key"] == ""
    assert any("literal secret" in w for w in imported.report.warnings)
    assert "sk-literal-secret" not in json.dumps(imported.report.to_dict())


@pytest.mark.parametrize("bad", ["not json at all", "[1, 2]", "{broken", ""])
def test_non_json_rejected(bad: str, tmp_path: Path) -> None:
    with pytest.raises(FlowFormatError):
        load(bad)


def test_non_utf8_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_bytes(b"\xff\xfe\x00{")
    with pytest.raises(FlowFormatError, match="UTF-8"):
        load(path)


def test_oversized_rejected(tmp_path: Path) -> None:
    doc = fx("f1_minimal_chat")
    doc["description"] = "x" * 2000
    path = tmp_path / "big.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(FlowTooLargeError):
        load(path, max_bytes=1000)
    with pytest.raises(FlowTooLargeError):
        load(doc, max_bytes=1000)
    with pytest.raises(FlowTooLargeError):
        load(json.dumps(doc), max_bytes=1000)
    with pytest.raises(FlowTooLargeError, match="too many"):
        load(fx("f3_chained_agents"), max_nodes=2)


def test_deeply_nested_json_rejected() -> None:
    with pytest.raises(FlowFormatError):
        load("[" * 100000 + "]" * 100000 if False else '{"a":' * 50000 + "1" + "}" * 50000)


def test_report_to_dict_is_json() -> None:
    json.dumps(load(fx("f2_tools")).report.to_dict())
