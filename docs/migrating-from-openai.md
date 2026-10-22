# Migrating from OpenAI Agent Builder and OpenAI Evals

OpenAI is retiring both products. The OpenAI deprecations page gives these dates
(checked 2026-10-18):

- **2026-10-31**: existing OpenAI Evals become read-only.
- **2026-11-30**: the Evals dashboard and API shut down, and so does Agent Builder. ChatKit
  stays available.

Agent Builder's export is OpenAI Agents SDK code. In the builder you click **Code**, then
**Advanced integration**, and copy the workflow code. The Agent Builder guide does not
mention a JSON export, so this page starts from that code. OpenAI Evals data is JSONL, one
`{"item": {...}}` object per line, and Nexura imports it as a Studio eval dataset.

Everything on this page runs offline with `FakeModel`. Nothing here has been checked against
a live OpenAI account or a real export. Treat the mappings as a starting point, and compare
answers on your own data before you switch.

Related: [Agents](agents.md), [Guardrails, hooks and limits](guardrails-hooks-limits.md),
[Human approval](approvals.md), [Evaluating agents and teams](evals.md),
[Importing Langflow flows](langflow-import.md).

## Concept map

| Agent Builder / OpenAI Agents SDK | Nexura | Notes |
| --- | --- | --- |
| `Agent(name, instructions, tools, handoffs, output_type)` | `nexura_agents.Agent(name, instructions, model, tools, handoffs, output_type)` | `model` is required, and it is an object: `FakeModel` offline, or a provider model ([Models and providers](providers.md)). |
| `Runner.run_sync(agent, input)` / `await Runner.run(...)` | `agent.run(input)` or `Runner().run(agent, input)` / `await Runner().run_async(agent, input)` | `result.final_output` is the raw text in both. In Nexura, `result.output` is the validated value. |
| `@function_tool` (shown as `@tool` from `agents.decorators` in current docs) | `@tool` from `nexura_agents` | The schema comes from type hints and the docstring in both ([Tools](tools.md)). |
| `handoffs=[agent]`, `handoff(agent, tool_name_override=, on_handoff=, input_type=, input_filter=)` | `handoffs=[agent]`, `Handoff(agent, tool_name=, on_handoff=, input_filter=)` | Same default tool name: `transfer_to_<agent_name>` ([Handoffs](agents.md#handoffs), [structured handoff input](agents.md#structured-handoff-input-and-conditional-handoffs)). |
| `@input_guardrail` / `@output_guardrail` returning `GuardrailFunctionOutput(tripwire_triggered=...)` | A plain function returning `GuardrailResult.passed()` / `.tripped(info)`, in `input_guardrails` / `output_guardrails` | One exception, `GuardrailTripwireTriggered` with `.stage`, replaces `InputGuardrailTripwireTriggered` / `OutputGuardrailTripwireTriggered`. Nexura input guardrails finish before the first model call, which is OpenAI's `run_in_parallel=False` behaviour. |
| Agent Builder **Guardrails** node (PII, jailbreak, hallucination) | [`nexura-guardrails`](guardrails-package.md): `JudgeCheck` (an LLM judge with your policy text) behind the [built-in pre-filters](guardrails-hooks-limits.md#built-in-pre-filters-heuristic) `block_pii`, `no_prompt_injection`, `max_length`, `allowed_topics` | The pre-filters are regex and checksum heuristics, not OpenAI's classifiers. Safety-classifier adapters come in a later release. There is no hallucination check. |
| `needs_approval=True`, `result.interruptions`, `result.to_state()`, `state.approve()`, `Runner.run(agent, state)` | `@tool(requires_approval=True)`, `result.status == "paused"`, `result.interruption`, `runner.resume(agent, interruption, approve=True)` | Save a pause with `interruption.to_json()` and load it with `RunInterruption.from_json()` ([Human approval](approvals.md)). |
| Agent Builder **Human approval** node | Approval tools, or approval gates in [Workflows](workflows.md#approval-gates) | |
| `SQLiteSession("user_123", "conversations.db")` with `session=` | `Runner(session_store=SQLiteSessionStore(path))` with `session_id="user_123"` | `MemorySessionStore()` keeps history in memory. Nothing replaces `OpenAIConversationsSession`: history stays in your store ([Sessions](agents.md#sessions-and-conversation-memory)). |
| `output_type=MyModel` | `output_type=MyModel` | Validation errors go back to the model so it can retry ([Structured output](structured-output.md)). |
| Tracing in the OpenAI dashboard | OpenTelemetry spans from `nexura_agents.tracing.instrument()`, exported to your collector | Old traces stay with OpenAI and are not migrated ([Observability](observability.md)). |
| `HostedMCPTool` | MCP tools ([MCP and OpenAPI tools](mcp-and-openapi.md)) | Your process connects to the server, not OpenAI's. |
| `FileSearchTool(vector_store_ids=...)`, **File search** node | Knowledge: `HybridKnowledgeIndex` ([Knowledge](knowledge.md)) | Vector stores do not move. Re-ingest the source files. |
| `WebSearchTool` | No hosted search. `web_fetch_tool()` fetches pages you name ([Tools](tools.md)), or call a search API as an OpenAPI or MCP tool. | |
| **If/else**, **While**, **Set state**, **Transform** nodes | [Workflows](workflows.md) (branches, loops, shared state), or plain Python around `agent.run` | The exported code already turns these nodes into Python. |
| ChatKit embed | No equivalent in the open-source edition. The embeddable chat widget (publishing) is part of the closed edition. | Not a drop-in replacement. ChatKit itself stays available. |
| Eval items JSONL | `nexura-runtime evals import-openai` or Studio's **Import JSON** | See [Evals data](#evals-data) below. |
| `testing_criteria` (graders) | Studio scorers: `exact_match`, `contains`, `regex` | Only matching checks are mapped. The rest are listed, not guessed. |
| Eval runs | Studio eval runs, `evaluate()` in code, and the CI gate ([Evaluating agents](evals.md)) | |

## Worked example: an exported Agents SDK script

This is the kind of script the export gives you: one tool, one handoff and one input
guardrail. We wrote it from the OpenAI Agents SDK docs (Agents, Tools, Handoffs, Guardrails).
It is illustrative, not a real export. It needs the `openai-agents` package and an API key,
so the test suite does not run it.

```python notest
import re

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    Runner,
    function_tool,
    input_guardrail,
)

CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,16}\b")


@function_tool
def lookup_order(order_id: str) -> str:
    """Look up an order's shipping status."""
    return f"Order {order_id} shipped on Monday."


@input_guardrail
async def no_card_numbers(ctx, agent, input):
    text = input if isinstance(input, str) else str(input)
    return GuardrailFunctionOutput(
        output_info=None, tripwire_triggered=bool(CARD_NUMBER.search(text))
    )


billing = Agent(name="Billing agent", instructions="You handle refunds and invoices.")
triage = Agent(
    name="Triage agent",
    instructions="Answer order questions with lookup_order. Hand refunds to the billing agent.",
    tools=[lookup_order],
    handoffs=[billing],
    input_guardrails=[no_card_numbers],
)

print(Runner.run_sync(triage, "Where is order A1?").final_output)
try:
    Runner.run_sync(triage, "My card is 4111 1111 1111 1111")
except InputGuardrailTripwireTriggered:
    print("blocked")
```

The same agents in Nexura. The only new piece is the model: `FakeModel` scripts the replies,
so the example runs offline and its assertions document what happens. Use a real model in
production, for example `OpenAIChatModel` ([Models and providers](providers.md)).

```python
import re

from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailResult,
    GuardrailTripwireTriggered,
    Runner,
    ToolCall,
    tool,
)

CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,16}\b")


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order's shipping status."""
    return f"Order {order_id} shipped on Monday."


def no_card_numbers(ctx, text: str) -> GuardrailResult:
    if CARD_NUMBER.search(text):
        return GuardrailResult.tripped({"reason": "card number in the message"})
    return GuardrailResult.passed()


billing = Agent(
    name="Billing agent",
    instructions="You handle refunds and invoices.",
    model=FakeModel(responses=["A refund for A1 is on its way."]),
)
triage = Agent(
    name="Triage agent",
    instructions="Answer order questions with lookup_order. Hand refunds to the billing agent.",
    model=FakeModel(
        responses=[
            [ToolCall(id="c1", name="lookup_order", arguments={"order_id": "A1"})],
            "Order A1 shipped on Monday.",
            [ToolCall(id="c2", name="transfer_to_billing_agent", arguments={"reason": "refund"})],
        ]
    ),
    tools=[lookup_order],
    handoffs=[billing],
    input_guardrails=[no_card_numbers],
)

runner = Runner()
answer = runner.run(triage, "Where is order A1?")
assert answer.output == "Order A1 shipped on Monday."
assert answer.tool_calls_count == 1

refund = runner.run(triage, "I want a refund for A1")
assert refund.final_output == "A refund for A1 is on its way."
assert refund.agent_path == ["Triage agent", "Billing agent"]

try:
    runner.run(triage, "My card is 4111 1111 1111 1111")
except GuardrailTripwireTriggered as exc:
    assert exc.stage == "input" and exc.guardrail == "no_card_numbers"
else:
    raise AssertionError("expected the guardrail to trip")
```

What changed:

- `function_tool` became `tool`, and the function body stayed the same.
- The guardrail takes `(ctx, text)` and returns `GuardrailResult`. It does not take
  `(ctx, agent, input)` or return `GuardrailFunctionOutput`. The Nexura guardrail gets the
  text of the latest user message.
- The handoff is still `handoffs=[billing]`, and the model sees the same
  `transfer_to_billing_agent` tool.
- `Runner` is an instance (`Runner()`) that carries sessions, limits and hooks.

### Approvals, sessions and structured output

The Agents SDK's `needs_approval` / `RunState` flow and `SQLiteSession` translate like this.

```python
from pydantic import BaseModel

from nexura_agents import Agent, FakeModel, MemorySessionStore, Runner, ToolCall, tool
from nexura_agents.types import RunInterruption

refunds = []


@tool(requires_approval=True)  # Agents SDK: needs_approval=True
def issue_refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    refunds.append((order_id, amount))
    return f"refunded {amount}"


class Reply(BaseModel):
    message: str
    refunded: bool


agent = Agent(
    name="Billing agent",
    instructions="Refund when asked. Reply as JSON.",
    model=FakeModel(
        responses=[
            [ToolCall(id="r1", name="issue_refund", arguments={"order_id": "A1", "amount": 40})],
            '{"message": "Refunded 40 for A1.", "refunded": true}',
            '{"message": "You were refunded 40 for A1.", "refunded": false}',
        ]
    ),
    tools=[issue_refund],
    output_type=Reply,
)
# Agents SDK: SQLiteSession("user_123", "conversations.db"); here SQLiteSessionStore(path).
runner = Runner(session_store=MemorySessionStore())

paused = runner.run(agent, "Refund A1, 40", session_id="user_123")
assert paused.status == "paused" and refunds == []  # Agents SDK: result.interruptions
assert [c.name for c in paused.interruption.pending] == ["issue_refund"]

# Agents SDK: state.to_json() / RunState.from_json(); state.approve(); Runner.run(agent, state).
stored = paused.interruption.to_json()
done = runner.resume(agent, RunInterruption.from_json(stored), approve=True)
assert done.output == Reply(message="Refunded 40 for A1.", refunded=True)
assert refunds == [("A1", 40)]

again = runner.run(agent, "What did you refund?", session_id="user_123")
assert again.output.message == "You were refunded 40 for A1."
```

## Evals data

Export the eval items, and the eval definition if you want its testing criteria mapped,
**before 2026-10-31**. From then on the evals are read-only, and on 2026-11-30 the API shuts
down.

The converter reads these shapes:

- JSONL with one `{"item": {...}}` per line, the documented upload format (`purpose="evals"`).
- Rows with a `sample` next to the item, from an inline run source
  `{"type": "file_content", "content": [...]}` or a JSON array of rows. The `sample` (an
  output a model already produced) is counted and dropped, because a Studio eval runs its
  target again.
- Bare objects per line, such as the open-source `openai/evals` registry's
  `{"input": [chat messages], "ideal": "..."}`. A chat-message input becomes the last user
  message, and the full list is kept in `metadata.openai_input_messages`. When `ideal` is a
  list, the first answer becomes `expected_output` and all of them are kept in
  `metadata.openai_acceptable_outputs`.

A UTF-8 byte order mark at the start of the file is ignored. Lines end at `\n` (a trailing
`\r` is dropped), so a raw U+2028 or U+0085 inside a JSON string does not split a line.

Field mapping:

- **Input**: `--input-field`, otherwise the first of `input`, `question`, `prompt`, `query`,
  `text`. If none of those exist, the import stops and lists the fields it found.
- **Expected output**: `--expected-field`, otherwise the `{{ item.<field> }}` reference of a
  string check in `--criteria`, otherwise the first of `expected_output`, `expected`, `ideal`,
  `answer`, `correct_label`, `label`, `reference`, `ground_truth`, `target`.
- **Name**: `--name-field` (values must be unique), otherwise `item-0001`, `item-0002` and so
  on, numbered by line.

OpenAI renders `{{ item.<field> }}` into the reference as text, and the import does the same:

- A number or `true`/`false` expected value becomes its text, so `4` becomes `"4"` and
  `exact_match` passes the output `4`. The report lists those lines.
- When some items have the expected field and others do not, the items without it get an empty
  `expected_output`, as OpenAI renders a missing field. The check then fails, where a case with
  no `expected_output` would score "n/a" and pass. The report names those lines. When no item
  has the field, the cases get no `expected_output`, and the report says so.

Every other item field is kept in `metadata.openai_item`. Each case gets the tag
`openai-evals` and its source line in `metadata.openai_line`. Field names accept
`ticket_text`, `item.ticket_text`, `{{ item.ticket_text }}` or a dotted path into a nested
object.

The result goes through the same validation as a dataset saved in Studio:

- at most 200 cases, and a file over the limit is refused, not truncated;
- 8,000 characters per input;
- 512 KiB per dataset (and 4 MiB for the raw file);
- secret-looking values are redacted before anything is stored.

A malformed line stops the import, and the error names the line: bad JSON, not an object, a
missing input field, or image or file content. A line nested deeper than 64 levels, or one
with a lone UTF-16 surrogate escape such as `\ud800` (valid JSON, but not valid text), is
also a line error, never a crash. `--skip-invalid` converts the rest and lists the skipped
lines.

### From the command line

```bash
# Offline: writes the dataset JSON and a report of every choice the converter made.
nexura-runtime evals import-openai items.jsonl --dataset-id it-tickets \
  --input-field ticket_text --criteria eval.json --out it-tickets.json --report report.json

# Also PUT it to a running service (the API key is read from NEXURA_API_KEY).
NEXURA_API_KEY=nxk_... nexura-runtime evals import-openai items.jsonl --dataset-id it-tickets \
  --input-field ticket_text --url http://127.0.0.1:8765
```

The command never connects anywhere unless you pass `--url`. It will not send an API key over
plain `http` to a host that is not loopback. Exit code 1 means the file could not be converted
(the line errors are printed), and exit code 2 means a usage, file or upload error.
`--criteria` takes the eval object (the one with `testing_criteria`) or the bare criteria list.

### In Studio

**Evaluations & Benchmarks > Studio evals > Datasets > Import JSON** accepts a pasted OpenAI Evals file or a
`.jsonl` file. Studio detects the format (including a JSON array of registry rows, or one
registry row) and shows fields for the input and expected names,
optional criteria, and "skip invalid lines". The conversion runs on the service
(`POST /api/v1/evaluations/studio/datasets/import-openai`), using the same Python code as the
CLI. The route returns `{dataset, report}` and saves nothing. Review the cases, then save the
dataset as usual.

### In code

```python
import json

from nexura_agents import Agent, FakeModel, evaluate
from nexura_agents.evals import exact_match

from nexura_runtime.evaluation.openai_import import convert_openai_evals, dataset_to_cases

jsonl = "\n".join(
    json.dumps({"item": item})
    for item in [
        {"ticket_text": "My monitor won't turn on!", "correct_label": "Hardware"},
        {"ticket_text": "I'm in vim and I can't quit!", "correct_label": "Software"},
    ]
)
criteria = [
    {
        "type": "string_check",
        "name": "Match output to human label",
        "input": "{{ sample.output_text }}",
        "operation": "eq",
        "reference": "{{ item.correct_label }}",
    },
    {"type": "score_model", "name": "Helpfulness", "model": "any", "input": []},
]

result = convert_openai_evals(
    jsonl, "it-tickets", input_field="ticket_text", testing_criteria=criteria
)
report = result.report()
assert report["expected_field"] == "correct_label"  # read from the criterion's reference
assert report["scorers"] == [{"type": "exact_match"}]
assert [u["name"] for u in report["unmapped_criteria"]] == ["Helpfulness"]
dataset = result.dataset  # PUT this to /api/v1/evaluations/studio/datasets/it-tickets

labels = {"My monitor won't turn on!": "Hardware", "I'm in vim and I can't quit!": "Hardware"}
agent = Agent(
    name="Ticket labeller",
    instructions="Reply with one category.",
    model=FakeModel(handler=lambda messages, tools: labels[messages[-1].content]),
)
scores = evaluate(agent, dataset_to_cases(dataset), [exact_match()])
assert [c.passed for c in scores.cases] == [True, False]
```

### Testing criteria

A criterion is mapped only when a Studio scorer checks the same thing. The report says where
the check differs, and lists every criterion it did not convert, with the reason.

| OpenAI criterion | Studio scorer | Difference |
| --- | --- | --- |
| `string_check` `eq`, reference `{{ item.<expected field> }}` | `exact_match` | Nexura ignores case and repeated whitespace. `eq` is case-sensitive. |
| `string_check` `ilike`, reference `{{ item.<expected field> }}` | `contains` | Same check. |
| `string_check` `like`, reference `{{ item.<expected field> }}` | `contains` | Nexura ignores case. `like` is case-sensitive. |
| `string_check` `eq` / `like` / `ilike` with a literal reference | `regex` (escaped literal; `\A...\Z` for `eq`, `(?i)` for `ilike`) | The regex scorer fails an output longer than 2,048 characters ("output too long"), where OpenAI still checks it. A literal whose escaped pattern is over 200 characters, or that looks like a secret, is not mapped. |
| `string_check` `ne` | not mapped | No scorer passes when the output differs from a value. |
| `string_check` on anything but `{{ sample.output_text }}` (for example tool calls) | not mapped | Use `tool_called` or `tool_trajectory` with an expected trajectory. |
| `string_check` that references another item field | not mapped | `expected_output` comes from one field. |
| `text_similarity` (`fuzzy_match`, `bleu`, `rouge_*`, `cosine`, ...) | not mapped | No similarity scorer. |
| `score_model`, `label_model` | not mapped | `llm_judge` needs a judge model and a rubric you write. |
| `python` | not mapped | Nexura never runs uploaded code. Write a scorer function in your own code ([Scorers](evals.md#scorers)). |
| `multi` | not mapped | |

The report is redacted like the dataset: criterion names, reasons and line errors do not
carry secret-looking values. A Studio run takes one scorer of each type, so a second criterion
that needs a different `regex` is listed as unmapped. The scorers in the report are suggestions: pass them as
`scorers` to `POST /api/v1/evaluations/studio/runs`, or use the matching scorer functions with
`evaluate()` in code. Studio's run form sends no scorers, and with no scorers given, a dataset
with expected outputs is scored with `exact_match`.

## What has no equivalent

- Hosted tools that run on OpenAI's servers: web search, code interpreter, hosted shell,
  image generation, computer use, tool search and programmatic tool calling.
- OpenAI vector stores, Conversations API sessions and response compaction. Bring your own
  data and history.
- The OpenAI traces dashboard, and stored eval runs and their scores. Export what you need to
  keep. Nexura re-runs evals and does not import past results.
- Model-graded and similarity graders, as automatic conversions. Python graders are never
  converted.
- Agent Builder's visual canvas as a file. There is no JSON export to import, so the
  migration starts from the exported code.
- Parallel input guardrails. Nexura's input guardrails always finish before the model is
  called.

## Checklist

1. **Before 2026-10-31** (Evals become read-only): export every eval's items as JSONL and save
   each eval's definition (its `testing_criteria`). Save past run results elsewhere if you
   need them; they are not imported.
2. Convert each file with `nexura-runtime evals import-openai ... --report report.json`. Read
   `unmapped_criteria` and `warnings`, and fix any line errors or rerun with `--skip-invalid`.
3. **Before 2026-11-30** (Agent Builder and the Evals API shut down): in Agent Builder, open
   each workflow, click **Code**, choose **Advanced integration**, and save the Agents SDK
   code. Note the published workflow versions you depend on.
4. Translate each script with the concept map: tools, handoffs, guardrails, approvals,
   sessions, structured output. Replace hosted tools as listed under
   [What has no equivalent](#what-has-no-equivalent).
5. Write `FakeModel` tests for the translated agents ([Testing your agents](testing.md)), then
   run the imported datasets against them in Studio or with `evaluate()`.
6. Run the same datasets against your real model, and compare pass rates with the last OpenAI
   Evals results you saved before you switch traffic.
7. If you embed with ChatKit, it stays available from OpenAI. The open-source edition has no
   embeddable chat widget; publishing is part of the closed edition.

## Sources

Read on 2026-10-18. "Verified" means the page was opened and the fact checked on it.

- Verified: [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations): Evals
  read-only 2026-10-31; Evals dashboard and API, and Agent Builder, shut down 2026-11-30;
  ChatKit remains available; announced 2026-06-03.
- Verified: [Agent Builder guide](https://developers.openai.com/api/docs/guides/agent-builder):
  **Code** > **Advanced integration** gives the workflow as Agents SDK code. The guide does not
  mention a JSON export.
- Verified: [Agent Builder node reference](https://developers.openai.com/api/docs/guides/node-reference):
  Start, Agent, Note, File search, Guardrails, MCP, If/else, While, Human approval, Transform,
  Set state.
- Verified: [Working with evals](https://developers.openai.com/api/docs/guides/evals):
  `{"item": {...}}` JSONL, `data_source_config` with `item_schema` and `include_sample_schema`,
  the `string_check` example with `{{ item.correct_label }}` and `{{ sample.output_text }}`.
- Verified: [Graders](https://developers.openai.com/api/docs/guides/graders): `string_check`
  (`eq`, `ne`, `like`, `ilike`; the page also spells the second one `neq`, so the converter
  accepts both), `text_similarity` metrics, `score_model`, `python`, `multi`.
- Verified, from search-result extracts of the page:
  [Create eval run](https://developers.openai.com/api/reference/resources/evals/subresources/runs/methods/create):
  a `file_content` source is `content: [{item, sample?}]`. The page itself did not open
  (404 to the fetcher).
- Verified: OpenAI Agents SDK docs:
  [overview](https://openai.github.io/openai-agents-python/),
  [tools](https://openai.github.io/openai-agents-python/tools/),
  [handoffs](https://openai.github.io/openai-agents-python/handoffs/),
  [guardrails](https://openai.github.io/openai-agents-python/guardrails/),
  [sessions](https://openai.github.io/openai-agents-python/sessions/),
  [human in the loop](https://openai.github.io/openai-agents-python/human_in_the_loop/).
- Verified: [openai/evals build-eval.md](https://github.com/openai/evals/blob/main/docs/build-eval.md):
  registry samples have `"input"` (preferably chat format) and `"ideal"` (a string or a list of
  strings). No example line was quoted from the repository.
- Unverified: the old `platform.openai.com/docs/api-reference/evals/create` page (403 to the
  fetcher), and `label_model` fields, which the Graders page does not describe.
