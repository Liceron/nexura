# Use the hosted Nexura app

Build and test locally with the open-source SDK, then upload a declarative workflow or
saved agent to your hosted workspace. SDK runs that stay on your machine can send traces
to the same workspace. The hosted service requires the enterprise edition; the SDK stays
MIT licensed and does not require a hosted account for local use.

## Create a workspace key and sign in

A workspace admin creates an **editor** key with the API below. A **viewer** key can inspect
traces and other readable resources but cannot upload, delete, or create keys. Keys act as
their own principals, belong to one workspace, and may expire. Keep the returned secret:
creation is the only time the service returns it. An admin can revoke it at any time.

These commands require a running hosted service. Substitute its API URL:

```bash
# Hidden prompt; the secret is never a command-line argument or printed.
nexura-runtime login --url https://YOUR-NEXURA-API
nexura-runtime whoami
```

For automation, use `--api-key-stdin`, or set `NEXURA_API_KEY` in your secret manager and
run `login --url ...`. Login verifies the credentials and prints the workspace and role.
It atomically writes `~/.config/nexura/credentials.json` with permissions **0600**.
`nexura-runtime logout` removes that local file; it does not revoke the server key.

`NEXURA_URL` and `NEXURA_API_KEY` override the saved login. SDK `connect()` arguments
override both. A saved key is never reused for a different URL supplied through the
environment or arguments. Remote URLs require HTTPS; loopback HTTP is allowed for tests
and local self-hosting. Matching `nexura.commands` entry points provide `nexura logout`,
`whoami` and `push`. The Cloud launcher owns its built-in `login`, so hosted SDK login is
available as `nexura project login --url ...`; all four commands also work under
`nexura project`. `nexura-runtime` is the direct command.

Until the key-management UI ships, an authenticated workspace admin uses:

| Method and path | Body / result |
|---|---|
| `GET /api/v1/workspace/whoami` | Authenticated workspace, principal, role |
| `GET /api/v1/workspace/api-keys` | `items`: metadata, never secrets or hashes |
| `POST /api/v1/workspace/api-keys` | `{"name":"SDK laptop","role":"editor","expires_in_days":90}`; metadata plus `api_key` once |
| `DELETE /api/v1/workspace/api-keys/{key_id}` | Revoked metadata; future requests fail on every instance |

Send `X-API-Key: ...` or `Authorization: Bearer ...`. Firebase callers use their bearer
token and the usual workspace header. Creating keys requires workspace admin; HTTP cannot
create an admin key. At most **20 active, unexpired keys** are allowed per workspace.

## Upload workflows and agents

These commands require an editor key and a running hosted service:

```bash
nexura-runtime push workflow workflow.json
nexura-runtime push workflow exported-bundle.zip
nexura-runtime push workflow exported-bundle-directory
nexura-runtime push workflow generated_workflow.py
nexura-runtime push agent saved-agent.json
```

The command prints the hosted resource id and its web link. Workflows are created through
the existing import API; importing an existing id follows that API's conflict rules.
Agents are created, or updated **by name** with the current revision checked by the server.
Use a saved `AgentSpec` JSON object, or a saved-agent response containing `spec`.
Python workflow files must use Nexura's generated, declarative export format: the importer
parses them and never executes them. Zip/directory bundles contribute only `workflow.json`.
The CLI checks handlers/tools against the hosted catalog before uploading.

Custom Python function tools, custom handler modules, arbitrary Python scripts, bundled
code/dependencies and local credentials cannot be deployed this way. Keep those runs local
and send their traces, or replace the tools with declarative modules already offered by
your hosted service. Model names, knowledge collections, skills, registered integration
tools and credentials must already exist in that workspace; the server validates them.
Uploading saves a definition. It does not start a run or publish a public endpoint.

## Send SDK traces

Install `opentelemetry-sdk` alongside the SDK. No OTLP exporter package is needed. This
example requires a running hosted service and a saved login or environment credentials:

```python notest
import asyncio
from nexura_agents import Agent, FakeModel, Runner, tracing

connection = tracing.connect(service_name="support-local")
# Optional: tracing.instrument(capture_content=True), if this backend may hold that data.
agent = Agent(name="Support", instructions="Be brief.", model=FakeModel(responses=["Hello!"]))
asyncio.run(Runner().run_async(agent, "Hello"))
connection.force_flush()  # explicitly wait, e.g. at the end of a short process
print(connection.dropped_spans)
```

`connect(url=None, api_key=None, *, service_name=None)` uses an isolated tracer provider;
it does not replace the global OpenTelemetry provider. It starts SDK instrumentation and
later `instrument()` calls reuse it unless given a different provider. Content capture is
off by default. See [Observability](observability.md) for capture and redaction behavior.

Export runs in a daemon thread: spans enqueue without network I/O. The queue holds 2,048
spans, batches hold up to 128, and queued spans flush every second. Overflow and failed
batches increment `dropped_spans`; server partial success increments `rejected_spans`.
429/5xx and transport failures retry up to three times with bounded backoff; redirects
are refused. Shutdown/normal process exit allows five seconds for flushing. This is best
effort telemetry: process crashes, timeouts and sustained rate limits can lose spans.
`force_flush(timeout_millis=5000)` reports whether queued work completed within the bound.
`shutdown()` flushes and stops this connection. It never logs span content or keys.

A stock OpenTelemetry **HTTP/protobuf or HTTP/JSON** exporter can also use:

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://YOUR-NEXURA-API/v1/traces
# Set OTEL_EXPORTER_OTLP_TRACES_HEADERS in your secret manager, e.g. x-api-key=<workspace key>.
```

The service accepts standard OTLP, including gzip, at `POST /v1/traces` and the alias
`POST /api/v1/sdk-traces/otlp`. There is no gRPC listener. This release instruments Nexura
SDK runs; it does not provide instrumentation adapters for other frameworks.

To upload an existing OTLP JSON export (including
`nexura_runtime.observability.export_spans_otlp_payload` output):

```bash
nexura-runtime push traces traces.otlp.json
```

## Trace limits and read APIs

Defaults: **4 MiB** compressed/on-wire body, **8 MiB** expanded body, **5,000 spans** per
request, **200,000 new spans and 1 GiB of stored span data per workspace per UTC day**, and
**30 days** retention. Resource attributes are kept up to 4 KiB per resource. A busy service
answers 429 with `Retry-After`; the SDK exporter and stock OpenTelemetry exporters retry.
Each attribute value is truncated to 8 KiB; a span's stored attributes plus events and its
resource attributes are each bounded to 64 KiB. Invalid spans and spans over the request
count limit are reported in OTLP `partialSuccess.rejectedSpans` (or protobuf
`partial_success.rejected_spans`). Invalid wire data gets 400; unsupported types 415;
oversized bodies 413; a daily quota overflow gets 429 and `Retry-After` until UTC midnight.
The quota refusal is atomic: none of that batch is inserted. Duplicate
`(workspace, trace_id, span_id)` inserts are ignored, so retrying does not use extra quota.

Content is stored as sent, subject to these bounds. Content capture is the sender's
choice; the service does not redact captured prompts. Retention runs in the operator's
existing authenticated trigger tick, deleting at most 10,000 expired spans per tick.

Viewers can use `GET /api/v1/sdk-traces?limit=50&service=...&status=error&since=...`:
`since` is UTC epoch seconds; `status` is `ok` or `error`. The result has `items` and an
opaque `next_cursor` for the next request's `cursor`. Each trace has its root name, service,
start time, duration, status (error if any span errored), span count and GenAI input/output
token totals. Time and duration values are decimal nanosecond strings. Missing token
attributes contribute zero. Token totals use `gen_ai.usage.input_tokens` and
`gen_ai.usage.output_tokens`, so deprecated aliases do not double-count.

`GET /api/v1/sdk-traces/{trace_id}` returns the trace view: up to 5,000 spans (earliest
first, `truncated` says whether there were more) with parent ids, timing, status and token
counts, names cut to 256 characters. One span's attributes, events and resource come from
`GET /api/v1/sdk-traces/{trace_id}/spans/{span_id}`. Editors can use
`DELETE /api/v1/sdk-traces/{trace_id}`. Deleting spans does not refund the daily quota.
All queries and deletes are workspace-scoped; trace ids alone never grant access.
