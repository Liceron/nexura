"""V5: export an ImportedFlow back to Langflow JSON. Offline, no model calls."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.integrations.langflow import (
    FlowExportError,
    FlowFormatError,
    ImportedFlow,
    LangflowImportError,
    UnsupportedFlowError,
    export_flow,
    load,
)
from nexura_runtime.integrations.langflow.flow import FlowEdge

FIX = Path(__file__).parent / "fixtures"


def _loadable() -> list[str]:
    names = []
    for p in sorted(FIX.glob("*.json")):
        try:
            load(json.loads(p.read_text("utf-8")), strict=False, extended=True)
        except LangflowImportError:
            continue
        names.append(p.stem)
    return names


NAMES = _loadable()


def doc(name: str) -> dict[str, Any]:
    return json.loads((FIX / f"{name}.json").read_text("utf-8"))


def ld(d: dict[str, Any], **kw: Any) -> ImportedFlow:
    return load(copy.deepcopy(d), strict=False, extended=True, **kw)


def graph(d: dict[str, Any]) -> dict[str, Any]:
    return d["data"] if isinstance(d.get("data"), dict) else d


def raw_node(d: dict[str, Any], nid: str) -> dict[str, Any]:
    return next(n for n in graph(d)["nodes"] if n["id"] == nid)


def test_fixture_set_is_substantial() -> None:
    assert len(NAMES) >= 18


@pytest.mark.parametrize("name", NAMES)
def test_unedited_roundtrip_is_identical(name: str) -> None:
    original = doc(name)
    snapshot = copy.deepcopy(original)
    flow = ld(original)
    out = export_flow(flow, original=original)
    if name == "f7_tweaks":
        # The only fixture with a literal secret: the exporter blanks it (documented).
        assert raw_node(snapshot, "SQL-q1")["data"]["node"]["template"]["database_url"]["value"]
        raw_node(snapshot, "SQL-q1")["data"]["node"]["template"]["database_url"]["value"] = ""
    assert out == snapshot
    again = ld(out)
    if name == "f7_tweaks":  # only the "literal secret ignored" warning goes away
        assert (again.nodes, again.edges) == (flow.nodes, flow.edges)
        return
    assert again == flow


@pytest.mark.parametrize("name", NAMES)
def test_text_roundtrip(name: str) -> None:
    original = doc(name)
    out = json.loads(json.dumps(export_flow(ld(original), original=json.dumps(original))))
    assert ld(out).nodes == ld(original).nodes and ld(out).edges == ld(original).edges


def _first_with(flow: ImportedFlow, kind: str) -> Any:
    return next(
        n
        for n in flow.nodes.values()
        if n.kind == kind and n.id not in {u.id for u in flow.report.unsupported}
    )


def test_prompt_edit_lands_in_template_field() -> None:
    original = doc("f4_prompt_agent")
    flow = ld(original)
    node = _first_with(flow, "Prompt")
    node.fields["template"] = "Be brief: {question}"
    out = export_flow(flow, original=original)
    entry = raw_node(out, node.id)["data"]["node"]["template"]["template"]
    assert entry["value"] == "Be brief: {question}"
    # everything else untouched: code, siblings, positions
    before = copy.deepcopy(original)
    raw_node(before, node.id)["data"]["node"]["template"]["template"]["value"] = (
        "Be brief: {question}"
    )
    assert out == before
    assert ld(out).nodes[node.id].fields["template"] == "Be brief: {question}"


def test_model_name_edit_roundtrips() -> None:
    original = doc("f9_language_model_step")
    flow = ld(original)
    node = _first_with(flow, "LanguageModel")
    key = "model" if "model" in node.fields else "model_name"
    old = node.fields[key]
    new: Any = (
        dict(old[0] if isinstance(old, list) else old) if not isinstance(old, str) else "gpt-x"
    )
    if isinstance(new, dict):
        new["name"] = "gpt-edited"
        node.fields[key] = [new] if isinstance(old, list) else new
    else:
        node.fields[key] = new
    out = export_flow(flow, original=original)
    again = ld(out)
    assert again.nodes[node.id].fields[key] == node.fields[key]
    assert again.nodes[node.id].fields[key] != old


def test_removed_node_drops_its_edges_and_added_edge_has_both_encodings() -> None:
    original = doc("f1_minimal_chat")
    flow = ld(original)
    assert flow.edges
    e = flow.edges[-1]
    flow.edges.remove(e)
    out = export_flow(flow, original=original)
    assert len(graph(out)["edges"]) == len(graph(original)["edges"]) - 1
    # Re-add: both handle encodings, parseable by load.
    flow.edges.append(FlowEdge(e.source, e.source_output, e.target, e.target_field))
    out2 = export_flow(flow, original=original)
    new = graph(out2)["edges"][-1]
    assert "œ" in new["sourceHandle"] and '"' not in new["sourceHandle"]
    assert new["data"]["sourceHandle"]["name"] == e.source_output
    assert new["data"]["targetHandle"]["fieldName"] == e.target_field
    assert {x.target_field for x in ld(out2).edges} == {x.target_field for x in flow.edges}


def test_new_edge_roundtrips_through_load() -> None:
    original = doc("f1_minimal_chat")
    flow = ld(original)
    e = flow.edges[0]
    flow.edges.remove(e)
    base_out = export_flow(flow, original=original)
    flow.edges.append(e)
    out = export_flow(flow, original=original)
    assert {(x.source, x.source_output, x.target, x.target_field) for x in ld(out).edges} == {
        (x.source, x.source_output, x.target, x.target_field) for x in flow.edges
    }
    assert len(graph(out)["edges"]) == len(graph(base_out)["edges"]) + 1


def test_removed_node() -> None:
    original = doc("f2_tools")
    flow = ld(original)
    victim = next(n for n in flow.nodes.values() if n.kind in {"Calculator", "URL"})
    del flow.nodes[victim.id]
    flow.edges = [e for e in flow.edges if victim.id not in (e.source, e.target)]
    out = export_flow(flow, original=original)
    assert all(n["id"] != victim.id for n in graph(out)["nodes"])
    assert all(victim.id not in (e["source"], e["target"]) for e in graph(out)["edges"])
    assert ld(out).nodes.keys() == flow.nodes.keys()


def test_viewport_positions_and_code_preserved() -> None:
    original = doc("f1_minimal_chat")
    flow = ld(original)
    nid = next(iter(flow.nodes))
    flow.nodes[nid].display_name = "Renamed"
    out = export_flow(flow, original=original)
    assert raw_node(out, nid)["data"]["node"]["display_name"] == "Renamed"
    assert raw_node(out, nid)["position"] == raw_node(original, nid)["position"]
    assert (
        raw_node(out, nid)["data"]["node"]["template"]["code"]
        == (raw_node(original, nid)["data"]["node"]["template"]["code"])
    )


# -- negative cases ---------------------------------------------------------------------


def test_unsupported_node_unchanged_passes_through_edited_is_refused() -> None:
    original = doc("f6_code_edited")
    flow = ld(original)
    bad = next(u for u in flow.report.unsupported)
    assert export_flow(flow, original=original) == original
    node = flow.nodes[bad.id]
    node.fields["__edit__"] = "x"
    with pytest.raises(FlowExportError, match="unsupported"):
        export_flow(flow, original=original)


def test_unsupported_edge_endpoint_refused() -> None:
    original = doc("f6_code_edited")
    flow = ld(original)
    bad = next(iter(flow.report.unsupported)).id
    other = next(n for n in flow.nodes if n != bad)
    flow.edges.append(FlowEdge(bad, "x", other, "input_value"))
    with pytest.raises(FlowExportError, match="unsupported"):
        export_flow(flow, original=original)


def test_sdk_only_node_refused_by_default_and_without_code_when_allowed() -> None:
    original = doc("f1_minimal_chat")
    flow = ld(original)
    template = copy.deepcopy(next(iter(flow.nodes.values())))
    template.id = "ChatInput-new"
    flow.nodes[template.id] = template
    with pytest.raises(FlowExportError, match="without code|no component code|code"):
        export_flow(flow, original=original)
    out = export_flow(flow, original=original, allow_sdk_only=True)
    new = raw_node(out, "ChatInput-new")
    assert "code" not in new["data"]["node"]["template"]
    assert new["data"]["node"]["metadata"]["nexura_sdk_only"] is True
    assert "code_hash" not in new["data"]["node"]["metadata"]


def test_no_original_refuses_unless_allowed() -> None:
    flow = ld(doc("f1_minimal_chat"))
    with pytest.raises(FlowExportError, match="original"):
        export_flow(flow)
    out = export_flow(flow, allow_sdk_only=True)
    assert all("code" not in n["data"]["node"]["template"] for n in graph(out)["nodes"])
    assert len(graph(out)["edges"]) == len(flow.edges)


def test_edge_to_missing_field_or_output_is_refused() -> None:
    original = doc("f1_minimal_chat")
    flow = ld(original)
    e = flow.edges[0]
    flow.edges.remove(e)
    flow.edges.append(FlowEdge(e.source, e.source_output, e.target, "no_such_field"))
    with pytest.raises(FlowExportError, match="no template field"):
        export_flow(flow, original=original)
    flow.edges[-1] = FlowEdge(e.source, "no_such_output", e.target, e.target_field)
    with pytest.raises(FlowExportError, match="no output"):
        export_flow(flow, original=original)


def test_edge_to_unknown_node_is_refused() -> None:
    original = doc("f1_minimal_chat")
    flow = ld(original)
    flow.edges.append(FlowEdge("ghost", "message", next(iter(flow.nodes)), "input_value"))
    with pytest.raises(FlowExportError, match="not in the export"):
        export_flow(flow, original=original)


def test_new_template_field_on_existing_node_refused() -> None:
    original = doc("f1_minimal_chat")
    flow = ld(original)
    next(iter(flow.nodes.values())).fields["invented"] = "x"
    with pytest.raises(FlowExportError, match="invents|invent"):
        export_flow(flow, original=original)


def test_class_change_refused() -> None:
    original = doc("f1_minimal_chat")
    flow = ld(original)
    next(iter(flow.nodes.values())).class_name = "Agent"
    with pytest.raises(FlowExportError, match="class"):
        export_flow(flow, original=original)


def _with_secret(literal: str | None, variable: str | None) -> dict[str, Any]:
    d = doc("f9_language_model_step")
    for n in graph(d)["nodes"]:
        t = n["data"]["node"]["template"]
        if "api_key" in t:
            if variable:
                t["api_key"]["value"], t["api_key"]["load_from_db"] = variable, True
            else:
                t["api_key"]["value"] = literal
            t["api_key"]["_input_type"] = "SecretStrInput"
            return d
    t = next(n for n in graph(d)["nodes"] if n["data"]["type"] != "ChatInput")["data"]["node"][
        "template"
    ]
    t["api_key"] = {
        "_input_type": "SecretStrInput",
        "type": "str",
        "show": True,
        "value": variable or literal,
        "load_from_db": bool(variable),
    }
    return d


def _secret_nid(d: dict[str, Any]) -> str:
    return next(n["id"] for n in graph(d)["nodes"] if "api_key" in n["data"]["node"]["template"])


def test_load_from_db_reference_preserved() -> None:
    d = _with_secret(None, "MY_OPENAI_KEY")
    out = export_flow(ld(d), original=d)
    entry = raw_node(out, _secret_nid(d))["data"]["node"]["template"]["api_key"]
    assert entry["value"] == "MY_OPENAI_KEY" and entry["load_from_db"] is True
    assert out == d


def test_literal_secret_not_reinserted_and_blanked() -> None:
    secret = "sk-live-VERYSECRET123"
    d = _with_secret(secret, None)
    flow = ld(d)
    out = export_flow(flow, original=d)
    assert secret not in json.dumps(out)
    assert raw_node(out, _secret_nid(d))["data"]["node"]["template"]["api_key"]["value"] == ""


def test_editing_a_literal_secret_is_refused_and_variable_is_allowed() -> None:
    d = _with_secret(None, "MY_OPENAI_KEY")
    nid = _secret_nid(d)
    flow = ld(d)
    flow.nodes[nid].from_db.discard("api_key")
    flow.nodes[nid].fields["api_key"] = "sk-new-LITERAL"
    with pytest.raises(FlowExportError, match="literal secret") as ei:
        export_flow(flow, original=d)
    assert "sk-new-LITERAL" not in str(ei.value)
    flow2 = ld(d)
    flow2.nodes[nid].fields["api_key"] = "OTHER_VAR"
    out = export_flow(flow2, original=d)
    entry = raw_node(out, nid)["data"]["node"]["template"]["api_key"]
    assert entry["value"] == "OTHER_VAR" and entry["load_from_db"] is True


def test_sdk_only_secret_not_written() -> None:
    d = _with_secret("sk-live-VERYSECRET123", None)
    out = export_flow(ld(d), allow_sdk_only=True)
    assert "VERYSECRET" not in json.dumps(out)


def test_wrong_input_type() -> None:
    with pytest.raises(TypeError):
        export_flow({"data": {}})  # type: ignore[arg-type]


def test_errors_are_importer_errors() -> None:
    assert issubclass(FlowExportError, LangflowImportError)
    assert FlowFormatError and UnsupportedFlowError


def _node(d: dict[str, Any], nid: str) -> dict[str, Any]:
    return next(n for n in d["data"]["nodes"] if n["id"] == nid)


def test_unsupported_passthrough_node_secret_blanked() -> None:
    d = doc("f6_unsupported_file")
    _node(d, "File-f1")["data"]["node"]["template"]["api_key"] = {
        "type": "str",
        "value": "sk-LITERAL-SECRET",
        "password": True,
    }
    for extended in (False, True):
        out = export_flow(load(copy.deepcopy(d), strict=False, extended=extended), original=d)
        assert "sk-LITERAL-SECRET" not in json.dumps(out)


def test_secret_blanked_with_and_without_extended() -> None:
    d = doc("f7_tweaks")
    for extended in (False, True):
        out = export_flow(load(copy.deepcopy(d), strict=False, extended=extended), original=d)
        tpl = _node(out, "SQL-q1")["data"]["node"]["template"]
        assert tpl["database_url"]["value"] == ""


def test_load_from_db_secret_reference_kept() -> None:
    d = doc("f1_minimal_chat")
    out = export_flow(load(copy.deepcopy(d), strict=False), original=d)
    tpl = _node(out, "Agent-b2")["data"]["node"]["template"]["api_key"]
    assert tpl["value"] and tpl.get("load_from_db") is True


def test_added_edge_to_field_without_input_types_refused() -> None:
    d = doc("f1_minimal_chat")
    _node(d, "Agent-b2")["data"]["node"]["template"]["system_prompt"]["input_types"] = []
    flow = load(copy.deepcopy(d), strict=False)
    flow.edges.append(FlowEdge("ChatInput-a1", "message", "Agent-b2", "system_prompt", "x"))
    with pytest.raises(FlowExportError, match="empty input_types"):
        export_flow(flow, original=d)


def test_added_edge_with_incompatible_types_refused() -> None:
    d = doc("f1_minimal_chat")
    flow = load(copy.deepcopy(d), strict=False)
    flow.edges.append(FlowEdge("ChatInput-a1", "message", "Agent-b2", "tools", "x"))
    with pytest.raises(FlowExportError, match="not accepted"):
        export_flow(flow, original=d)


def test_deleted_template_field_refused_naming_node_and_field() -> None:
    d = doc("f1_minimal_chat")
    flow = load(copy.deepcopy(d), strict=False)
    del flow.nodes["Agent-b2"].fields["system_prompt"]
    with pytest.raises(FlowExportError, match=r"Agent-b2.*system_prompt"):
        export_flow(flow, original=d)


@pytest.mark.parametrize(
    ("before", "after"), [(0, False), (1, True), (1, 1.0), (1, "1"), (True, 1), (False, 0)]
)
def test_type_exact_edit_detection(before: Any, after: Any) -> None:
    d = doc("f1_minimal_chat")
    _node(d, "Agent-b2")["data"]["node"]["template"]["n_messages"]["value"] = before
    flow = load(copy.deepcopy(d), strict=False)
    flow.nodes["Agent-b2"].fields["n_messages"] = after
    out = export_flow(flow, original=d)
    v = _node(out, "Agent-b2")["data"]["node"]["template"]["n_messages"]["value"]
    assert v == after and type(v) is type(after)
