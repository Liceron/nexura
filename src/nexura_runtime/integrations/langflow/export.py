# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Write an ``ImportedFlow`` back to Langflow 1.12.x flow JSON (so V2's editor can open it).

Lossless by construction: with ``original=`` the export starts from a deep copy of the
original document and changes only what differs between the flow and a fresh load of the
original. Component ``code`` is never invented or rewritten.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from . import registry
from .errors import LangflowImportError
from .flow import (
    _SECRET_NAMES,
    FlowEdge,
    FlowNode,
    ImportedFlow,
    _decode_handle,
    _read,
    load,
)

_ESCAPED_QUOTE = "œ"
_EdgeKey = tuple[str, str, str, str]


class FlowExportError(LangflowImportError):
    """The flow cannot be written back to Langflow JSON; the message says why."""


def _key(e: FlowEdge) -> _EdgeKey:
    return (e.source, e.source_output, e.target, e.target_field)


def _same(a: Any, b: Any) -> bool:
    """Type-exact equality: False/0, True/1, 1/1.0 and "1"/1 all differ."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(v, b[k]) for k, v in a.items())
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def _is_sdk_only(defn: dict[str, Any]) -> bool:
    """Whether a node definition was built by the exporter and carries no type metadata."""
    meta = defn.get("metadata")
    return isinstance(meta, dict) and meta.get("nexura_sdk_only") is True


def _is_secret_entry(name: str, entry: dict[str, Any]) -> bool:
    """The importer's secret rule, with connection-string fields always counted as secret."""
    return (
        entry.get("_input_type") == "SecretStrInput"
        or entry.get("password") is True
        or name.lower() in _SECRET_NAMES
        or name in registry.CONNECTION_FIELDS
    )


def _blank_secrets(raw: dict[str, Any]) -> None:
    """Blank every literal secret in a raw node's template; variable references are kept."""
    data = raw.get("data")
    defn = data.get("node") if isinstance(data, dict) else None
    template = defn.get("template") if isinstance(defn, dict) else None
    if not isinstance(template, dict):
        return
    for name, entry in template.items():
        if not isinstance(entry, dict) or name == "code" or not _is_secret_entry(name, entry):
            continue
        value = entry.get("value")
        if entry.get("load_from_db") is True and isinstance(value, str) and value:
            continue
        if value not in ("", None):
            entry["value"] = ""


def _handle_str(obj: dict[str, Any]) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.replace('"', _ESCAPED_QUOTE)


def _graph(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["data"] if isinstance(doc.get("data"), dict) else doc


def _definition(raw: dict[str, Any]) -> dict[str, Any]:
    defn: dict[str, Any] = raw["data"]["node"]
    return defn


def _outputs(defn: dict[str, Any]) -> list[dict[str, Any]]:
    outs = defn.get("outputs")
    return [o for o in outs if isinstance(o, dict)] if isinstance(outs, list) else []


def _new_edge(e: FlowEdge, nodes: dict[str, dict[str, Any]], taken: set[str]) -> dict[str, Any]:
    if e.source not in nodes or e.target not in nodes:
        raise FlowExportError(f"edge {e.source}->{e.target}: endpoint node is not in the export")
    s_def, t_def = _definition(nodes[e.source]), _definition(nodes[e.target])
    outs = {o.get("name"): o for o in _outputs(s_def)}
    if e.source_output not in outs:
        raise FlowExportError(
            f"edge {e.source}.{e.source_output} -> {e.target}.{e.target_field}: "
            f"source has no output '{e.source_output}' (has {sorted(map(str, outs))})"
        )
    template: dict[str, Any] = t_def["template"] if isinstance(t_def.get("template"), dict) else {}
    entry = template.get(e.target_field)
    if not isinstance(entry, dict):
        raise FlowExportError(
            f"edge {e.source}.{e.source_output} -> {e.target}.{e.target_field}: "
            f"target has no template field '{e.target_field}'"
        )
    out = outs[e.source_output]
    types = [out["selected"]] if out.get("selected") else list(out.get("types") or [])
    accepted = list(entry.get("input_types") or [])
    where = f"edge {e.source}.{e.source_output} -> {e.target}.{e.target_field}"
    if _is_sdk_only(s_def) or _is_sdk_only(t_def):
        pass
    elif not accepted:
        raise FlowExportError(
            f"{where}: target field '{e.target_field}' accepts no connections (empty input_types)"
        )
    elif not any(t in accepted for t in types):
        raise FlowExportError(
            f"{where}: output types {types} are not accepted by the target (input_types {accepted})"
        )
    source = {
        "dataType": nodes[e.source]["data"].get("type"),
        "id": e.source,
        "name": e.source_output,
        "output_types": types,
    }
    target = {
        "fieldName": e.target_field,
        "id": e.target,
        "inputTypes": accepted,
        "type": entry.get("type", "str"),
    }
    sh, th = _handle_str(source), _handle_str(target)
    base = f"xy-edge__{e.source}{sh}-{e.target}{th}"
    eid, n = base, 1
    while eid in taken:
        n += 1
        eid = f"{base}#{n}"
    taken.add(eid)
    return {
        "id": eid,
        "source": e.source,
        "target": e.target,
        "sourceHandle": sh,
        "targetHandle": th,
        "data": {"sourceHandle": source, "targetHandle": target},
    }


def _sdk_only_node(node: FlowNode, index: int) -> dict[str, Any]:
    """A node with no original: class + template fields, deliberately without ``code``."""
    spec = registry.spec_for(node.class_name, True)
    if spec is None:
        raise FlowExportError(
            f"node {node.id} ({node.class_name or '<no type>'}) is not a supported component"
        )
    template: dict[str, Any] = {}
    for name, value in node.fields.items():
        entry: dict[str, Any] = {
            "type": node.field_types.get(name) or "str",
            "value": value,
            "show": name not in node.hidden_fields,
        }
        if name in node.from_db:
            entry["load_from_db"] = True
        template[name] = entry
    for name in sorted(spec.inputs or ()):
        template.setdefault(name, {"type": "other", "value": "", "show": True})
    raw_node: dict[str, Any] = {
        "id": node.id,
        "type": "genericNode",
        "position": {"x": 400 * index, "y": 0},
        "data": {
            "id": node.id,
            "type": node.class_name,
            "node": {
                "display_name": node.display_name,
                "template": template,
                "outputs": [{"name": o} for o in sorted(spec.outputs)],
                "metadata": {"nexura_sdk_only": True},
            },
        },
    }
    _blank_secrets(raw_node)
    return raw_node


def _apply_fields(node: FlowNode, base: FlowNode, raw: dict[str, Any]) -> None:
    defn = _definition(raw)
    template: dict[str, Any] = defn["template"] if isinstance(defn.get("template"), dict) else {}
    if node.class_name != base.class_name:
        raise FlowExportError(f"node {node.id}: changing the component class is not supported")
    if node.display_name != base.display_name:
        defn["display_name"] = node.display_name
    for name in base.fields:
        if name not in node.fields:
            raise FlowExportError(
                f"node {node.id}: field '{name}' was deleted; Langflow components cannot "
                "lose template fields"
            )
    for name, value in node.fields.items():
        in_db = name in node.from_db
        entry = template.get(name)
        if name not in base.fields and not isinstance(entry, dict):
            raise FlowExportError(
                f"node {node.id}: field '{name}' is not in the node's template; the exporter "
                "never invents template fields on an existing node"
            )
        if not isinstance(entry, dict):
            continue
        if (
            name in base.fields
            and _same(value, base.fields[name])
            and in_db == (name in base.from_db)
        ):
            continue
        if name in node.secret_fields and not in_db and value not in ("", None):
            raise FlowExportError(
                f"node {node.id}: refusing to write a literal secret into field '{name}'; "
                "store it as a global variable and reference it (load_from_db)"
            )
        entry["value"] = value
        if in_db or "load_from_db" in entry:
            entry["load_from_db"] = in_db


def export_flow(
    flow: ImportedFlow,
    *,
    original: dict[str, Any] | str | Path | None = None,
    allow_sdk_only: bool = False,
) -> dict[str, Any]:
    """Return Langflow flow JSON for ``flow``; with ``original`` every unmodelled field is kept."""
    if not isinstance(flow, ImportedFlow):
        raise TypeError("export_flow expects an ImportedFlow (from load())")
    unsupported = {u.id for u in flow.report.unsupported}
    if original is None:
        if flow.nodes and not allow_sdk_only:
            raise FlowExportError(
                "no original flow given: every node would be SDK-only, and a node without "
                "component code cannot be built by Langflow. Pass original=<the JSON you "
                "loaded>, or allow_sdk_only=True to emit code-less nodes anyway."
            )
        bad = sorted(unsupported & set(flow.nodes))
        if bad:
            raise FlowExportError(f"unsupported node(s) cannot be exported: {', '.join(bad)}")
        nodes = [_sdk_only_node(n, i) for i, n in enumerate(flow.nodes.values())]
        by_id = {n["id"]: n for n in nodes}
        taken: set[str] = set()
        edges = [_new_edge(e, by_id, taken) for e in flow.edges]
        return {
            "name": flow.name,
            "description": flow.description,
            "is_component": False,
            "data": {
                "nodes": nodes,
                "edges": edges,
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        }

    doc = copy.deepcopy(_read(original, 1 << 40))
    base = load(doc, strict=False, extended=flow.extended)
    graph = _graph(doc)
    raw_nodes: list[dict[str, Any]] = graph["nodes"]
    raw_by_id = {r["id"]: r for r in raw_nodes if isinstance(r, dict) and "id" in r}

    # Nodes: refuse edits to unsupported ones, drop removed ones, update the rest.
    for nid, node in flow.nodes.items():
        changed = nid not in base.nodes or not _same(node.fields, base.nodes[nid].fields)
        if nid in unsupported and changed:
            raise FlowExportError(
                f"node {nid} ({node.class_name or '<no type>'}) is unsupported and was "
                "edited or added; unsupported nodes can only pass through unchanged"
            )
    for nid, node in flow.nodes.items():
        if nid in unsupported and nid in base.nodes:
            for name in base.nodes[nid].fields:
                if name not in node.fields:
                    raise FlowExportError(
                        f"node {nid}: field '{name}' was deleted; Langflow components cannot "
                        "lose template fields"
                    )
    removed = {nid for nid in base.nodes if nid not in flow.nodes}
    for nid, node in flow.nodes.items():
        if nid in unsupported or nid not in base.nodes:
            continue
        _apply_fields(node, base.nodes[nid], raw_by_id[nid])
    if removed:
        graph["nodes"] = [r for r in raw_nodes if r.get("id") not in removed]
        raw_by_id = {k: v for k, v in raw_by_id.items() if k not in removed}
    added = [n for nid, n in flow.nodes.items() if nid not in base.nodes]
    if added:
        if not allow_sdk_only:
            raise FlowExportError(
                "node(s) "
                + ", ".join(n.id for n in added)
                + " exist only on the SDK side and have no component code; Langflow cannot "
                "build a node without code. Pass allow_sdk_only=True to emit them without it."
            )
        for i, n in enumerate(added):
            if n.id in unsupported:
                raise FlowExportError(f"unsupported node {n.id} cannot be exported")
            raw = _sdk_only_node(n, len(raw_nodes) + i)
            graph["nodes"].append(raw)
            raw_by_id[n.id] = raw

    # Edges: keep raw edges (incl. unsupported/unmodelled ones) unless the edit removed them.
    before = {_key(e) for e in base.edges}
    now = {_key(e) for e in flow.edges}
    gone = before - now
    kept: list[dict[str, Any]] = []
    for raw_edge in graph["edges"]:
        s, d = raw_edge.get("source"), raw_edge.get("target")
        if s in removed or d in removed:
            continue
        sink: list[str] = []
        sh = _decode_handle(raw_edge, "source", sink, "e")
        th = _decode_handle(raw_edge, "target", sink, "e")
        if sh and th:
            k = (s, sh.get("name"), d, th.get("fieldName") or th.get("name"))
            if k in gone:
                continue
        kept.append(raw_edge)
    taken = {str(e.get("id")) for e in kept}
    for e in flow.edges:
        if _key(e) in before:
            continue
        if e.source in unsupported or e.target in unsupported:
            raise FlowExportError(f"edge {e.source}->{e.target}: touches an unsupported node")
        kept.append(_new_edge(e, raw_by_id, taken))
    graph["edges"] = kept
    for raw_node in graph["nodes"]:
        if isinstance(raw_node, dict):
            _blank_secrets(raw_node)

    if flow.name != base.name and "name" in doc:
        doc["name"] = flow.name
    if flow.description != base.description and "description" in doc:
        doc["description"] = flow.description
    return doc
