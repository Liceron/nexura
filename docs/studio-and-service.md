# Service and Studio

The MIT open-source edition is **single user, this machine only; for a team or a public endpoint use the hosted or enterprise edition**. This holds when you embed the service too: `create_api_app()` answers 403 `NON_LOOPBACK_CLIENT` to any peer that is not loopback unless you give the explicit insecure opt-in, even under `uvicorn.run(app, host="0.0.0.0")`.

The platform package `nexura-runtime` includes a local REST service and a browser Studio. The service stores workflows and runs in a SQLite file and exposes them under `/api/v1`. Studio is a React app that talks to that API, and `@liceron/nexura-sdk` is a TypeScript client for the same API. All three are optional: the Python SDK works on its own, and a workflow behaves the same whichever way you start it. Both packages are alpha and unpublished, so install from a source checkout.

## One `nexura` command

The runtime declares `nexura.commands` entry points for `serve`, `worker`, `workflow`,
`repro`, `redteam`, `evals` and `sandbox`; the `nexura-studio` package declares `studio`.
Once the Nexura Cloud client supports this group, `nexura serve` and the other
subcommands work through that single command. Until then, use `nexura-runtime`
(or `nexura-studio` for Studio).

## Try the API without a server

You can call the service in-process with `httpx.ASGITransport`, so there is no port and no network. This needs the `studio` extra (FastAPI). A new database is seeded with a demo workflow, `wf-order-support`, which pauses for approval on a refund order.

```python
import asyncio
import os
import tempfile

import httpx

from nexura_runtime.service.api.app import create_api_app


async def main() -> None:
    db = os.path.join(tempfile.mkdtemp(), "service.sqlite3")
    app = create_api_app(db_path=db, profile="local-owner")
    transport = httpx.ASGITransport(app=app)
    # Loopback base URL; state-changing requests carry the per-session token (see below).
    session = {"X-Nexura-Session": app.state.session_token}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers=session
    ) as client:
        caps = (await client.get("/api/v1/capabilities")).json()
        assert caps["active_profile"] == "local-owner"
        assert caps["supported_profiles"] == ["local-owner"]

        listing = (await client.get("/api/v1/workflows")).json()
        assert "wf-order-support" in [w["workflow_id"] for w in listing["items"]]

        started = await client.post(
            "/api/v1/runs",
            json={
                "workflow_id": "wf-order-support",
                "initial_state": {"order_id": "ORD-DELHI-501"},
            },
        )
        run = started.json()
        assert run["status"] == "pending_approval"

        decision = {"approval_id": run["pending_approval"]["approval_id"], "decision": "approve"}
        decided = await client.post(f"/api/v1/runs/{run['run_id']}/decide", json=decision)
        assert decided.json()["status"] == "completed"

        # An approval can be used once: a second decision is a conflict.
        again = await client.post(f"/api/v1/runs/{run['run_id']}/decide", json=decision)
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "NOT_PENDING_APPROVAL"


asyncio.run(main())
```

## Run the service

```bash
uv sync --extra studio
uv run nexura-runtime serve --port 8765 --db ./nexura_service.sqlite3
```

The command is `nexura-runtime` (installed by `nexura-runtime`). The bare `nexura` command
belongs to the separate Nexura Cloud client package `nexura`, so this project does not install
a command with that name.

`nexura-runtime serve` takes `--host` (default `127.0.0.1`), `--port` (default `8765`), `--db` (default `./nexura_service.sqlite3`) and `--worker`, which also runs scheduled and webhook jobs (see [Schedules and webhooks](#schedules-and-webhooks)). It starts the `local-owner` profile: one user, no login. Keep it on `127.0.0.1`; do not expose this profile on a network. The open-source edition has one principal, the local owner (role `admin`): no logins, tokens, API keys or workspace members, and `X-Principal-*` headers are ignored. A shared or networked service needs authentication, which is in the Nexura enterprise edition; started on a non-loopback address without it, the service refuses to start unless you pass the explicit insecure opt-in. If `nexura-runtime serve` prints that it needs the `studio` extra, install it as shown above.

### Per-session token

Every request other than GET, HEAD and OPTIONS must send the server's per-session token in the `X-Nexura-Session` header; without it the answer is 403 `SESSION_TOKEN_REQUIRED` (or `SESSION_TOKEN_INVALID` for a wrong or old value). This stops a web page in your browser, including one served by another local tool on another port, from changing anything. `nexura-runtime serve` and `nexura-studio` generate a new random token at every start; set `NEXURA_STUDIO_TOKEN` before starting to use a fixed one.

- Studio reads the token from `GET /api/v1/local-session` on its own origin and sends it for you. The server refuses that read when the browser marks it cross-site or same-site (`Sec-Fetch-Site`) or when its `Origin` is another server. The built-in Studio page (`nexura-studio`) also receives it in the page itself; with `NEXURA_STUDIO_TOKEN` set and a non-local browser (Docker), open the page as `http://127.0.0.1:8765/#token=<your token>`.
- The TypeScript client (`@liceron/nexura-sdk`) and the Python `NexuraClient` read the token from that endpoint when the server refuses a request for it, then retry once; pass `sessionToken` / `session_token` to set it yourself.
- Scripts on this machine can read it the same way (`curl http://127.0.0.1:8765/api/v1/local-session`). An app that embeds `create_api_app()` finds it on `app.state.session_token`.
- State-changing requests must be JSON: `text/plain`, form and multipart bodies are answered with 415. Raw Python source sent to `/api/v1/workflows/import/python` with `Content-Type: text/plain` by a non-browser client is still accepted. Signed webhook deliveries (`/api/v1/triggers/webhooks/{id}/deliver`) need no token.

`GET /api/v1/capabilities` reports `supported_profiles: ["local-owner"]` in OSS. The shared profile requires an installed authenticating provider from the hosted or enterprise edition; selecting its name alone fails closed.

Requests can carry an `X-Workspace-ID` header to choose a workspace. Errors come back as JSON with a `detail` object that has a `code` and a `message`.

The service covers these areas under `/api/v1`:

| Area | Paths |
|---|---|
| Workflows | `/workflows` (list, create, get, update, delete, validate, export, import) |
| Runs | `/runs` (start, get, list, cancel, decide an approval, `/{run_id}/input`, `/{run_id}/history`, `/{run_id}/fork`, `/{run_id}/subruns`, `/{run_id}/effects`, `/events` as server-sent events, `/{run_id}/diagnostics`) |
| Chat sessions | `/sessions`, `/sessions/{id}/chat`, and `/sessions/{id}/chat/stream` for streamed turns |
| Modules and capabilities | `/modules`, `/capabilities` |
| Knowledge | `/knowledge/collections`, `/documents`, `/retrieve`, `/answer` |
| Credentials | `/credentials` (create, list, get, delete, `/verify`, `/revoke`) |
| Integrations | `/integrations` (MCP and OpenAPI discover and test-run, register) |
| Evaluations | `/evaluations` (datasets, runs, compare) |
| Triggers | `/triggers` (schedules, webhooks, jobs, outbox) |
| Workspaces | `/workspaces` |

Saving a workflow that someone else changed first returns HTTP 409 with code `REVISION_CONFLICT`, so you can reload or save a copy. Chat streaming sends ordered events for each model turn and tool call, then the same body as the non-streaming endpoint. Reply text arrives whole, not token by token, and a turn that is cancelled or disconnected is not saved. The chat endpoints answer with a local deterministic `FakeModel`, not a real model, so the playground needs no keys and shows no live model output. The playground also has a `knowledge_search` tool over the caller's workspace knowledge, so answers about your documents carry real citation footnotes (see [Knowledge](knowledge.md)).

## Run Studio

Studio lives in `studio-web/`. It is a private app served by Vite, not a published package.

```bash
cd studio-web
npm ci
npm run dev
```

The dev server proxies `/api` to `http://127.0.0.1:8765`. If your service is elsewhere, set `VITE_BACKEND_URL`. If Studio shows "Unable to connect", the service is not running or that variable points at the wrong place.

Today Studio has these sections: workflows (a graph builder with undo history, validation, import and export, sub-workflow nodes through the composite inspector), templates (the starter workflows), approvals (an inbox for pending approvals and a decision dialog), runs (a timeline, a state replayer and diagnostics, fed by the event stream), a chat playground, modules, knowledge, credentials, tools (MCP and OpenAPI integrations), evaluations (evals and benchmarks), triggers, run traces (a span tree per run), onboarding and system status. The interface is available in English and Hindi.

In the builder, "+ Add Step" opens the module drawer. Its categories follow the module kinds that `GET /api/v1/modules` reports: channels are the `nexura.channel.*` modules, Connectors are the 15 `nexura.connectors.*` integrations, and Tools & APIs holds tools plus the other integrations (HTTP, SQL, speech, MCP). A descriptor with `"internal": true` is a service-internal module (`nexura.service.triggers`, `nexura.observability`, `nexura.evaluation.runner`, `nexura.runtime.approvals`). The drawer does not offer these as new steps. They stay registered, so saved workflows that use them still validate and run. A module sets the flag with `ModuleManifest(internal=True)`. The flag is not part of the manifest digest. The sidebar badge on Approvals shows the number of runs waiting for a decision. It refreshes every 30 seconds while Studio is connected and the tab is visible.

Every run has its own page at `#/runs/{run_id}`: the full Run Console (approve or reject, answer an input request, history and fork, sub-runs, diagnostics, trace, Save as test) and a link back to its workflow. It works after a reload and for runs started by a trigger, the API or another local process. An unknown run, or one from another workspace, shows a notice instead. A run opened on a workflow page is kept in the URL as `#/workflows/{workflow_id}/runs/{run_id}`, so a reload reopens it in the Run Console tab. The workflow page has a Runs tab: the workflow's runs, newest first, 20 per page, with a status filter (`running`, `pending_approval`, `waiting_for_input`, `completed`, `failed`, `rejected`, `cancelled`, `interrupted`). "Open run" links lead to the run page from that tab, the run traces list and trace page, Approvals inbox items, trigger jobs that have a `run_id`, and workflow-test and eval case results saved from a run (`metadata.source_run_id`). The Approvals inbox also has a "Waiting for input" section that lists runs with status `waiting_for_input`; you answer on the run page. The sidebar badge still counts approvals only. The run traces list filters by the same statuses and pages 50 runs at a time.

In the triggers view, the schedule and webhook forms pick the target workflow from the workspace's workflows. There is no free-text workflow id and no default id; a workspace without workflows says so and the forms cannot be submitted. The Triggers button on a workflow page opens `#/triggers/{workflow_id}`: the triggers view lists only that workflow's schedules and webhooks (with a link back to all triggers), and both forms start with that workflow selected. After a webhook is registered, Studio shows the signing secret once, with a Copy button, the delivery URL (`POST /api/v1/triggers/webhooks/{endpoint_id}/deliver`), the three headers (`X-Webhook-Timestamp`, `X-Webhook-Event-ID`, `X-Webhook-Signature`) and curl and Python snippets that sign with the v2 scheme described under [Schedules and webhooks](#schedules-and-webhooks). The secret is kept only in the page's memory. Studio does not write it to local or session storage, the URL or the console, and it is gone after you dismiss the panel, leave the triggers view or switch workspace. The service never returns it again, so if it is lost, delete the webhook and register it again. The snippets read the secret from the `NEXURA_WEBHOOK_SECRET` environment variable and do not contain it, so each webhook card keeps showing its delivery URL and a "How to sign deliveries" section after the secret is dismissed.  The test endpoint picker and the signing section follow the workflow filter: opened from a workflow page they offer only that workflow's webhooks. "Show all triggers" lifts the filter without leaving the view, so a secret still on screen stays there. A workflow id in `#/triggers/{workflow_id}` that is not in the workspace's (complete) workflow list is neither offered nor preselected. The delivery URL is built from the address Studio uses for the service. Under `npm run dev` that is the Vite origin, which proxies `/api` to the service. A sender outside your machine needs an address that reaches the service itself.

There is also an older built-in Studio, started with `uv run nexura-studio --db ./studio.sqlite3` (it defaults to port 8000). It is the separate `nexura-studio` package (`pip install nexura-studio`; the repository's development environment installs it), which also ships its static web page. The REST service needs only the runtime's `studio` extra. New work goes to the REST service and `studio-web`.

## TypeScript client

`clients/typescript` is `@liceron/nexura-sdk`, an HTTP client with no runtime of its own. Build it with `npm install && npm run build` in that directory. Its default `baseUrl` is `http://127.0.0.1:8765`, the port `nexura-runtime serve` uses.

```typescript
import { NexuraClient } from "@liceron/nexura-sdk";

const client = new NexuraClient({ baseUrl: "http://127.0.0.1:8765" });

const run = await client.runs.start({
  workflow_id: "wf-order-support",
  initial_state: { order_id: "ORD-DELHI-501" },
});

if (run.status === "pending_approval" && run.pending_approval) {
  await client.runs.decide(run.run_id, {
    approval_id: run.pending_approval.approval_id,
    decision: "approve",
  });
}
```

The client has `workflows`, `runs`, `modules`, `capabilities`, `workspaces`, `sessions`, `knowledge`, `credentials`, `integrations`, `evaluations` (including `evaluations.studio`), `triggers`, `agents`, `skills`, `templates` and `builder`. It covers only the routes the open-source service serves: clients for the closed edition's hosted routes (organisations, publishing, the hosted secrets admin API and workspace members) ship with the closed edition, for TypeScript and Python alike. `runs.streamEvents()` and `sessions.chatStream()` are async generators for the event streams. An error response throws `NexuraApiError` with `statusCode`, `code` and `details`. A request validation error (FastAPI's 422 list) has code `VALIDATION_ERROR`, a `field: message` text and the original list in `details`. `workspaceId` is sent as `X-Workspace-ID`. The optional `apiKey` client setting is for a hosted or enterprise server; it adds no authentication to OSS.

## Interrupts, history, forks and sub-runs

A handler that calls `interrupt()` pauses the run with status `waiting_for_input`. The run record carries `pending_input`, which is `{"interrupt_id", "node_id", "value"}`. This is different from `pending_approval`, which is only set for approval gates.

```bash
# Start a run; if a handler calls interrupt() the response has status "waiting_for_input".
curl -s -X POST http://127.0.0.1:8765/api/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"workflow_id": "wf-draft-review", "initial_state": {"draft": "Hello"}}'

# Answer it. The value can be any JSON; the run continues and may pause again.
curl -s -X POST http://127.0.0.1:8765/api/v1/runs/RUN_ID/input \
  -H 'Content-Type: application/json' \
  -d '{"interrupt_id": "INTERRUPT_ID", "value": {"approved": true}}'

# Every saved checkpoint of the run, oldest first (state is redacted for credential keys).
curl -s http://127.0.0.1:8765/api/v1/runs/RUN_ID/history

# Start a NEW run from checkpoint 1 with a changed state; returns 201 and the new run.
curl -s -X POST http://127.0.0.1:8765/api/v1/runs/RUN_ID/fork \
  -H 'Content-Type: application/json' \
  -d '{"checkpoint_index": 1, "state_update": {"bonus": 10}}'

# Latest checkpoint of each child run started by sub-workflow nodes.
curl -s http://127.0.0.1:8765/api/v1/runs/RUN_ID/subruns
# One child run (key from the list above): status, steps, redacted history.
curl -s http://127.0.0.1:8765/api/v1/runs/RUN_ID/subruns/CHILD_KEY
```

- `POST /runs/{run_id}/input` needs the `approve` permission, like `/decide`. It returns `409 NOT_WAITING_FOR_INPUT` if the run is not waiting, `404 INTERRUPT_NOT_FOUND` for a different interrupt id, and `409 ALREADY_ANSWERED` if another caller answered first. The answer is recorded as an `INPUT_PROVIDED` event with credential-like fields redacted.
- `GET /runs/{run_id}/history` returns `{"items": [checkpoint, ...]}`. A checkpoint has `state`, `current_node_id`, `step_count`, `status`, `interrupt` and `forked_from`.
- `POST /runs/{run_id}/fork` needs the `run` permission and runs synchronously. The new run belongs to the caller's workspace and its record has `forked_from`. The source run is not changed. A bad or out-of-range `checkpoint_index`, or the final checkpoint of a run, returns `400`; a run of another workspace returns `403 CROSS_WORKSPACE_ACCESS_DENIED` (an unknown run `404`); a checkpoint from a different workflow spec returns `409`. Approval gates after the fork point ask again.
- `GET /runs/{run_id}/subruns` returns `{"items": [...]}`: the latest checkpoint of every child run whose id starts with `<run_id>/`, in natural id order (numbers inside an id compare as numbers, so `[2]` comes before `[10]`). It takes `limit` (default 50, 1 to 200) and `offset` and also returns `total`, `limit`, `offset` and `has_more`, like `GET /runs`. A run without sub-workflows returns an empty list. An unknown `child_key` is `404 RUN_NOT_FOUND` naming the key.
- `GET /runs/{run_id}/trace` returns a span tree for the run: `run_id`, `status`, `include_content`, `truncated`, `span_count`, `duration_ms` and a flat `spans` list linked by `parent_id` (a root `run` span, one `node` span per workflow node attempt with status, timing, tokens, cost, model and module, and child spans for model calls, tool calls, retries and handoffs inside agent nodes). The only query parameter is `include_content` (default `false`); inputs, outputs and errors are included, redacted, only when it is `true`, and are always dropped for runs older than the diagnostic retention window. An unknown run returns `404 RUN_NOT_FOUND`. Studio shows it under Run Traces.
- All of these return `403 CROSS_WORKSPACE_ACCESS_DENIED` for a run owned by another workspace and `404 RUN_NOT_FOUND` for an unknown run.
- `GET /runs/{run_id}/effects` returns `{"effects": [...]}`: the run's journaled external effects (`effect_id`, `intent` with its `idempotency_key` and `effect_class`, `status` `pending`/`confirmed`/`indeterminate`/`resolved`, `resolution`, `result` with credential-like fields redacted, `resolved_by`). Only executors that write the execution journal record effects (today `DurableExecutionEngine` sharing the service database); other runs return an empty list. `POST /runs/{run_id}/effects/{effect_id}/resolve` with `{"resolution": "confirmed" | "not_applied" | "failed", "result": ...}` settles an `indeterminate` effect once. It needs the `approve` permission and records the authenticated caller as `resolved_by` (the body cannot name an actor). It returns `409 EFFECT_ALREADY_RESOLVED` the second time, `409 EFFECT_NOT_INDETERMINATE` for another status, `409 RUN_LEASE_HELD` while a worker holds the run and `409 RUN_FINISHED` for a finished run; an unknown effect is `404 EFFECT_NOT_FOUND`. Resolving runs nothing: the run continues when its executor next claims it. A journaled run is looked up in the caller's workspace, so another workspace's run is `404 RUN_NOT_FOUND`. See [Durable execution](durable-execution.md).

The TypeScript client has matching methods:

```typescript
const waiting = await client.runs.start({ workflow_id: "wf-draft-review" });
if (waiting.status === "waiting_for_input" && waiting.pending_input) {
  const done = await client.runs.provideInput(
    waiting.run_id,
    waiting.pending_input.interrupt_id,
    { approved: true },
  );
  const { items } = await client.runs.history(done.run_id);
  const fork = await client.runs.fork(done.run_id, {
    checkpointIndex: 1,
    stateUpdate: { bonus: 10 },
  });
  const children = await client.runs.subruns(done.run_id);
}
```

## Schedules and webhooks

Schedules and signed webhooks enqueue durable trigger jobs. A trigger worker turns each job into a normal workflow run: same engine, approval policy, workspace ownership and events as `POST /runs`, with the schedule payload or webhook body as the initial state. The run is owned by the job's workspace and started by the principal `trigger:<schedule|webhook>:<id>`. Without a worker, jobs stay `pending` and nothing runs.

Start the worker inside the service, or as separate processes that share the same SQLite file. Claims are atomic, so `nexura-runtime serve --worker` and any number of `nexura-runtime worker` processes can run together and each job runs once.

```bash
nexura-runtime serve --db ./nexura_service.sqlite3 --worker --poll-interval 1
# or, in another terminal (add --once to process due jobs and exit):
nexura-runtime worker --db ./nexura_service.sqlite3 --poll-interval 1
```

Each run in `running` records the process executing it (host name, process id and the process start time). When a `serve` or `worker` process opens the database it marks a `running` run `interrupted` ("Run was interrupted by server restart or shutdown.") only if that process is on the same machine and has exited. A process id reused by a newer process, or one from before a reboot, counts as exited. Runs of a process that is still alive are left alone, so starting a worker, or restarting `serve` while a worker runs, does not interrupt another process's runs. Runs recorded by a database from before this change have no owner and are still marked `interrupted` on open. Runs owned by another machine are never marked, so share a SQLite file between processes on one machine only. On systems without `/proc` the check is weaker: macOS checks only the process id, so a reused process id keeps a dead run in `running`, and Windows treats every owning process as alive, so runs there are marked `interrupted` only by a clean shutdown. A service that shuts down cleanly still marks its own unfinished runs `interrupted` ("Run was interrupted by server shutdown.").

Create a schedule that fires every 60 seconds (`cron_expr` with `timezone` also works). `release_id` pins what may run: `current` (default, no pin), a revision such as `rev:3`, or the workflow's spec hash. If the workflow has changed since, the job fails with a clear error and no run starts. Jobs whose workflow is missing or belongs to another workspace fail the same way.

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/triggers/schedules \
  -H 'Content-Type: application/json' \
  -d '{"schedule_id": "nightly", "workflow_id": "wf-123", "interval_seconds": 60,
       "payload": {"msg": "hello from the schedule"}}'
```

Register a webhook, then sign and deliver an event. Webhooks registered through the API use signature scheme v2: HMAC-SHA256, hex encoded, over `v2.<integer unix timestamp>.<event id>.` followed by the canonical JSON body (keys sorted, separators `,` and `:`, no spaces). The event id is part of the signed string, so a captured delivery cannot be replayed under a different `X-Webhook-Event-ID`. Event ids are 1-200 characters of `[A-Za-z0-9_:-]` (no dots). The legacy v1 signature (`v1.<timestamp>.<body>`, no event id) is rejected for API-registered endpoints; it is only still accepted for endpoints that existed before v2 and for direct `TriggerManager` use. Deliveries older than the replay window (300 seconds by default) are rejected, and a repeated `(endpoint, X-Webhook-Event-ID)` pair is accepted but creates no second job; ids are scoped per endpoint. The body must be a JSON object. The signing secret is returned only when the webhook is registered and is stored encrypted at rest (key from `NEXURA_TRIGGER_SECRET_KEY`, or a 0600 `<database>.trigger.key` file next to the database). An endpoint id or schedule id already used by another workspace returns 409. Registering a webhook requires the `edit` and `run` permissions, deleting and scheduling require `edit`, and `POST /webhooks/{id}/test` requires `admin` and does not return the computed signature. The signature, replay window and body limit (1 MB) apply in every profile. `openssl -hmac` and `curl -H` put the secret on the command line, where other local users can see them in the process list; on a shared machine sign with the Python snippet instead. Schedule `interval_seconds` must be at least 60 by default (set `NEXURA_MIN_SCHEDULE_INTERVAL_SECONDS` to change it).

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/triggers/webhooks \
  -H 'Content-Type: application/json' \
  -d '{"endpoint_id": "orders", "workflow_id": "wf-123", "secret": "change-me"}'

TS=$(date +%s)
BODY='{"msg":"hello from a webhook"}'   # already canonical: sorted keys, no spaces
EID=evt-0001
SIG=$(printf 'v2.%s.%s.%s' "$TS" "$EID" "$BODY" | openssl dgst -sha256 -hmac 'change-me' | awk '{print $NF}')

curl -s -X POST http://127.0.0.1:8765/api/v1/triggers/webhooks/orders/deliver \
  -H 'Content-Type: application/json' \
  -H "X-Webhook-Timestamp: $TS" \
  -H "X-Webhook-Event-ID: $EID" \
  -H "X-Webhook-Signature: $SIG" \
  -d "$BODY"
```


`GET /api/v1/triggers/jobs` lists pending and claimed jobs. After a worker has processed a job it records `status` (`completed` or `failed`), `error_message` and `run_id`. A run that pauses for approval or input counts as a started job: the job is `completed` and the run is `pending_approval` or `waiting_for_input`. A run that ends `failed` marks the job `failed` and keeps its `run_id`. Local limits: the worker uses the handlers registered in its own process, so pass `--handlers package.module:attr` to `nexura-runtime worker` for custom handlers.


### Job leases and crash recovery

A claimed job carries a lease: `lease_expires_at = claimed_at + lease_seconds` (default 300, set with `TriggerWorker(lease_seconds=...)` or `nexura-runtime worker --lease-seconds`). While a run executes, the worker renews the lease every `lease_seconds / 3` and stops when the run finishes. If a worker dies hard (SIGKILL, power loss), its lease expires. At the start of each `run_once`, any worker reclaims expired jobs with an atomic compare-and-swap (SQLite `UPDATE ... WHERE` on the observed owner and lease; a lock in the memory store), so two workers racing on the same job produce exactly one new owner.

A reclaimed job is retried up to `max_attempts` claims in total (default 3, `--max-attempts`; the counter is the persisted `attempts` field). When the attempts are used up the job becomes `failed` with `worker lease expired N times`. The run record the dead worker left in `running` is marked `interrupted` before the retry starts. The retry creates a NEW run: the job's `run_id` is the latest run and `previous_run_ids` lists earlier attempts.

Idempotency: a retried job starts again from the beginning of the workflow, not from the point of failure. Side effects that ran before the crash may run again; approval gates still stop gated handlers until a human approves, so they protect side effects. Design handlers that call external systems to tolerate repeats. A worker that loses its lease (for example after a long stall) cancels its local run and can no longer complete or fail the job. Its run may have produced partial side effects.

Each claimed job runs as its own asyncio task, at most `--max-concurrent-jobs` at a time (default 4), so a long run does not delay schedule ticks or lease recovery. On shutdown the worker waits a grace period (default 20 seconds) for running jobs, then cancels them; a cancelled job is failed with `worker cancelled`. Databases created before leases are migrated by adding nullable columns on open; an old `claimed` job without a lease is treated as expiring one lease period after `claimed_at`, so it is recovered rather than stuck. `nexura-runtime serve --worker` uses the default lease settings.

## Using a real model in chat

The Studio playground and `POST /api/v1/sessions/{id}/chat` (and `/chat/stream`) use the offline
`fake-deterministic` fixture by default. The operator can allow real models; clients then pick one
by name.

```bash
export OPENAI_API_KEY=...          # keys come ONLY from the server environment
nexura-runtime serve --chat-model gpt=openai:gpt-4o-mini --chat-model gem=gemini:gemini-2.5-flash
# or: NEXURA_CHAT_MODELS="gpt=openai:gpt-4o-mini,gem=gemini:gemini-2.5-flash" nexura-runtime serve
```

- `--chat-model NAME=PROVIDER:MODEL` is repeatable; `NEXURA_CHAT_MODELS` holds the same entries
  comma-separated. A CLI entry overrides an environment entry with the same name.
  `PROVIDER:MODEL` is the same string `infer_model` accepts (`openai`, `anthropic`, `gemini`,
  `vertex`, `sarvam`, `ollama`, `litellm`). Programmatic use:
  `create_api_app(chat_models={"gpt": "openai:gpt-4o-mini"}, chat_usage_limits=UsageLimits(...))`
  (values may also be model objects).
- Why an allowlist: requests carry only a configured name (`"model": "gpt"`). Any other value is
  rejected with `400 MODEL_NOT_AVAILABLE` listing the allowed names, so callers cannot choose
  arbitrary providers, endpoints or keys, and no key is ever read from a request.
- Models are resolved lazily on first use. If the key or dependency is missing, only that model
  fails, with `503 MODEL_UNAVAILABLE` (the message never includes key material); the offline
  fixture keeps working.
- `GET /api/v1/chat/models` returns `[{name, provider, label, offline, streaming, default}]`
  (no secrets, no model ids). `fake-deterministic` is always listed and is the default.
- Real models get the playground agent's instructions and tools. Each turn is capped by
  `chat_usage_limits` (default: 10 model requests and 50,000 total tokens per request; exceeding
  it returns `429 USAGE_LIMIT_EXCEEDED`). Usage is what the model reports; unknown stays `null`.
- The response `model` is `"<name> (<provider>)"`. `delivery` is `"streamed"` for `/chat/stream`
  when the model produced text deltas, otherwise `"final_only"`.
- Live use calls a paid provider: it is not covered by the test suite, which uses injected fake
  models only. Try it manually from a shell with your own key.

## Describe a workflow

In Studio, **New Workflow** has a **Describe it** button. Type what you want, for example "a support agent that looks up orders and asks a manager before refunds over 100", and press **Draft**. You get an explanation, a list of things to review and a read-only drawing of the draft graph. The draft is marked unsaved: nothing has been saved or run. Press **Create from draft** to save it through the normal validate-then-create path and open it on the canvas, where you edit it like any other workflow, or **Discard draft** to throw it away.

The assistant calls **your** model, with your provider key. It never uses a key of ours:

- Add a provider credential (OpenAI, Anthropic, Gemini or Sarvam) in Credentials. The draft endpoint uses the workspace's first active provider credential, or the one you name with `credential` (and `model`, to choose the model name).
- On the local service, an operator-configured chat model can be named with `model_ref`. The offline `fake-deterministic` fixture cannot draft.
- With no usable model, `POST /api/v1/builder/draft` returns `400 NO_MODEL_CREDENTIAL`.

```
POST /api/v1/builder/draft
{"description": "...", "credential": "my_openai", "model": "gpt-4o-mini"}
```

The response holds `spec`, `explanation`, `warnings`, `attempts`, `saved: false` and the engine's `validation` report. It follows workflow creation's local-owner and workspace-scoping rules. The description is limited to 4000 characters (`422 DESCRIPTION_TOO_LONG`) and each workspace gets 10 drafts a minute (`429` with `Retry-After`).

What makes the draft trustworthy:

- **The real engine checks it.** The draft goes through the same validation as `POST /workflows/validate`. If it fails, the errors are sent back to the model and it tries again, up to `max_attempts` (default 3, at most 5).
- **Only installed modules.** The model is shown the catalog of installed modules (with schemas) and the template list as examples. A module that is not installed is never accepted: the draft fails with `422 UNKNOWN_MODULES` and a warning naming it. If the registry has a handler called `unconfigured_step`, the step is replaced by it instead.
- **Approvals are enforced in code, and unknown means write.** Any step whose module can write (`idempotent_write` or `non_idempotent_write`), whose effect is unknown, or that the server requires approval for, gets `requires_approval` even if the model, or an instruction hidden in the description, said otherwise. A module with a manifest is judged by its `effect_policy`. A plain handler with no manifest counts as unknown (so it is gated whatever its name) unless you declare it safe by setting `effect_policy = "pure"` (or `"read"`) on the handler function before registering it. Only the handlers shipped with the built-in templates are judged by name. If you ask for sign-off and no step has a gate, a warning says so.
- **The description is data.** It is delimited, cannot close its own delimiter, and text in it that looks like a secret is removed before the model sees it.
- **Bounded prompts.** The catalog lists at most 150 modules, ranked by how well they match your description, followed by "and K more modules not shown". A retry replays at most 4000 characters of the model's previous reply, and each attempt's messages stay under 90,000 characters. A reply that is nested too deeply to parse counts as an unusable reply and is retried.
- **No secrets in the spec.** Every free-form step field (`config`, `input_mapping`, `output_mapping`) is scrubbed. A secret-named key loses its whole value whatever its type, and secret-shaped text anywhere inside is redacted, each with a warning. Reference a credential by name with the config key `credential`.

From Python, with a model you control. This example uses a scripted offline model:

```python
import asyncio
import json

from nexura_agents.models.fake import FakeModel

from nexura_runtime.builder_assistant import draft_from_description
from nexura_runtime.templates import list_templates, template_registry

reply = {
    "explanation": "Classify the message, then refund after approval.",
    "spec": {
        "name": "Refund desk",
        "entry_node": "classify",
        "nodes": [
            {
                "id": "classify",
                "handler": "tpl_support_classify",
                "transitions": {"refund": "refund"},
                "default_transition": "answer",
            },
            {"id": "refund", "handler": "tpl_support_issue_refund", "default_transition": "answer"},
            {"id": "answer", "handler": "tpl_support_reply"},
        ],
        "terminal_nodes": ["answer"],
    },
}
model = FakeModel([json.dumps(reply)])
draft = asyncio.run(
    draft_from_description(
        "support agent that asks a manager before refunds",
        model=model,
        registry=template_registry(),
        templates=list_templates(),
    )
)
print(draft.spec["nodes"]["refund"]["requires_approval"])  # True: refunds write, so they are gated
```

The Sub-runs tab appears only for runs whose workflow has sub-workflow steps (`GET /runs/{run_id}` returns `has_subworkflows`, derived from the spec; the spec itself is not returned), and each child row opens a child view with its status and checkpoint timeline and a link back to the parent run. Child runs are checkpoints inside the parent, so they are read through the parent: `GET /runs/{run_id}/subruns` lists them with an opaque URL-safe `child_key`, and `GET /runs/{run_id}/subruns/{child_key}` returns that child's `status`, `steps` and redacted `history`. A child is visible to whoever can see the parent; an unknown key is `404 RUN_NOT_FOUND`. Studio's graph builder can add and edit sub-workflow (composite) nodes. The same operations remain available over the HTTP endpoints above, the TypeScript client and the Python SDK. See [Workflows](workflows.md).

## See also

- [Workflows](workflows.md): durable graphs, approvals, `interrupt()`, history and fork.
- [Human approval](approvals.md): approvals for agent tools.
- [Troubleshooting](troubleshooting.md): common errors.
- [Models and providers](providers.md): credentials for live models.
- [Credentials and the local encrypted secrets file](local-secrets.md): env-var references,
  `NEXURA_MASTER_KEY` and the `/api/v1/credentials` routes.

## Editions

The open-source edition is MIT: **single user, this machine only; for a team or a
public endpoint use the hosted or enterprise edition**. Local workspaces are project
folders for that one owner, not tenant or member accounts. Loopback binding, Host/Origin
checks, local credentials, approvals, budgets and sandbox safety stay in this edition.
"This machine only" also holds when you embed the service: `create_api_app()` refuses
peers that are not loopback, and state-changing requests need a per-session token
(`X-Nexura-Session`, see [Service and Studio](studio-and-service.md)).
The token stops other web pages, not other programs on this machine, which can read it.
An insecure network opt-in does not add authentication or make a public endpoint safe.

The closed edition adds authentication, workspace members, organisations and invites,
managed secrets and rotation, publishing and the embeddable widget, and hosted-product
runtime compatibility. It has not had an external security review. Installing the MIT
packages alone does not enable those features.

## Run the service and Studio web

```bash
uv run nexura-runtime serve --port 8765 --db ./nexura_service.sqlite3   # REST API
cd studio-web && npm ci && npm run dev                                  # Studio on Vite
```

Studio's dev server proxies `/api` to `http://127.0.0.1:8765` (override with
`VITE_BACKEND_URL`). The service defaults to the single-owner `local-owner`
profile and binds to `127.0.0.1`; do not expose it on a network as is.

Schedules and webhooks only enqueue jobs; a worker runs them. Start one with
`nexura-runtime serve --worker` or a standalone `nexura-runtime worker --db ... [--once]`. Claimed jobs
hold a lease the worker renews (`--lease-seconds`, default 300); if a worker dies, the
job is reclaimed and retried up to `--max-attempts`, and the dead run is marked
`interrupted`. Release pins are enforced and several workers never double-fire a schedule.

Chat in the service and Studio is offline by default. An operator can allow real models
with `nexura-runtime serve --chat-model NAME=PROVIDER:MODEL` (or `NEXURA_CHAT_MODELS`); API keys
come from the server's environment only, clients choose from `GET /api/v1/chat/models`
and cannot pass provider strings, and viewers can use only the offline fixture. Studio's
playground has a model picker for this.

Runs that wait for a person are visible in the API: a run waiting on `interrupt()` has
status `waiting_for_input` and `pending_input`; `POST /api/v1/runs/{id}/input` answers it,
`GET /api/v1/runs/{id}/history` lists checkpoints, `POST .../fork` starts a new run from
one (optionally with edited state) and `GET .../subruns` lists sub-workflow runs. The TS
client has `runs.provideInput/history/fork/subruns/effects/resolveEffect`, and Studio shows a Pending input
banner and a History tab with "Fork from here".

Other commands:

```bash
uv run nexura-runtime workflow export spec.json --out ./bundle   # standalone bundle
uv run nexura-runtime workflow run ./bundle --state '{"order_id": "ORD-1"}'
uv run nexura-studio --db ./studio.sqlite3                # older built-in Studio (nexura-studio)
```

Workflows that use your own handlers name them explicitly; workflow JSON never
chooses what code is imported:

```bash
uv run nexura-runtime workflow export spec.json --out ./bundle --handlers my_app.handlers:registry
python ./bundle/run.py '{"order_id": "ORD-1"}' --handlers my_app.handlers:registry
```

`--handlers` takes `package.module:attribute`, where the attribute is a `ModuleRegistry`
or a function that registers handlers on one. Name clashes with built-ins are rejected.

Chat turns can be streamed: `POST /api/v1/sessions/{id}/chat/stream` sends ordered
server-sent events for each step (model turn, tool start and finish) and ends with the
same body as the non-streaming endpoint. Reply text arrives whole (no token deltas).
A turn is saved only when it completes; if the client disconnects or stops it, nothing
is saved. The TS client exposes this as `sessions.chatStream()`.

## Package overview

**A local web page to build, run and approve `nexura-runtime` workflows, and the
`nexura-studio` command that serves it.**

```bash
pip install nexura-studio   # installs nexura-runtime, FastAPI and uvicorn too
nexura-studio --db ./studio.sqlite3   # then open http://127.0.0.1:8000
```

- **What it shows.** The available modules, saved workflows, runs with their state and
  traces, and the approval gates waiting for a person. Runs use the runtime's real
  `WorkflowEngine`; the built-in demo modules use synthetic data only.
- **This machine only.** It binds to `127.0.0.1` by default and refuses a non-loopback
  `--host` unless `NEXURA_STUDIO_TOKEN` is set (or the loudly named
  `--insecure-allow-unauthenticated-network` is passed). Every state-changing request needs
  the per-session token, which the page reads from its own origin. The token stops other
  web pages, not other programs on this machine, which can read it.
- **What stays in the runtime.** The run store, built-in modules, traces and the security
  middleware are part of `nexura-runtime` (`nexura_runtime.studio`), shared with the REST
  service. `nexura-runtime serve` (the API that the React Studio in `studio-web/` and the
  TypeScript client call) needs only `pip install "nexura-runtime[studio]"`, not this package.
- Without this package, `nexura_runtime.studio.create_app()` raises `MissingDependencyError`:
  "Nexura Studio is provided by the 'nexura-studio' package, which is not installed. Install
  it with: pip install nexura-studio".

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Single user, local
> use; it has not had an external security review.

## Offline example

The app is a FastAPI application, so it can be driven in-process without opening a port.

```python
import asyncio
import tempfile
from pathlib import Path

import httpx

from nexura_studio import create_app


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(db_path=Path(tmp) / "studio.sqlite3")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"X-Nexura-Session": app.state.session_token},
        ) as client:
            modules = (await client.get("/api/v1/modules")).json()["modules"]
            print(len(modules), "modules, for example", sorted(m["name"] for m in modules)[0])
            page = await client.get("/")
            assert page.status_code == 200 and "nexura-session" in page.text


asyncio.run(main())
```
