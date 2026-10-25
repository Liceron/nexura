# Guardrail policy

A `GuardrailPolicy` (in `nexura-guardrails`) is one declarative policy that applies guardrail
rules to every stage of every agent run: the user's input, every model request and reply, every
tool call's arguments and result, and the final answer. You write it once, in Python or as a
JSON or YAML file, and install it for the process or for a service; you do not tag tools one by
one. **Guardrail checks reduce risk and give an audit trail. They are not a security
boundary**: the real protection is structural (a tool allowlist, human approval for side
effects, a sandbox for code, least-privilege credentials), as the [security notes](#security-notes)
below explain.

```bash
pip install "nexura-guardrails[yaml]"   # [yaml] only for YAML files; JSON needs nothing extra
```

## A first policy

```python
import asyncio

from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_guardrails import GuardrailPolicy, Rule, When, pii


@tool
def lookup(q: str) -> str:
    """Look up a customer."""
    return f"{q}: write to bob@example.com"


@tool
def shell(command: str) -> str:
    """Run a shell command."""
    raise AssertionError("never runs: the policy blocks it")


policy = GuardrailPolicy(
    name="acme-default",
    checks={"redact": pii(action="redact")},
    rules=[
        Rule("no-shell", When(stage="tool_input", tools="shell*"), action="block"),
        Rule("scrub-results", When(stage=["tool_output", "output"]), check="redact", action="scrub"),
    ],
)

agent = Agent(
    name="support",
    instructions="Help the customer.",
    model=FakeModel(
        responses=[
            [
                ToolCall(id="c1", name="lookup", arguments={"q": "Asha"}),
                ToolCall(id="c2", name="shell", arguments={"command": "ls"}),
            ],
            "Done: mail asha@example.com",
        ]
    ),
    tools=[lookup, shell],
)
result = asyncio.run(Runner(hooks=[policy]).run_async(agent, "Find Asha"))
tool_results = [m.content for m in result.messages if m.role == "tool"]
assert tool_results[0] == "Asha: write to [REDACTED:email]"
assert tool_results[1].startswith("Tool call blocked by policy rule 'no-shell'")
assert result.output == "Done: mail [REDACTED:email]"
```

`Runner(hooks=[policy])` applies it to one runner. `policy.install()` makes it the process
default for every `Runner` (including the runs that agents-as-tools, teams, MCP and A2A servers,
templates and workflow agent nodes start); `policy.install(locked=True)` also stops code in the
same process from removing it (`set_default_hooks(..., locked=True)`, see
[Guardrails, hooks and limits](guardrails-hooks-limits.md)).

Two runtime paths call models without a `Runner`: `AgentCoordinator` (and the
`nexura.agents.coordinator` workflow module) and `AgentDefinition` subagents. They cannot apply
the `input`, `model_input`, `model_output` and `output` stages, so a coordinator run is
**refused** with `SDKValidationError` when it starts whenever the hooks it would use include a
policy with rules for any of those stages, whatever their source: `AgentCoordinator(hooks=policy)`,
`policy.install()` with or without the lock, or an enclosing run's hooks. A policy with only tool
rules does not refuse it: its tool calls go through the policy as in a `Runner`.

**AgentDefinition subagents: their own model calls are not checked by model_input/model_output
rules, and their own input and output stages are not checked either, until a follow-up lands.
The parent's stages still check what reaches the user.** (A
subagent that calls a tool is refused at that call under such a policy.) Workflow module nodes
other than agent nodes and `guardrails.check` are not covered yet.

## Stages and actions

| Stage | When | `block` | `scrub` | `require_approval` | `fail` |
| --- | --- | --- | --- | --- | --- |
| `input` | the user's latest message, once per run, before any model call | the run trips (`GuardrailTripwireTriggered`) | not allowed | not allowed | the run trips |
| `model_input` | every model request (the new messages since the last reply) | a refusal reply, the model is not called | not allowed | not allowed | the run fails |
| `model_output` | every model reply, including its tool calls | the reply and its tool calls become a refusal | the reply text is redacted | not allowed | the run fails |
| `tool_input` | every tool call's arguments | the tool does not run; the model is told the rule | not allowed (arguments are never rewritten) | a person approves the call first | the run fails |
| `tool_output` | every tool result | the result is withheld; the model is told the rule | the result is redacted | not allowed | the run fails |
| `output` | the final answer | the run trips | the answer is redacted | not allowed | the run trips |

**Streaming.** When a `model_output` or `output` rule (`scrub`, `block` or `fail`) applies to an
agent, its streamed text (`stream_text=True`, the chat SSE, Studio) goes through the same holding
path as output guardrails: with `stream_guard="auto"` the reply is **held** until every rule has
run, or, when every such rule is a `scrub` by regex pre-filters, checked in **windows** before
each part is shown (`"chunked"`). Text a model writes before its tool calls is checked the same
way, and so are the reply's streamed tool calls: `TOOL_CALL_DELTA` events are held with the
text and sent only when the reply's tool calls come through `model_output` unchanged (a blocked
reply sends none). An agent with `stream_guard="passthrough"` is **refused** with `SDKValidationError` at the
start of its run (and at a handoff to it) while such a rule applies: it would show text before
the rules see it.

`allow` is an allowlist entry: it never cancels another rule. For each stage the **most severe
matching action wins**: `fail` > `block` > `require_approval` > `scrub` > `allow`. Rules without
checks act on every item they match; rules with checks act when their checks do not allow the
text (a block, a review, a rewrite or, with `on_error: block`, an error). Unconditional rules are
applied first and checks are run most severe first, so a cheap finding never hides a `fail`.

What the model is told when a tool call or result is blocked is built from fixed words, the rule
id, the check name and taxonomy category ids, never from a judge's free-text reason.

## Matching

`When` fields must all match; within a field any pattern may match. Patterns are `fnmatch`
globs, case-sensitive.

- `stage`: one or more stages (default: all).
- `tools`: tool names. `origin`: `ToolInfo.origin`, for example `mcp:<server>:<tool>`,
  `openapi:<METHOD>:<path>`, `python:<module>.<function>`, `agent:<name>`,
  `knowledge:<collection>`. `toolset`: the names in `toolset:<name>` tags (MCP servers, OpenAPI
  toolsets). `effect`: effect classes `pure` (`read` is an alias), `idempotent`,
  `non_idempotent`, `reconcilable`. These filter tool calls, so a rule that uses them may only
  name `tool_input` and `tool_output`.
- `agents`: agent names.
- `args`: argument conditions, `{"$.amount": {"gt": 100}}`, with `eq`, `ne`, `gt`, `gte`, `lt`,
  `lte`, `in`, `not_in`, `contains`, `matches` (a regular expression) and `exists`. A missing
  argument fails every operator except `exists: false`. `gt`, `gte`, `lt` and `lte` compare
  finite numbers (`int`, `float` and `Decimal`, exactly, never rounded through `float`); a value
  that is present but is not a finite number (text, `NaN`, an infinity, `true`, `null`) cannot be
  compared, so the condition counts as **met** for a `block`, `fail`, `scrub` or
  `require_approval` rule (fail closed) and as unmet for an `allow` rule. `true` is not `1` for
  `eq`. Argument rules are matched twice: on the arguments as the model sent them
  and on the arguments the tool will receive after its schema validation (`"5000"` becomes
  `5000` for an `int` parameter, `"true"` becomes `true`). A `block`, `fail`, `scrub` or
  `require_approval` rule applies when either matches; an `allow` rule only when both do, so
  the most severe reading wins. Before any hook or policy sees a call, the `Runner` builds the
  exact arguments the tool will receive: a plain `@tool` drops names it does not declare (as it
  always has; a stray key never fails a call) and coerces values; an MCP or OpenAPI tool keeps
  its JSON Schema rules (extra keys pass unless `additionalProperties` is false). The policy
  checks those dispatched arguments together with the raw ones, so a `block` rule on a key that
  is dropped before dispatch still fires. Arguments that do not validate are refused
  (`ToolValidationError`, told to the model when the tool allows retries); such a call is never
  sent for approval.
  Where the arguments cannot be validated (a call outside a `Runner`), an argument condition
  counts as met for a restricting rule and as unmet for an `allow` rule.
  Because an `allow` rule needs both readings to match, allowlist mode is strict about types:
  `allow when $.amount <= 100` does not admit `{"amount": "50"}` even though the tool would
  receive `50`. Send numbers as JSON numbers, or add a separate rule for the string form.

A model reply with two tool calls sharing one id (two empty ids included) is refused with
`ProviderResponseError` before any check, approval gate, journal entry or dispatch: approvals and
rejections are bound to call ids, so such calls could not be decided one by one.

An `allow` rule that names `tools` without `origin` admits only local tools (no origin,
`python:`, `builtin:`, `agent:`): an allowlist entry `lookup` never admits an MCP or OpenAPI tool
that happens to be called `lookup`. Name those by `origin`. A `block` rule by name catches every
origin.

## Allowlist mode and approval

`default_tool_action="block"` turns the policy into an allowlist: a tool call that no `allow`
rule matches is refused ("it is not on the tool allowlist") whatever its arguments.
`require_approval` rules pause the run for a person before the call (the standard approval
contract: the decision is bound to the exact call, and the gate is evaluated again on resume and
recovery). They take no checks: an approval gate must be deterministic, so it may only match
tool facts and arguments.

```python
import asyncio

from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_guardrails import GuardrailPolicy, Rule, When

ran = []


@tool
def read_file(path: str) -> str:
    """Read a file."""
    return "contents"


@tool
def shell(command: str) -> str:
    """Run a shell command."""
    ran.append(command)
    return "done"


policy = GuardrailPolicy(
    default_tool_action="block",
    rules=[
        Rule("reads", When(stage="tool_input", tools=["read_file", "shell"]), action="allow"),
        Rule("approve-shell", When(stage="tool_input", tools="shell"), action="require_approval"),
    ],
)
agent = Agent(
    name="ops",
    instructions="Help.",
    model=FakeModel(
        responses=[[ToolCall(id="c1", name="shell", arguments={"command": "rm -fr /"})], "ok"]
    ),
    tools=[read_file, shell],
)
runner = Runner(hooks=[policy])
paused = asyncio.run(runner.run_async(agent, "clean up"))
assert paused.status == "paused" and ran == []  # a person sees the exact command first
done = asyncio.run(runner.resume_async(agent, paused.interruption, reject={"c1": "no"}))
assert done.status == "completed" and ran == []
```

## Policy files

The same policy as data (`version: 1`). Unknown keys, duplicate keys, unknown check types,
rules that name unknown checks and actions a stage cannot take are refused with the path of the
offending field, and so are YAML anchors and aliases and documents nested deeper than 32
levels. The path is resolved once (symbolic links are followed, so a Kubernetes ConfigMap mount
such as `policy.yaml -> ..data/policy.yaml` works); the resolved target must be a regular file
(not a device, pipe or directory) of at most 1 MB, and relative paths in it resolve against the
resolved file's directory. A judge's `policy_file` is read the same way.
A policy file never names code and never holds credentials. Relative paths
(a judge's `policy_file`, the audit `path`) resolve against the file's directory, and the policy
digest covers the document and the contents of every `policy_file`.

```yaml
version: 1
name: acme-default
on_error: block
default_tool_action: allow
timeouts: {check_seconds: 5, stage_seconds: 8}
checks:
  pii: {type: pii, kinds: [card, aadhaar, pan, secret, upi]}
  redact: {type: pii, action: redact}
  inject_fast: {type: injection, threshold: 0.5}
  safety: {type: llama_guard, model: "llama-guard3:1b"}
  bank_judge: {type: judge, model: "ollama:qwen3:8b", policy_file: policies/bank.md}
rules:
  - id: gate-writes
    when: {stage: tool_input, effect: [non_idempotent], origin: ["mcp:*", "openapi:*"]}
    action: require_approval
  - id: no-secrets-to-tools
    when: {stage: tool_input}
    check: pii
    action: block
  - id: scan-tool-results
    when: {stage: tool_output, origin: ["mcp:*", "openapi:*", "knowledge:*", "builtin:web"]}
    checks: [inject_fast, safety]
    mode: tiered
    action: block
  - id: scrub-output
    when: {stage: [model_output, output]}
    checks: [redact]
    action: scrub
  - id: input-safety
    when: {stage: input}
    checks: [inject_fast, bank_judge]
    action: fail
audit: {sink: jsonl, path: audit/guardrails.jsonl}
```

Check types: `pii` (`kinds`, `action: block | redact`, `mask`), `injection` (`threshold`,
`extra_patterns`), `length` (`chars`, `tokens`), `topics` (`keywords`, `deny`), `json_only`,
`judge` (`model`, `policy` or `policy_file`, `categories`, `timeout`, `context_tokens`,
`max_output_tokens`, `on_error`, `api`, `base_url`), and the local safety models `llama_guard`,
`shieldgemma`, `granite_guardian` and `nemotron` (see the
[guardrails package guide](guardrails-package.md)). A rule with several checks runs them in
order (`mode: tiered`, stopping at the first finding) or together (`mode: parallel`).

```python
from nexura_guardrails import GuardrailPolicy

policy = GuardrailPolicy.from_yaml(
    """
version: 1
default_tool_action: block
checks:
  redact: {type: pii, action: redact}
rules:
  - {id: allow-lookup, when: {stage: tool_input, tools: [lookup]}, action: allow}
  - {id: scrub, when: {stage: [tool_output, output]}, check: redact, action: scrub}
"""
)
assert [rule.id for rule in policy.rules] == ["allow-lookup", "scrub"]
assert len(policy.digest) == 64
# From a file: GuardrailPolicy.from_file("policy.yaml") or nexura_guardrails.load_policy(path)
```

## Errors, timeouts and journaled verdicts

Any check error, timeout or malformed verdict follows `on_error` (policy-wide, or per rule):
`block` (the default) fails closed, `allow` lets the item through (never for a hard error such
as a judge's `ContentTooLong`). `timeouts.check_seconds` bounds each check and
`timeouts.stage_seconds` a whole stage. A stage timeout follows `on_error` like any error:
with `block` the item is blocked, with `allow` it goes through.

Model-based verdicts (a judge, a safety model) are not deterministic: asked again after a
crash, a judge could allow a call it blocked before, and it costs money again. So the policy
records every model-based verdict under a key made of the policy digest, the rule, the check, the
stage, the agent, the tool's name and origin, the step (`turn:<n>/call:<id>` with the call's
position among calls sharing its id, `turn:<n>/model`, `input` or `final`) and a digest of the
exact text judged. The record keeps all of those parts, and a stored verdict is reused only when
every one matches exactly; otherwise the check runs again. In a durable run (a `checkpoint_store` with an execution journal: the built-in
memory, SQLite and Postgres stores) the verdict is **committed to the run's journal before it is
acted on**, as a fenced write like every other journal write. A resumed or recovered run finds
it and does not call the model again (the decision event says `source: journal`); a changed
text is a different key and is checked afresh. Deterministic checks are not recorded: running
them again gives the same answer. Without a durable store the verdicts live in the run only.

## Audit

Every decision other than `allow` (all of them with `audit_allows=True`) produces:

- a `GUARDRAIL_DECISION` run event and a `nexura.guardrail.decision` trace event with the stage,
  rule, check, backend, action, categories, score, latency, source (`live`, `cache` or
  `journal`) and the first 16 characters of the policy digest; on tool stages also the tool
  name and call id;
- an `AuditRecord` for each audit sink: `JsonlAuditSink(path)` (one JSON line per decision,
  file mode `0600`, `fsync` for `block` and `fail`), `CallbackAuditSink(fn)` or
  `MemoryAuditSink()`.

Records never hold the checked text, tool arguments or results. A failing sink is logged and
counted (`policy.audit_errors`); with `audit_required=True` (`audit: {required: true}`) it fails
the run instead.

## In a workflow

The `guardrails.check` module checks a text and branches on the verdict: `allow`, `block`,
`review`, `error` or `rewrite`. Its outputs are `decision`, `categories`, `score`, `reason`,
`replacement` and `text` (the redacted text after a rewrite, the original when allowed, empty
otherwise). A workflow document may configure only the deterministic pre-filters, without
custom regular expressions (`extra_patterns` is refused: an editor's pattern could backtrack for
minutes; topic keywords are matched literally); model checks and custom patterns are given by the
operator in Python (`GuardrailCheckModule(checks={...})`) and named with `use`. `nexura-runtime` registers the module when `nexura-guardrails` is installed.

```python
import asyncio

from nexura_guardrails.runtime import GuardrailCheckModule
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec


def answer(state):
    return {"reply": f"Handled: {state['safe_text']}"}


def refuse(state):
    return {"reply": "Sorry, I cannot help with that."}


registry = ModuleRegistry()
registry.register("guard", GuardrailCheckModule())
registry.register("answer", answer)
registry.register("refuse", refuse)
spec = WorkflowSpec(
    name="guarded",
    entry_node="guard",
    nodes={
        "guard": WorkflowNode(
            id="guard",
            handler="guard",
            config={"checks": {"pii": {"type": "pii", "action": "redact"}, "inject": {"type": "injection"}}},
            input_mapping={"text": "message"},
            output_mapping={"text": "safe_text"},
            transitions={"allow": "answer", "rewrite": "answer", "block": "refuse", "error": "refuse"},
        ),
        "answer": WorkflowNode(id="answer", handler="answer"),
        "refuse": WorkflowNode(id="refuse", handler="refuse"),
    },
    terminal_nodes=["answer", "refuse"],
)
result = asyncio.run(WorkflowEngine(spec, registry).run({"message": "mail me at bob@example.com"}))
assert result.state["reply"] == "Handled: mail me at [REDACTED:email]"
```

Policy rules for module nodes themselves (`WorkflowEngine(guardrail_policy=...)`) are not part of
this release; agent nodes are covered, since their runs use the process default.

## In the service

`nexura-runtime serve --guardrail-policy policy.yaml` (or `NEXURA_GUARDRAIL_POLICY`, or
`create_api_app(guardrail_policy=...)`) enforces a policy file on the agent runs of the service
(see the exceptions above).
See [Service and Studio](studio-and-service.md#guardrail-policy) for what the operator gets.

## Security notes

- Substring and regex blocklists for commands are a **first filter only**. `"rm -rf"` is
  bypassed by `rm -fr`, `rm -r -f`, `rm --recursive --force`, `find / -delete`,
  `python -c "import shutil; shutil.rmtree('/')"`, `$(echo cm0gLXJmIC8= | base64 -d)`, variable
  splitting (`r=rm; $r -rf`), a script file written by one tool call and run by another, or a
  different tool altogether. A denied keyword list for topics fails the same way.
- Model-based checks are probabilistic: published recall for the best guards is 70% to 97%
  depending on the dataset, worse in Hindi, and unknown in Tamil, Bengali or
  Hinglish. Adaptive attackers target them.
- **Real protection is structural**: (1) a **tool allowlist** (offer the model only the tools
  it needs; `default_tool_action: block` in the policy); (2) **human approval for
  `non_idempotent` effects** (the A6 effect classes plus the approval contract); (3)
  **`nexura-sandbox`** (WASI, bubblewrap with seccomp, gVisor) for any code or shell execution,
  with no network by default; (4) least-privilege credentials per tool; (5) treating every tool
  result and document as untrusted data. Guardrail checks reduce risk and give an audit trail.
  They are not a security boundary.

The measurements behind these notes are in [Guardrails, hooks and limits](guardrails-hooks-limits.md)
(the built-in pre-filters) and the [guardrails package guide](guardrails-package.md) (published
guard-model results). The test suite keeps the first note honest: it runs 7 of the 9 bypasses
above, the seven command strings, against a `contains: "rm -rf"` blocklist rule and asserts that
each gets through and that an allowlist or an approval gate stops every one; the other two (a
script written by one call and run by another, a different tool) are not single commands.

## Limits

- The `input` stage runs once per run, not on resume or recovery (as agent input guardrails).
  Handoff targets do not run their own input guardrails; use `model_input` to cover them.
- `model_input` checks only the new messages of each request, not the whole conversation.
- `scrub` on `model_output` redacts the reply text only; tool-call arguments are checked on
  `tool_input`, where they can only be blocked.
- `require_approval` matches tool facts and arguments only (no checks), and needs a run that can
  pause (a `Runner`; refused over MCP, as other approval gates).
- One policy per process for the service. Per-workspace policies are not supported yet.
- `AgentCoordinator` runs are refused (not covered) under a policy with input, model or output
  rules. AgentDefinition subagents: their own model calls are not checked by
  model_input/model_output rules, and their own input and output stages are not checked either,
  until a follow-up lands; the parent's stages still check what reaches the user. Workflow module nodes are not covered yet.
- A `require_approval` rule is skipped when a check-less `block` or `fail` rule (or allowlist
  mode) refuses the call anyway: a person is never asked to approve a refused call. Block rules
  with checks run after approval, in `before_tool`.
- `capture_content` (a redacted excerpt in audit records) is not supported: audit records never
  hold content.
