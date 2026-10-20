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

Runs that hand off between agents are recorded too. Every agent reachable through
`handoffs` gets its own entry in the bundle (`spec["agents"]`, with its tools and
handoff list), and each model response and tool fixture is tagged with the agent that
produced it. Replay rebuilds the same graph: each agent gets its own scripted replies
and fixtures, and a reply or fixture consumed by a different agent is a divergence
(`Model response 1: recorded for agent 'triage', requested by 'billing'`). Agent names
must be unique within the graph. An `on_handoff` callback is recorded as a fixture
(`on_handoff:<tool name>`) and is not run on replay.

For a workflow, call `await recorder.run_workflow(engine, initial_state)`
(extra keyword arguments such as `workflow_run_id` go to `engine.run`).
Continue an approval pause through
`await recorder.resume_workflow(result.workflow_run_id, approval_id, decision="approve")`
(or `decision="reject"`; `state_update=` is recorded and replayed, and `engine=`
continues the recording on a fresh engine for the same run). Use one recorder per run. Recording executes the original
tools and handlers under their usual approval rules; only replay blocks side effects.

The exporter also accepts a complete recording dict, a dict with `repro`, or a stored
`RunRecord` whose `metadata["repro"]` contains `export_repro_bundle(recorder).data`.
Persist that metadata through the existing `StorageRepository.create_run` or
`update_run` interface. Ordinary tracing events and historical checkpoints lack full
model responses and handler outputs. Export refuses incomplete records rather than
inventing a history. Recording is opt-in; the service captures runs only when failed-run
capture is turned on (below).

## Capturing failed service runs

The REST service can keep a bundle for every workflow run that fails. It is off by
default. Turn it on with one of:

```bash
nexura-runtime serve --capture-failed-runs
NEXURA_CAPTURE_FAILED_RUNS=1 nexura-runtime serve      # also applies to `worker`
```

or `create_api_app(capture_failed_runs=True)` when embedding the app (an explicit
`True`/`False` wins over the environment variable).

While it is on, runs started with `POST /api/v1/runs` and by the trigger worker execute
under `ReproRecorder`, and approval decisions (`POST /runs/{id}/decide`) made in the same
process continue the recording. When the stored run status ends `failed`, the recording
is exported with the same redaction as `export_repro_bundle`, plus the workspace's known
credential values, and stored in the service database (table
`nexura_service_repro_bundles`). Completed, rejected and cancelled runs keep nothing.
Download a bundle with:

```bash
curl -OJ http://127.0.0.1:8765/api/v1/runs/RUN_ID/repro        # 404 REPRO_BUNDLE_NOT_FOUND if none
nexura-runtime repro export RUN_ID --db ./nexura_service.sqlite3 -o bundle.json
```

The route applies the usual run ownership checks and only returns bundles of the
caller's workspace; bundles stay downloadable after capture is turned off. Not captured:
runs continued after a service restart (the in-memory recording is lost), runs that
pause for `interrupt()` input, forks, and agent chat. Those runs simply have no bundle.
Recording keeps each in-flight recording in memory until the run settles (at most 256
runs paused for approval). Review a bundle before sharing it.

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
`ReplayResult` contains `reproduced`, `outcome`, `first_divergence` and `notes`.

The outcome includes the completed output: the agent's final output (the validated
structured output when the agent has an `output_type`, otherwise the text) or, for a
workflow, the final state. Both are stored as canonical JSON with a SHA-256 digest
(`output_digest` / `state_digest`). A replay that finishes with a different value is not
reproduced, and `first_divergence` names the first differing path, replayed value first:

```text
output differs at $.amount: 500 != 5000
state differs at $.lines[0].qty: 2 != 7
```

Bundles written before this comparison existed have no output or final state. They
still load and replay; status, error and failing step are compared, `reproduced` can be
true, and `notes` (also printed by `repro run` as `note: ...`) says the output was not
recorded.

## Captured data and safety

The versioned document contains creation time and framework version, run kind, input,
static agent instructions and tool schemas or the workflow node graph, model responses
in observed order (including usage), tool/node outputs or error names and messages,
approval decisions, run budget config, and the expected final status, error and failing
step, plus the completed output or final workflow state with its canonical digest. For
runs with handoffs, every agent's spec and handoffs, with responses and fixtures tagged
by agent name. Tool arguments have a canonical SHA-256 digest. Workflow fixtures cover whole
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
- Agent recording requires static instructions for every agent in the handoff graph
  and unique agent names. Custom guardrails, output validators, hooks, memory, history
  processors, dynamic tool preparation, approval predicates, handoff `input_filter`s and
  `enabled` rules are not serialized as executable code; behavior depending on them can
  diverge (a validator that changed the output shows up as an output divergence). Use
  the returned divergence rather than assuming they were replayed. Structured handoff
  input (`input_type`) is replayed as given; its validation is not recreated.
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
- Outcome comparison checks status, error type/message, failing step and the completed
  output (agents) or final state (workflows), and requires all recorded model responses,
  tool fixtures and workflow gate records to be consumed. A failed run has no output to
  compare; agent `RunResult.state` is not compared (replayed tools do not change it).
  Outputs are compared in redacted form, so a difference hidden by redaction is not
  seen. A recording fingerprint
  also detects fixture edits; it is an integrity check, not a cryptographic signature or
  a guarantee that an untrusted recording describes a real run.
- Schema version mismatches are refused. Framework version is recorded for diagnosis;
  use matching package versions for reliable comparisons. This is local fixture
  verification, not live-model validation or a production-readiness claim.
