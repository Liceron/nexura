# Agents in Studio

Studio users can build their own agents and use them in workflows, the chat playground,
evaluations and red-team runs. A saved agent is a workspace resource: a declarative
`AgentSpec` (JSON, no code) stored in the service database, with a revision number.

The workflow node `nexura.agents.agent` runs a saved agent of the run's own workspace. The
older node `nexura.agents.coordinator` is unchanged; it is the offline demo (a canned support
agent that needs no model).

## The spec

Everything in a spec is a reference or text. Nothing in it is evaluated, and it cannot hold
provider strings or keys.

| Field | Meaning | Limit |
| --- | --- | --- |
| `name` | Same rule as skill names: lowercase letters, digits, `_`, `-`; unique in the workspace | 1-64 characters |
| `description` | Short text, no control characters other than tab and newline | 500 characters |
| `instructions` | The agent's instructions | 20,000 characters |
| `model` | A name from the server's chat model allowlist (`GET /api/v1/chat/models`); default `fake-deterministic`, the offline fixture | must be configured |
| `tools` | Module ids from the app registry, exposed with `module_tool` | 20, no duplicates |
| `knowledge` | Knowledge collection ids of the workspace, searched by one `knowledge_search` tool | 10 |
| `skills` | Workspace skill names, read at `skills_label` (default `production`) when the agent runs | 20 |
| `output_schema` | Optional JSON schema for the final answer (the subset `nexura_agents` supports for dict `output_type`) | 16,000 characters; `minLength` at most 2,000 and `minItems` at most 50 |
| `limits` | `max_turns` (1-20, default 6), `max_tool_calls` (0-50, default 10), `timeout_seconds` (1-300, default 60) | |

Unknown fields are refused. The whole spec is also capped at 64,000 JSON characters.

When an agent is saved, and again every time it runs, the service checks each name against
the workspace and the server:

- the model must be in the allowlist (unknown names: `MODEL_NOT_AVAILABLE`);
- each tool must be a registered module that this workspace can see, is available on this
  server, is not flagged `internal` and is not itself an agent node. Model adapters
  (`nexura.models.*`) and modules that call a provider with a server API key (for example
  `nexura.speech`) are refused: the agent's model comes only from `model`, so a tool cannot
  choose an unlisted (paid) model or send the server's key to a URL the model picks;
- each knowledge collection and each skill must belong to the workspace; at run time the
  skills must also carry `skills_label`.

Any tool whose module can write is forced to `requires_approval=True`: a module with
`write_operations` or `always_write`, one the server gates (`approval_required_handlers`),
or any `effect_policy` other than `pure` or `read`. Its calls never run without the
approval flow.

## Routes

All under `/api/v1`. Errors are `{"detail": {"code", "message"}}`; a refused spec adds
`field_errors` (`[{"field", "message"}]`).

| Route | Role | Notes |
| --- | --- | --- |
| `GET /agents` | viewer | Summaries (name, description, model, `tool_count`, `tools`, `approval_tools`: the tool module ids whose calls need approval, revision) plus server `limits` |
| `GET /agents/options` | viewer | Models, attachable tools (with `requires_approval`), collections, skills, limits |
| `POST /agents` | editor | Body: the spec. 201 with the record (revision 1) |
| `GET /agents/{agent_id}` | viewer | Record: `agent_id`, `revision`, `spec`, timestamps, `tools` (per-tool policy) |
| `PUT /agents/{agent_id}` | editor | Body: the spec plus `expected_revision`; 409 `REVISION_CONFLICT` (with `current_revision`) when stale; the same spec again keeps the revision |
| `DELETE /agents/{agent_id}` | admin | Optional `?expected_revision=` |

Other codes: `AGENT_NOT_FOUND` (404, also for agents of other workspaces),
`AGENT_NAME_TAKEN` (409), `TOO_MANY_AGENTS` (413, 100 agents per workspace),
`EXPECTED_REVISION_REQUIRED` (422). API keys scoped to resources cannot reach these routes.

## The agent node

Config: `agent_id` (required), `input_key` (default `input`), `output_key` (default
`output`). The node reads `state[input_key]` (text, or other JSON sent as JSON text, at most
32,000 characters), builds the agent from the spec for the run's workspace, runs it, and
writes the final answer to `state[output_key]` (the parsed object when the spec has an
`output_schema`). An `agent_id` of another workspace is not found and the node fails. The
node declares `non_idempotent_write`, so it cannot be retried.

When the agent calls a tool that needs approval, the run pauses on the normal approval path
(`status` `pending_approval` in the service, `paused` in the engine) before the call runs.
`pending_approval.details` holds `kind: "agent_tool_calls"`, the agent id, name and revision,
and the exact `calls` (`tool`, `arguments`, `digest`). Approving resumes the agent and runs
exactly those calls; rejecting ends the run and they never run. If the agent was edited in
between, the approved run fails with a message instead of running the old calls.

The pause uses two engine helpers any node can call, `request_approval(details)` and
`approved_request()` from `nexura_runtime.workflow`.

## Offline example

```python
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app

db = Path(tempfile.mkdtemp()) / "service.sqlite3"
app = create_api_app(db_path=db)  # local-owner profile, offline fixture
# Loopback base URL and peer, plus the per-session token on state-changing requests.
client = TestClient(
    app,
    base_url="http://127.0.0.1",
    client=("127.0.0.1", 50000),
    headers={"X-Nexura-Session": app.state.session_token},
)

created = client.post(
    "/api/v1/agents",
    json={"name": "greeter", "instructions": "Greet the user.", "limits": {"max_turns": 3}},
)
assert created.status_code == 201, created.text
agent = created.json()
assert agent["revision"] == 1 and agent["spec"]["model"] == "fake-deterministic"

# Use it in a workflow.
spec = {
    "name": "greet",
    "entry_node": "ask",
    "nodes": {
        "ask": {
            "id": "ask",
            "handler": "nexura.agents.agent",
            "module_id": "nexura.agents.agent",
            "config": {"agent_id": agent["agent_id"], "input_key": "q", "output_key": "a"},
        }
    },
    "terminal_nodes": ["ask"],
}
workflow_id = client.post("/api/v1/workflows", json={"spec": spec}).json()["workflow_id"]
run_id = client.post(
    "/api/v1/runs", json={"workflow_id": workflow_id, "initial_state": {"q": "hello"}}
).json()["run_id"]
run = client.get(f"/api/v1/runs/{run_id}").json()
assert run["status"] == "completed"
print(run["current_state"]["a"])  # Offline fake reply from agent 'greeter': hello

# A stale revision is refused.
stale = client.put(
    f"/api/v1/agents/{agent['agent_id']}",
    json={"name": "greeter", "instructions": "Hi!", "expected_revision": 7},
)
assert stale.status_code == 409 and stale.json()["detail"]["code"] == "REVISION_CONFLICT"
```

The offline fixture is deterministic: it echoes the input, searches knowledge for document
questions when the agent has collections, and with an `output_schema` answers with a minimal
value matching the schema (bounded to about 16,000 characters, so a recursive `$ref` stays
small). It never calls the agent's other tools.

## Write tools pause for approval

This example registers a local write module and a scripted `FakeModel` as a chat model named
`scripted`. The agent asks to create a ticket; nothing is written until a human approves.

```python
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from nexura_agents import FakeModel
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.types import ToolCall

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import ModuleRegistry

created: list[str] = []


class CreateTicket(BaseModule):
    manifest = ModuleManifest(
        module_id="demo.ticket.create",
        version="1.0.0",
        kind="integration",
        title="Create ticket",
        description="Create a support ticket.",
        input_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        effect_policy="non_idempotent_write",
        always_write=True,
    )

    async def invoke(self, context, inputs, config):
        context.require_write_approval("Ticket creation")
        created.append(inputs["title"])
        return ModuleResult.success({"ticket_id": "T-1"})


def scripted(messages, tools):
    if messages[-1].role == "tool":
        return "Ticket filed."
    return [ToolCall(id="c1", name="demo_ticket_create", arguments={"title": "Printer"})]


registry = ModuleRegistry()
registry.register("demo.ticket.create", CreateTicket())
db = Path(tempfile.mkdtemp()) / "service.sqlite3"
app = create_api_app(
    registry=registry, db_path=db, chat_models={"scripted": FakeModel(handler=scripted)}
)
client = TestClient(
    app,
    base_url="http://127.0.0.1",
    client=("127.0.0.1", 50000),
    headers={"X-Nexura-Session": app.state.session_token},
)

agent = client.post(
    "/api/v1/agents",
    json={"name": "filer", "model": "scripted", "tools": ["demo.ticket.create"]},
).json()
assert agent["tools"][0]["requires_approval"] is True

spec = {
    "name": "file",
    "entry_node": "ask",
    "nodes": {
        "ask": {
            "id": "ask",
            "handler": "nexura.agents.agent",
            "module_id": "nexura.agents.agent",
            "config": {"agent_id": agent["agent_id"]},
        }
    },
    "terminal_nodes": ["ask"],
}
workflow_id = client.post("/api/v1/workflows", json={"spec": spec}).json()["workflow_id"]
run_id = client.post(
    "/api/v1/runs", json={"workflow_id": workflow_id, "initial_state": {"input": "help"}}
).json()["run_id"]
run = client.get(f"/api/v1/runs/{run_id}").json()
assert run["status"] == "pending_approval" and created == []
call = run["pending_approval"]["details"]["calls"][0]
print(call["tool"], call["arguments"])  # demo_ticket_create {'title': 'Printer'}

client.post(
    f"/api/v1/runs/{run_id}/decide",
    json={"approval_id": run["pending_approval"]["approval_id"], "decision": "approve"},
)
run = client.get(f"/api/v1/runs/{run_id}").json()
assert run["status"] == "completed" and created == ["Printer"]
print(run["current_state"]["output"])  # Ticket filed.
```

## Playground, evals and red-team

- **Chat:** `POST /api/v1/sessions/{id}/chat` (and `/chat/stream`) accept `agent_id`. The
  agent must be in the caller's workspace and runs on its own model, which must be in the
  allowlist; sending a different `model` is refused (`AGENT_MODEL_FIXED`) and viewers may
  use only the offline fixture. Tool calls that need approval are not run: the model is told
  they need approval, and the response lists them in `refused_tool_calls`. An agent that keeps
  asking for such tools until its turn limit gets `422 AGENT_TURN_LIMIT`. Without `agent_id`
  the playground demo is unchanged.
- **Evals:** `GET /api/v1/evaluations/studio/targets` lists saved agents as
  `{"kind": "agent", "id": "agent:<agent_id>", "version": "rev_<n>", "saved": true}`;
  `POST /evaluations/studio/runs` accepts that id. The agent runs with its real tools, so a
  call that needs approval pauses that case instead of running.
- **Red-team:** `POST /api/v1/evaluations/studio/redteam` accepts `agent_id` instead of
  `workflow_id` (exactly one). The pack uses the agent's instructions with the server's
  red-team model, exactly as for workflows: the offline fake unless the operator set
  `app.state.redteam_model`. The agent's own model is never used here (the pack sends many
  requests, and that model may be a paid one), but it must still be in the allowlist. Every
  tool that can write becomes an inert stub of the same name; other tools are not attached.

## Studio

The Studio pages use the routes above; nothing in them is evaluated, and every name,
description, instruction and argument is shown as plain text.

- **Agents page** (`#/agents`, sidebar "Agents"): the list shows each agent's model, tool count,
  revision and a "need approval" badge when any tool needs approval. The editor has name,
  description, instructions, a model select (the names from `GET /agents/options`), a tool
  checklist with a "needs approval" badge per tool, knowledge collections, skills and the
  skills label, an optional output schema as JSON text and the three limits. It checks the form
  against the server's bounds (`limits` from the options) before saving; refused fields from
  `field_errors` are shown next to the field. Saving sends `expected_revision`; a
  `REVISION_CONFLICT` shows a "Reload agent" button that loads the current revision and
  discards the unsaved edits. Viewers read only, editors create and edit, admins also delete
  (a second click confirms). The role comes from the same membership call as the Skills page.
  With no agents the page says that the model list comes from the operator
  (`NEXURA_CHAT_MODELS`) and that the offline fake (`fake-deterministic`) works without keys.
- **Workflow builder:** the node inspector of a `nexura.agents.agent` node shows a picker of the
  workspace's saved agents (name and revision) and the `input_key` and `output_key` fields
  instead of the raw config form. An `agent_id` that is not a saved agent of the workspace
  (deleted, or from another workspace) stays selected and is marked missing.
- **Approvals:** for `details.kind == "agent_tool_calls"` the approval dialog shows the agent's
  name and revision and each call's tool and arguments (indented JSON text). The `resume`
  snapshot and the call digests are never shown.
- **Playground:** an agent select sits next to the model select. With an agent chosen the turn
  sends `agent_id` and no `model`, and the model select is hidden (the agent runs on its own
  model). Tool calls in `refused_tool_calls` are listed under the transcript; a turn that ends
  with `AGENT_TURN_LIMIT` explains that tools needing approval are not run in chat.
- **Evaluations:** the run target select lists saved agents as "Saved agent: name (rev_n)". The
  Red-team tab's target select lists saved workflows and saved agents; a saved agent is sent as
  `{"agent_id": ...}` and the result shows `agent_id` and `agent_version`.

The browser test `studio-web/tests/e2e/agents.spec.ts` is written for the page, the node picker, a
workflow run and a chat with the offline model, but has not yet completed a run; component tests are in
`studio-web/tests/unit/p26_agents_studio.test.tsx`.

## Limitations

- Tools are attached with an empty module config, so modules that need config (for example
  `nexura.http.request` with `allowed_hosts`) refuse their calls.
- Agents cannot use other agent nodes as tools, and have no handoffs.
- Agent nodes cannot ask for approval inside parallel or mapped steps; the node fails there
  instead.
- The paused agent run is stored with the approval (at most 400,000 JSON characters), and a
  `request_approval` payload is capped at 512,000 characters.
