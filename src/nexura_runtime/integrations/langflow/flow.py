# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Parse and validate Langflow 1.12.x saved-flow JSON into an inert ``ImportedFlow``.

Nothing here executes flow content. The ``code`` field of every node is dropped after a
hash check; behaviour is chosen from the component class name.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import registry
from .errors import FlowFormatError, FlowTooLargeError, UnsupportedFlowError

MAX_FLOW_BYTES = 5 * 1024 * 1024
MAX_NODES = 1000
MAX_EDGES = 5000
_ESCAPED_QUOTE = "œ"  # the oe ligature Langflow uses in place of '"' in handle strings
_SECRET_NAMES = frozenset({"api_key", "apikey", "token", "secret", "password", "access_token"})


@dataclass(frozen=True)
class FlowEdge:
    source: str
    source_output: str
    target: str
    target_field: str
    id: str = ""


@dataclass
class FlowNode:
    id: str
    class_name: str
    display_name: str
    #: Field name -> value, without the ``code`` field. Literal secrets are blanked.
    fields: dict[str, Any] = field(default_factory=dict)
    #: Field name -> Langflow coarse type (``str``, ``code``, ``table`` ...).
    field_types: dict[str, str] = field(default_factory=dict)
    #: Field names whose value is the NAME of a global variable (``load_from_db``).
    from_db: set[str] = field(default_factory=set)
    #: Field names holding secrets.
    secret_fields: set[str] = field(default_factory=set)
    #: Fields the template marks as not shown (``show: false``).
    hidden_fields: set[str] = field(default_factory=set)
    kind: str | None = None
    has_code: bool = False
    code_verified: bool = False


@dataclass(frozen=True)
class SupportedNode:
    id: str
    class_name: str
    display_name: str


@dataclass(frozen=True)
class UnsupportedNode:
    id: str
    class_name: str
    reason: str
    in_main_path: bool


@dataclass(frozen=True)
class SkippedNote:
    id: str
    text: str


@dataclass
class ImportReport:
    supported: list[SupportedNode] = field(default_factory=list)
    unsupported: list[UnsupportedNode] = field(default_factory=list)
    notes: list[SkippedNote] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unsupported_edges: list[str] = field(default_factory=list)
    #: (variable name, node id, field, is_secret)
    variables: list[tuple[str, str, str, bool]] = field(default_factory=list)

    @property
    def required_variables(self) -> list[str]:
        return sorted({v[0] for v in self.variables})

    def to_dict(self) -> dict[str, Any]:
        """Return the report as a JSON-ready dict."""
        return {
            "supported": [vars(n) for n in self.supported],
            "unsupported": [vars(n) for n in self.unsupported],
            "notes": [vars(n) for n in self.notes],
            "warnings": list(self.warnings),
            "unsupported_edges": list(self.unsupported_edges),
            "required_variables": self.required_variables,
        }


@dataclass
class ImportedFlow:
    name: str
    description: str
    nodes: dict[str, FlowNode]
    edges: list[FlowEdge]
    report: ImportReport
    strict: bool = True
    #: True when loaded with the extended (V3) component families enabled.
    extended: bool = False

    def supported_nodes(self) -> list[FlowNode]:
        """Return the imported nodes that have no unsupported-feature finding."""
        bad = {u.id for u in self.report.unsupported}
        return [n for n in self.nodes.values() if n.id not in bad]


def _decode_handle(edge: dict[str, Any], side: str, problems: list[str], label: str) -> Any:
    data = edge.get("data")
    parsed = data.get(f"{side}Handle") if isinstance(data, dict) else None
    if isinstance(parsed, dict):
        return parsed
    raw = edge.get(f"{side}Handle")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw.replace(_ESCAPED_QUOTE, '"'))
        except (ValueError, RecursionError):
            problems.append(f"{label}: {side}Handle is not valid handle JSON")
            return None
        if isinstance(value, dict):
            return value
    problems.append(f"{label}: missing or unreadable {side}Handle")
    return None


def _read(flow: dict[str, Any] | str | Path, max_bytes: int) -> dict[str, Any]:
    try:
        if isinstance(flow, dict):
            text = json.dumps(flow, default=str)
            if len(text.encode()) > max_bytes:
                raise FlowTooLargeError(f"Flow exceeds the {max_bytes} byte limit")
            doc: Any = flow
        else:
            if isinstance(flow, str) and flow.lstrip().startswith(("{", "[")):
                raw = flow.encode("utf-8")
            elif isinstance(flow, (str, Path)):
                try:
                    path = Path(flow)
                    is_file = path.is_file()
                except (OSError, ValueError):  # overlong name, NUL byte, ...
                    is_file = False
                if not is_file:
                    raise FlowFormatError("Input is neither JSON text nor an existing file")
                if path.stat().st_size > max_bytes:
                    raise FlowTooLargeError(f"Flow file exceeds the {max_bytes} byte limit")
                raw = path.read_bytes()
            else:
                raise FlowFormatError(f"Unsupported input type {type(flow).__name__}")
            if len(raw) > max_bytes:
                raise FlowTooLargeError(f"Flow exceeds the {max_bytes} byte limit")
            try:
                doc = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise FlowFormatError("Flow is not valid UTF-8 text") from exc
            except json.JSONDecodeError as exc:
                raise FlowFormatError(
                    f"Flow is not valid JSON: {exc.msg} (line {exc.lineno})"
                ) from exc
    except RecursionError as exc:
        raise FlowFormatError("Flow JSON is nested too deeply") from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, FlowFormatError):
            raise
        raise FlowFormatError(f"Flow cannot be serialised as JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise FlowFormatError("Flow JSON must be an object")
    return doc


def _template_value(entry: Any) -> Any:
    return entry.get("value") if isinstance(entry, dict) else entry


def _convert_node(raw: dict[str, Any], index: int, problems: list[str]) -> tuple[Any, ...] | None:
    nid = raw.get("id")
    if not isinstance(nid, str) or not nid:
        problems.append(f"node #{index}: missing string id")
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        problems.append(f"node {nid}: missing data object")
        return None
    return nid, data


def load(
    flow: dict[str, Any] | str | Path,
    *,
    strict: bool = True,
    max_bytes: int = MAX_FLOW_BYTES,
    max_nodes: int = MAX_NODES,
    max_edges: int = MAX_EDGES,
    extended: bool = False,
) -> ImportedFlow:
    """Parse and validate a saved Langflow flow. Never executes embedded component code.

    ``flow`` is a dict, JSON text or a file path. Accepts a bare starter, a database export
    or a bare ``data`` object. With ``strict=True`` (default) an unsupported node on the
    path to a ChatOutput raises ``UnsupportedFlowError``; with ``strict=False`` the report
    lists it and the supported part is kept. ``extended=True`` also recognises the V3 families
    (File, MemoryBase, KnowledgeBase/Knowledge, LoopComponent, SQLComponent, SplitText); they
    stay unsupported by default so existing callers see no change.
    """
    doc = _read(flow, max_bytes)
    if doc.get("is_component") is True:
        raise FlowFormatError("This file is a single custom component, not a flow")
    graph: dict[str, Any] = doc["data"] if isinstance(doc.get("data"), dict) else doc
    nodes_raw, edges_raw = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
        raise FlowFormatError("Flow needs data.nodes and data.edges lists")
    if len(nodes_raw) > max_nodes or len(edges_raw) > max_edges:
        raise FlowTooLargeError(f"Flow has too many nodes/edges (limits {max_nodes}/{max_edges})")

    problems: list[str] = []
    report = ImportReport()
    nodes: dict[str, FlowNode] = {}
    note_ids: set[str] = set()
    unsupported: dict[str, UnsupportedNode] = {}
    edited: list[str] = []
    unverifiable: list[str] = []

    for index, raw in enumerate(nodes_raw):
        if not isinstance(raw, dict):
            problems.append(f"node #{index}: not an object")
            continue
        parsed = _convert_node(raw, index, problems)
        if parsed is None:
            continue
        nid, data = parsed
        if nid in nodes or nid in note_ids:
            problems.append(f"duplicate node id {nid}")
            continue
        definition = data.get("node") if isinstance(data.get("node"), dict) else {}
        class_name = data.get("type") if isinstance(data.get("type"), str) else ""
        if raw.get("type") == "noteNode" or class_name == "note":
            note_ids.add(nid)
            text = definition.get("description") or _template_value(
                (definition.get("template") or {}).get("note")
            )
            report.notes.append(SkippedNote(nid, str(text or "")[:500]))
            continue
        template = definition.get("template")
        template = template if isinstance(template, dict) else {}
        display = str(definition.get("display_name") or class_name or nid)
        node = FlowNode(id=nid, class_name=class_name, display_name=display)
        for key, entry in template.items():
            if not isinstance(entry, dict):
                continue
            ftype = str(entry.get("type") or "")
            node.field_types[key] = ftype
            if key == "code" or ftype == "code":
                code = entry.get("value")
                node.has_code = isinstance(code, str) and bool(code.strip())
                declared = (definition.get("metadata") or {}).get("code_hash")
                if node.has_code and isinstance(declared, str) and declared:
                    digest = hashlib.sha256(str(code).encode()).hexdigest()[:12]
                    if digest == declared:
                        node.code_verified = True
                    else:
                        edited.append(nid)
                elif node.has_code:
                    unverifiable.append(nid)
                continue
            value = entry.get("value")
            if entry.get("show") is False:
                node.hidden_fields.add(key)
            secret = (
                entry.get("_input_type") == "SecretStrInput"
                or entry.get("password") is True
                or key.lower() in _SECRET_NAMES
                or (extended and key in registry.CONNECTION_FIELDS)
            )
            if secret:
                node.secret_fields.add(key)
            if entry.get("load_from_db") and isinstance(value, str) and value:
                node.from_db.add(key)
            elif secret and isinstance(value, str) and value:
                report.warnings.append(
                    f"node {nid}: literal secret in field '{key}' was ignored, not copied"
                )
                value = ""
            node.fields[key] = value
        spec = registry.spec_for(class_name, extended)
        node.kind = spec.kind if spec else None
        nodes[nid] = node
        if not class_name:
            unsupported[nid] = UnsupportedNode(nid, "", "node has no data.type", False)
        elif nid in edited:
            unsupported[nid] = UnsupportedNode(
                nid,
                class_name,
                "component code was edited (code hash mismatch); code is never executed",
                False,
            )
        elif spec is None:
            reason = registry.KNOWN_UNSUPPORTED.get(class_name, "unknown component class")
            unsupported[nid] = UnsupportedNode(nid, class_name, reason, False)

    # Edges
    edges: list[FlowEdge] = []
    for index, raw in enumerate(edges_raw):
        label = f"edge #{index}"
        if not isinstance(raw, dict):
            problems.append(f"{label}: not an object")
            continue
        src, dst = raw.get("source"), raw.get("target")
        if not isinstance(src, str) or not isinstance(dst, str):
            problems.append(f"{label}: source/target must be node ids")
            continue
        label = f"edge {src}->{dst}"
        if src in note_ids or dst in note_ids:
            report.warnings.append(f"{label}: edge touches a note node and was dropped")
            continue
        for end in (src, dst):
            if end not in nodes:
                problems.append(f"{label}: unknown node id {end}")
        sh = _decode_handle(raw, "source", problems, label)
        th = _decode_handle(raw, "target", problems, label)
        if sh is None or th is None or src not in nodes or dst not in nodes:
            continue
        out_name = sh.get("name")
        in_field = th.get("fieldName") or th.get("name")
        if not isinstance(out_name, str) or not isinstance(in_field, str):
            problems.append(f"{label}: handle lacks output name or field name")
            continue
        if sh.get("id") != src or th.get("id") != dst:
            problems.append(f"{label}: handle id does not match edge source/target")
            continue
        edges.append(FlowEdge(src, out_name, dst, in_field, str(raw.get("id") or "")))
    if problems:
        raise FlowFormatError("Invalid Langflow flow", problems)

    _check_cycles(nodes, edges)

    # Reachability: main path = ancestors of ChatOutput nodes.
    parents: dict[str, list[str]] = {}
    for e in edges:
        parents.setdefault(e.target, []).append(e.source)
    main: set[str] = set()
    queue = deque(n.id for n in nodes.values() if n.kind == registry.CHAT_OUTPUT)
    while queue:
        cur = queue.popleft()
        if cur in main:
            continue
        main.add(cur)
        queue.extend(parents.get(cur, ()))
    for nid, u in list(unsupported.items()):
        unsupported[nid] = UnsupportedNode(u.id, u.class_name, u.reason, nid in main)

    # Edge compatibility between supported nodes.
    good: list[FlowEdge] = []
    into: dict[tuple[str, str], int] = {}
    for e in edges:
        if e.source in unsupported or e.target in unsupported:
            report.unsupported_edges.append(
                f"{e.source}.{e.source_output} -> {e.target}.{e.target_field} (unsupported node)"
            )
            continue
        s_spec = registry.spec_for(nodes[e.source].class_name, extended)
        t_spec = registry.spec_for(nodes[e.target].class_name, extended)
        assert s_spec is not None and t_spec is not None
        why = None
        if e.source_output not in s_spec.outputs:
            why = f"{nodes[e.source].class_name} has no supported output '{e.source_output}'"
        elif t_spec.inputs is not None and e.target_field not in t_spec.inputs:
            why = f"{nodes[e.target].class_name} has no supported input '{e.target_field}'"
        elif t_spec.kind == registry.PROMPT and e.target_field in {"template"}:
            why = "Prompt template text cannot be wired"
        elif e.target_field != "tools" and into.get((e.target, e.target_field)):
            why = "several edges into one single-value field"
        if why:
            report.unsupported_edges.append(
                f"{e.source}.{e.source_output} -> {e.target}.{e.target_field} ({why})"
            )
            continue
        into[(e.target, e.target_field)] = into.get((e.target, e.target_field), 0) + 1
        good.append(e)

    for node in nodes.values():
        if node.id in unsupported:
            continue
        report.supported.append(SupportedNode(node.id, node.class_name, node.display_name))
        if extended and node.kind in registry.EXTENDED_KINDS:
            for opt in registry.unsupported_options(
                node.class_name, node.fields, node.hidden_fields
            ):
                report.warnings.append(
                    f"node {node.id} ({node.class_name}): option '{opt}' is not supported "
                    "and was ignored"
                )
        for fname in sorted(node.from_db):
            if extended and node.kind == registry.SQL and fname in registry.CONNECTION_FIELDS:
                report.warnings.append(
                    f"node {node.id}: '{fname}' is never used; the connection comes from "
                    "V3Options.sql_connection"
                )
                continue
            report.variables.append(
                (str(node.fields[fname]), node.id, fname, fname in node.secret_fields)
            )
    report.unsupported = list(unsupported.values())
    if unverifiable:
        report.warnings.append(
            "component code of node(s) "
            + ", ".join(unverifiable)
            + " cannot be verified (no matching code_hash); it was ignored and never executed"
        )
    if not any(n.kind == registry.CHAT_OUTPUT for n in nodes.values()):
        report.warnings.append("flow has no ChatOutput node")

    blocking = [u for u in report.unsupported if u.in_main_path]
    blocking_edges = report.unsupported_edges
    if strict and (blocking or blocking_edges):
        lines = [f"{u.class_name or '<no type>'} ({u.id}): {u.reason}" for u in blocking]
        lines += [f"edge {e}" for e in blocking_edges]
        raise UnsupportedFlowError(
            "Flow uses unsupported parts on the path to ChatOutput:\n  - " + "\n  - ".join(lines),
            report,
        )
    for u in report.unsupported:
        if not u.in_main_path:
            report.warnings.append(
                f"unsupported node {u.id} ({u.class_name or '<no type>'}) is off the main path: "
                f"{u.reason}"
            )
    doc_name = doc.get("name")
    doc_desc = doc.get("description")
    return ImportedFlow(
        name=str(doc_name) if isinstance(doc_name, str) else "flow",
        description=str(doc_desc) if isinstance(doc_desc, str) else "",
        nodes=nodes,
        edges=good,
        report=report,
        strict=strict,
        extended=extended,
    )


def _check_cycles(nodes: dict[str, FlowNode], edges: list[FlowEdge]) -> None:
    """Reject every cycle that does not pass through a Loop component.

    Cycles through a ``LoopComponent`` are allowed (the Loop is reported unsupported). Every
    other cycle survives deleting the Loop nodes, so Kahn's algorithm on that remainder finds
    exactly the forbidden ones.
    """
    loops = {nid for nid, n in nodes.items() if n.class_name == "LoopComponent"}
    out: dict[str, list[str]] = {nid: [] for nid in nodes if nid not in loops}
    indeg = dict.fromkeys(out, 0)
    for e in edges:
        if e.source in out and e.target in out:
            out[e.source].append(e.target)
            indeg[e.target] += 1
    ready = deque(nid for nid, d in indeg.items() if d == 0)
    while ready:
        nid = ready.popleft()
        indeg.pop(nid)
        for child in out[nid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    if not indeg:
        return
    # Every remaining node still has a remaining predecessor; walking predecessors must loop.
    pred = {nid: next(s for s in out if nid in out[s] and s in indeg) for nid in indeg}
    seen: dict[str, int] = {}
    path: list[str] = []
    cur = next(iter(indeg))
    while cur not in seen:
        seen[cur] = len(path)
        path.append(cur)
        cur = pred[cur]
    cycle = path[seen[cur] :][::-1]
    raise FlowFormatError(
        "Flow contains a cycle that is not a Loop",
        ["cycle: " + " -> ".join([*cycle, cycle[0]])],
    )


_VARIABLE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def template_variables(template: str) -> list[str]:
    """Names of ``{variable}`` placeholders in order, ignoring ``{{escaped}}`` braces."""
    seen: list[str] = []
    for name in _VARIABLE.findall(template):
        if name not in seen:
            seen.append(name)
    return seen


def render_template(template: str, values: dict[str, str]) -> str:
    """Substitute ``{name}`` placeholders; unknown names become empty. No attribute access."""
    # Un-escape ``{{``/``}}`` only in template text, never in substituted values.
    parts: list[str] = []
    last = 0
    for m in _VARIABLE.finditer(template):
        parts.append(template[last : m.start()].replace("{{", "{").replace("}}", "}"))
        parts.append(values.get(m.group(1), ""))
        last = m.end()
    parts.append(template[last:].replace("{{", "{").replace("}}", "}"))
    return "".join(parts)
