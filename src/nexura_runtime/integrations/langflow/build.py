# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Turn an ``ImportedFlow`` into something runnable on the SDK.

Semantics (documented choice): Langflow is a dataflow graph, so a flow becomes a
dataflow runner over SDK ``Agent`` objects, executed in topological order. An Agent whose
``response`` feeds another Agent's ``input_value`` is a **pipeline step** (like
``SequentialAgent``), not a handoff: the first agent always finishes and its text becomes the
next agent's input. An Agent wired into another Agent's ``tools`` port becomes an
**agent-as-tool** (``Agent.as_tool``). Handoffs have no Langflow equivalent and are not used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx
from nexura_agents import (
    Agent,
    MemorySessionStore,
    RunEvent,
    RunEventType,
    Runner,
    RunResult,
    SessionStore,
    Tool,
)
from nexura_agents.history import keep_last_turns

from . import components, registry
from .components import LoopPlan, RunScope, V3Options
from .errors import ComponentRefusedError, FlowBuildError, MissingVariablesError
from .flow import FlowEdge, FlowNode, ImportedFlow, render_template, template_variables
from .models import (
    ModelRef,
    ModelsArg,
    extract_model_ref,
    needs_default_mapping,
    resolve_model,
)
from .tools import (
    SearchProvider,
    calculator_tool,
    current_date_tool,
    url_tool,
    web_search_tool,
)
from .tweaks import apply_tweaks

ToolsArg = Mapping[str, "Tool | Callable[[FlowNode], Tool]"]

EventT = TypeVar("EventT")
_EventT_co = TypeVar("_EventT_co", covariant=True)


class FlowEventMapper(Protocol[_EventT_co]):
    """Turns the steps of one flow run into events of some wire format.

    The engine calls these hooks in run order: ``vertices_sorted`` once, then per vertex
    ``build_start``, ``add_message`` / ``token`` for chat input and output, ``end_vertex``;
    then ``build_end`` and ``end``, or ``error`` and ``end`` on failure. ``from_run_event``
    maps the SDK events of a model call while it runs (an empty list skips one). Pass an
    implementation to ``BuiltFlow.stream_events`` or ``stream_flow_events``. The open-source
    edition ships no wire format of its own; the closed edition's Nexura V2 compatibility
    layer provides one.
    """

    def new_id(self) -> str:
        """A fresh message id (one per vertex, made before the run starts)."""
        ...

    def vertices_sorted(
        self, ids: Iterable[str], to_run: Iterable[str] | None = None
    ) -> _EventT_co:
        """The run order (``ids``) and the vertices that will run, once at the start."""
        ...

    def build_start(self, vertex_id: str) -> _EventT_co:
        """A vertex starts."""
        ...

    def end_vertex(
        self, vertex_id: str, display_name: str, class_name: str, results: dict[str, Any]
    ) -> _EventT_co:
        """A vertex finished with ``results`` (output name to value)."""
        ...

    def add_message(
        self, message_id: str, text: str, *, sender: str, session_id: str | None
    ) -> _EventT_co:
        """A complete chat message (``sender`` is ``User`` or ``Machine``)."""
        ...

    def token(self, message_id: str, chunk: str) -> _EventT_co:
        """One streamed text chunk of the ChatOutput message ``message_id``."""
        ...

    def error(self, vertex_id: str | None, exc: BaseException) -> _EventT_co:
        """The run failed in ``vertex_id`` (``None`` outside a vertex); ``end`` follows."""
        ...

    def build_end(self) -> _EventT_co:
        """Every vertex finished."""
        ...

    def end(self) -> _EventT_co:
        """The last event of every run, successful or not."""
        ...

    def from_run_event(
        self, vertex_id: str, event: RunEvent, *, message_id: str | None = None
    ) -> Sequence[_EventT_co]:
        """Events for one SDK ``RunEvent`` of a model call in ``vertex_id`` (may be empty)."""
        ...


class _SilentMapper:
    """The mapper ``run``/``run_async`` use: message ids only, no events."""

    def new_id(self) -> str:
        return str(uuid.uuid4())

    def vertices_sorted(self, ids: Iterable[str], to_run: Iterable[str] | None = None) -> None:
        return None

    def build_start(self, vertex_id: str) -> None:
        return None

    def end_vertex(
        self, vertex_id: str, display_name: str, class_name: str, results: dict[str, Any]
    ) -> None:
        return None

    def add_message(
        self, message_id: str, text: str, *, sender: str, session_id: str | None
    ) -> None:
        return None

    def token(self, message_id: str, chunk: str) -> None:
        return None

    def error(self, vertex_id: str | None, exc: BaseException) -> None:
        return None

    def build_end(self) -> None:
        return None

    def end(self) -> None:
        return None

    def from_run_event(
        self, vertex_id: str, event: RunEvent, *, message_id: str | None = None
    ) -> list[None]:
        return []


_TYPE_MAP = {
    "str": "string",
    "text": "string",
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "dict": "object",
    "object": "object",
    "list": "array",
}
_CONFIG_OUTPUTS = {"component_as_tool", "model_output"}


def _schema_from_table(rows: Any, node_id: str) -> dict[str, Any] | None:
    """Langflow output-schema table rows -> JSON schema (all fields required)."""
    if not isinstance(rows, list) or not rows:
        return None
    props: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise FlowBuildError(f"node {node_id}: output schema rows need a name")
        jtype = _TYPE_MAP.get(str(row.get("type", "str")).lower())
        if jtype is None:
            raise FlowBuildError(
                f"node {node_id}: unsupported schema type {row.get('type')!r} for {row['name']}"
            )
        prop: dict[str, Any] = {"type": jtype}
        if jtype == "array":
            prop["items"] = {"type": "string"}
        if row.get("description"):
            prop["description"] = str(row["description"])
        if row.get("multiple") in (True, "True", "true"):
            prop = {"type": "array", "items": {k: v for k, v in prop.items() if k != "description"}}
            if row.get("description"):
                prop["description"] = str(row["description"])
        props[row["name"]] = prop
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _agent_name(node: FlowNode) -> str:
    base = re.sub(r"\W+", "_", node.display_name).strip("_") or "agent"
    return f"{base}_{re.sub(r'\W+', '_', node.id)}"[:64]


@dataclass
class FlowRunResult:
    """Outcome of one flow run."""

    #: Text of the selected ChatOutput (the first one unless ``output_component`` is given).
    output: str
    #: ChatOutput node id -> text.
    outputs: dict[str, str]
    #: Node id -> output name -> value, for every vertex that ran.
    node_outputs: dict[str, dict[str, Any]]
    #: Node id -> SDK result, for vertices that called a model.
    run_results: dict[str, RunResult[Any]]
    session_id: str | None = None


class _QueueSink:
    """List-like event sink feeding an ``asyncio.Queue`` consumed while the node runs."""

    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue

    def extend(self, events: Iterable[Any]) -> None:
        for ev in events:
            self._queue.put_nowait(ev)


def _make_closer(
    queue: asyncio.Queue[Any],
) -> Callable[[asyncio.Future[Any]], None]:
    def close(_task: asyncio.Future[Any]) -> None:
        queue.put_nowait(None)

    return close


@dataclass
class _Holder:
    result: FlowRunResult | None = None
    error: BaseException | None = None


@dataclass
class BuiltFlow:
    """A runnable imported flow. ``agents`` maps node id to the SDK ``Agent`` built for it."""

    imported: ImportedFlow
    fields: dict[str, dict[str, Any]]
    order: list[str]
    passive: set[str]
    agents: dict[str, Agent[Any]]
    tools: dict[str, Tool]
    model_refs: dict[str, ModelRef]
    warnings: list[str]
    #: Extended (V3) component options and per-node build results.
    v3: V3Options = field(default_factory=V3Options)
    v3_nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Loop node id -> plan; ``loop_body`` holds every node a Loop runs per item.
    loops: dict[str, LoopPlan] = field(default_factory=dict)
    loop_body: set[str] = field(default_factory=set)
    runner: Runner = field(default_factory=Runner)
    _store: SessionStore | None = None
    _identity: str | None = None

    @property
    def flow_identity(self) -> str:
        """Stable content hash of the imported flow (name, nodes, fields, edges).

        Part of every history key, so two flows sharing a store and session id never read
        each other's history.
        """
        if self._identity is None:
            imp = self.imported
            payload = {
                "name": imp.name,
                "description": imp.description,
                "nodes": {
                    nid: [n.class_name, n.display_name, n.fields] for nid, n in imp.nodes.items()
                },
                "edges": [[e.source, e.source_output, e.target, e.target_field] for e in imp.edges],
            }
            raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
            self._identity = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return self._identity

    # ------------------------------------------------------------------ public API
    async def run_async(
        self,
        input_value: str = "",
        *,
        session_id: str | None = None,
        session_store: SessionStore | None = None,
        output_component: str | None = None,
    ) -> FlowRunResult:
        """Run the flow once and return the ChatOutput text (raises the original error)."""
        holder = _Holder()
        async for _ in self._execute(
            input_value, session_id, session_store, output_component, _SilentMapper(), holder
        ):
            pass
        if holder.error is not None:
            raise holder.error
        assert holder.result is not None
        return holder.result

    def run(
        self,
        input_value: str = "",
        *,
        session_id: str | None = None,
        session_store: SessionStore | None = None,
        output_component: str | None = None,
    ) -> FlowRunResult:
        """Blocking ``run_async`` (do not call from a running event loop)."""
        return asyncio.run(
            self.run_async(
                input_value,
                session_id=session_id,
                session_store=session_store,
                output_component=output_component,
            )
        )

    async def stream_events(
        self,
        input_value: str = "",
        *,
        session_id: str | None = None,
        session_store: SessionStore | None = None,
        output_component: str | None = None,
        mapper: FlowEventMapper[EventT],
    ) -> AsyncIterator[EventT]:
        """Yield the run's events as ``mapper`` shapes them (see ``FlowEventMapper``).

        ``mapper`` is required: the open-source edition ships no event wire format. A failure
        becomes an ``error`` event followed by ``end``.
        """
        holder = _Holder()
        async for event in self._execute(
            input_value,
            session_id,
            session_store,
            output_component,
            mapper,
            holder,
        ):
            yield event

    # ------------------------------------------------------------------ engine
    def _incoming(self, nid: str) -> list[FlowEdge]:
        return [e for e in self.imported.edges if e.target == nid]

    def _output_nodes(self, output_component: str | None) -> list[str]:
        outs = [nid for nid in self.order if self.imported.nodes[nid].kind == registry.CHAT_OUTPUT]
        if output_component is None:
            return outs
        chosen = [
            n
            for n in outs
            if n == output_component or self.imported.nodes[n].display_name == output_component
        ]
        if not chosen:
            raise FlowBuildError(f"output_component {output_component!r} is not a ChatOutput")
        return chosen

    async def _execute(
        self,
        input_value: str,
        session_id: str | None,
        session_store: SessionStore | None,
        output_component: str | None,
        mapper: FlowEventMapper[EventT],
        holder: _Holder,
    ) -> AsyncIterator[EventT]:
        nodes = self.imported.nodes
        current: str | None = None
        try:
            wanted = self._output_nodes(output_component)
            store = session_store
            if store is None and session_id is not None:
                if self._store is None:
                    self._store = MemorySessionStore()
                store = self._store
            for nid in self.order:
                node = nodes[nid]
                own = node.fields.get("session_id")
                if node.kind == registry.CHAT_INPUT and isinstance(own, str) and own.strip():
                    session_id = own.strip() if session_id is None else session_id
            mids = {nid: mapper.new_id() for nid in self.order}
            streamed: set[str] = set()
            # Agents whose response feeds a ChatOutput directly stream tokens to it.
            token_target: dict[str, str] = {}
            for e in self.imported.edges:
                if (
                    nodes[e.target].kind == registry.CHAT_OUTPUT
                    and nodes[e.source].kind == registry.AGENT
                    and e.source_output == "response"
                    and e.source in self.order
                ):
                    token_target.setdefault(e.source, e.target)
            values: dict[str, dict[str, Any]] = {}
            run_results: dict[str, RunResult[Any]] = {}
            texts: dict[str, str] = {}
            yield mapper.vertices_sorted(self.order)
            for nid in self.order:
                if nid in self.loop_body:
                    continue  # run by its Loop
                current = nid
                node = nodes[nid]
                yield mapper.build_start(nid)
                if nid in self.passive:
                    values[nid] = {}
                elif node.kind == registry.LOOP:
                    async for ev in self._run_loop(
                        node, values, input_value, mids, mapper, run_results, streamed
                    ):
                        yield ev
                else:
                    gathered = self._gather(nid, values)
                    box: dict[str, Any] = {}
                    async for ev in self._drive(
                        node,
                        gathered,
                        input_value,
                        session_id,
                        store,
                        token_target.get(node.id),
                        mids,
                        mapper,
                        run_results,
                        streamed,
                        box,
                    ):
                        yield ev
                    produced = box["produced"]
                    values[nid] = produced
                    if node.kind == registry.CHAT_INPUT:
                        yield mapper.add_message(
                            mids[nid],
                            str(produced["message"]),
                            sender="User",
                            session_id=session_id,
                        )
                    elif node.kind == registry.CHAT_OUTPUT:
                        text = str(produced["message"])
                        texts[nid] = text
                        if nid not in streamed and text:
                            yield mapper.token(mids[nid], text)
                        yield mapper.add_message(
                            mids[nid], text, sender="Machine", session_id=session_id
                        )
                shown = {
                    k: v if isinstance(v, (str, int, float, bool, dict, list)) else repr(v)
                    for k, v in values[nid].items()
                }
                yield mapper.end_vertex(nid, node.display_name, node.class_name, shown)
            current = None
            outputs = {n: texts.get(n, "") for n in wanted}
            holder.result = FlowRunResult(
                output=outputs[wanted[0]] if wanted else "",
                outputs=outputs,
                node_outputs=values,
                run_results=run_results,
                session_id=session_id,
            )
            yield mapper.build_end()
            yield mapper.end()
        except Exception as exc:
            holder.error = exc
            yield mapper.error(current, exc)
            yield mapper.end()

    async def _drive(
        self,
        node: FlowNode,
        gathered: dict[str, list[Any]],
        input_value: str,
        session_id: str | None,
        store: SessionStore | None,
        output_node: str | None,
        mids: dict[str, str],
        mapper: FlowEventMapper[EventT],
        run_results: dict[str, RunResult[Any]],
        streamed: set[str],
        box: dict[str, Any],
    ) -> AsyncIterator[EventT]:
        """Run one node, forwarding its events as they arrive; the result lands in ``box``."""
        queue: asyncio.Queue[Any] = asyncio.Queue()
        task = asyncio.ensure_future(
            self._run_node(
                node,
                gathered,
                input_value,
                session_id,
                store,
                output_node,
                mids,
                mapper,
                run_results,
                _QueueSink(queue),
                streamed,
            )
        )
        task.add_done_callback(_make_closer(queue))
        try:
            # Forward events as they arrive so tokens are not held until the node ends.
            while (ev := await queue.get()) is not None:
                yield ev
            box["produced"] = await task
        finally:
            if not task.done():
                task.cancel()

    async def _run_loop(
        self,
        node: FlowNode,
        values: dict[str, dict[str, Any]],
        input_value: str,
        mids: dict[str, str],
        mapper: FlowEventMapper[EventT],
        run_results: dict[str, RunResult[Any]],
        streamed: set[str],
    ) -> AsyncIterator[EventT]:
        """Map semantics: run the body once per item (bounded), collect the feedback values.

        Body nodes keep no session history, so items never see each other. A body vertex is
        reported once, with its last iteration's values.
        """
        plan = self.loops[node.id]
        gathered = self._gather(node.id, values)
        raw = gathered["data"][0] if gathered.get("data") else self.fields[node.id].get("data")
        items = components.parse_items(raw)
        components.check_loop_bound(len(items), self.v3, node.id)
        for b in plan.body:
            yield mapper.build_start(b)
        collected: list[str] = []
        for item in items:
            values[node.id] = {"item": item, "done": ""}
            for b in plan.body:
                box: dict[str, Any] = {}
                async for ev in self._drive(
                    self.imported.nodes[b],
                    self._gather(b, values),
                    input_value,
                    None,
                    None,
                    None,
                    mids,
                    mapper,
                    run_results,
                    streamed,
                    box,
                ):
                    yield ev
                values[b] = box["produced"]
            if plan.result is not None:
                collected.append(str(values[plan.result[0]][plan.result[1]]))
            else:
                collected.append(item)
        values[node.id] = {
            "item": items[-1] if items else "",
            "done": "\n".join(collected),
            "items": collected,
        }
        for b in plan.body:
            n = self.imported.nodes[b]
            shown = {
                k: v if isinstance(v, (str, int, float, bool, dict, list)) else repr(v)
                for k, v in values.get(b, {}).items()
            }
            yield mapper.end_vertex(b, n.display_name, n.class_name, shown)

    def _gather(self, nid: str, values: dict[str, dict[str, Any]]) -> dict[str, list[Any]]:
        got: dict[str, list[Any]] = {}
        for e in self._incoming(nid):
            if e.source_output in _CONFIG_OUTPUTS:
                continue
            if registry.is_feedback_edge(self.imported.nodes[nid].kind, e.target_field):
                continue
            src = values.get(e.source, {})
            if e.source_output not in src:
                raise FlowBuildError(
                    f"{e.source}.{e.source_output} produced no value for {nid}.{e.target_field}"
                )
            got.setdefault(e.target_field, []).append(src[e.source_output])
        return got

    async def _call_agent(
        self,
        node: FlowNode,
        agent: Agent[Any],
        prompt: str,
        session_key: str | None,
        store: SessionStore | None,
        message_id: str | None,
        mapper: FlowEventMapper[Any],
        out: _QueueSink,
        streamed: set[str],
        stream_to: str | None,
        run_results: dict[str, RunResult[Any]],
    ) -> RunResult[Any]:
        final: RunResult[Any] | None = None
        async for ev in self.runner.stream_async(
            agent,
            prompt,
            session_id=session_key if store is not None else None,
            session_store=store if session_key is not None else None,
            stream_text=message_id is not None,
        ):
            out.extend(mapper.from_run_event(node.id, ev, message_id=message_id))
            if ev.type is RunEventType.TEXT_DELTA and stream_to is not None:
                streamed.add(stream_to)
            if ev.type is RunEventType.RUN_COMPLETED:
                final = _result_from_event(ev, agent)
        if final is None:
            raise FlowBuildError(f"{node.display_name}: run did not complete")
        run_results[node.id] = final
        return final

    async def _run_extended(
        self, node: FlowNode, text_in: Callable[..., str], session_id: str | None
    ) -> dict[str, Any]:
        """Data-mode execution of the V3 components (tool mode is built in ``build``)."""
        kind, info = node.kind, self.v3_nodes[node.id]
        if kind == registry.FILE:
            paths = (
                components.file_paths([text_in("path")] if "path" in self._wired(node.id) else None)
                or components.file_paths(self.fields[node.id].get("path"))
                or components.file_paths(self.fields[node.id].get("file_path"))
            )
            if not paths:
                raise ComponentRefusedError(f"File {node.id} has no path")
            text = components.read_files_text(info["root"], paths, self.v3)
            return {"message": text, "data": text, "dataframe": text, "raw_content": text}
        if kind == registry.SPLIT_TEXT:
            f = self.fields[node.id]
            source = (
                (text_in("data_inputs") if "data_inputs" in self._wired(node.id) else "")
                or text_in("data")
                or text_in("input_value")
                or _text_field(f, "data_inputs")
            )
            size = components._positive_int(f.get("chunk_size"), 1000, 100_000)
            raw_overlap = f.get("chunk_overlap")
            overlap = (
                raw_overlap
                if isinstance(raw_overlap, int) and not isinstance(raw_overlap, bool)
                else 200
            )
            sep = f.get("separator")
            chunks = components.split_text(
                source, size, max(0, min(overlap, size - 1)), sep if isinstance(sep, str) else "\n"
            )
            if len(chunks) > self.v3.max_split_chunks:
                raise ComponentRefusedError(
                    f"SplitText {node.id}: {len(chunks)} chunks exceeds {self.v3.max_split_chunks}"
                )
            return {
                "chunks": json.dumps(chunks, ensure_ascii=False),
                "dataframe": json.dumps(chunks, ensure_ascii=False),
                "message": "\n\n".join(chunks),
            }
        if kind == registry.KNOWLEDGE:
            hits = await components.search_knowledge(
                self.v3, info["name"], text_in("search_query"), info["top_k"], session_id
            )
            text = components.format_hits(hits)
            return {"results": text, "dataframe": text, "message": text}
        if kind == registry.MEMORY:
            found = await components.search_memory(text_in("search_query"), info["limit"])
            text = "\n".join(f"{m['role']}: {m['content']}" for m in found.get("matches", []))
            return {"messages": text, "message": text}
        if kind == registry.SQL:
            query = text_in("query")
            result = await components.run_sql(self.v3, query)
            text = json.dumps(result, ensure_ascii=False, default=str)
            return {"run_sql_query": text, "message": text}
        raise FlowBuildError(f"node {node.id} ({node.class_name}) cannot be executed")

    def _wired(self, nid: str) -> set[str]:
        return {e.target_field for e in self.imported.edges if e.target == nid}

    async def _run_node(
        self,
        node: FlowNode,
        gathered: dict[str, list[Any]],
        input_value: str,
        session_id: str | None,
        store: SessionStore | None,
        output_node: str | None,
        mids: dict[str, str],
        mapper: FlowEventMapper[Any],
        run_results: dict[str, RunResult[Any]],
        out: _QueueSink,
        streamed: set[str],
    ) -> dict[str, Any]:
        fields = self.fields[node.id]
        kind = node.kind
        if session_id is not None and store is not None:
            keys = []
            for aid, anode in self.imported.nodes.items():
                if anode.kind == registry.AGENT and aid in self.agents:
                    actx = self.fields[aid].get("context_id")
                    actx = actx.strip() if isinstance(actx, str) and actx.strip() else None
                    keys.append(json.dumps([self.flow_identity, session_id, actx, aid]))
            components.RUN_SCOPE.set(RunScope(session_id, store, keys))
        else:
            components.RUN_SCOPE.set(RunScope(None, None, []))

        def text_in(name: str, default: str = "") -> str:
            if name in gathered:
                return "\n".join(str(v) for v in gathered[name])
            value = fields.get(name)
            return default if value is None else str(value)

        if kind == registry.CHAT_INPUT:
            fallback = fields.get("input_value")
            return {"message": input_value or (str(fallback) if fallback else "")}
        if kind == registry.TEXT_INPUT:
            return {"text": text_in("input_value")}
        if kind == registry.CHAT_OUTPUT:
            return {"message": text_in("input_value")}
        if kind == registry.PROMPT:
            template = str(fields.get("template") or "")
            vals = {v: text_in(v) for v in template_variables(template)}
            return {"prompt": render_template(template, vals)}
        if kind == registry.PARSER:
            if gathered:
                text = "\n".join(str(v) for vs in gathered.values() for v in vs)
            else:
                text = str(fields.get("input_value") or "")
            if str(fields.get("mode", "Parser")).lower() == "stringify":
                parsed = text
            else:
                pattern = str(fields.get("pattern") or fields.get("template") or "{text}")
                parsed = render_template(pattern, {"text": text, "data": text})
            return {"parsed_text": parsed, "parsed": parsed, "message": parsed, "text": parsed}
        if kind in registry.EXTENDED_KINDS:
            return await self._run_extended(node, text_in, session_id)
        # History is scoped by (session_id, context_id, node id). JSON encoding of the triple
        # is injective, so ids containing ":" cannot collide.
        ctx = fields.get("context_id") if kind == registry.AGENT else None
        context = ctx.strip() if isinstance(ctx, str) and ctx.strip() else None
        session_key = (
            json.dumps([self.flow_identity, session_id, context, node.id])
            if session_id or context
            else None
        )
        if kind == registry.AGENT:
            agent = self.agents[node.id]
            override: dict[str, Any] = {}
            if "system_prompt" in gathered or "format_instructions" in gathered:
                prompt = text_in("system_prompt", str(fields.get("system_prompt") or ""))
                if agent.output_schema is not None:
                    prompt = f"{prompt}\n\n{text_in('format_instructions')}".strip()
                override["instructions"] = prompt
            live = agent.clone(**override) if override else agent
            result = await self._call_agent(
                node,
                live,
                text_in("input_value"),
                session_key,
                store,
                mids[output_node] if output_node else None,
                mapper,
                out,
                streamed,
                output_node,
                run_results,
            )
            produced: dict[str, Any] = {"response": result.final_output}
            if agent.output_schema is not None:
                produced["structured_response"] = result.output
            return produced
        if kind in (registry.LANGUAGE_MODEL, registry.STRUCTURED_OUTPUT):
            agent = self.agents[node.id]
            override = {}
            sys_field = "system_message" if kind == registry.LANGUAGE_MODEL else "system_prompt"
            if sys_field in gathered:
                override["instructions"] = text_in(sys_field)
            live = agent.clone(**override) if override else agent
            input_name = (
                "input"
                if kind == registry.STRUCTURED_OUTPUT and "input" in gathered
                else "input_value"
            )
            result = await self._call_agent(
                node,
                live,
                text_in(input_name),
                None,
                None,
                None,
                mapper,
                out,
                streamed,
                None,
                run_results,
            )
            if kind == registry.LANGUAGE_MODEL:
                return {"text_output": result.final_output}
            return {"structured_output": json.dumps(result.output, ensure_ascii=False)}
        raise FlowBuildError(f"node {node.id} ({node.class_name}) cannot be executed")


def _text_field(fields: Mapping[str, Any], name: str) -> str:
    value = fields.get(name)
    return value if isinstance(value, str) else ""


def _result_from_event(ev: RunEvent, agent: Agent[Any]) -> RunResult[Any]:
    d = ev.data
    return RunResult(
        run_id=ev.run_id,
        final_output=str(d.get("output", "")),
        messages=list(d.get("messages") or []),
        usage=d["usage"],
        tool_calls_count=int(d.get("tool_calls_count", 0)),
        turns_count=int(d.get("turns", 0)),
        output=d.get("output_value"),
        last_agent=d.get("last_agent"),
        agent_path=list(d.get("agent_path") or []),
        state=dict(d.get("state") or {}),
    )


def _is_feedback(imported: ImportedFlow, e: FlowEdge) -> bool:
    return registry.is_feedback_edge(imported.nodes[e.target].kind, e.target_field)


def _reach(start: Iterable[str], adj: Mapping[str, Iterable[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(start)
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            stack.extend(adj.get(cur, ()))
    return seen


def _plan_loops(imported: ImportedFlow, keep: set[str]) -> dict[str, tuple[set[str], str | None]]:
    """Loop id -> (body node ids, ``source.output`` of the feedback edge)."""
    out: dict[str, list[str]] = {}
    back: dict[str, list[str]] = {}
    for e in imported.edges:
        if _is_feedback(imported, e) or e.source not in keep or e.target not in keep:
            continue
        out.setdefault(e.source, []).append(e.target)
        back.setdefault(e.target, []).append(e.source)
    plans: dict[str, tuple[set[str], str | None]] = {}
    claimed: set[str] = set()
    for lid in (n for n in keep if imported.nodes[n].kind == registry.LOOP):
        fb = [e for e in imported.edges if e.target == lid and _is_feedback(imported, e)]
        item_edges = [e for e in imported.edges if e.source == lid and e.source_output == "item"]
        if len(fb) > 1:
            raise FlowBuildError(f"Loop {lid}: more than one feedback edge")
        if item_edges and not fb:
            raise FlowBuildError(f"Loop {lid}: `item` is used but nothing feeds back to the Loop")
        if not item_edges:
            plans[lid] = (set(), None)
            continue
        forward = _reach((e.target for e in item_edges), out)
        backward = _reach([fb[0].source], back)
        body = forward & backward
        if fb[0].source not in body:
            raise FlowBuildError(f"Loop {lid}: the feedback edge does not come from the loop body")
        if any(imported.nodes[b].kind == registry.LOOP for b in body):
            raise FlowBuildError(f"Loop {lid}: nested loops are not supported")
        if lid in body or body & claimed:
            raise FlowBuildError(f"Loop {lid}: loop bodies overlap")
        claimed |= body
        plans[lid] = (body, f"{fb[0].source}.{fb[0].source_output}")
    return plans


def _topological(
    imported: ImportedFlow,
) -> tuple[list[str], dict[str, tuple[set[str], str | None]]]:
    """Supported nodes that lead to a ChatOutput, in dependency order (document order ties).

    Feedback edges are not dependencies; a Loop depends on everything its body reads from
    outside the body. Also returns the loop plans (body ids, feedback ``source.output``).
    """
    nodes = {n.id for n in imported.supported_nodes()}
    parents: dict[str, list[str]] = {}
    ordering: dict[str, list[str]] = {}
    for e in imported.edges:
        parents.setdefault(e.target, []).append(e.source)
        if not _is_feedback(imported, e):
            ordering.setdefault(e.target, []).append(e.source)
    keep: set[str] = set()
    stack = [n for n in nodes if imported.nodes[n].kind == registry.CHAT_OUTPUT]
    while stack:
        cur = stack.pop()
        if cur not in keep:
            keep.add(cur)
            stack.extend(parents.get(cur, ()))
    plans = _plan_loops(imported, keep)
    for lid, (body, _fb) in plans.items():
        for b in body:
            for p in ordering.get(b, ()):
                if p not in body and p != lid:
                    ordering.setdefault(lid, []).append(p)
    order: list[str] = []
    done: set[str] = set()
    pending = [n for n in imported.nodes if n in keep]
    while pending:
        progressed = False
        for nid in list(pending):
            if all(p in done or p not in keep for p in ordering.get(nid, ())):
                order.append(nid)
                done.add(nid)
                pending.remove(nid)
                progressed = True
        if not progressed:  # load() rejects cycles; defensive
            raise FlowBuildError("flow graph is not acyclic")
    return order, plans


def build(
    imported: ImportedFlow,
    *,
    models: ModelsArg = None,
    tools: ToolsArg | None = None,
    tweaks: Mapping[str, Any] | None = None,
    variables: Mapping[str, str] | None = None,
    search_provider: SearchProvider | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    v3: V3Options | None = None,
) -> BuiltFlow:
    """Build a runnable ``BuiltFlow``.

    ``models``: see ``models.ModelsArg``. ``tools``: replace a tool node's implementation, keyed
    by node id, display name, class name or kind. ``tweaks``: node id / display name -> fields.
    ``variables``: values for ``load_from_db`` global variables (never read from the flow);
    API-key variables are only required when the provider table builds the model.
    ``search_provider``: backs web search tools (default refuses). ``http_transport``: transport
    for URL tools (for example ``httpx.MockTransport``). ``v3``: options for the extended
    components (see ``V3Options``); only used by flows loaded with ``extended=True``.
    """
    tw = apply_tweaks(imported, tweaks)
    fields = tw.fields
    given = dict(variables or {})
    order, raw_plans = _topological(imported)
    nodes = imported.nodes
    if not any(nodes[n].kind == registry.CHAT_OUTPUT for n in order):
        raise FlowBuildError("flow has no supported ChatOutput node to run")
    missing: list[str] = []
    for nid in order:
        node = nodes[nid]
        for fname in node.from_db:
            if fname in tw.literal[nid] or fname in node.secret_fields:
                continue
            var = str(node.fields[fname])
            if var in given:
                fields[nid][fname] = given[var]
            else:
                missing.append(var)

    incoming: dict[str, list[FlowEdge]] = {nid: [] for nid in order}
    outgoing: dict[str, list[FlowEdge]] = {nid: [] for nid in order}
    for e in imported.edges:
        if e.source in incoming and e.target in incoming:
            incoming[e.target].append(e)
            outgoing[e.source].append(e)
    for e in imported.edges:
        if e.source not in incoming or e.target not in incoming:
            continue
        label = f"edge {e.source}.{e.source_output} -> {e.target}.{e.target_field}"
        if (e.target_field == "model") != (e.source_output == "model_output"):
            raise FlowBuildError(f"{label}: model ports only connect model_output to model")
        if (e.target_field == "tools") != (e.source_output == "component_as_tool"):
            raise FlowBuildError(f"{label}: tools ports only connect component_as_tool to tools")
    for nid in order:
        if (
            nodes[nid].kind == registry.CHAT_OUTPUT
            and not incoming[nid]
            and not fields[nid].get("input_value")
        ):
            raise FlowBuildError(f"ChatOutput {nid} has no connected input")

    passive = {
        nid
        for nid in order
        if nodes[nid].kind in registry.TOOL_KINDS
        or (outgoing[nid] and all(e.source_output in _CONFIG_OUTPUTS for e in outgoing[nid]))
    }

    refs: dict[str, ModelRef] = {}
    for nid in order:
        node = nodes[nid]
        if node.kind not in (registry.AGENT, registry.LANGUAGE_MODEL, registry.STRUCTURED_OUTPUT):
            continue
        key_var = None
        if "api_key" in node.from_db and "api_key" not in tw.literal[nid]:
            key_var = str(node.fields["api_key"])
        refs[nid] = extract_model_ref(nid, node.display_name, fields[nid], key_var, given)
    for nid in list(refs):
        for e in incoming[nid]:
            if e.target_field == "model":
                if nodes[e.source].kind != registry.LANGUAGE_MODEL:
                    raise FlowBuildError(f"{nid}: model input must come from a language model node")
                refs[nid] = refs[e.source]
    for ref in refs.values():
        if (
            needs_default_mapping(ref, models)
            and ref.api_key_variable
            and ref.api_key_variable not in given
        ):
            missing.append(ref.api_key_variable)
    if missing:
        raise MissingVariablesError(missing)

    def override_for(node: FlowNode) -> Tool | None:
        if not tools:
            return None
        for key in (node.id, node.display_name, node.class_name, node.kind or ""):
            if key in tools:
                chosen = tools[key]
                return chosen(node) if callable(chosen) and not isinstance(chosen, Tool) else chosen
        return None

    agents: dict[str, Agent[Any]] = {}
    built_tools: dict[str, Tool] = {}
    opts = v3 or V3Options()
    if any(nodes[n].kind == registry.KNOWLEDGE for n in order):
        opts = components.knowledge_defaults(opts)
    v3_nodes: dict[str, dict[str, Any]] = {}
    for nid in order:
        node = nodes[nid]
        f = fields[nid]
        kind = node.kind
        if kind not in registry.EXTENDED_KINDS:
            continue
        info: dict[str, Any] = {}
        as_tool = any(e.source_output == "component_as_tool" for e in outgoing[nid])
        wired = {e.target_field for e in incoming[nid]}
        if kind == registry.FILE:
            if opts.files_root is None:
                raise FlowBuildError(f"File node {nid} needs build(v3=V3Options(files_root=...))")
            info["root"] = Path(opts.files_root).resolve()
            has_path = components.file_paths(f.get("path")) or components.file_paths(
                f.get("file_path")
            )
            if not has_path and not wired & {"path", "file_path"}:
                raise FlowBuildError(f"File node {nid} has no path")
        elif kind == registry.KNOWLEDGE:
            if opts.embedder is None:
                raise FlowBuildError(f"Knowledge node {nid} needs V3Options(embedder=...)")
            raw_name = f.get("knowledge_base") or f.get("knowledge_base_name") or f.get("kb_name")
            info["name"] = components.knowledge_name(raw_name, nid)
            info["top_k"] = components._positive_int(f.get("top_k"), 5, opts.max_top_k)
            if as_tool:
                built_tools[nid] = components.knowledge_tool(opts, info["name"], info["top_k"])
        elif kind == registry.MEMORY:
            info["limit"] = components._positive_int(f.get("n_messages"), 5, 50)
            if as_tool:
                built_tools[nid] = components.memory_tool(info["limit"])
        elif kind == registry.SQL:
            if not opts.sql_connection:
                raise FlowBuildError(f"SQL node {nid} needs V3Options(sql_connection=...)")
            fixed = f.get("query")
            if as_tool:
                built_tools[nid] = components.sql_tool(
                    opts, fixed if isinstance(fixed, str) and fixed.strip() else None
                )
        v3_nodes[nid] = info
    loops = {
        lid: LoopPlan(lid, [n for n in order if n in body], tuple(fb.split(".", 1)) if fb else None)  # type: ignore[arg-type]
        for lid, (body, fb) in raw_plans.items()
    }
    loop_body = {b for plan in loops.values() for b in plan.body}
    for nid in order:
        node = nodes[nid]
        f = fields[nid]
        kind = node.kind
        if kind in registry.EXTENDED_KINDS:
            continue
        if kind in registry.TOOL_KINDS:
            chosen = override_for(node)
            if chosen is None:
                if kind == registry.CALCULATOR:
                    chosen = calculator_tool()
                elif kind == registry.URL:
                    chosen = url_tool(f.get("urls"), http_transport)
                else:
                    chosen = web_search_tool(search_provider)
            built_tools[nid] = chosen
        elif kind in (registry.AGENT, registry.LANGUAGE_MODEL, registry.STRUCTURED_OUTPUT):
            model = resolve_model(refs[nid], models)
            name = _agent_name(node)
            if kind == registry.LANGUAGE_MODEL:
                agents[nid] = Agent(name, str(f.get("system_message") or ""), model)
            elif kind == registry.STRUCTURED_OUTPUT:
                schema = _schema_from_table(f.get("output_schema"), nid)
                if schema is None:
                    raise FlowBuildError(f"StructuredOutput {nid} has no output schema rows")
                instructions = str(f.get("system_prompt") or "Extract the requested fields.")
                agents[nid] = Agent(name, instructions, model, output_type=schema)
            else:
                agent_tools: list[Tool] = []
                for e in incoming[nid]:
                    if e.target_field != "tools":
                        continue
                    if e.source in built_tools:
                        agent_tools.append(built_tools[e.source])
                    elif e.source in agents:
                        agent_tools.append(
                            agents[e.source].as_tool(name=agents[e.source].name.lower())
                        )
                    else:
                        raise FlowBuildError(f"{nid}: tool source {e.source} is not a tool")
                present = {t.name for t in agent_tools}
                if f.get("add_calculator_tool") is True and "calculator" not in present:
                    agent_tools.append(calculator_tool())
                if f.get("add_current_date_tool") is True:
                    agent_tools.append(current_date_tool())
                schema = _schema_from_table(f.get("output_schema"), nid)
                kwargs: dict[str, Any] = {}
                n_msgs = f.get("n_messages")
                if isinstance(n_msgs, int) and not isinstance(n_msgs, bool) and n_msgs >= 1:
                    kwargs["history_processors"] = [keep_last_turns(n_msgs)]
                max_it = f.get("max_iterations")
                if isinstance(max_it, int) and not isinstance(max_it, bool) and max_it >= 1:
                    kwargs["max_turns"] = max_it
                instructions = str(f.get("system_prompt") or "")
                if schema is not None and f.get("format_instructions"):
                    instructions = f"{instructions}\n\n{f['format_instructions']}".strip()
                agents[nid] = Agent(
                    name, instructions, model, tools=agent_tools, output_type=schema, **kwargs
                )
    for e in imported.edges:
        if (
            e.source_output == "structured_response"
            and e.source in agents
            and agents[e.source].output_schema is None
        ):
            raise FlowBuildError(f"{e.source}: structured_response used without an output schema")
    warnings = list(imported.report.warnings)
    for u in imported.report.unsupported:
        if u.in_main_path:
            warnings.append(f"unsupported node {u.id} ({u.class_name}) skipped: {u.reason}")
    warnings.extend(f"edge dropped: {x}" for x in imported.report.unsupported_edges)
    return BuiltFlow(
        imported=imported,
        fields=fields,
        order=order,
        passive=passive,
        agents=agents,
        tools=built_tools,
        model_refs=refs,
        warnings=warnings,
        v3=opts,
        v3_nodes=v3_nodes,
        loops=loops,
        loop_body=loop_body,
    )


async def run_flow_async(
    flow: ImportedFlow | BuiltFlow,
    input_value: str = "",
    *,
    session_id: str | None = None,
    session_store: SessionStore | None = None,
    **build_kwargs: Any,
) -> str:
    """Run a flow once and return the ChatOutput text. ``build_kwargs`` go to ``build``."""
    built = flow if isinstance(flow, BuiltFlow) else build(flow, **build_kwargs)
    result = await built.run_async(input_value, session_id=session_id, session_store=session_store)
    return result.output


def run_flow(
    flow: ImportedFlow | BuiltFlow,
    input_value: str = "",
    *,
    session_id: str | None = None,
    session_store: SessionStore | None = None,
    **build_kwargs: Any,
) -> str:
    """Blocking ``run_flow_async``."""
    return asyncio.run(
        run_flow_async(
            flow, input_value, session_id=session_id, session_store=session_store, **build_kwargs
        )
    )


def stream_flow_events[E](
    flow: ImportedFlow | BuiltFlow,
    input_value: str = "",
    *,
    session_id: str | None = None,
    session_store: SessionStore | None = None,
    mapper: FlowEventMapper[E],
    **build_kwargs: Any,
) -> AsyncIterator[E]:
    """Async iterator of one run's events as ``mapper`` shapes them (``build_kwargs`` go to
    ``build``). ``mapper`` is required; see ``FlowEventMapper``."""
    built = flow if isinstance(flow, BuiltFlow) else build(flow, **build_kwargs)
    return built.stream_events(
        input_value, session_id=session_id, session_store=session_store, mapper=mapper
    )
