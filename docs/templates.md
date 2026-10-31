# Starter templates

Nexura ships a catalog of 30 starter workflows. Each one is a versioned JSON file, validates
against the workflow engine, and has a test fixture that runs it end to end offline, with no
network and no model provider. The same tests run in CI for every template.

| Template id | Category | What it shows |
| --- | --- | --- |
| `faq-chatbot` | support | Answer from your FAQ by closest match; escalate to a person instead of guessing. |
| `sentiment-escalation` | support | Unhappy customers get a goodwill credit a person approves, then an empathetic reply. |
| `support-desk-refund-approval` | support | Classify a message; a refund waits for a human approval before it is issued. |
| `ticket-triage` | support | Route tickets to billing, account, shipping or technical queues with an SLA; outages page on-call first. |
| `lead-qualification` | sales | Score leads; hot leads become a CRM deal after approval, the rest are nurtured. |
| `sales-outreach-email` | sales | Industry-specific outreach email, queued only after a person approves the text. |
| `employee-onboarding` | hr | Dated onboarding checklist by department; accounts created after IT approves; welcome note. |
| `leave-request` | hr | Count working days; short leave auto-approved, longer leave to the manager, over-balance declined. |
| `resume-screening` | hr | Match job-relevant skills and years against a job; shortlist or draft a decline for a recruiter. |
| `expense-approval` | finance | Policy limits and the receipt rule; over-limit claims wait for a manager. |
| `invoice-reconciliation` | finance | Match payments to invoices by reference, then amount; report exceptions. |
| `code-review-checklist` | engineering | Scan a diff for TODOs, debug output, possible secrets and huge changes; write a verdict. |
| `incident-log-summary` | engineering | Count errors by service, rate severity, page on-call after approval, write a summary. |
| `release-notes` | engineering | Group conventional commits, call out breaking changes, publish after approval. |
| `seo-metadata` | content | Keywords, a title under 60 characters, a description under 155 and a slug. |
| `social-post-generator` | content | One announcement to X, LinkedIn and Instagram posts within each length limit; scheduled after approval. |
| `document-summarizer` | knowledge | Extractive summary: the most central sentences, in their original order. |
| `rag-qa-citations` | knowledge | Answer from the documents you supply with numbered citations; refuse when nothing matches. |
| `customer-feedback-analysis` | data | Tag feedback with themes and sentiment; summarise the top themes. |
| `data-quality-check` | data | Missing fields, invalid emails and duplicate ids; publish or quarantine with a report. |
| `structured-extraction` | data | Free text to a typed invoice record; incomplete documents are flagged, not guessed. |
| `research-team` | agents | Planner, researcher, writer and reviewer agents in sequence. |
| `trip-planner` | agents | Plan a trip from interests, price it and trim paid activities to fit the budget. |
| `meeting-notes` | automation | Attendees, decisions and owned action items from notes; shared after approval. |
| `order-alerts` | automation | Alert the sales channel about orders above a threshold; log the rest. |
| `scheduled-report` | automation | Collect, summarise and queue a report; ships a suggested weekly schedule. |
| `approval-workflow` | governance | Small requests pass; large ones pause for approve or reject. |
| `content-moderation` | governance | Blocked terms, personal data, link spam and shouting: publish, review or reject. |
| `contract-review` | governance | Flag risky clauses; high-risk contracts need legal sign-off. |
| `pii-redaction` | governance | Replace emails, Luhn-checked card numbers, phones and IPs with labels. |

## What "offline" means here

The handlers behind the templates (`tpl_*`, in `nexura_runtime.templates.handlers` and `nexura_runtime.templates.library`) are
deterministic stand-ins. Steps that stand for an LLM call run a real `Agent` on a scripted
`FakeModel`. Steps that stand for an outside system (a payment API, a mail server) only record
what they would do: the refund is `simulated`, and the report goes to a dry-run outbox
(`sent: false`). A template is therefore a runnable, tested skeleton of the flow and its
approval gates. To go live, register your own handlers or modules and point the instantiated
workflow's nodes at them.

`required_credentials` lists, by name only, what a live version needs (for example
`model_api_key`). The offline template does not use them. They are never stored in a template.

## File format

Each file in `src/nexura_runtime/templates/data/<id>.json` has:

- `template_version` (currently `1`), `id` (equal to the file name), `title`, `description`,
  `category`, `difficulty` (`beginner`, `intermediate` or `advanced`), `tags`.
- `required_modules`: the handler names the spec uses (checked against the spec on load).
- `required_credentials`: credential names.
- `spec`: a normal workflow spec.
- `suggested_trigger` (optional): a schedule you can attach with the triggers API.
- `sample_input`: state that runs the template.
- `fixture.runs`: test cases with `input`, an optional `approval` (`approve` or `reject` at a
  node), `expect_status`, `expect_state` (dotted paths), `expect_contains`, `expect_absent` and
  `expect_path`.

A file that is malformed, whose spec does not validate, or whose `required_modules` do not
match the spec raises `TemplateError` when the catalog loads.

## Run a template in Python

```python
import asyncio

from nexura_runtime.templates import get_template, list_templates, run_fixture

print([t.id for t in list_templates(category="support")])

template = get_template("approval-workflow")
assert template is not None
outcomes = asyncio.run(run_fixture(template))
for outcome in outcomes:
    assert outcome.failures == [], outcome.failures
    print(outcome.name, "->", outcome.status)
```

## REST API

| Method and path | Purpose |
| --- | --- |
| `GET /api/v1/templates?category=&tag=` | List the catalog (metadata, `categories` and `tags` facets). |
| `GET /api/v1/templates/{id}` | One template with `spec`, a Mermaid `mermaid` diagram and `sample_input`. |
| `POST /api/v1/templates/{id}/instantiate` | Create a workflow from it. Body: optional `name`, `workflow_id`. |

Instantiating follows the rules of `POST /api/v1/workflows`: viewers get `403`, the full
create-time validation and approval policy apply, and the new workflow belongs to the caller's
workspace only. An unknown template is `404 TEMPLATE_NOT_FOUND`. The response is:

```json
{
  "workflow_id": "wf-...",
  "workflow": {},
  "template_id": "research-team",
  "template_version": 1,
  "missing_credentials": ["model_api_key", "search_api_key"],
  "runs_offline": true,
  "suggested_trigger": null
}
```

`missing_credentials` names the credentials the workspace does not hold; nothing is filled in
for you. The suggested trigger is returned but not created.

## Studio

Open **Templates** in the sidebar (or "Browse the template gallery" in Create Workflow). Filter by
category, tag or search text, preview a template's graph read-only, then choose **Use template**
to create the workflow in the current workspace.

## Adding a template

Add `data/<id>.json` with a fixture, add any new `tpl_*` handler to
`nexura_runtime.templates.handlers`, and run `uv run pytest tests/test_templates.py`. The new
file is picked up and tested automatically.
