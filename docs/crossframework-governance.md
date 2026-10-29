# Govern another framework's tools through Nexura

The [cross-framework experiment](../experiments/crossframework/) runs an external
agent loop against a separate Nexura refund-tool process over MCP stdio. The client
uses only the official `mcp` Python SDK and the standard library. Its model,
reviewer and refunds are explicitly fake; no provider or payment service is called.

**Enforcement works with ANY unmodified MCP client:** blocking, human approval and
exact-action binding happen on the server before dispatch. A client cannot bypass
the policy by ignoring a pending result or changing arguments. Only the pause/resume
experience needs host or adapter support: recognizing pending state, retaining the
exact action, waiting for an operator, retrying and displaying the outcome correctly.
An ordinary MCP client can also repeat the original tool arguments with an
`approval_id`; server enforcement does not require a custom client implementation.

No LangGraph, OpenAI Agents SDK or CrewAI adapter exists or was tested in this
prototype. The official MCP Python SDK client is the tested external caller.

## Run the offline experiment

Use Python 3.12 and a repository checkout. The experiment owns its environment,
pins `mcp==2.3.0`, and uses local path dependencies on `nexura-agents`,
`nexura-guardrails` and `nexura-runtime`. The reference SDK is not a dependency of
the main packages or their test suite.

```bash
cd experiments/crossframework
uv sync --frozen
uv run --frozen python -m crossframework.demo
uv run --frozen pytest -q
```

The default demo creates and removes temporary state. To retain its local evidence,
run `uv run --frozen python -m crossframework.demo --state-dir .refund-pilot`.
The automatic reviewer is a **fake**, not evidence of human authorization. It runs
as a separate process calling Nexura's existing Python approval API; no approval
decision tool is exposed to the agent.

| Amount (USD) | Result | Fake effect |
| --- | --- | --- |
| 50 | `completed`, `executed: true` | One refund |
| 200 | `pending_approval`, `executed: false` | None |
| Same 200 action after operator approval | `completed`, `executed: true` | One refund |
| 900 | `blocked`, `executed: false` | None |
| Repeat a completed request id with identical arguments | Same saved receipt | No further refund |

## Policy and approval

Stock `AgentMCPServer` refuses approval-gated plain tools with an MCP error and
human-readable text; it does not issue an approval id or a resume operation. This
experiment explicitly registers one governed handler on `AgentMCPServer.session`.
It uses public `GuardrailPolicy.check_call` and `call_needs_approval` APIs followed
by the existing `ApprovalGate` and SQLite-backed `ApprovalPolicyManager`.
The rest of the SDK's MCP server behavior is unchanged.

The policy has a $500 per-action ceiling and requires approval above $100:

```python notest
from nexura_guardrails import Rule, When

rules = [
    Rule("refund-ceiling",
         When(stage="tool_input", tools="refund", args={"$.amount": {"gt": 500}}),
         action="block"),
    Rule("refund-review",
         When(stage="tool_input", tools="refund", args={"$.amount": {"gt": 100}}),
         action="require_approval"),
]
```

Amounts are strict positive integer USD dollars. Strings, booleans, fractions,
negatives, unknown fields and empty identifiers are refused before dispatch.
The approval payload contains every business argument: `request_id`, `order_id`
and `amount`. The gate also binds workspace, run, node, action kind/name and policy
digest. A different amount, order or request id cannot reuse the grant, including
when a changed amount is below the approval threshold. The ceiling blocks a $900
attempt even when it carries an approval for $200.

The gate uses the stable approval id `refund-<request_id>`. Repeated proposals for
the same business request return the same approval, rather than creating two items
in a review queue. Once approved, an exact retry can include that id or resolve it
from the original request id. Rejected or expired requests stay denied: a new
business action requires a new request id and a fresh review. Never change the id
merely to retry an action with an uncertain outcome.

## Wire contract

Responses use a JSON envelope in MCP text content, not `structuredContent`, an
elicitation, a suspended RPC or a task continuation. Every completed response has
`isError=false` and `executed: true`, with a saved receipt. Repeating a completed
request returns that same receipt; `executed: true` describes its recorded outcome,
not a second execution.

Pending, blocked, refused, rejected, expired, invalid, conflict and unknown outcomes use
`isError=true` and `executed: false`. The first line is plain English; the remaining
text is the JSON envelope. For example:

```text
NOT EXECUTED: The refund is awaiting operator approval. No money has moved.
{"status":"pending_approval","executed":false,"approval_id":"refund-R-1","payload_digest":"...","retry":"Repeat the exact arguments after operator review."}
```

The first line of an `outcome_unknown` reply says that this retry did not dispatch,
but the **prior action may have moved money**. It must not assert non-execution of
an interrupted attempt. `executed: false` in this case means no new attempt ran.
Investigate before any further business action; the server never repeats an unknown
dispatch. A host must not translate an MCP error or unknown state into “refund sent.”

Reusing a `request_id` with different arguments returns `request_id_conflict` and
never dispatches. Its `prior_phase` field and first line say what that id already
did: `completed` (a refund was paid), `dispatching` (money may have moved) or `ready`
(nothing has run). Only `ready` says that no money has moved for the id.
If a `ready` request is pending approval and the caller has changed its arguments,
the operator should reject that superseded request.

The client must retain original arguments outside model output. On pending state,
wait for an operator decision and repeat the exact call. Polling while undecided
returns the same pending approval; the demo waits for its fake reviewer and retries
once. Blocked/refused/rejected/expired results are explicit failures, not success
messages. After success, retries resolve through the journal even if the original
reply was lost or the approval has since expired.

For manual review, use `crossframework.client.connect` and `call_refund` to propose
the action instead of the automatic demo harness. From a separate operator terminal,
inspect the authoritative request and then decide it:

```bash
uv run --frozen python -m crossframework.review --state-dir .refund-pilot \
  --approval-id refund-R-1
# Review the complete action_payload, identity, expiry and payload_digest above.
uv run --frozen python -m crossframework.review --state-dir .refund-pilot \
  --approval-id refund-R-1 --decision approved \
  --expected-digest DIGEST_FROM_REVIEWED_REQUEST
```

Use `--decision rejected` to deny. The command calls
`ApprovalPolicyManager.submit_decision` with an `ApprovalDecisionRecord`, not a
workflow REST resume endpoint. The operator has protected local database access;
the configured reviewer role is not an authentication system.

## Durable dispatch and audit

The dispatch journal shares the SQLite approval database and has a primary key on
`request_id`. It binds that key to the full business payload digest. Before gate
evaluation or dispatch, a transaction reserves the request. Pending and denied
outcomes are recorded and may be reevaluated without executing an effect. Before
an effect, the grant is atomically claimed and the dispatch reservation is already
durable. Independent server processes cannot send the same business request twice.

Once a receipt is saved, exact retries return the saved success. If an attempt
stops after its reservation without a saved outcome, retries return
`outcome_unknown` and never send again. This conservative behavior also covers a
crash before the effect actually started. Local journaling prevents automatic
duplicates; it does not prove exactly-once payments across an external provider.

| File | Evidence |
| --- | --- |
| `approvals.sqlite3` | Exact actions, expiry, reviewer decisions, claims and `crossframework_dispatches` outcomes |
| `policy.jsonl` | Policy decisions, without action values |
| `lifecycle.jsonl` | Pending, decision, claim, dispatch and completion events |
| `fake-refunds.jsonl` | Toy side-effect ledger and receipts, including actual amounts and order ids |

A pre-dispatch policy audit failure stops the call. A `completed` audit failure
after the refund returns **success with `audit_warning`**, because money already
moved. It never reports a refund as unexecuted merely because post-effect logging
failed. Persisted receipts remain authoritative. Journal/storage failure after an
effect can leave future retries unknown; inspect the receipt and provider before
reconciliation. These local logs are not tamper-evident or one atomic transaction
with a payment provider. Protect the state directory and reviewer access.

## Host support and native adapters

| Application | Enforcement and integration work |
| --- | --- |
| Any unmodified MCP client/framework | Server-side block, approval and exact-action binding apply today. Host support improves pending-state display, operator review, exact-argument retention, polling and replayed receipt handling. |
| LangGraph direct Python tools | No native adapter exists. A wrapper would govern dispatch, persist the pending action with a graph interrupt/checkpoint, and validate/claim the retained exact action on resume. |
| OpenAI Agents SDK function tools without MCP | No native adapter exists. A wrapper would govern tool invocation and map pending state onto its approval/resume lifecycle without granting the model decision authority. |
| CrewAI direct tools | No native adapter exists. A tool wrapper and application pause/resume coordinator would need the same binding, review and journal contract. |

No LangGraph, OpenAI Agents SDK or CrewAI integration was tested. Native adapters
need pinned official APIs and fake integration tests for concurrent calls,
retries, cancellation and recovery. Applications must route governed operations
exclusively through the server or wrapper; alternate direct payment access defeats
this control. Prompt instructions alone cannot enforce it.

The $500 limit is per action, not an aggregate budget or protection against refund
splitting. HTTP transports, optional MCP tasks/elicitation, Windows and desktop
clients were not verified in this experiment. Real payments need provider
idempotency keys, reconciliation and an authenticated reviewer surface. No live
model/payment, arbitrary crash-recovery, production-readiness or customer-outcome
claim is made.
