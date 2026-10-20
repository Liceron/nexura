# Importing Langflow flows (Nexura V2)

`nexura_runtime.integrations.langflow` reads flows saved by Nexura V2 (Langflow 1.12.x JSON)
and runs them on the Nexura SDK. The
importer never executes the component code stored in the flow, never reads API keys from it
and makes no network call unless a tool you allowed does.

The importer, the runner and the export back to Langflow JSON are open source. The V2-shaped
run API (`V2FlowServer`), the V2 SSE event shapes, V2 identity and policy adapters and the
shadow-run migration harness are part of the closed edition.

```text
load(flow)  ->  ImportedFlow (+ ImportReport)  ->  build(imported, models=...)  ->  BuiltFlow
                                                      run_async / run / stream_events(mapper=...)
```

## Offline example

```python
import asyncio
import json

from nexura_agents import FakeModel
from nexura_runtime.integrations.langflow import build, load, run_flow


def node(nid, cls, template=None):
    return {
        "id": nid,
        "type": "genericNode",
        "data": {"id": nid, "type": cls, "node": {"display_name": cls, "template": template or {}}},
    }


def edge(src, out, dst, field):
    return {
        "source": src,
        "target": dst,
        "data": {
            "sourceHandle": {"id": src, "name": out},
            "targetHandle": {"id": dst, "fieldName": field},
        },
    }


agent_template = {
    "model": {"type": "model", "value": [{"name": "gpt-4o-mini", "provider": "OpenAI"}]},
    "system_prompt": {"type": "str", "value": "You are a concise assistant."},
}
flow = {
    "name": "demo",
    "data": {
        "nodes": [
            node("ChatInput-1", "ChatInput"),
            node("Agent-1", "Agent", agent_template),
            node("ChatOutput-1", "ChatOutput"),
        ],
        "edges": [
            edge("ChatInput-1", "message", "Agent-1", "input_value"),
            edge("Agent-1", "response", "ChatOutput-1", "input_value"),
        ],
    },
}

imported = load(flow)  # raises on bad JSON, bad handles, cycles, unsupported nodes
print([n.class_name for n in imported.report.supported])

# Offline: every model step uses a local fake. In production pass a model, a
# mapping or a factory (see "Models and keys").
model = FakeModel(["Hello from the imported flow"])
print(run_flow(imported, "hi", models=model))

built = build(imported, models=FakeModel(["built answer"]))


async def main():
    result = await built.run_async("hi", session_id="demo")
    print(result.output, json.dumps(result.outputs)[:60])


asyncio.run(main())
```

## Supported components

| Langflow class | Becomes |
| --- | --- |
| `ChatInput`, `ChatOutput` | run input (with its session id) and result text |
| `TextInput` | constant text |
| `Prompt` | `{variable}` template; each variable is filled by the edge wired to it, else the template value |
| `ParserComponent`, `parser` | pattern with `{text}` applied to its input (`mode` Stringify passes text through) |
| `LanguageModelComponent` | one plain model call (no tools, no history); its `model_output` can also configure an Agent or StructuredOutput |
| `Agent` | SDK `Agent`: system prompt, model, tools, `n_messages` as history window, `max_iterations` as `max_turns`, `output_schema` rows as structured output |
| `StructuredOutput` | an agent with `output_type` built from the schema table; outputs the JSON as text |
| `CalculatorComponent` | safe arithmetic tool (no names, calls or huge exponents) |
| `URLComponent` | fetch tool using the SDK `web_fetch_tool` (private addresses blocked); domains in the flow's `urls` field are the only ones allowed; with no domains the tool refuses every fetch (`error: refused`) |
| `UnifiedWebSearch` | search tool backed by `search_provider=`; without one it returns a `web_search_not_configured` error |
| note nodes | skipped, listed in the report |

Everything else (File, Memory, Knowledge, SQL, SaveToFile, Loop, BatchRun, hidden bundles,
unknown or custom components, nodes without `data.type`) is reported as unsupported with a
reason unless you load with `extended=True` (next section). A node whose `code` does not match
its recorded `metadata.code_hash` is unsupported (edited component); code with no hash to check
is ignored with a warning.

## Extended components (V3)

`load(flow, extended=True)` also recognises File, MemoryBase, KnowledgeBase/Knowledge,
LoopComponent, SQLComponent and SplitText. They are opt-in so existing callers see no change.
The caller supplies the resources through `build(imported, v3=V3Options(...))`; a flow never
names a path root, a database or an API key itself. Component source code is never run.

| Class | Fields used | Outputs | Becomes |
| --- | --- | --- | --- |
| `File` | `path` or `file_path` (string or list; may be wired) | `message`, `data`, `dataframe`, `raw_content` (all the text) | Reads text files and PDFs under `V3Options.files_root` |
| `MemoryBase` | `search_query`, `n_messages` (match limit, 1-50) | `component_as_tool` (tool `search_memory`), `messages`, `message` | Searches the run's saved session history in the SDK session store |
| `KnowledgeBase`, `Knowledge` | `knowledge_base` (or `knowledge_base_name`, `kb_name`), `top_k`, `search_query` | `component_as_tool` (tool `search_knowledge`), `results`, `dataframe`, `message` | Embeds the query and searches the SDK vector store (`results` is the passage text) |
| `LoopComponent` | `data` (wired or literal) | `item`, `done` | A bounded map: the body runs once per item |
| `SQLComponent` | `query` (a literal fixes the statement), `database_url` (never used) | `component_as_tool` (tool `sql_query`), `run_sql_query`, `message` | One parameterised statement on the connection `V3Options.sql_connection` |
| `SplitText` | `data_inputs`, `chunk_size` (1000), `chunk_overlap` (200), `separator` (newline) | `chunks` and `dataframe` (JSON list), `message` | Character chunker; its `chunks` feed a Loop |

Not used from these classes: any other visible field with a non-empty value is listed in the
report as `option '<name>' is not supported and was ignored`. Secrets never come from a flow:
a literal `database_url` or API key is blanked, and a `load_from_db` reference to a
`database_url` is ignored with a warning (it is not a required variable). `KnowledgeIngestion`,
`SaveToFile`, `BatchRunComponent`, `ArXivComponent`, `AstraDB` and audio components stay
unsupported.

**File.** `files_root` is required. A path with `..` is refused, an absolute path must resolve
(after symlinks) inside the root, and only `.txt .md .markdown .csv .tsv .json .yaml .log` and
`.pdf` are read. A file over `max_file_bytes` (1 MB) is refused before it is read, binary
content is refused, and PDF text goes through the platform's PDF parser (`pypdf`, at most
`max_pdf_pages`). A refusal raises `ComponentRefusedError`.

**Memory.** The tool reads only the history of the current `session_id` (it needs a session
store); another session, or a run with no session, finds nothing. A `session_id` set on the
node is ignored.

**Knowledge.** Needs `V3Options.embedder` (`embed(text) -> list[float]`, sync or async) and uses
`V3Options.vector_store` (default: a fresh `InMemoryVectorStore`). Every search is scoped to
`workspace_id`. With `knowledge_scope="session"` the collection also depends on the run's
session, so one session never reads another's passages. The knowledge base name is limited to
letters, digits and `_ . -`. `ingest_knowledge(opts, name, texts, session_id=...)` fills it.

**Loop.** Items come from a list, a JSON list in text, or one item per non-empty line. The
nodes between `item` and the feedback edge (the edge into the Loop's `item` handle) are the
body. Each item runs the body once with no session history, so items never see each other, and
the feedback edge's value is collected. `done` is those values joined by newlines (the list is
also in `node_outputs[loop]["items"]`). More than `max_loop_items` (100) items raises
`ComponentRefusedError`; items are never truncated. A body vertex is reported once with its
last iteration's values. Nested loops, a loop with `item` consumers but no feedback edge, and
overlapping bodies are build errors.

**SQL.** Read-only by default: the statement must be one SELECT, WITH or VALUES, and the
database connection runs with `query_only`. `INSERT`, `UPDATE`, `DELETE` and `REPLACE` need
`sql_allow_write=True` and a connection registered with `read_only=False`; DDL, `ATTACH`,
`PRAGMA`, `VACUUM` and extension loading are always refused, and so is any second statement.
Values go through bound parameters (`?` or `:name`), never into the text. Rows are capped by
`sql_max_rows` and `sql_max_bytes`. A tool refusal is returned to the model as `{"error":
"refused"}`. Connections come from `SqlConnectionManager`; the flow's `database_url` is never a
DSN.

```python
import asyncio
import json
import tempfile
from pathlib import Path

from nexura_agents import FakeModel
from nexura_runtime.integrations.langflow import V3Options, build, load


def node(nid, cls, template=None):
    return {
        "id": nid,
        "type": "genericNode",
        "data": {"id": nid, "type": cls, "node": {"display_name": cls, "template": template or {}}},
    }


def edge(src, out, dst, field):
    return {
        "source": src,
        "target": dst,
        "data": {
            "sourceHandle": {"id": src, "name": out},
            "targetHandle": {"id": dst, "fieldName": field},
        },
    }


def field(value):
    return {"type": "str", "value": value}


root = Path(tempfile.mkdtemp())
(root / "todo.txt").write_text("write docs\nship it", encoding="utf-8")

flow = {
    "name": "file-loop",
    "data": {
        "nodes": [
            node("File-1", "File", {"path": field("todo.txt")}),
            node("Loop-1", "LoopComponent"),
            node("Prompt-1", "Prompt", {"template": field("TODO: {item}"), "item": field("")}),
            node("ChatOutput-1", "ChatOutput"),
        ],
        "edges": [
            edge("File-1", "message", "Loop-1", "data"),
            edge("Loop-1", "item", "Prompt-1", "item"),
            edge("Prompt-1", "prompt", "Loop-1", "item"),  # feedback into the Loop
            edge("Loop-1", "done", "ChatOutput-1", "input_value"),
        ],
    },
}
imported = load(flow, extended=True)
built = build(imported, v3=V3Options(files_root=root, max_loop_items=10))
print(asyncio.run(built.run_async("go")).output)
```

## Agent-to-agent semantics

Langflow is a dataflow graph, so an Agent `response` wired into another Agent's `input_value`
is a **pipeline step**: the first agent finishes, then its text is the second agent's input
(like `SequentialAgent`). Handoffs are not used, because Langflow has no model-driven
transfer of control. An Agent wired into another Agent's `tools` port becomes an
**agent-as-tool** (`Agent.as_tool`). Nodes run in dependency order, one at a time.

## The import report

`imported.report` holds `supported`, `unsupported` (class, id, reason, whether it is on the
path to a ChatOutput), `notes`, `warnings`, `unsupported_edges` and `required_variables`.
By default an unsupported node on the path to a ChatOutput raises `UnsupportedFlowError`
(its `.report` has the details). `load(flow, strict=False)` keeps the report instead and
`build` runs what it can; dropped nodes and edges appear in `built.warnings`.
Malformed input raises `FlowFormatError` with every problem listed; limits (5 MiB, 1000
nodes, 5000 edges by default) raise `FlowTooLargeError`. Both handle forms are accepted: the
parsed `data.sourceHandle`/`data.targetHandle` objects are preferred, then the string with
its U+0153 quote escapes. A cycle that does not pass through a Loop is rejected.

## Models and keys

`models=` accepts `None` (a provider table: OpenAI, Anthropic, Google, Ollama, Sarvam,
Vertex), one model or spec string for every step, a mapping keyed by node id, display name,
`"provider:name"` or `"*"`, or a factory `fn(ModelRef) -> model`. Unknown providers raise
`ModelMappingError`. Keys are never read from the flow: fields with `load_from_db` name a
variable, and you supply it with `variables={"OPENAI_API_KEY": "..."}`. Missing variables are
all listed in one `MissingVariablesError`. A key variable is required only when the provider
table builds the model; a factory receives it in `ModelRef.api_key` when you supplied it.

## Tweaks

`build(imported, tweaks={"Agent-1": {"system_prompt": "..."}, "Prompt": {...}})` overrides
fields by node id, or by display name when unique. A non-dict value is a global override for
every node that has that field. A literal value replaces a `load_from_db` reference. Refused
(all reported together in `TweakRefusedError.refused`): `code`, database/query fields
(`database_url`, `query`, `sql`, ...), secret fields (use `variables=`), unknown nodes or
fields, ambiguous display names.

## Tools and network

`tools={"CalculatorComponent": my_tool}` replaces a tool node by node id, display name,
class name or kind. `http_transport=httpx.MockTransport(...)` serves URL tools offline and
`search_provider=fn(query)` supplies web search results. Tests should always use these.

## Running

- `run_flow(imported_or_built, "text", session_id=..., session_store=..., **build_kwargs)`
  returns the ChatOutput text (`run_flow_async` for async code).
- `built.run_async(...)` returns a `FlowRunResult` (`output`, `outputs`, `node_outputs`,
  per-node SDK `run_results`). `output_component=` picks one ChatOutput by id or name.
- Sessions: pass `session_id` (plus a `session_store`, or a built-in in-memory store is
  used). Each Agent keeps its own history keyed by the JSON-encoded triple
  `[session_id, context_id, node id]`, so ids containing `:` cannot collide. An Agent's
  literal `context_id` narrows the key and never replaces the caller's session id: distinct
  sessions never share history, even when the flow sets the same `context_id`.
- Streaming: `built.stream_events(..., mapper=...)` and `stream_flow_events(..., mapper=...)`
  yield the events your `FlowEventMapper` builds for each step of the run (`vertices_sorted`,
  `build_start`, `add_message`, `token`, `end_vertex`, `build_end`, `end`, `error`), while a
  node runs, not after it ends. `mapper=` is required: the open-source edition ships no event
  wire format of its own.
- Prompt templates: `{{`/`}}` are un-escaped in the template text only; substituted values
  are inserted verbatim.
- Loops: cycles through a Loop component are tolerated (reported unsupported); any other
  cycle raises `FlowFormatError`. Non-JSON strings that are not an existing file raise
  `FlowFormatError`.

## Exporting back to Langflow

`export_flow(imported, original=...)` writes an `ImportedFlow` back to Langflow 1.12.x JSON so
V2's editor can keep opening it during the transition.

```python
from nexura_runtime.integrations.langflow import export_flow, load


def node(nid, cls, template):
    return {
        "id": nid,
        "type": "genericNode",
        "position": {"x": 10, "y": 20},
        "data": {"id": nid, "type": cls, "node": {"display_name": cls, "template": template}},
    }


original = {
    "name": "demo",
    "data": {
        "nodes": [
            node("TextInput-a", "TextInput", {"input_value": {"type": "str", "value": "hi"}}),
            node("ChatOutput-b", "ChatOutput", {"input_value": {"type": "str", "value": ""}}),
        ],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    },
}
flow = load(original, strict=False)
assert export_flow(flow, original=original) == original  # no edits: identical
flow.nodes["TextInput-a"].fields["input_value"] = "hello"
edited = export_flow(flow, original=original)
assert edited["data"]["nodes"][0]["data"]["node"]["template"]["input_value"]["value"] == "hello"
assert edited["data"]["nodes"][0]["position"] == {"x": 10, "y": 20}
```

How it works: the export starts from a deep copy of `original` and applies only the
difference between your `ImportedFlow` and a fresh `load(original)`. Everything the SDK does not
model (component `code`, `code_hash`, positions, `measured`, viewport, outputs, `selected_output`,
notes, unsupported nodes and their edges, edge ids and handle text) is carried over unchanged.

What can change in the output:

- Template `value` (and `load_from_db`) of fields you edited in `flow.nodes[id].fields`.
- `display_name` of a node, and the top-level `name`/`description` when present in the original.
- Edges you removed from `flow.edges` disappear; nodes you deleted from `flow.nodes` disappear
  with their edges. Edges you added are written with both handle encodings (the parsed
  `data.sourceHandle`/`data.targetHandle` objects and the oe-ligature strings, keys sorted), and
  the output name and target field must exist in the original node's `outputs`/`template`.
- Every exported node, including unsupported pass-through nodes, has its literal secrets
  blanked, never carried over, whether or not `load()` was called with `extended=True`. A field
  is secret when its template entry has `password: true` or `_input_type: SecretStrInput`, when
  its name is `api_key`, `apikey`, `token`, `secret`, `password` or `access_token`
  (case-insensitive), or when it is a connection field (`database_url`, `connection_string`).
  This is why the `f7_tweaks` fixture, which holds a literal `database_url`, round-trips with
  that one value emptied. Variable references (`load_from_db` plus the variable name) are kept.
- An unchanged field is detected type-exactly: `False` vs `0`, `True` vs `1`, `1` vs `1.0` and
  `"1"` vs `1` are all edits.

Refusals (`FlowExportError`, a `LangflowImportError`):

- editing or adding an unsupported node, or wiring an edge to one (unchanged ones pass through);
- writing a literal value into a secret field (use a global variable reference instead);
- an edge whose output or target field is not in the node, or whose endpoint is missing;
- an added edge into a target field with empty `input_types`, or whose source output types
  include none of the target's `input_types` (edges to or from SDK-only nodes, which carry no
  type metadata, are not type-checked);
- deleting a field from `flow.nodes[id].fields` (Langflow components cannot lose template
  fields);
- changing a node's component class, or adding a template field that does not exist;
- a node with no original (SDK-only) unless you pass `allow_sdk_only=True`.

Missing `code`: the SDK never invents component source. Langflow builds a vertex from
`template.code.value` (V2's lfx reads it in graph/vertex param handling and flow validation, and
only the editor's component-update step fills it from the registry), so a node without `code`
may fail to build until the editor updates it. That is why SDK-only nodes are refused by
default. With `allow_sdk_only=True` they are emitted without a `code` field and marked
`metadata.nexura_sdk_only`; this path is unverified against a live Langflow. Without `original`
every node is SDK-only, so `export_flow(flow)` needs the same flag.

## Limits

Not live-tested against real models or against real V2-saved flows. Hand-written fixtures
follow the documented format. One flow run is sequential. The extended components are opt-in
(`extended=True`). File, SQL and Knowledge nodes build only when a `V3Options` is supplied;
without one they refuse to build. Memory, Loop and SplitText always work.

Extended-component caps: SQL results are capped by `sql_max_bytes` while rows are fetched
(SQLite is told to refuse any value over the cap, so a huge blob is never built); a File node's
`max_file_bytes` applies to the sum of all its paths and `max_file_paths` (default 20) caps the
path count; saved history is keyed by a content hash of the flow, so two flows sharing a store
and session id never see each other's messages (this includes the Memory tool). Coverage
numbers count classes, not instances: an instance may set options that are ignored with a
load warning.
