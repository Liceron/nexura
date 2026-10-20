# Reproducing failures offline

Record a run before it fails, export one JSON file, and replay it without credentials.
Agent replay uses `Runner` with a scripted `FakeModel`; workflow replay uses
`WorkflowEngine` with recorded node fixtures. Neither replay loads the original tools,
connectors, handler modules, or provider adapters.

Run the complete keyless example:

```bash
uv run --frozen python examples/repro_failure_demo.py
# reproduced: True
```

## Record and export

Import `ReproRecorder`, `export_repro_bundle`, `ReproBundle`, and
`replay_repro_bundle` from `nexura_agents.repro`.

For an agent, create a recorder and call
`await recorder.run_agent(runner, agent, prompt)` instead of `runner.run_async`.
This delegates to the existing Runner. Exceptions still propagate; catch them and
export the recorder with `export_repro_bundle(recorder).save("bundle.json")`.
An approval pause returns the usual interruption. Continue through
`await recorder.resume_agent(result.interruption, approve=True)` or pass
`reject={call_id: "reason"}`. Decisions are recorded along with the run.

For a workflow, call `await recorder.run_workflow(engine, initial_state)`.
Continue an approval pause through
`await recorder.resume_workflow(result.workflow_run_id, approval_id, decision="approve")`
(or `decision="reject"`). Use one recorder per run. Recording executes the original
tools and handlers under their usual approval rules; only replay blocks side effects.

The exporter also accepts a complete recording dict, a dict with `repro`, or a stored
`RunRecord` whose `metadata["repro"]` contains `export_repro_bundle(recorder).data`.
Persist that metadata through the existing `StorageRepository.create_run` or
`update_run` interface. Ordinary tracing events and historical checkpoints lack full
model responses and handler outputs. Export refuses incomplete records rather than
inventing a history. Recording is opt-in; the service does not automatically capture runs.

## Share and replay

Install the same framework version in the receiving environment from local wheels or
an existing offline environment. Agent bundles need `nexura-agents`; workflow bundles
also need `nexura-runtime`. The JSON file contains data only, with no Python source,
dependency downloads, or imports selected by the bundle.

```bash
nexura-runtime repro export RUN_ID --db ./nexura_service.sqlite3 -o bundle.json
nexura-runtime repro run bundle.json
```

Use `--workspace NAME` on export when the stored run belongs to another workspace.
The CLI exits 0 when the outcome is reproduced and 1 on divergence or an invalid bundle.
From async Python, use `await replay_repro_bundle(ReproBundle.load("bundle.json"))`.
`ReplayResult` contains `reproduced`, `outcome`, and `first_divergence`.

## Captured data and safety

The versioned document contains creation time and framework version, run kind, input,
static agent instructions and tool schemas or the workflow node graph, model responses
in observed order (including usage), tool/node outputs or error names and messages,
approval decisions, run budget config, and the expected final status, error and failing
step. Tool arguments have a canonical SHA-256 digest. Workflow fixtures cover whole
node invocations, including composite nodes, rather than rerunning their children.
Tool retry policy (attempts, delays and exception names), timeout and timeout behavior,
cache/idempotency settings and async status are recorded. Each executed attempt has
an output or error fixture, including BaseException cancellations; cache hits consume
no new fixture. Replay uses these policies with inert tools, translating a cancelled
timed tool attempt into the Runner's timeout path without waiting for wall-clock expiry.
Workflow gate records are consumed per node identity, including map pre-approval checks.

Credential-reference dictionaries are stripped before resolution, following the
standalone exporter's `resolve_credential_refs` separation. Repro never resolves a
reference for replay. This is best-effort redaction of known secret shapes; review a
bundle before sharing it. Coverage includes:

- Fields matching api_key/api_token, password/passwd, secret, credential,
  access/refresh/bearer/session/id_token, private_key/private_token, authorization,
  cookie and Ocp-Apim-Subscription-Key (case insensitive, with common separators).
  This includes X-Auth-Token and X-Api-Key. Schema property definitions are retained.
- Authorization, Proxy-Authorization, Cookie, Set-Cookie, X-Auth-Token, X-Api-Key,
  X-Api-Token, PRIVATE-TOKEN and Ocp-Apim-Subscription-Key in dictionaries and
  `[[name, value], ...]` header lists; colon-delimited header text in strings.
- Valid JSON-looking strings are parsed, scrubbed recursively and re-serialized,
  including tool output and ToolCall.raw_arguments. Existing `$redacted` contents
  are recursively scrubbed too. Fields use `{"$redacted": "field name"}` markers;
  free text uses `[REDACTED]`, preserving its string type.
- Every scalar below a secret field, including nested dict/list values and stringified
  numbers, is learned and scrubbed from other values. Declared credential values,
  named environment refs and inline `{"ref": "MY_KEY", "value": ...}` values are
  learned too. Literal, URL percent/form-encoded, standard/URL-safe base64 copies and
  base64 containing `user:secret` are covered. Unlabelled Bearer/Basic text and
  `sk-` provider keys of at least 12 following characters are also scrubbed.

Bare `key`, `token` and `auth` names are ambiguous: ordinary strings such as
`user:42`, `next-page` and `basic` are retained. They are sensitive when their values
contain Bearer/Basic/Token credentials or an `sk-` shape, including nested dict/list
values, or contain a nested unambiguous secret field (also `pass`). An auth mode
dictionary such as `{"mode": "basic"}` is retained. Unambiguous names above remain sensitive regardless of value.
Supply `ReproRecorder(credential_values={"MY_KEY": resolved_value})` or the exporter's
`credential_values` argument for otherwise unknown values. Arbitrary unlabelled secrets,
other encodings and secrets in field/reference names are not reliably recognized.
Review inputs and outputs for personal or proprietary information too.

Replay replaces every tool with a fixture stub. Unknown names or argument digests
cause divergence. The guard blocks `socket.socket.connect`, `connect_ex`, `sendto`,
and `sendmsg` (where available); `socket.create_connection`, `gethostbyname`,
`gethostbyname_ex`, `gethostbyaddr`, `getaddrinfo`, and `getnameinfo`; and the active
asyncio loop's `getaddrinfo` and `getnameinfo`. This guard is process-wide: use a dedicated replay process
when other application tasks need network access. No tool functions, subprocess tools,
plugins or provider code are loaded from a bundle.
It is defense in depth, not a sandbox for arbitrary Python: direct `_socket` APIs,
already-connected socket `send`/`sendall`, subprocesses, native extensions and other
event loops are not intercepted.

## Limits

- Nondeterministic tools are replayed from fixtures; replay cannot verify their original
  real-world effects or diagnose behavior inside a recorded workflow node.
- Agent recording currently requires static instructions and no handoffs. Custom
  guardrails, hooks, memory, history processors, dynamic tool preparation and approval
  predicates are not serialized as executable code; behavior depending on them can
  diverge. Use the returned divergence rather than assuming they were replayed.
- The convenience recorder takes a text prompt or initial workflow state. It does not
  capture pre-existing sessions, edited approval arguments, interrupt answers, wall-clock
  scheduling, cancellation timing, or process-crash timing. Cancellation error outcomes
  are recorded, but original timing is not recreated. A checkpoint alone cannot recover
  missing historical fixtures.
- Budget configuration and SDK usage limits are captured. Provider billing, external
  ledgers, inherited workspace budgets and pricing changes are not recreated. FakeModel
  does not make billed calls. Such accounting failures can diverge.
- Redaction can remove data required to reproduce a failure. A changed schema, message,
  or argument can cause divergence; secrets are never restored to make replay pass.
  Ordinary ambiguous parameter names retain their values; outcomes are compared in
  equally scrubbed form. Very short declared/discovered secrets can still reduce fidelity.
- Tool retries, timeouts and cache replay at recorded attempt boundaries; custom
  exception inheritance and custom cache/timeout hooks are not executable behavior
  in the bundle. Composite workflow internals remain opaque even for approved maps.
- Outcome comparison checks status, error type/message and failing step, and requires
  all recorded model responses, tool fixtures and workflow gate records to be consumed.
  Completed output/final state are not compared. A recording fingerprint
  also detects fixture edits; it is an integrity check, not a cryptographic signature or
  a guarantee that an untrusted recording describes a real run.
- Schema version mismatches are refused. Framework version is recorded for diagnosis;
  use matching package versions for reliable comparisons. This is local fixture
  verification, not live-model validation or a production-readiness claim.
