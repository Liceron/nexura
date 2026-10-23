# Evaluation access and recorded evidence

The [evaluation API](evals.md) requires an authenticated workspace context. Resource-scoped
principals (`Principal.resources` set, including an empty list) receive HTTP 403 on evaluation
routes, including imports, simulations, report lists/details and comparisons. These routes
do not support per-workflow/starter resource scopes. Workspace permissions still apply:
trace import requires `edit`; replay and simulation require `run`.

Imported traces and their nested run/event references cannot name existing runs or workflows
owned by another workspace. Existing unowned Studio records are available only to the default
local owner. Standalone imported captures are stored in the active workspace. Replay and report
access recheck recorded source references so an ownership change fails closed.

For simulation and Studio evaluation, register factories in
`app.state.eval_agents_by_workspace[workspace_id]`
under agent IDs and persona models in `app.state.eval_user_models` under workspace IDs.
The previous string-keyed `eval_agents` and singular `eval_user_model` registrations
are available only to the default local owner. Factories must return fresh agents. The built-in
`local-fake` echo agent remains available in every workspace. Dataset lookup always uses the
active workspace. Studio target discovery and dispatch use the same registration policy.
Workspace registrations override legacy registrations with the same agent ID; the built-in
`local-fake` agent keeps its reserved meaning. Saved Studio agents continue to use their
workspace-scoped `agent:<id>` targets.

Studio cases saved from runs retain `metadata.source_run_id`. Their input and expected output
remain subject to the source run's current ownership. Dataset lists/details/exports, updates
and grading recheck each source run and its workflow. Saved reports retain the source IDs in
case metadata and a complete `source_run_ids` list, independently of later dataset edits or
deletion. Report lists/details and comparisons recheck these sources; ownership changes,
missing sources and unowned sources in shared mode deny access. Grading also checks sources
again before saving or returning its result.

Historical Studio reports without source provenance require their original dataset with an
unchanged content hash so its sources can be checked. If that dataset changed or was deleted,
access fails with `SOURCE_PROVENANCE_UNAVAILABLE` (403). New reports with no captured sources
record an empty source list and remain readable independently of their dataset.

An explicit imported snapshot takes precedence over a Studio row with the same run ID.
Its timestamp, workflow/agent identity, output and capture remain one coherent snapshot;
workflow, agent and inclusive time filters apply to that snapshot. A run is graded once.

Trajectory extraction combines message and `tool_started` ordering constraints using run/call
IDs as anchors. An event sequence can fill gaps in message capture. Message-only requested calls
remain present when their position can be determined. Disjoint or conflicting captures with no
unique order fail grading explicitly. No order is guessed.

Numeric arguments retain exact integer/decimal precision, including numeric normalisation
and absolute/relative tolerances. Booleans remain distinct from numbers; nonfinite values
fail comparison. Sensitive content is redacted recursively before persistence, including
numeric card values and list outputs. JSON structure, typed grader scores and top-level imported
trace identity/time fields are preserved. Graders compare original evidence before result
redaction; imported evidence has already been redacted.
