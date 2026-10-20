# Studio browser tests (P12)

Each spec starts a real local backend (`create_api_app(profile='local-owner')` through `harness.ts`) and the
Vite frontend on random ports, and uses only local fakes. Run a spec by path, for example
`npx playwright test tests/e2e/connectors.spec.ts`.

## Coverage of P1 to P11

| Item | Studio surface | Spec |
| --- | --- | --- |
| P1 Threat model and secure defaults | Partial. Imported flows are reviewed before saving and unsafe code is rejected with line numbers; no browser spec for auth-by-default (the harness runs `local-owner`) or the MCP stdio allowlist, which are service-level rules covered by Python tests | `code_export.spec.ts` (unsafe import), `chat_to_build.spec.ts` (review unsaved draft), `negative.spec.ts`, `credentials.spec.ts` (zero-secret UX) |
| P2 Composite steps | Parallel, map and sub-workflow editing, undo/redo, invalid input | `composite_steps.spec.ts` |
| P3 Run traces | Trace timeline, usage and cost, replay | `traces.spec.ts` (also `runs.spec.ts`) |
| P4 Evals view | Run suite, compare, regression gate | `evals_view.spec.ts` (also `evaluations_and_triggers.spec.ts`) |
| P5 Export to code | Export as Python, import back, unsafe code rejected | `code_export.spec.ts` (also `integrations.spec.ts` runnable export) |
| P6 Template gallery | List, filter, preview, use, unknown template 404 | `templates.spec.ts` |
| P7 Chat-to-build | Describe, review draft, create | `chat_to_build.spec.ts` |
| P8 Publish | Moved to the closed edition with publishing (EDITIONS_PLAN step 4): `enterprise/studio-web/tests/e2e/publish.spec.ts` | (none in the open-source suite) |
| P9 Teams and secrets | Credential references (open source). Organisation members, invites and roles moved to the closed edition with organisations (EDITIONS_PLAN step 5): `enterprise/studio-web/tests/e2e/organisation.spec.ts` | `credentials.spec.ts` |
| P10 Connectors pack | Module Registry drawer lists the 15 connectors | `connectors.spec.ts` (added in P12; P14 setup test: operations, credential picker) |
| P11 Knowledge | Ingestion, hybrid search, citation cards (knowledge page); chat citation footnotes | `knowledge.spec.ts`; `chat_citations.spec.ts` (added in P12); `chat_citations_real.spec.ts` (real tool output, no interception) |
| P13 Durable runs | Answer an interrupt (schema-checked), checkpoint history and state, fork with edited state to a child run with parent link, sub-runs tab, approval dialog details (mapped writes: count, first 20, cap, re-approval note) | `durable_runs.spec.ts` (starts its own backend: default registry plus three offline handlers, one calling `interrupt()`, because no builtin or template workflow does) |
| P17b Red-team preset | Evals view Red-team tab: run the pack against a saved workflow (offline model), per-case results, both scorers, failure flagged, server error shown | `redteam.spec.ts` (the failure and error tests rewrite the response; the pass test is a real run) |
| P20b Skills | Skills page: create from pasted SKILL.md, text-only rendering (markup shown as text), version labels, label filter, remove; agent node inspector skills picker (module descriptor stubbed; the spec does not run an agent node) | `skills.spec.ts` |
| P22 Workflow tests | Run console "Save as test" (new dataset, then append with prefilled output key; secret redacted and redaction count shown; duplicate case name refused), workflow page Tests tab (case counts, run against the current revision, per-case pass/fail, no-baseline note, regression gate failing after the workflow is changed), another workflow lists none of these tests. | `workflow_tests.spec.ts` |
| P25 Runs reachable after reload | A run waiting for input: workflow URL keeps the run and a reload reopens its Run Console, Runs tab row, Approvals inbox "Waiting for input" row, open `#/runs/{id}`, reload, answer there; unknown run id shows a notice. A run started over the API at an approval gate: inbox "Open run" link, reload `#/runs/{id}`, approve there | `durable_runs.spec.ts` (written, not yet run) |
| B34 Studio dead ends | Module drawer: Channels lists email and WhatsApp, Connectors lists the 15 connectors, service-internal modules absent (also in search); create dialog without the test fixture (server still rejects it); templates missing-credentials link | `modules.spec.ts`, `workflows.spec.ts`, `templates.spec.ts` |
| P26 Agents in Studio | Agents page: empty state names `NEXURA_CHAT_MODELS` and the offline fake, editor validation (bad name, output schema that is not JSON), create on `fake-deterministic`, description markup shown as text, edit bumps the revision; workflow `nexura.agents.agent` node inspector picker with input and output keys saved on the node config; the node runs over the API and writes the offline reply; Playground agent select sends `agent_id` without `model` and hides the model select; delete needs a second click and the node then shows the agent as missing | `agents.spec.ts` (written, not yet run; conflicts, roles, approval details, refused tool calls, turn limit and the red-team agent target are covered by `tests/unit/p26_agents_studio.test.tsx`) |
| B39 Webhook setup | Schedule and webhook forms pick the workflow from the workspace (select, no free-text id); after registering a webhook the signing secret is shown once with the delivery URL and the v2 headers; the spec reads the secret, signs a delivery with Node `crypto` (`v2.<ts>.<event id>.<canonical body>`) and POSTs it to the real `/deliver` route, both through the URL Studio shows (the Vite `/api` proxy) and directly on the backend, expecting `accepted` and no duplicate; the secret is not in local or session storage or the URL and is gone after dismiss; the workflow page Triggers button opens the triggers view filtered to that workflow with it preselected | `evaluations_and_triggers.spec.ts` (written, not yet run; picker paging, empty workspace, copy button, console and storage checks and snippet contents are covered by `tests/unit/b39_webhook_setup.test.tsx` and `tests/unit/b39_triggers_route_shell.test.tsx`; the generated curl and Python snippets are run against the deliver route by `tests/test_b39_webhook_snippet.py`) |

## Notes on the added specs

- `connectors.spec.ts`: the connectors appear in the builder's Module Registry drawer only. Since B22 the API
  reports `requires_approval` and `write_operations` for every connector with a write operation (all but
  IMAP), and the spec checks that each of those cards shows the "Approval Gate" badge and IMAP's does not.
  It also checks that a registered credential's value appears in no page, input, storage, or network
  traffic other than the one request that creates it. The stale `product-gap` annotation of the first test was removed in P14 (operations and credential types are now shown).
  P14 (second test): the drawer lists each operation as "reads" or "writes - needs approval" with the
  required credential type; the node inspector's `credential_ref` is a picker limited to credentials of the
  connector's type (id and description only; a connector with none shows "none of type X" and a link to
  Credentials) and the `operation` input is a select with the same markers. Credentials are registered
  through the API. No secret appears in the page, form values, storage or responses. (The Credentials form's
  module credential types and the literal storage of the selected operation are covered by
  `tests/unit/connector_setup_fix.test.tsx` and `tests/test_workflow_literal_inputs.py`, not by this spec.)
- `chat_citations.spec.ts`: covers citations that no real tool would produce (a forged chunk id, an unknown
  document, a markup title). The chat run and the chunk lookups are real; only the final `chat_completed`
  stream event is rewritten (Playwright `route.fetch` then `route.fulfill`) to add those citations. Real
  citations from the playground's `knowledge_search` tool are covered by `chat_citations_real.spec.ts`.
- `chat_citations_real.spec.ts`: since P15 the playground has a `knowledge_search` tool. The spec ingests a
  document through the Knowledge page, asks the playground a question, and checks that the footnotes come
  from the real tool output and link to the chunk. No route interception.
