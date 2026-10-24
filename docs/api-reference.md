# API reference

Generated from docstrings by `scripts/gen_api_reference.py`; do not edit by hand.
The guides in this folder explain how the pieces fit together; this page lists
the selected public exports with their signatures.
MIT open-source edition: single user, this machine only; for a team or a public endpoint use the hosted or enterprise edition.

## Failure reproduction (`nexura_agents.repro`)

### `ReproBundle`

class `ReproBundle(data: 'dict[str, Any]') -> None`

~~~text
One versioned JSON document. Loading never imports or executes bundle code.
~~~

- `load(cls, path: 'str | Path') -> 'ReproBundle'`
  Read a supported bundle; reject schema mismatches before execution.
- `save(self, path: 'str | Path') -> 'None'`
  Use best-effort redaction of known secret shapes; review a bundle before sharing it.

### `ReplayResult`

class `ReplayResult(reproduced: 'bool', outcome: 'dict[str, Any]', first_divergence: 'str | None' = None, notes: 'tuple[str, ...]' = ()) -> None`

~~~text
Outcome comparison and the first observed divergence, if any.

A different completed output or workflow final state is reported as
``"output differs at $.amount: 500 != 5000"`` (replayed value first, then the
recorded one). ``notes`` says what could not be compared, for example an older
bundle that did not record the output.
~~~


### `ReproRecorder`

class `ReproRecorder(*, credential_values: 'dict[str, str] | None' = None) -> 'None'`

~~~text
Opt-in recorder for Runner and WorkflowEngine, including approval resumes.

Use one recorder per run. The original agent, model and tools are not mutated.
Persist ``export_repro_bundle(recorder).data`` in RunRecord.metadata['repro']
to make it available to the runtime CLI's stored-run lookup.
~~~

- `resume_agent(self, interruption: 'Any', *, approve: 'bool' = False, reject: 'dict[str, str] | None' = None) -> 'Any'`
  Record an approval or rejection; normal Runner approval checks still apply.
- `resume_workflow(self, run_id: 'str', approval_id: 'str', *, decision: 'str' = 'approve', state_update: 'dict[str, Any] | None' = None, engine: 'Any' = None) -> 'Any'`
  Record a workflow approval decision without retaining authority to act.
- `run_agent(self, runner: 'Any', agent: 'Any', prompt: 'str') -> 'Any'`
  Record a Runner run, including handoffs; exceptions keep their normal behavior.
- `run_workflow(self, engine: 'Any', initial_state: 'dict[str, Any]', **run_kwargs: 'Any') -> 'Any'`
  Record node boundary results, including composite-node fixtures.

### `export_repro_bundle`

function `export_repro_bundle(run_record_or_checkpoint: 'Any', *, credential_values: 'dict[str, str] | None' = None) -> 'ReproBundle'`

~~~text
Export a complete recording or RunRecord with metadata['repro'].

Incomplete historical checkpoints are refused rather than fabricating fixtures.
Supply resolved credential values by reference name to scrub copies in free text.
~~~

### `replay_repro_bundle`

function `replay_repro_bundle(bundle: 'ReproBundle') -> 'ReplayResult'`

~~~text
Run a bundle offline through Runner/WorkflowEngine using only inert fixtures.
~~~

### `offline_guard`

function `offline_guard() -> 'Any'`

~~~text
Block connect/connect_ex/sendto/sendmsg and socket resolver entry points.

Process-wide for the duration: run replay in a dedicated process when other
application tasks need network access. Blocks socket create_connection,
gethostbyname/gethostbyname_ex/gethostbyaddr/getaddrinfo/getnameinfo and the
active asyncio loop's getaddrinfo/getnameinfo. sendmsg is patched if present.
Direct _socket APIs, send/sendall, subprocesses and other loops are not guarded.
No subprocess or plugin code is loaded from a bundle.
~~~

### `register_workflow_replay`

function `register_workflow_replay(replay: 'WorkflowReplay') -> 'None'`

~~~text
Install the workflow replayer (the runtime package registers it on import).

The SDK does not depend on the workflow runtime; workflow bundles replay only
when ``nexura_runtime`` is installed and imported.
~~~

## Agent SDK (`nexura_agents`)

### `Agent`

class `Agent(name: 'str', instructions: 'Instructions', model: 'ModelProtocol | str', *, tools: 'Sequence[Tool] | None' = None, max_turns: 'int' = 10, max_tool_calls: 'int' = 20, timeout: 'float | None' = None, output_type: 'type[BaseModel] | dict[str, Any] | None' = None, output_retries: 'int' = 1, input_guardrails: 'Sequence[GuardrailFn]' = (), output_guardrails: 'Sequence[GuardrailFn]' = (), parallel_tool_calls: 'bool' = False, description: 'str' = '', memory: 'Memory | None' = None, handoffs: 'Sequence[Agent | Handoff]' = (), max_handoffs: 'int' = 10, history_processors: 'Sequence[HistoryProcessor]' = (), prepare_tools: 'PrepareTools | None' = None, model_settings: 'ModelSettings | None' = None, tool_use_behavior: 'ToolUseBehavior' = 'run_llm_again', output_validators: 'Sequence[OutputValidator]' = (), reset_tool_choice: 'bool' = True, ask_user: 'bool' = False, stream_guard: 'StreamGuardMode | StreamGuard' = 'auto', harness: 'Harness | None' = None) -> 'None'`

~~~text
Stateless configuration defining an agent.

An Agent encapsulates the name, system instructions, model adapter,
registered tools, and execution limits.

Agent configurations are completely immutable and stateless, allowing
a single Agent instance to be safely reused across concurrent runs without
state collisions or leaking conversation history between callers.
~~~

- `add_handoffs(self, *targets: 'Agent | Handoff') -> 'None'`
  Add handoff targets after construction (needed for agents that hand back to each other).
- `as_tool(self, name: 'str | None' = None, description: 'str | None' = None, *, runner: 'Runner | None' = None) -> 'Tool'`
  Expose this agent as a tool another agent can call.
- `clone(self, **overrides: 'Any') -> 'Agent[OutputT]'`
  A new agent with the same configuration and ``overrides`` applied.
- `definition_digest(self) -> 'str'`
  A stable ``sha256:<hex>`` of what a durable run depends on.
- `native_output_schema(self) -> 'dict[str, Any] | None'`
  The schema to hand to the model's native structured-output mode, or ``None``.
- `override(self, **overrides: 'Any') -> 'Iterator[Agent[OutputT]]'`
  Temporarily change this agent in place, e.g. ``with agent.override(model=fake):``.
- `render_instructions(self, ctx: 'RunContext[Any]') -> 'str'`
  System prompt for this run, including the output schema when typed.
- `render_instructions_async(self, ctx: 'RunContext[Any]') -> 'str'`
  Like :meth:`render_instructions`, awaiting async instruction functions.
- `run(self, prompt: 'Prompt', **kwargs: 'Any') -> 'RunResult[OutputT]'`
  Run this agent with a default ``Runner``. Shortcut for ``Runner().run(agent, ...)``.
- `run_async(self, prompt: 'Prompt', **kwargs: 'Any') -> 'RunResult[OutputT]'`
  Async form of :meth:`run`.
- `to_mermaid(self, *, include_tools: 'bool' = True) -> 'str'`
  A Mermaid flowchart of this agent, its handoffs and tools (see ``diagrams``).

### `Runner`

class `Runner(session_store: 'SessionStore | None' = None, *, hooks: 'AgentHooks | Sequence[AgentHooks] | None' = None, usage_limits: 'UsageLimits | None' = None, model_settings: 'ModelSettings | None' = None, budget: 'Any | None' = None, max_handoffs: 'int | None' = None, checkpoint_store: 'RunCheckpointStore | None' = None, approval_ttl: 'float' = 3600.0, approval_signing_secret: 'str | bytes | None' = None, approval_store: 'ApprovalStore | None' = None, lease_ttl: 'float' = 30.0, default_hooks: 'bool' = True) -> 'None'`

~~~text
Orchestrates agent execution against models and registered tools.

Stateless: can be used to run multiple agents concurrently.
Supports optional persistent session storage across runs.
~~~

- `effective_hooks(self) -> 'AgentHooks | None'`
  The hooks a run started now would use: the hooks inherited from an enclosing
- `recover(self, agent: 'Agent[OutputT]', run_id: 'str', **kwargs: 'Any') -> 'RunResult[OutputT]'`
  Synchronous form of :meth:`recover_async`.
- `recover_async(self, agent: 'Agent[OutputT]', run_id: 'str', *, deps: 'Any' = None, on_uncertain: 'str' = 'pause', timeout: 'float | None' = None, session_store: 'SessionStore | None' = None, cancel: 'CancelToken | None' = None) -> 'RunResult[OutputT]'`
  Continue a run from its last checkpoint after a crash.
- `recover_stream_async(self, agent: 'Agent', run_id: 'str', *, deps: 'Any' = None, on_uncertain: 'str' = 'pause', timeout: 'float | None' = None, session_store: 'SessionStore | None' = None, stream_text: 'bool' = False, cancel: 'CancelToken | None' = None) -> 'AsyncGenerator[RunEvent, None]'`
  Event-stream form of :meth:`recover_async`.
- `resume(self, agent: 'Agent[OutputT]', interruption: 'RunInterruption', **kwargs: 'Any') -> 'RunResult[OutputT]'`
  Synchronous form of :meth:`resume_async`.
- `resume_async(self, agent: 'Agent[OutputT]', interruption: 'RunInterruption', *, approve: 'bool | Iterable[str] | Mapping[str, str | ApproveWith]' = (), reject: 'Mapping[str, str] | None' = None, answers: 'Mapping[str, str] | None' = None, deps: 'Any' = None, timeout: 'float | None' = None, session_store: 'SessionStore | None' = None, cancel: 'CancelToken | None' = None, outcomes: 'Mapping[str, ResolvedOutcome] | None' = None, actor: 'str | None' = None) -> 'RunResult[OutputT]'`
  Continue a paused run once every pending tool call has a decision.
- `resume_stream_async(self, agent: 'Agent', interruption: 'RunInterruption', *, approve: 'bool | Iterable[str] | Mapping[str, str | ApproveWith]' = (), reject: 'Mapping[str, str] | None' = None, answers: 'Mapping[str, str] | None' = None, deps: 'Any' = None, timeout: 'float | None' = None, session_store: 'SessionStore | None' = None, stream_text: 'bool' = False, cancel: 'CancelToken | None' = None, outcomes: 'Mapping[str, ResolvedOutcome] | None' = None, actor: 'str | None' = None) -> 'AsyncGenerator[RunEvent, None]'`
  Event-stream form of :meth:`resume_async`.
- `run(self, agent: 'Agent[OutputT]', prompt: 'Prompt', *, session_id: 'str | None' = None, history: 'list[Message] | None' = None, timeout: 'float | None' = None, session_store: 'SessionStore | None' = None, deps: 'Any' = None, cancel: 'CancelToken | None' = None, state: 'dict[str, Any] | None' = None, tags: 'Sequence[str]' = (), metadata: 'Mapping[str, Any] | None' = None) -> 'RunResult[OutputT]'`
  Synchronously execute an agent run.
- `run_async(self, agent: 'Agent[OutputT]', prompt: 'Prompt', *, session_id: 'str | None' = None, history: 'list[Message] | None' = None, timeout: 'float | None' = None, session_store: 'SessionStore | None' = None, deps: 'Any' = None, cancel: 'CancelToken | None' = None, state: 'dict[str, Any] | None' = None, tags: 'Sequence[str]' = (), metadata: 'Mapping[str, Any] | None' = None) -> 'RunResult[OutputT]'`
  Asynchronously execute an agent to completion (or approval pause, or cancel).
- `stream_async(self, agent: 'Agent', prompt: 'Prompt', *, session_id: 'str | None' = None, history: 'list[Message] | None' = None, timeout: 'float | None' = None, session_store: 'SessionStore | None' = None, deps: 'Any' = None, stream_text: 'bool' = False, cancel: 'CancelToken | None' = None, state: 'dict[str, Any] | None' = None, tags: 'Sequence[str]' = (), metadata: 'Mapping[str, Any] | None' = None) -> 'AsyncGenerator[RunEvent, None]'`
  Stream ordered lifecycle and execution events for an agent run.

### `Handoff`

class `Handoff(agent: 'Agent', *, tool_name: 'str | None' = None, description: 'str | None' = None, input_filter: 'InputFilter | None' = None, on_handoff: 'OnHandoff | None' = None, input_type: 'type[BaseModel] | None' = None, enabled: 'EnabledRule' = True) -> 'None'`

~~~text
A way for one agent to pass the conversation to another agent.

Args:
    agent: The agent that takes over.
    tool_name: Name of the tool the model calls. Defaults to ``transfer_to_<agent name>``
        in lower snake case.
    description: Tool description shown to the model. Defaults to a generic sentence.
    input_filter: Receives a copy of the conversation (without the system message) and
        returns the messages the target sees, for example to drop earlier tool noise. The
        result must keep every tool call paired with its result.
    on_handoff: Called with the run context and the model's reason (or, with
        ``input_type``, the validated input object) when the handoff happens. May be async.
    input_type: A Pydantic model for structured handoff arguments (for example a ticket
        with a priority). The model fills it in when it calls the handoff tool; the target
        agent finds it in ``ctx.metadata["handoff_input"]`` (as a dict).
    enabled: ``True``/``False`` or a function of the ``RunContext`` (sync or async),
        checked before each model call, like ``@tool(enabled=...)``. A disabled handoff
        is not offered, and a call to it fails like an unknown tool.
~~~

- `parsed_input(self, reason: 'str') -> 'Any'`
  The ``input_type`` object for a stored ``reason`` (or the reason text itself).
- `reason(self, arguments: 'dict[str, Any]') -> 'str'`
  Validated ``reason`` from the model's tool arguments.

### `handoff`

function `handoff(agent: 'Agent', **kwargs: 'Any') -> 'Handoff'`

~~~text
Shorthand for ``Handoff(agent, **kwargs)``.
~~~

### `SequentialAgent`

class `SequentialAgent(name: 'str', agents: 'Sequence[Agent | Team]', *, handoff: 'Callable[[Prompt, RunResult | TeamResult], Prompt] | None' = None, description: 'str | None' = None, runner: 'Runner | None' = None) -> 'None'`

~~~text
Run members one after another; each gets the previous member's output as its prompt.

The first member gets the team's prompt and the result's ``output`` is the last
member's. ``handoff(original_prompt, previous_result)`` replaces how the next prompt
is built (by default the previous output as text).
~~~


### `ParallelAgent`

class `ParallelAgent(name: 'str', agents: 'Sequence[Agent | Team]', *, combine: 'Callable[[dict[str, RunResult | TeamResult]], Any] | None' = None, max_concurrency: 'int' = 10, description: 'str | None' = None, runner: 'Runner | None' = None) -> 'None'`

~~~text
Run every member at once on the same prompt.

At most ``max_concurrency`` run at the same time. ``steps`` keep declaration order. The
output is ``combine({member name: result})`` when given, else ``{member name: output}``.
If one member raises, the others are cancelled and that error is raised.
~~~


### `LoopAgent`

class `LoopAgent(name: 'str', agent: 'Agent | Team', *, max_iterations: 'int', until: 'Callable[[RunResult | TeamResult], Any] | None' = None, feedback: 'Callable[[Prompt, RunResult | TeamResult], Prompt] | None' = None, description: 'str | None' = None, runner: 'Runner | None' = None) -> 'None'`

~~~text
Run one member repeatedly, feeding each answer back, until ``until`` or the cap.

Iteration 1 gets the team's prompt; later iterations get ``feedback(original_prompt,
last_result)``, by default the last output as text. The loop stops when
``until(last_result)`` is truthy or after ``max_iterations`` (1 to 100). Steps are
named ``iter-1``, ``iter-2`` and so on.
~~~


### `Team`

class `Team(name: 'str', *, description: 'str | None' = None, runner: 'Runner | None' = None) -> 'None'`

~~~text
Base class of teams: a named group of agents (or other teams) run as one unit.

Args:
    name: Team name; used for the tool name and in results.
    description: Shown to MCP clients and to models when the team is used as a tool.
    runner: Runs every member, so its hooks, ``UsageLimits`` and budget cover the whole
        team. A nested team without its own runner uses its parent's.
~~~

- `as_tool(self, name: 'str | None' = None, description: 'str | None' = None, *, runner: 'Runner | None' = None) -> 'Tool'`
  Expose this team as a tool another agent can call.
- `resume(self, interruption: 'TeamInterruption', **kwargs: 'Any') -> 'TeamResult'`
  Synchronous form of :meth:`resume_async`.
- `resume_async(self, interruption: 'TeamInterruption', *, approve: 'bool | Iterable[str] | Mapping[str, str]' = (), reject: 'Mapping[str, str] | None' = None, deps: 'Any' = None, timeout: 'float | None' = None, runner: 'Runner | None' = None, cancel: 'CancelToken | None' = None) -> 'TeamResult'`
  Continue a paused team once every pending tool call has a decision.
- `resume_stream_async(self, interruption: 'TeamInterruption', *, approve: 'bool | Iterable[str] | Mapping[str, str]' = (), reject: 'Mapping[str, str] | None' = None, deps: 'Any' = None, timeout: 'float | None' = None, runner: 'Runner | None' = None, cancel: 'CancelToken | None' = None) -> 'AsyncGenerator[RunEvent, None]'`
  Event-stream form of :meth:`resume_async`; see :meth:`stream_async`.
- `run(self, prompt: 'Prompt', *, deps: 'Any' = None, timeout: 'float | None' = None, runner: 'Runner | None' = None, cancel: 'CancelToken | None' = None) -> 'TeamResult'`
  Synchronously run the team. Refuses to run inside an active asyncio event loop.
- `run_async(self, prompt: 'Prompt', *, deps: 'Any' = None, timeout: 'float | None' = None, runner: 'Runner | None' = None, cancel: 'CancelToken | None' = None) -> 'TeamResult'`
  Run the team to completion, or until a member needs approval.
- `stream_async(self, prompt: 'Prompt', *, deps: 'Any' = None, timeout: 'float | None' = None, runner: 'Runner | None' = None, cancel: 'CancelToken | None' = None) -> 'AsyncGenerator[RunEvent, None]'`
  Run the team and yield events as it goes.
- `to_mermaid(self, *, include_tools: 'bool' = True) -> 'str'`
  A Mermaid flowchart of this team, its members and their tools.

### `TeamResult`

class `TeamResult(name: 'str', status: "Literal['completed', 'paused', 'cancelled']", output: 'Any' = None, steps: 'list[TeamStep]' = <factory>, usage: 'Usage' = <factory>, interruption: 'TeamInterruption | None' = None, stop_reason: 'str | None' = None) -> None`

~~~text
Outcome of a team run.

``output`` is the last meaningful output (a ``ParallelAgent`` gives a dict or its
``combine`` value). ``usage`` sums every sub-run, nested teams included. When
``status`` is ``"paused"``, ``interruption`` holds what a human must decide. When the
``cancel`` token passed to the run fired, ``status`` is ``"cancelled"``: ``output`` is
``None``, ``steps`` holds the members that finished or stopped, ``usage`` what they
used, and nothing is raised. A cancelled team cannot be resumed.
~~~


### `TeamStep`

class `TeamStep(path: 'str', member: 'str', result: 'RunResult | TeamResult') -> None`

~~~text
One member's run inside a team.

``path`` locates it: ``"0"``, ``"1"`` for positions, ``"2/1"`` for a member of a nested
team, ``"iter-2"`` for a loop iteration. ``result`` is a ``RunResult`` for an agent or a
``TeamResult`` for a nested team. After a resume, finished steps are rebuilt from the
saved output and usage only (no transcript or events).
~~~


### `TeamInterruption`

class `TeamInterruption(team: 'str', prompt: 'Prompt', completed: 'dict[str, _Done]' = <factory>, pending: 'dict[str, RunInterruption]' = <factory>, nested_prompts: 'dict[str, Prompt]' = <factory>, interruption_id: 'str' = <factory>, expires_at: 'float' = 0.0, signature: 'str | None' = None) -> None`

~~~text
A team paused because one or more members need human approval.

``pending`` maps the path of each paused agent to its ``RunInterruption`` (nested teams
are flattened into full paths such as ``"1/0"``). ``completed`` keeps the output and
usage of finished members so ``team.resume_async`` continues without re-running them.
Persist it with ``to_json()`` and rebuild it, even in another process, with
``TeamInterruption.from_json``. Outputs of finished members come back as plain JSON
values (a Pydantic model becomes a dict).

The whole envelope is authenticated, including prompts and completed usage.
Fresh processes must share the issuing Runner's signing key or approval store;
reviewed action digests alone do not authenticate team state. ``expires_at``
is the earliest pending member deadline. Without an approval/checkpoint store,
single use is the caller's responsibility, including for signed envelopes.
~~~

- `from_json(cls, data: 'str') -> 'TeamInterruption'`
  Rebuild an interruption from ``to_json`` output; reject unknown schema versions.
- `pending_calls(self) -> 'list[tuple[str, ToolCall]]'`
  Every tool call waiting for a decision as ``(key, call)``; key is ``path:call_id``.
- `pending_digests(self) -> 'dict[str, str]'`
  Show these to the reviewer; pass them back as ``approve={key: digest}``.
- `to_json(self) -> 'str'`
  Serialize the paused team (prompt, finished outputs and usage, paused runs).

### `tool`

function `tool(fn: 'Callable[..., Any] | None' = None, *, name: 'str | None' = None, description: 'str | None' = None, args_schema: 'type[BaseModel] | None' = None, requires_approval: 'ApprovalRule' = False, max_retries: 'int | None' = None, idempotent: 'bool' = False, enabled: 'EnabledRule' = True, input_guardrails: 'Sequence[GuardrailFn]' = (), output_guardrails: 'Sequence[GuardrailFn]' = (), timeout: 'float | None' = None, on_timeout: 'OnTimeout' = 'tell_model', retry: 'ToolRetry | None' = None, cache: 'bool' = False, effect: 'EffectName | None' = None, reconcile: 'ReconcileFn | None' = None, tags: 'Iterable[str]' = ()) -> 'Tool | Callable[[Callable[..., Any]], Tool]'`

~~~text
Decorator to define a typed Agent tool from a function.

``effect`` is what a durable run may do with the call after a crash: ``"pure"`` and
``"idempotent"`` are re-run (the latter with the same ``ctx.idempotency_key``),
``"non_idempotent"`` (the default, also for undeclared tools) pauses the run, and
``"reconcilable"`` runs ``reconcile(ctx, intent)`` to find out. ``idempotent=True``
is an alias of ``effect="idempotent"``.

``tags`` are labels that hooks, guardrails and policy rules can match
(``ctx.tool.tags``); the tool's ``origin`` is ``python:<module>.<qualname>``.
~~~

### `Tool`

class `Tool(name: 'str', fn: 'Callable[..., Any]', *, description: 'str | None' = None, args_schema: 'type[BaseModel] | None' = None, requires_approval: 'ApprovalRule' = False, max_retries: 'int | None' = None, idempotent: 'bool' = False, enabled: 'EnabledRule' = True, input_guardrails: 'Sequence[GuardrailFn]' = (), output_guardrails: 'Sequence[GuardrailFn]' = (), timeout: 'float | None' = None, on_timeout: 'OnTimeout' = 'tell_model', retry: 'ToolRetry | None' = None, cache: 'bool' = False, effect: 'EffectName | None' = None, reconcile: 'ReconcileFn | None' = None, origin: 'str | None' = None, tags: 'Iterable[str]' = ()) -> 'None'`

~~~text
Represents a typed, executable tool for an agent.
~~~

- `arun(self, **kwargs: 'Any') -> 'Any'`
  Asynchronously execute tool with given keyword arguments.
- `effect_class(self) -> 'EffectClass'`
  The tool's effect class (``non_idempotent`` when a subclass never set one).
- `execute(self, arguments: 'dict[str, Any]', ctx: 'RunContext[Any] | None' = None) -> 'str'`
  Validate arguments, execute the tool, and return JSON-serialized result.
- `info(self) -> 'ToolInfo'`
  This tool's metadata for hooks, guardrails and policies (``ctx.tool``).
- `is_enabled(self, ctx: 'RunContext[Any]') -> 'bool'`
  Whether the model may see and call this tool now (the ``enabled`` flag or rule).
- `needs_approval(self, ctx: 'RunContext[Any]', arguments: 'dict[str, Any]') -> 'bool'`
  Whether this call needs human approval, from the ``requires_approval`` flag or rule.
- `run(self, **kwargs: 'Any') -> 'Any'`
  Synchronously execute tool with given keyword arguments.
- `to_schema(self) -> 'dict[str, Any]'`
  Generate OpenAI-compatible tool JSON schema.
- `validate_arguments(self, arguments: 'dict[str, Any]') -> 'None'`
  Raise ``ToolValidationError`` if ``arguments`` do not fit this tool's schema.

### `RunContext`

class `RunContext(deps: 'DepsT', run_id: 'str', agent_name: 'str', turn: 'int' = 0, usage: 'Usage' = <factory>, messages: 'list[Message]' = <factory>, tool_call_id: 'str | None' = None, tool_name: 'str | None' = None, retry: 'int' = 0, metadata: 'dict[str, Any]' = <factory>, state: 'dict[str, Any]' = <factory>, tags: 'list[str]' = <factory>, tool_attachments: 'list[Any]' = <factory>, prompt: 'dict[str, Any] | None' = None, idempotency_key: 'str | None' = None, fencing_token: 'int | None' = None, tool: 'ToolInfo | None' = None, _nested_usage: 'list[Usage]' = <factory>, _events: '_EventChannel' = <factory>, _compaction_summaries: 'dict[str, str]' = <factory>, _save_compaction: 'Callable[[], Awaitable[None]] | None' = None) -> None`

~~~text
What a tool or callback can see about the run it belongs to.

``deps`` is whatever you passed to ``Runner.run(..., deps=...)``: database handles,
HTTP clients, the current user. A tool receives it by declaring a parameter
annotated ``RunContext[YourDeps]``; that parameter is never shown to the model.
~~~

- `add_usage(self, usage: 'Usage | None') -> 'None'`
  Count usage from work done inside a tool (e.g. a sub-agent) toward this run.
- `emit(self, name: 'str', **data: 'Any') -> 'None'`
  Send a custom progress event to whoever streams this run.
- `for_tool(self, tool_call_id: 'str', tool_name: 'str', retry: 'int', *, idempotency_key: 'str | None' = None, fencing_token: 'int | None' = None, tool: 'ToolInfo | None' = None) -> 'RunContext[DepsT]'`
  Copy scoped to one tool call (``tool``: its metadata, see ``ToolInfo``).
- `take_nested_usage(self) -> 'list[Usage]'`
  Return the usage recorded by nested runs and clear the buffer.

### `CancelToken`

class `CancelToken() -> 'None'`

~~~text
A one-way switch that asks one or more runs to stop.
~~~

- `cancel(self, reason: 'str | None' = None) -> 'None'`
  Ask every run using this token to stop. Safe from any thread; repeat calls do nothing.
- `wait(self) -> 'None'`
  Return once the token is cancelled (used by the runner to interrupt waits).

### `PromptRegistry`

class `PromptRegistry(store: 'PromptStore | None' = None) -> 'None'`

~~~text
Register, version, label and resolve prompts.
~~~

- `get(self, name: 'str', *, version: 'int | None' = None, label: 'str | None' = None) -> 'PromptVersion'`
  Fetch by ``version`` or ``label`` (default label ``latest``).
- `instructions(self, name: 'str', *, label: 'str | None' = None, version: 'int | None' = None, variables: 'Mapping[str, Any] | Callable[[RunContext[Any]], Mapping[str, Any]] | None' = None, pin: 'bool' = False) -> 'Callable[[RunContext[Any]], str]'`
  A callable for ``Agent(instructions=...)``.
- `names(self) -> 'list[str]'`
  Names of all registered prompts.
- `register(self, name: 'str', template: 'str', *, labels: 'tuple[str, ...] | list[str]' = ('latest',), metadata: 'Mapping[str, Any] | None' = None) -> 'PromptVersion'`
  Add a new version. The same template as the newest version returns that version.
- `set_label(self, name: 'str', label: 'str', version: 'int') -> 'PromptVersion'`
  Point ``label`` (for example ``production``) at ``version``; other versions lose it.
- `versions(self, name: 'str') -> 'list[PromptVersion]'`
  All versions of a prompt, oldest first.

### `PromptVersion`

class `PromptVersion(name: 'str', version: 'int', template: 'str', labels: 'tuple[str, ...]' = (), metadata: 'dict[str, Any]' = <factory>, created_at: 'str' = '', sha256: 'str' = '') -> None`

~~~text
One immutable version of a named prompt template.
~~~

- `from_dict(cls, data: 'Mapping[str, Any]') -> 'PromptVersion'`
  Rebuild from ``to_dict`` output; verifies the recorded sha256.
- `render(self, variables: 'Mapping[str, Any] | None' = None) -> 'str'`
  Substitute variables in one pass; extra variables are ignored.
- `to_dict(self) -> 'dict[str, Any]'`
  JSON-safe dict.

### `InMemoryPromptStore`

class `InMemoryPromptStore() -> 'None'`

~~~text
Process-local store, for tests and prototypes.
~~~

- `load(self, name: 'str') -> 'list[PromptVersion]'`
  All versions of ``name``, oldest first; empty if unknown.
- `names(self) -> 'list[str]'`
  Sorted prompt names in this store.
- `save(self, name: 'str', versions: 'list[PromptVersion]') -> 'None'`
  Replace all versions of ``name``.

### `FilePromptStore`

class `FilePromptStore(directory: 'str | os.PathLike[str]') -> 'None'`

~~~text
One JSON file per prompt name (``<directory>/<name>.json``) holding all versions.

Writes are atomic (temp file in the same directory, then ``os.replace``). The lock is shared by
every store on the same directory in this process and adds an OS file lock
(``.prompts.lock``, POSIX) so registrations from several processes serialise too.
~~~

- `load(self, name: 'str') -> 'list[PromptVersion]'`
  All versions of ``name``, oldest first; empty if unknown.
- `names(self) -> 'list[str]'`
  Sorted prompt names in this store.
- `save(self, name: 'str', versions: 'list[PromptVersion]') -> 'None'`
  Replace all versions of ``name``.

### `Skill`

class `Skill(name: 'str', description: 'str', instructions: 'str', tools: 'tuple[str, ...]' = (), resources: 'Mapping[str, str]' = <factory>, metadata: 'Mapping[str, Any]' = <factory>, format: 'str' = 'nexura') -> None`

~~~text
One skill version: text only, identified by name and the sha256 of its content.
~~~

- `from_directory(cls, path: 'str | Path', *, format: 'str' = 'nexura') -> 'Skill'`
  Load ``path/SKILL.md`` plus text files beside it as resources (no scripts).
- `from_markdown(cls, text: 'str', *, resources: 'Mapping[str, str] | None' = None, name: 'str | None' = None, format: 'str' = 'nexura') -> 'Skill'`
  Parse a ``SKILL.md``: ``---`` front matter (name, description, tools) then the body.

### `SkillLibrary`

class `SkillLibrary(skills: 'Iterable[Skill]' = ()) -> 'None'`

~~~text
Versioned skills by name, with labels such as ``production`` (thread-safe).
~~~

- `add(self, skill: 'Skill', *, labels: 'Sequence[str]' = ()) -> 'SkillVersion'`
  Store a new version (same content as the newest returns it); ``latest`` moves to it.
- `add_directory(self, path: 'str | Path') -> 'list[SkillVersion]'`
  Add every ``*/SKILL.md`` folder directly under ``path``, all or nothing.
- `catalog(self, *, label: 'str' = 'latest', heading: 'str' = 'Available skills') -> 'str'`
  Names and one-line descriptions, for an agent's instructions.
- `continue_numbering(self, name: 'str', next_version: 'int') -> 'None'`
  Start ``name``'s next version at ``next_version`` or later (never lower).
- `export(self) -> 'dict[str, Any]'`
  All skills, versions and labels as a JSON-ready dict (text only), for persistence.
- `from_directory(cls, path: 'str | Path') -> 'SkillLibrary'`
  Load a bounded directory of Agent Skills, in sorted order, without execution.
- `get(self, name: 'str', *, label: 'str' = 'latest', version: 'int | None' = None) -> 'Skill'`
  The skill at ``version``, or the one carrying ``label``.
- `import_versions(self, data: 'Mapping[str, Any]', *, replace: 'bool' = False) -> 'list[str]'`
  Restore skills from :meth:`export` output, all or nothing; returns the names added.
- `loader_tool(self, *, label: 'str' = 'latest', available_tools: 'Iterable[str]' = (), name: 'str' = 'load_skill', progressive: 'bool' = False) -> 'Tool'`
  A read-only ``load_skill(name)`` tool returning a skill's text (never runs anything).
- `names(self) -> 'list[str]'`
  Names of all stored skills.
- `names_with_label(self, label: 'str') -> 'list[str]'`
  Names of skills that have a version carrying ``label``.
- `next_version(self, name: 'str') -> 'int'`
  The number the next new version of ``name`` gets (also after it was removed).
- `remove(self, name: 'str', version: 'int | None' = None) -> 'int'`
  Delete every version of ``name``, or only ``version``; returns how many were removed.
- `render(self, name: 'str', *, label: 'str' = 'latest', available_tools: 'Iterable[str]' = (), progressive: 'bool' = False) -> 'str'`
  The text ``load_skill`` returns: instructions, resources and tool availability.
- `resource_tool(self, *, label: 'str' = 'latest') -> 'Tool'`
  Read one snapshot resource on demand. No filesystem access or execution.
- `set_label(self, name: 'str', label: 'str', version: 'int') -> 'None'`
  Point ``label`` at ``version``; other versions lose it.
- `versions(self, name: 'str') -> 'list[SkillVersion]'`
  All versions of a skill, oldest first.

### `Harness`

class `Harness(skills: 'SkillLibrary | None' = None, compaction: 'HistoryProcessor | None' = None, todo: 'bool' = False, filesystem: 'ScopedFilesystem | None' = None, require_write_approval: 'bool' = False) -> None`

~~~text
``Agent(harness=Harness(...))``; every capability is separately opt-in.

Share the same filesystem explicitly with child harnesses, then delegate with
``child.as_tool()``. Existing G2 hooks and budget inheritance remain authoritative.
No shell is installed. Skill ``allowed-tools`` metadata grants no permissions.
~~~

- `build_tools(self, available_tools: 'list[str]') -> 'list[Tool]'`
  Build normal SDK tools for the enabled capabilities, with effect metadata.
- `catalog(self) -> 'str'`
  Only skill names/descriptions enter the system prompt.

### `TodoItem`

class `TodoItem(*, id: Annotated[str, MinLen(min_length=1), MaxLen(max_length=64)], text: Annotated[str, MinLen(min_length=1), MaxLen(max_length=1000)], status: Literal['pending', 'in_progress', 'completed'] = 'pending') -> None`

~~~text
One task, identified across plan replacements by its unique id.
~~~


### `todo_tools`

function `todo_tools(*, requires_approval: 'bool' = False) -> 'list[Tool]'`

~~~text
Replace/read the run's plan. Replacement is idempotent by construction.

State is checkpointed with the tool result; the normal effect journal admits replay
after a crash before that checkpoint. Re-emitted progress events may repeat on replay.
Notifications contain only a monotonic version, total count and fixed status counts;
replay of a recorded call retains the current version and cannot revert newer state.
Configure sequential tool calls when ordering multiple plan replacements matters.
~~~

### `ScopedFilesystem`

class `ScopedFilesystem(root: 'str | Path | None' = None, *, max_file_bytes: 'int' = 65536, max_total_bytes: 'int' = 1048576, max_entries: 'int' = 256) -> 'None'`

~~~text
Share one instance explicitly between parent and child agents.

Without ``root`` files live in memory. With an existing ``root``, Linux Landlock
ABI >= 3 and no-follow traversal enforce a directory capability. A private worker
restricts only its own thread; unsupported disk isolation is refused.
Files must be regular, singly linked UTF-8 files. Writes replace atomically, never
follow links, and are conservatively non-idempotent for durable recovery.

Caps cover UTF-8 bytes and directory entries. Use one instance per root to serialize
cap checks. Landlock checks ancestry at opens/creates/renames, including both ends
of replacements, even when a host actor moves a traversed directory out of the root.
~~~

- `build_tools(self, *, require_write_approval: 'bool' = False) -> 'builtins.list[Tool]'`
  Tools use the normal runner approval, hook and guardrail pipeline.
- `close(self) -> 'None'`
  Release the directory descriptor. Subsequent operations are refused.
- `edit(self, path: 'str', old: 'str', new: 'str') -> 'str'`
  Replace exactly one literal match. Refuse empty or ambiguous matches.
- `list(self) -> 'list[str]'`
  List file paths, sorted, relative to the workspace.
- `read(self, path: 'str') -> 'str'`
  Read one bounded UTF-8 file; never follow a symlink.
- `write(self, path: 'str', content: 'str') -> 'str'`
  Replace a file, creating parent directories within the configured caps.

### `compact_context`

function `compact_context(max_tokens: 'int', *, trigger_ratio: 'float' = 0.9, target_ratio: 'float' = 0.7, model: 'Any' = None, max_summary_chars: 'int' = 2000, estimator: 'TokenEstimator | None' = None, timeout: 'float | None' = None) -> 'Callable[[RunContext[Any], list[Message]], Awaitable[list[Message]]]'`

~~~text
Opt-in budget-triggered compaction, preserving complete tool exchanges.

Old turns are removed as units. Exchanges spanning user boundaries are merged;
all system messages, the latest user turn, and unanswered calls (including pending
approvals) are pinned. If pinned content exceeds the target it stays intact and
``nexura.context_compacted`` reports overflow. Estimates exclude attachments and
tool schemas: reserve headroom in ``max_tokens``.

With ``model`` use the normal model admission path and count summary usage in the
run; without it trim deterministically. Errors, including budget refusal, propagate.
Cached summaries are scoped by run id and content (64 entries per processor).
With durable storage, completed summaries and their config/transcript digest are
checkpointed before the main model request. Recovery reuses the saved summary and
its already-accounted usage without calling the summary model again.
Stored history, approval records and checkpoints are never rewritten.
~~~

### `ModelRetry`

class `ModelRetry(message: str) -> None`

~~~text
Raise inside a tool (or output validator) to send ``message`` back to the model
and let it try again, instead of failing the run.
~~~


### `RunResult`

class `RunResult(run_id: 'str', final_output: 'str', messages: 'list[Message]', events: 'list[RunEvent]' = <factory>, usage: 'Usage' = <factory>, tool_calls_count: 'int' = 0, turns_count: 'int' = 0, duration_seconds: 'float' = 0.0, status: "Literal['completed', 'failed', 'cancelled', 'paused']" = 'completed', output: 'OutputT' = None, interruption: 'RunInterruption | None' = None, prompt: 'dict[str, Any] | None' = None, last_agent: 'Agent | None' = None, agent_path: 'list[str]' = <factory>, state: 'dict[str, Any]' = <factory>, usage_by_agent: 'dict[str, Usage]' = <factory>, usage_by_model: 'dict[str, Usage]' = <factory>, tags: 'list[str]' = <factory>) -> None`

~~~text
Final result of an agent run.
~~~


### `RunInterruption`

class `RunInterruption(run_id: 'str', agent_name: 'str', messages: 'list[Message]', pending: 'list[ToolCall]', usage: 'Usage' = <factory>, turn: 'int' = 0, tool_calls_count: 'int' = 0, session_id: 'str | None' = None, expected_revision: 'int' = 0, requests: 'int' = 0, memory_block: 'str' = '', active_agent: 'str | None' = None, agent_path: 'list[str]' = <factory>, handoffs_count: 'int' = 0, root_system: 'Message | None' = None, reason: 'str | None' = None, pending_handoff: 'dict[str, str] | None' = None, state: 'dict[str, Any]' = <factory>, question_ids: 'list[str]' = <factory>, usage_by_agent: 'dict[str, Usage]' = <factory>, usage_by_model: 'dict[str, Usage]' = <factory>, tags: 'list[str]' = <factory>, metadata: 'dict[str, Any]' = <factory>, output_attempts: 'int' = 0, agent_started_at: 'int' = 0, interruption_id: 'str' = <factory>, expires_at: 'float' = <factory>, signature: 'str | None' = None, action_digests: 'dict[str, str]' = <factory>, tool_scopes: 'dict[str, str]' = <factory>, compaction_summaries: 'dict[str, str]' = <factory>) -> None`

~~~text
A run paused before tool calls that need approval.

Persist it with ``to_json()`` and continue later, even in another process, with
``Runner.resume_async(agent, interruption, approve={id: reviewed_digest})``.
Unsigned JSON belongs in trusted server-side storage. Expiry is enforced by
Runner; single use requires a configured approval/checkpoint store.
~~~

- `action_digest(call: 'ToolCall', scope: 'str | None' = None) -> 'str'`
  Stable effect hash of tool name, canonical arguments and optional target scope.
- `decision_digest(self, call: 'ToolCall') -> 'str'`
  Bind a reviewed action to this run and SDK-issued approval occurrence.
- `from_json(cls, data: 'str') -> 'RunInterruption'`
  Rebuild an interruption from ``to_json`` output; reject unknown schema versions.
- `pending_digests(self) -> 'dict[str, str]'`
  Review these occurrence-bound hashes; return ``approve={id: digest}``.
- `to_json(self) -> 'str'`
  Serialize the paused run (transcript, pending calls, usage, session state) to JSON.

### `RunEvent`

class `RunEvent(type: 'RunEventType', run_id: 'str', timestamp: 'float' = <factory>, data: 'dict[str, Any]' = <factory>) -> None`

~~~text
An event emitted during agent execution.

Ordered per-run events allow callers to observe lifecycle progression,
tool invocations, intermediate model turns, and final outcomes.
~~~


### `RunEventType`

class `RunEventType(*values)`

~~~text
Types of execution lifecycle events.
~~~


### `Message`

class `Message(role: 'Role', content: 'str | None' = None, tool_calls: 'list[ToolCall] | None' = None, tool_call_id: 'str | None' = None, name: 'str | None' = None, attachments: 'list[Attachment] | None' = None, provider_data: 'dict[str, Any] | None' = None) -> None`

~~~text
Represents a conversation message in the agent history.
~~~

- `to_dict(self) -> 'dict[str, Any]'`
  Convert message to a dictionary representation.

### `Attachment`

class `Attachment(mime_type: 'str', data: 'str | None' = None, url: 'str | None' = None, name: 'str | None' = None) -> None`

~~~text
A file given to the model: inline bytes (base64) or a URL the provider fetches.

Build one with ``from_path``, ``from_bytes`` or ``from_url``. Which kinds a model
accepts depends on the provider; unsupported ones fail before any request is sent.
~~~

- `from_bytes(cls, data: 'bytes', mime_type: 'str', *, name: 'str | None' = None) -> 'Attachment'`
  Base64-encode bytes into an inline attachment; reject data over the size limit.
- `from_dict(cls, raw: 'dict[str, str]') -> 'Attachment'`
  Rebuild an attachment from the dict produced by ``to_dict``.
- `from_path(cls, path: 'str | Path', *, mime_type: 'str | None' = None) -> 'Attachment'`
  Read a local file; the MIME type is guessed from the extension.
- `from_url(cls, url: 'str', *, mime_type: 'str | None' = None) -> 'Attachment'`
  A file the provider downloads itself (``https://``, or ``gs://`` on Vertex AI).
- `to_dict(self) -> 'dict[str, str]'`
  Return a dict of the attachment's set fields (``mime_type`` plus data, url, name).

### `ToolCall`

class `ToolCall(id: 'str', name: 'str', arguments: 'dict[str, Any]', raw_arguments: 'str | None' = None) -> None`

~~~text
Represents a tool call requested by a model.
~~~


### `Usage`

class `Usage(prompt_tokens: 'int | None' = None, completion_tokens: 'int | None' = None, total_tokens: 'int | None' = None, is_complete: 'bool' = True, cached_prompt_tokens: 'int | None' = None, cache_creation_input_tokens: 'int | None' = None, cache_read_input_tokens: 'int | None' = None, reasoning_tokens: 'int | None' = None) -> None`

~~~text
Token usage reported by an LLM provider.

Fields are None when the provider does not report token counts.
Values are never fabricated as zero when unknown.
is_complete indicates whether all turns reported complete token metrics.
~~~

- `add(self, other: 'Usage | None') -> 'Usage'`
  Accumulate token counts across turns without fabricating zeros.

### `ModelResponse`

class `ModelResponse(message: 'Message', usage: 'Usage | None' = None, finish_reason: 'str | None' = None, response_id: 'str | None' = None, response_model: 'str | None' = None) -> None`

~~~text
Standardized response from an LLM model.
~~~


### `UsageLimits`

class `UsageLimits(request_limit: 'int | None' = None, input_tokens_limit: 'int | None' = None, output_tokens_limit: 'int | None' = None, total_tokens_limit: 'int | None' = None, allow_unknown_usage: 'bool' = False) -> None`

~~~text
Hard caps checked after every model call. ``None`` means unlimited.

Unknown token counts are not treated as zero: if a limit on tokens is set and the
model did not report them, the run fails closed unless ``allow_unknown_usage``.
~~~

- `check(self, requests: 'int', usage: 'Usage') -> 'None'`
  Raise ``UsageLimitExceededError`` if requests or token usage exceed a limit.

### `ModelSettings`

class `ModelSettings(temperature: 'float | None' = None, max_tokens: 'int | None' = None, stop_sequences: 'list[str] | None' = None, tool_choice: 'str | None' = None, top_p: 'float | None' = None, seed: 'int | None' = None, presence_penalty: 'float | None' = None, frequency_penalty: 'float | None' = None, reasoning_effort: 'str | None' = None, thinking_budget_tokens: 'int | None' = None, extra: 'dict[str, Any] | None' = None) -> None`

~~~text
Generation settings forwarded to models. ``None`` means the model's default.

Set them on the ``Runner`` for every agent, or on an ``Agent``; the agent's fields win.
~~~

- `is_empty(self) -> 'bool'`
  True when no field is set.
- `merged(self, override: 'ModelSettings | None') -> 'ModelSettings'`
  These settings, with every field that ``override`` sets taking precedence.
- `to_openai_params(self) -> 'dict[str, Any]'`
  The set fields as OpenAI chat-completions parameters (also used by LiteLLM).

### `AgentHooks`

class `AgentHooks()`

~~~text
Async callbacks around model and tool calls.

Hooks observe by default. ``before_tool`` may raise ``ModelRetry`` to refuse a call
and tell the model why, or any other exception to fail the run.

The model and tool hooks may also take over by returning a value (``None`` changes
nothing):

- ``before_model`` returns a ``ModelResponse`` to skip the model call (a cache hit, a
  policy refusal). Nothing is billed: requests and usage are not counted.
- ``after_model`` returns a ``ModelResponse`` to replace the reply. Usage still counts
  what the provider billed.
- ``before_tool`` returns a string to skip the tool and use it as the result. The
  result is not cached; ``after_tool`` and output guardrails still run.
- ``after_tool`` returns a string to replace the result. Files the tool returned are
  then not sent, as when a guardrail rewrites a result.

``tool_needs_approval`` is the one synchronous hook: return ``True`` to pause a tool
call for human approval. It can add an approval gate, never remove one.
~~~

- `after_model(self, ctx: 'RunContext[Any]', response: 'ModelResponse') -> 'ModelResponse | None'`
  Called after each model response. Return a ``ModelResponse`` to replace it.
- `after_tool(self, ctx: 'RunContext[Any]', call: 'ToolCall', result: 'str') -> 'str | None'`
  Called after a tool returns its result. Return a string to replace the result.
- `before_model(self, ctx: 'RunContext[Any]', messages: 'list[Message]') -> 'ModelResponse | None'`
  Called before each model request with the outgoing messages.
- `before_tool(self, ctx: 'RunContext[Any]', call: 'ToolCall') -> 'str | None'`
  Called before a tool runs; may raise to refuse the call.
- `on_agent_end(self, ctx: 'RunContext[Any]', agent: 'Agent', output: 'Any') -> 'None'`
  Called when an agent stops being active: with the final output when the run
- `on_agent_start(self, ctx: 'RunContext[Any]', agent: 'Agent') -> 'None'`
  Called when an agent becomes active: at the start of a run, and for the target
- `on_handoff(self, ctx: 'RunContext[Any]', source: 'Agent', target: 'Agent') -> 'None'`
  Called when control moves from ``source`` to ``target``; does nothing by default.
- `on_run_end(self, ctx: 'RunContext[Any]', output: 'Any') -> 'None'`
  Called when the run finishes with its output; does nothing by default.
- `on_run_start(self, ctx: 'RunContext[Any]') -> 'None'`
  Called once when a run starts; does nothing by default.
- `output_guardrails(self, ctx: 'RunContext[Any]', agent: 'Agent') -> 'Sequence[Any]'`
  Sync. Output guardrails these hooks add to ``agent``'s own (P39 G4).
- `tool_needs_approval(self, ctx: 'RunContext[Any]', tool: 'Tool', arguments: 'Mapping[str, Any]') -> 'bool | None'`
  Sync. Return ``True`` to require human approval for this call.

### `ComposedHooks`

class `ComposedHooks(*hooks: 'Any') -> 'None'`

~~~text
Several hooks objects run as one: ``Runner(hooks=[policy, MyHooks()])``.

``Runner`` builds this for a list; a single hooks object is used as it is. The order:

- ``on_run_start``, ``on_agent_start`` and ``on_handoff``: every member, in list order.
- ``before_model``: list order. The first member that returns a ``ModelResponse`` wins:
  the model is not called and later members' ``before_model`` is skipped.
- ``after_model``: **reverse** list order, chained. Each member gets the current
  response (the original, or the latest replacement); the result is the last
  replacement, or ``None`` when no member replaced it.
- ``before_tool``: list order. The first member that returns a string wins: the tool
  does not run and later members' ``before_tool`` is skipped (``after_tool`` and the
  tool's output guardrails still run, as with a single hooks object).
- ``after_tool``: reverse list order, chained like ``after_model``.
- ``tool_needs_approval``: list order; any ``True`` requires approval (later members
  are not asked). No member can remove a gate the tool itself declares.
- ``on_agent_end`` and ``on_run_end``: every member, in reverse list order.
- ``output_guardrails``: every member's guardrails, concatenated in list order.

So the first member is the outermost: its ``before_*`` hooks take over before the
others and its ``after_*`` hooks see the final, replaced value. Put a policy first.

The first exception a member raises propagates at once and the remaining members are
not called, exactly as an exception from a single hooks object fails the run
(``ModelRetry`` from ``before_tool`` keeps its meaning). A member's invalid return
value raises ``SDKValidationError`` naming the member's class. Members may omit
methods (objects that do not subclass ``AgentHooks``); a missing method is skipped.
A nested plain ``ComposedHooks`` is flattened into the member list; a subclass of
``ComposedHooks`` is kept as one member, so its own overrides still run. Members are
not deduplicated. A member with none of the ``AgentHooks`` methods (for example a list
passed as one member) raises ``SDKValidationError``.
~~~

- `after_model(self, ctx: 'RunContext[Any]', response: 'ModelResponse') -> 'ModelResponse | None'`
  Members' ``after_model`` in reverse list order, each on the current response.
- `after_tool(self, ctx: 'RunContext[Any]', call: 'ToolCall', result: 'str') -> 'str | None'`
  Members' ``after_tool`` in reverse list order, each on the current result.
- `before_model(self, ctx: 'RunContext[Any]', messages: 'list[Message]') -> 'ModelResponse | None'`
  The first member's ``ModelResponse`` (list order); later members are skipped.
- `before_tool(self, ctx: 'RunContext[Any]', call: 'ToolCall') -> 'str | None'`
  The first member's string answer (list order); later members are skipped.
- `on_agent_end(self, ctx: 'RunContext[Any]', agent: 'Agent', output: 'Any') -> 'None'`
  Every member's ``on_agent_end``, in reverse list order.
- `on_agent_start(self, ctx: 'RunContext[Any]', agent: 'Agent') -> 'None'`
  Every member's ``on_agent_start``, in list order.
- `on_handoff(self, ctx: 'RunContext[Any]', source: 'Agent', target: 'Agent') -> 'None'`
  Every member's ``on_handoff``, in list order.
- `on_run_end(self, ctx: 'RunContext[Any]', output: 'Any') -> 'None'`
  Every member's ``on_run_end``, in reverse list order.
- `on_run_start(self, ctx: 'RunContext[Any]') -> 'None'`
  Every member's ``on_run_start``, in list order.
- `output_guardrails(self, ctx: 'RunContext[Any]', agent: 'Agent') -> 'Sequence[Any]'`
  Every member's output guardrails, in list order.
- `tool_needs_approval(self, ctx: 'RunContext[Any]', tool: 'Tool', arguments: 'Mapping[str, Any]') -> 'bool | None'`
  ``True`` when any member says so (list order, stopping at the first).

### `set_default_hooks`

function `set_default_hooks(hooks: 'AgentHooks | Sequence[AgentHooks] | None', *, locked: 'bool' = False) -> 'None'`

~~~text
Install hooks (a policy) that every ``Runner`` in this process uses.

They run before the runner's own hooks, which cannot remove them. ``None`` removes
the default. A list is composed in order. Runs already in progress keep the hooks
they started with. Code is never loaded from the environment: the operator's
process calls this (``nexura_guardrails.GuardrailPolicy.install()`` does).

``locked=True`` makes the default binding for the rest of the process: later calls
that remove or replace it, and ``Runner(default_hooks=False)`` /
``AgentCoordinator(default_hooks=False)``, raise ``SDKValidationError``. Installing
the same object again is allowed. Use it when the default is an operator's policy
that application code must not be able to drop.
~~~

### `default_hooks`

function `default_hooks() -> 'AgentHooks | None'`

~~~text
The process default hooks set with :func:`set_default_hooks` (``None`` when unset).
~~~

### `GuardrailResult`

class `GuardrailResult(tripwire: 'bool', info: 'Any' = None, replacement: 'str | None' = None, rejection: 'str | None' = None) -> None`

~~~text
Outcome of one guardrail check. ``tripwire=True`` stops the run.
~~~

- `passed(cls, info: 'Any' = None) -> 'GuardrailResult'`
  Build a result that lets the run continue.
- `rejected(cls, message: 'str', info: 'Any' = None) -> 'GuardrailResult'`
  Tool guardrails only: do not run the call (input) or withhold its result
- `rewritten(cls, replacement: 'str', info: 'Any' = None) -> 'GuardrailResult'`
  Build a non-tripping result that replaces the final text output.
- `tripped(cls, info: 'Any' = None) -> 'GuardrailResult'`
  Build a result whose tripwire stops the run.

### `StreamGuard`

class `StreamGuard(mode: 'StreamGuardMode' = 'auto', chunk_chars: 'int' = 400, overlap_chars: 'int' = 120, max_windows: 'int' = 64) -> None`

~~~text
Streaming settings for an agent with output guardrails (see the module docstring).

``chunk_chars``: new characters per checked window (``"chunked"``). ``overlap_chars``:
visible characters (after NFKC, without invisible characters) held back from each window
and checked again with the next one; at least :data:`MIN_OVERLAP_CHARS` (32), default 120.
Make it at least as long as the longest value a guardrail must see whole.
``max_windows``: window checks per model turn (default 64); after that the rest of the
turn is held until the check of the whole answer, so guardrail calls stay bounded.
~~~


### `detect_pii`

function `detect_pii(text: 'str', kinds: 'Iterable[str] | None' = None) -> 'list[PIIFinding]'`

~~~text
Find personal data and secrets in ``text`` (a fast heuristic pre-filter, see the
module docstring for what it misses).

Kinds: ``email``, ``phone`` (Indian mobile with optional +91/91/0 prefix, or
E.164 with a leading ``+``), ``aadhaar`` (12 digits, not starting with 0 or 1,
Verhoeff checksum valid), ``pan`` (AAAAA9999A with a valid 4th-letter entity code),
``card`` (13-19 digits, Luhn valid, space/dash separators allowed), ``ifsc``,
``upi`` (``name@handle`` for common bank handles), ``ipv4`` and ``secret`` (common
API-key prefixes, JWTs and private-key headers). Overlaps resolve in ``PII_KINDS``
order, so a Luhn-valid card is never also reported as a phone number. Findings are
sorted by position and hold a masked preview, never the full value.

Text is NFKC-folded first (fullwidth forms, Unicode digits such as Devanagari, hidden
zero-width characters) with spans mapped back to the original text. Cards tolerate up
to two whitespace/dash separators. Matches glued to alphanumerics or joined by a dash
to an alphanumeric segment (UUID / hex fragments) are ignored. Checksums are only a
filter: about 1 in 10 random 12-digit ids passes Verhoeff (Aadhaar) and about 1 in 10
random 13-19 digit ids passes Luhn (card), so long numeric ids can false-positive.
~~~

### `redact_pii`

function `redact_pii(text: 'str', kinds: 'Iterable[str] | None' = None, mask: 'str' = '[REDACTED:{kind}]') -> 'str'`

~~~text
Replace detected PII in ``text``. ``{kind}`` in ``mask`` becomes the finding kind.
~~~

### `block_pii`

function `block_pii(kinds: 'Iterable[str] | None' = None, on: "Literal['input', 'output']" = 'input') -> 'GuardrailFn'`

~~~text
Pre-filter guardrail that trips when ``detect_pii`` finds any of ``kinds`` (default: all).

Regex and checksum only, so it has the misses listed in the module docstring.
``on`` labels the stage the guardrail is meant for; pass the result to
``input_guardrails`` or ``output_guardrails`` accordingly. Typed outputs are
scanned as JSON. Evidence holds kinds, spans and masked previews only.
~~~

### `redact_output_pii`

function `redact_output_pii(kinds: 'Iterable[str] | None' = None, mask: 'str' = '[REDACTED:{kind}]', on_structured: "Literal['trip', 'pass']" = 'trip') -> 'GuardrailFn'`

~~~text
Output pre-filter that rewrites text output with PII redacted instead of tripping.

It redacts only what ``detect_pii`` finds (see its misses). It returns
``GuardrailResult.rewritten(...)``, which the runner applies to the final
text before it is returned, stored in the session or emitted. The rewrite hook works
for text output only. For an agent with ``output_type``, output containing PII
cannot be rewritten safely, so by default the guardrail trips (``on_structured="trip"``);
``"pass"`` lets it through unredacted.
~~~

### `no_prompt_injection`

function `no_prompt_injection(extra_patterns: 'Sequence[str | re.Pattern[str]]' = (), threshold: 'float' = 0.5) -> 'GuardrailFn'`

~~~text
Fast prompt-injection pre-filter. Trips when the summed signal score >= ``threshold``.

Signals (each counted once): instruction-override phrases ("ignore previous
instructions"), system-prompt exfiltration requests, jailbreak and role-play
markers, chat-template delimiter spoofing, in English, Hindi (Devanagari) and
Hinglish; plus hidden-text tricks: zero-width characters, bidi overrides, Unicode
tag characters and long base64 blobs. Strong phrases score 0.5-0.8, weak role-play
markers 0.3, each ``extra_patterns`` match 1.0. Lower ``threshold`` to be stricter.

Paraphrases, spaced-out words, other languages (French, Spanish, Tamil, Bengali),
Hindi and Hinglish paraphrases, short base64, markdown fake turns, role-play and
indirect injection are missed, and benign text quoting the phrases can trip it. Put a
model-based check behind it. Evidence lists signal ids, offsets and the score, not
the matched text.
~~~

### `max_length`

function `max_length(chars: 'int | None' = None, tokens: 'int | None' = None) -> 'GuardrailFn'`

~~~text
Trip when text is longer than ``chars`` characters or ``tokens`` estimated tokens.

The token count is a rough estimate (about 4 ASCII characters or 1.5 non-ASCII
characters per token), not a real tokenizer. Works for input and output.
~~~

### `allowed_topics`

function `allowed_topics(keywords: 'Iterable[str]' = (), deny: 'Iterable[str]' = ()) -> 'GuardrailFn'`

~~~text
Keyword topic pre-filter. This is lexical matching, not semantic understanding.

With ``keywords``, text must contain at least one (whole word, case-insensitive) or
the guardrail trips; empty text passes. With ``deny``, text containing any denied
word trips. A paraphrase that avoids every keyword defeats both checks, and an
allowed keyword can appear in an off-topic message. Measured: ``deny=["bomb"]``
passes "b0mb", "explosive device" and the Hindi word; ``keywords=["emi"]`` trips on
"How much is my monthly instalment?" and on the same question in Hindi.
~~~

### `json_output_only`

function `json_output_only() -> 'GuardrailFn'`

~~~text
Output guardrail: text must be exactly one JSON object or array, nothing else.

Markdown fences or prose around the JSON trip it. Already-structured output (an
agent with ``output_type``) passes. Evidence has the parser's error position, not
the text.
~~~

### `ModelProtocol`

class `ModelProtocol(*args, **kwargs)`

~~~text
Protocol defining the asynchronous interface for language model adapters.

All model adapters must implement generate(). Implementations are expected
to mask sensitive credentials in repr and errors, handle timeouts, and
distinguish reported versus unknown token usage.

Optional structured-output opt-in (not part of the protocol, so existing models are
unaffected): set ``supports_response_schema = True`` on the class and accept a
``response_schema`` keyword in ``generate()``. The runner then passes the output JSON
schema natively instead of putting it in the prompt. Also set
``response_schema_with_tools = True`` if the model accepts a schema on turns where tools
are offered; otherwise the schema stays in the prompt for agents that have tools.
~~~

- `generate(self, messages: 'list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None) -> 'ModelResponse'`
  Generate a model completion.

### `FakeModel`

class `FakeModel(responses: 'Sequence[FakeResponseItem] | None' = None, *, handler: 'Callable[[list[Message], list[dict[str, Any]] | None], FakeResponseItem] | None' = None, default_usage: 'Usage | None' = None, repeat_last: 'bool' = False, native_schema: 'bool' = False, schema_with_tools: 'bool' = False, stream: 'bool' = False, chunk_size: 'int' = 8) -> 'None'`

~~~text
Deterministic, keyless model adapter for tests and local examples.

Does not perform network calls or require credentials. Supports canned
response queues, custom programmatic handlers, and an endless tool-call fixture
to verify runner bounds.
~~~

- `endless(cls, tool_name: 'str' = 'ping', arguments: 'dict[str, Any] | None' = None, *, default_usage: 'Usage | None' = None) -> 'FakeModel'`
  Create a fixture that endlessly returns a tool call to verify runner limits.
- `generate(self, messages: 'list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None, response_schema: 'dict[str, Any] | None' = None, model_settings: 'ModelSettings | None' = None) -> 'ModelResponse'`
  Generate the next canned or handler response.
- `stream(self, messages: 'list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None, response_schema: 'dict[str, Any] | None' = None, model_settings: 'ModelSettings | None' = None) -> 'AsyncIterator[StreamDelta]'`
  Stream the next scripted response as deltas (used when ``stream=True``).

### `OpenAIChatModel`

class `OpenAIChatModel(model: 'str', *, api_key: 'str | None' = None, base_url: 'str' = 'https://api.openai.com/v1', timeout: 'float' = 30.0, client: 'httpx.AsyncClient | None' = None, transport: 'httpx.AsyncBaseTransport | None' = None, headers: 'dict[str, str] | None' = None, extra_body: 'dict[str, Any] | None' = None, query_params: 'dict[str, Any] | None' = None) -> 'None'`

~~~text
Adapter for OpenAI-compatible chat completion endpoints.

Secrets (API keys) are masked in string representations and error messages.
Supports injected httpx.AsyncClient or httpx.AsyncBaseTransport for deterministic testing.

``headers``, ``extra_body`` and ``query_params`` are defaults sent with every request.
``headers`` cannot set Content-Type or Accept, nor Authorization when ``api_key`` is set.
``extra_body`` cannot set ``model``, ``messages``, ``tools``, ``stream``,
``stream_options`` or ``response_format``. Precedence in the body: SDK fields, then
``ModelSettings`` fields and per-call ``ModelSettings.extra``, then ``extra_body``.
Header values are never logged, shown in ``repr`` or put in errors.
~~~

- `close(self) -> 'None'`
  Close internally managed HTTP client if present.
- `generate(self, messages: 'list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None, response_schema: 'dict[str, Any] | None' = None, model_settings: 'ModelSettings | None' = None) -> 'ModelResponse'`
  Send chat completion request to the OpenAI-compatible endpoint.
- `stream(self, request: 'GenerationRequest | list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None, response_schema: 'dict[str, Any] | None' = None, model_settings: 'ModelSettings | None' = None) -> 'AsyncIterator[StreamDelta]'`
  Stream a chat completion (SSE) as ``StreamDelta`` values.

### `LiteLLMModel`

class `LiteLLMModel(model: 'str', *, api_key: 'str | None' = None, api_base: 'str | None' = None, timeout: 'float | None' = None, completion: 'CompletionFn | None' = None, **options: 'Any') -> 'None'`

~~~text
Adapter that sends chat requests through ``litellm.acompletion``.

``model`` is any LiteLLM model string, e.g. ``"bedrock/anthropic.claude-3-5-sonnet"``,
``"azure/my-deployment"``, ``"ollama/llama3.1"`` or ``"groq/llama-3.1-8b-instant"``.
``api_key``, ``api_base`` and ``timeout`` are passed per call; extra ``**options``
(``temperature``, ``max_tokens``, ...) are forwarded unchanged. ``completion`` can be
any async callable with ``litellm.acompletion``'s signature (used by tests).

Retries are owned by the SDK: every call passes ``num_retries=0`` and
``max_retries=0`` so ``RetryModel``/``FallbackModel`` decide, not LiteLLM. The
adapter never mutates ``os.environ`` or LiteLLM globals. Set
``LITELLM_LOCAL_MODEL_COST_MAP=True`` in your environment *before* the first
import of litellm to stop it fetching its model-cost map over HTTP at import.

``generate()`` returns whole responses. Token streaming is opt-in through
:meth:`stream`, which ``Runner.stream_text`` / ``stream_async(stream_text=True)`` use
(not for structured-output turns): it calls ``litellm.acompletion`` with ``stream=True``
and ``stream_options={"include_usage": True}`` and yields ``StreamDelta`` values.
Users still cannot pass ``stream=True`` in ``**options``.
~~~

- `generate(self, messages: 'list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None, response_schema: 'dict[str, Any] | None' = None, model_settings: 'ModelSettings | None' = None) -> 'ModelResponse'`
  Send a chat completion through LiteLLM and map the response.
- `stream(self, request: 'GenerationRequest | list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None, response_schema: 'dict[str, Any] | None' = None, model_settings: 'ModelSettings | None' = None) -> 'AsyncIterator[StreamDelta]'`
  Stream a chat completion through LiteLLM as ``StreamDelta`` values.

### `RetryModel`

class `RetryModel(model: 'ModelProtocol', *, max_attempts: 'int' = 3, base_delay: 'float' = 0.5, max_delay: 'float' = 8.0, retry_on: 'tuple[type[BaseException], ...]' = (<class 'nexura_agents.errors.ProviderRateLimitError'>, <class 'nexura_agents.errors.ProviderRequestError'>, <class 'nexura_agents.errors.ProviderResponseError'>, <class 'nexura_agents.providers.errors.ProviderServerError'>, <class 'TimeoutError'>), trim_history: 'int' = 0) -> 'None'`

~~~text
Retry transient provider failures with exponential backoff and jitter.

Authentication and request-validation errors are never retried. Retrying a model
call repeats its token cost; set ``max_attempts`` with that in mind.

``trim_history=N`` (default 0, off) recovers from a :class:`ProviderContextLengthError`
by dropping the oldest conversation turn and calling again, up to ``N`` times. System
messages and the latest turn are always kept; trimming applies to plain message lists
(not to provider ``GenerationRequest`` calls). ``turns_trimmed`` counts what was dropped.
~~~

- `generate(self, messages: 'list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None, **kwargs: 'Any') -> 'ModelResponse'`
  Call the wrapped model, retrying transient errors with backoff.
- `stream(self, *args: 'Any', model_settings: 'Any' = None, **kwargs: 'Any') -> 'AsyncIterator[Any]'`
  Stream from the wrapped model, retrying only failures before the first delta.

### `FallbackModel`

class `FallbackModel(*models: 'ModelProtocol', fallback_on: 'tuple[type[BaseException], ...]' = (<class 'nexura_agents.errors.ProviderError'>, <class 'TimeoutError'>)) -> 'None'`

~~~text
Try each model in order, moving on after a provider failure.

``fallback_on`` defaults to every :class:`ProviderError` except authentication,
so a misconfigured key is reported instead of silently switching providers.
``last_used`` records which model produced the most recent response.
~~~

- `generate(self, messages: 'list[Message]', tools: 'list[dict[str, Any]] | None' = None, *, timeout: 'float | None' = None, **kwargs: 'Any') -> 'ModelResponse'`
  Return the first successful response, trying each model in order.
- `stream(self, *args: 'Any', model_settings: 'Any' = None, **kwargs: 'Any') -> 'AsyncIterator[Any]'`
  Stream from the first model that starts; fall back only before the first delta.

### `infer_model`

function `infer_model(spec: 'str') -> 'Any'`

~~~text
Create a model from ``"provider:model-name"``.

Keys come from the usual environment variables (``OPENAI_API_KEY``,
``ANTHROPIC_API_KEY``, ``GEMINI_API_KEY``, ``SARVAM_API_KEY``); Vertex AI uses
Google Cloud credentials and needs ``pip install nexura-vertex``. ``"litellm:<litellm model>"``
(e.g. ``"litellm:bedrock/anthropic.claude-3-5-sonnet"``)
uses ``LiteLLMModel`` and needs ``pip install "nexura-agents[litellm]"``.
``"test"`` returns an offline ``FakeModel``.
~~~

### `OpenAIProvider`

class `OpenAIProvider(model: 'str', *, api_key: 'str | None' = None, base_url: 'str' = 'https://api.openai.com/v1', organization: 'str | None' = None, timeout: 'float' = 30.0, client: 'httpx.AsyncClient | None' = None, transport: 'httpx.AsyncBaseTransport | None' = None, capabilities: 'ProviderCapabilities | None' = None, extra_headers: 'dict[str, str] | None' = None) -> 'None'`

~~~text
Adapter for OpenAI, Ollama, vLLM, LocalAI, and other OpenAI-compatible endpoints.

Features:
- Streaming delta parser for Server-Sent Events (SSE).
- Preserves server continuation values (id, system_fingerprint).
- Handles rate limits (429) with Retry-After header extraction.
- Handles model refusal responses explicitly.
- Preserves usage omission without zero-cost fabrication.
- Rejects unsupported features early (CapabilityError) rather than silently downgrading.
- Configurable for local compatible stubs without API keys (e.g. Ollama/vLLM).
~~~

- `close(self) -> 'None'`
  Close internally managed HTTP client.

### `AnthropicProvider`

class `AnthropicProvider(model: 'str' = 'claude-haiku-5-5', *, api_key: 'str | None' = None, base_url: 'str' = 'https://api.anthropic.com', timeout: 'float' = 30.0, client: 'httpx.AsyncClient | None' = None, transport: 'httpx.AsyncBaseTransport | None' = None, capabilities: 'ProviderCapabilities | None' = None, default_max_tokens: 'int' = 4096, extra_headers: 'dict[str, str] | None' = None, structured_output: 'str' = 'auto') -> 'None'`

~~~text
Native Anthropic provider adapter communicating with Anthropic Messages API.

Guarantees:
- Native content-block ordering and tool IDs preserved across turns.
- Mixed text and tool_use blocks survive turn replay.
- Validates missing or duplicate tool IDs eagerly.
- Max-token/refusal termination is mapped explicitly to 'length'/'refusal'.
- Stream cancellation cleanly closes owned transports and connections.
- Rejects unsupported audio or vector embedding requests via CapabilityError.
- Credentials are redacted from representations and errors.
~~~

- `aclose(self) -> 'None'`
  Close owned HTTP client and release underlying connections.
- `close(self) -> 'None'`
  Synchronously close client if event loop is not active.

### `GeminiProvider`

class `GeminiProvider(model: 'str' = 'gemini-2.5-flash', *, api_key: 'str | None' = None, base_url: 'str' = 'https://generativelanguage.googleapis.com/v1beta', timeout: 'float' = 30.0, client: 'httpx.AsyncClient | None' = None, transport: 'httpx.AsyncBaseTransport | None' = None, capabilities: 'ProviderCapabilities | None' = None, extra_headers: 'dict[str, str] | None' = None) -> 'None'`

~~~text
Native Gemini provider adapter communicating with Google Generative Language APIs.

Guarantees:
- Preserves thought and reasoning signatures without discarding metadata.
- Translates native Gemini functionCall/functionResponse tool wire contracts.
- Emits real-time SSE stream deltas for text and fragmented tool arguments.
- Rejects CLI aliases (e.g. 'gemini-cli') early.
- Preserves omitted usage without zero-cost fabrication.
- Rejects unsupported native capabilities eagerly via CapabilityError.
~~~

- `close(self) -> 'None'`
  Close internally managed HTTP client.
- `delete_cached_content(self, cached_content_id: 'str') -> 'bool'`
  Explicitly delete a stored cached content resource.

### `SarvamProvider`

class `SarvamProvider(model: 'str' = 'sarvam-105b', *, api_key: 'str | None' = None, base_url: 'str' = 'https://api.sarvam.ai', timeout: 'float' = 30.0, max_input_length: 'int' = 25000, output_limit_bytes: 'int' = 2000000, client: 'httpx.AsyncClient | None' = None, transport: 'httpx.AsyncBaseTransport | None' = None, capabilities: 'ProviderCapabilities | None' = None, extra_headers: 'dict[str, str] | None' = None) -> 'None'`

~~~text
Sarvam AI native provider for multilingual Indic text generation and translation.

Guarantees:
- Chat and translation modeled as separate native capabilities.
- Supported Indic language codes validated eagerly via CapabilityError.
- Preserves templated identifiers, order IDs, URLs, and currencies across translation.
- Omitted usage is not fabricated as zero cost.
- Rate limits (429) extract Retry-After headers cleanly.
- Secret keys redacted from representations, logs, and error messages.
~~~

- `aclose(self) -> 'None'`
  Close owned client and connections.
- `close(self) -> 'None'`
  Synchronously close owned client if no loop is active.
- `translate(self, request: 'TranslationRequest', *, context: 'ExecutionContext | None' = None) -> 'TranslationResult'`
  Execute Indic text translation via Sarvam translation endpoint.

### `MCPServer`

class `MCPServer(transport: 'McpTransport', *, name: 'str' = 'mcp', prefix: 'str | None' = None, include: 'Collection[str] | None' = None, exclude: 'Collection[str]' = (), requires_approval: 'ApprovalPolicy' = False, read_only: 'Collection[str]' = (), max_result_chars: 'int | None' = 20000, request_timeout: 'float | None' = None, tool_timeout: 'float | None' = None, on_timeout: 'OnTimeout' = 'tell_model', input_guardrails: 'Sequence[GuardrailFn]' = (), output_guardrails: 'Sequence[GuardrailFn]' = (), enabled: 'EnabledRule' = True, protocol_version: 'str | None' = None) -> 'None'`

~~~text
A connection to one MCP server whose tools an agent can call.

Use as an async context manager (or call ``connect()``/``close()``). The session
stays open while the agent runs; tools fail cleanly once it is closed.

Args:
    transport: An MCP transport; usually built with ``MCPServer.stdio`` or ``.http``.
    name: Label used in module ids and logs.
    prefix: Prepended to tool names (``prefix_toolname``) to avoid clashes.
    include / exclude: Tool names to keep or drop.
    requires_approval: ``True`` for every tool, a set of tool names, or a predicate
        ``(tool_name, arguments) -> bool``. Server-declared hints such as
        ``readOnlyHint`` are untrusted and never skip approval on their own.
    read_only: Tool names the operator vouches are free of side effects. They get the
        ``pure`` effect class (a durable run repeats them after a crash instead of
        pausing); every other tool stays ``non_idempotent``.
    max_result_chars: Truncate long tool results before they reach the model.
    request_timeout: Seconds to wait for each server reply before failing
        (default: the transport's, 60 s for stdio and the HTTP ``timeout``).
    tool_timeout / on_timeout / input_guardrails / output_guardrails / enabled:
        Applied to every tool, as on ``@tool``. ``tool_timeout`` is ``@tool``'s
        ``timeout``; the name differs because ``MCPServer.http`` already uses
        ``timeout`` for the HTTP client.
    protocol_version: ``None`` (default) prefers MCP 2026-07-28 and falls back to the
        ``initialize`` handshake of 2025-11-25 ... 2024-11-05 when the server is older.
        Pass a version to pin it (see ``McpClientSession``).
~~~

- `close(self) -> 'None'`
  Close the underlying MCP session.
- `connect(self) -> 'MCPServer'`
  Initialize the MCP session if needed and return this server.
- `get_prompt(self, name: 'str', arguments: 'dict[str, str] | None' = None) -> 'list[Message]'`
  Render a prompt template as ``user``/``assistant`` messages.
- `http(cls, url: 'str', *, headers: 'dict[str, str] | None' = None, timeout: 'float' = 30.0, transport: 'httpx.AsyncBaseTransport | None' = None, **options: 'Any') -> 'MCPServer'`
  Connect to a remote MCP server over Streamable HTTP (``headers`` for auth).
- `list_prompts(self) -> 'list[McpPromptDefinition]'`
  Prompt templates the server offers (``McpIntegrationError`` without the capability).
- `list_resources(self) -> 'list[McpResourceDefinition]'`
  Resources the server offers. Remembers the URIs for ``resource_tools()``.
- `list_tools(self, *, refresh: 'bool' = False) -> 'list[McpToolDefinition]'`
  Tool definitions from the server (cached; ``refresh=True`` asks again).
- `read_resource(self, uri: 'str') -> 'str'`
  Text of one resource: its text blocks joined with newlines.
- `resource_tools(self) -> 'list[SchemaTool]'`
  Opt-in tools that let an agent list and read this server's resources.
- `stdio(cls, command: 'str', *args: 'str', env: 'dict[str, str] | None' = None, cwd: 'str | None' = None, allowed_commands: 'Collection[str] | None' = None, allow_inline_code: 'bool' = False, **options: 'Any') -> 'MCPServer'`
  Start a local MCP server as a subprocess (e.g. ``npx``, ``uvx``, ``python``).
- `tools(self) -> 'list[SchemaTool]'`
  Agent tools for this server's (filtered) MCP tools.

### `AgentMCPServer`

class `AgentMCPServer(name: 'str', *, agents: 'Sequence[Agent | Team]' = (), tools: 'Sequence[Tool]' = (), deps: 'Any' = None, runner: 'Any' = None, version: 'str' = '0.4.0', list_ttl_ms: 'int' = 60000, instructions: 'str | None' = None) -> 'None'`

~~~text
Expose agents and tools as an MCP server.

Args:
    name: Server name shown to MCP clients.
    agents: Each becomes a tool named after the agent (``Agent(description=...)``
        is shown to the client, so describe what the agent is for). Teams
        (``SequentialAgent`` and friends) are served the same way.
    tools: Plain tools, exposed with their own schemas.
    deps: Passed to every run and tool call as ``RunContext.deps``.
    runner: Runner used for agent calls (for usage limits, hooks or budgets), and for
        teams that were not given their own runner. Plain tools use the hooks a run of
        this runner would use (``Runner.effective_hooks()``: the process default from
        ``set_default_hooks`` plus the runner's own): ``before_tool``, ``after_tool``
        and ``tool_needs_approval`` apply to them as in a run.
    list_ttl_ms: ``ttlMs`` cache hint on 2026-07-28 discover and list results. The
        tool list is fixed when the server is built, so clients may reuse it for this
        long (default 60 s).
    instructions: Optional guidance for the client's model, sent by ``server/discover``.

Both MCP eras are served: 2026-07-28 clients send stateless requests (no session), and
clients of the handshake revisions (2025-11-25 back to 2024-11-05) send ``initialize``.
~~~

- `http_app(self, *, token: 'str | None' = None, path: 'str' = '/mcp', allowed_origins: 'Sequence[str]' = (), max_body_bytes: 'int' = 4194304, max_sessions: 'int' = 1000, session_ttl_seconds: 'float' = 3600.0) -> 'Callable[[Scope, Receive, Send], Awaitable[None]]'`
  An ASGI app speaking MCP Streamable HTTP. Run it with any ASGI server.
- `run_stdio(self) -> 'None'`
  Serve on stdin/stdout until the client closes the pipe (blocks).
- `serve_stdio(self, reader: 'Callable[[], Awaitable[str]] | None' = None, writer: 'Callable[[str], Awaitable[None]] | None' = None) -> 'None'`
  Newline-delimited JSON-RPC over stdio. Logs must go to stderr, never stdout.

### `A2AServer`

class `A2AServer(agent_or_team: 'Agent | Team', *, name: 'str | None' = None, description: 'str | None' = None, url: 'str', version: 'str' = '1.0.0', skills: 'Sequence[dict[str, Any]] | None' = None, deps: 'Any' = None, runner: 'Any' = None, signing_key: 'Any' = None, signing_kid: 'str | None' = None, signing_jku: 'str | None' = None) -> 'None'`

~~~text
Expose an ``Agent`` or ``Team`` as an A2A agent.

Args:
    agent_or_team: What to serve. Teams run as one unit and may pause for approval.
    name: Agent card name (default: the agent's name).
    description: Agent card description (default: the agent's description).
    url: Public URL of the JSON-RPC endpoint, as clients will reach it.
    version: Your agent's version, shown in the card (not the protocol version).
    skills: Card skills as dicts (``id``, ``name``, ``description``, ``tags``); the
        default is one skill describing the agent.
    deps: Passed to every run as ``RunContext.deps``.
    runner: ``Runner`` for agent runs (usage limits, hooks, budgets) and for teams
        without their own runner.
    signing_key: Optional ``cryptography`` private key (EC P-256/P-384, Ed25519 or RSA).
        When set, the served card carries a JWS signature (A2A 1.0 section 8.4).
    signing_kid: Key id put in the signature header; required with ``signing_key``.
    signing_jku: Optional JWK Set URL advertised in the signature header.

Security model. One bearer token guards the whole endpoint, so every holder of it can
read, continue and cancel every task; task and context ids are random 144-bit values
but are not per-user. Use one server per tenant if you need isolation.
~~~

- `card(self, *, authenticated: 'bool' = False) -> 'dict[str, Any]'`
  The agent card. ``authenticated=True`` advertises bearer auth (``http_app`` does).
- `http_app(self, *, token: 'str | None' = None, allowed_origins: 'Sequence[str]' = (), max_body_bytes: 'int' = 4194304, max_tasks: 'int' = 1000, task_ttl_seconds: 'float' = 3600.0, path: 'str' = '/') -> 'Callable[[Scope, Receive, Send], Awaitable[None]]'`
  An ASGI app: ``GET /.well-known/agent-card.json`` and JSON-RPC on ``POST path``.

### `A2AClient`

class `A2AClient(url: 'str', *, headers: 'dict[str, str] | None' = None, timeout: 'float' = 60, transport: 'httpx.AsyncBaseTransport | None' = None, policy: 'DestinationPolicy | None' = None, card_keys: 'Mapping[str, Any] | None' = None, card_algorithms: 'Sequence[str] | None' = None) -> 'None'`

~~~text
Talk to one remote A2A agent over JSON-RPC.

Args:
    url: The agent's base URL; its card is at ``<url>/.well-known/agent-card.json``.
    headers: Sent with every request, e.g. ``{"Authorization": "Bearer ..."}``.
    timeout: Seconds per HTTP request (a stream may run longer than this in total).
    transport: An ``httpx`` transport, e.g. ``httpx.ASGITransport(app=...)`` for tests.
    policy: Outbound ``DestinationPolicy``. As with ``MCPServer.http``, the default
        permits loopback and private ranges and trusts the host you configured; other
        hosts (a card pointing elsewhere) must pass the policy. Pass your own policy to
        tighten this. Cloud metadata addresses are always blocked.
    card_keys: Trusted public keys for signed agent cards, by key id (``cryptography``
        public keys or JWK dicts). When given, the card must carry a JWS signature that
        verifies with one of them, or every call fails with ``AgentCardSignatureError``.
        ``jku`` URLs in the card are never fetched. Without ``card_keys`` signatures
        are not checked.
    card_algorithms: JWS algorithms accepted for card signatures.

The client speaks 1.0 when the card offers a 1.0 JSON-RPC interface and falls back to
the 0.3 method names and shapes for older cards. No connection is kept between calls,
so one client is safe to use across event loops.
~~~

- `as_tool(self, *, name: 'str | None' = None, description: 'str | None' = None, requires_approval: 'bool' = False) -> 'Tool'`
  This remote agent as a tool; see :func:`remote_agent_tool`.
- `cancel(self, task_id: 'str') -> 'A2ATask'`
  Cancel a running or paused task; returns the task in its ``canceled`` state.
- `decide(self, task_id: 'str', *, approve: 'bool | Sequence[str] | dict[str, str]' = (), reject: 'dict[str, str] | None' = None) -> 'A2ATask'`
  Answer an ``input-required`` approval pause on a Nexura A2A server.
- `get_card(self, *, refresh: 'bool' = False) -> 'dict[str, Any]'`
  Fetch (and cache) the agent card.
- `get_task(self, task_id: 'str', *, history_length: 'int | None' = None) -> 'A2ATask'`
  Fetch a task's current state, artifacts and (optionally limited) history.
- `send(self, content: 'str | Sequence[PartInput]', *, task_id: 'str | None' = None, context_id: 'str | None' = None, history_length: 'int | None' = None, return_immediately: 'bool' = False) -> 'A2ATask'`
  Send a message and wait for the task to finish or pause.
- `stream(self, content: 'str | Sequence[PartInput]', *, task_id: 'str | None' = None, context_id: 'str | None' = None) -> 'AsyncGenerator[A2AEvent, None]'`
  Send a message and yield each update (server-sent events) until the stream ends.

### `remote_agent_tool`

function `remote_agent_tool(url_or_client: 'str | A2AClient', *, name: 'str | None' = None, description: 'str | None' = None, requires_approval: 'bool' = False, **client_options: 'Any') -> 'Tool'`

~~~text
A tool that asks a remote A2A agent and returns its output.

The tool takes one ``request`` string. The description comes from the agent card's
skills when the card is already cached on the client (``await client.get_card()``
first); otherwise pass ``description=`` so the model knows what the agent is for.
``client_options`` (``headers``, ``timeout``, ``transport``, ``policy``) build the
client when you pass a URL.

If the remote task needs input or approval, or fails, the tool raises ``ToolError``
so the calling model sees a clear message. Remote text is untrusted content.
~~~

### `mcp_tools`

function `mcp_tools(servers: 'MCPServer | Sequence[MCPServer]') -> 'list[SchemaTool]'`

~~~text
Tools from one or more connected servers, in order.
~~~

### `openapi_tools`

function `openapi_tools(spec: 'dict[str, Any] | str', *, base_url: 'str | None' = None, headers: 'dict[str, str] | None' = None, prefix: 'str | None' = None, include: 'Collection[str] | None' = None, exclude: 'Collection[str]' = (), requires_approval: 'ApprovalPolicy' = False, allow_loopback: 'bool' = False, allowed_hosts: 'Collection[str]' = (), transport: 'httpx.AsyncBaseTransport | None' = None, max_result_chars: 'int | None' = 20000, timeout: 'float | None' = None, on_timeout: 'OnTimeout' = 'tell_model', input_guardrails: 'Sequence[GuardrailFn]' = (), output_guardrails: 'Sequence[GuardrailFn]' = (), enabled: 'EnabledRule' = True, toolset: 'str | None' = None, read_only: 'Collection[str]' = (), idempotent: 'Collection[str]' = ()) -> 'list[SchemaTool]'`

~~~text
One agent tool per operation, named by ``operationId``.

``base_url`` defaults to the spec's first server. ``headers`` (for example an
``Authorization`` header) are added to every request and never shown to the model.
``requires_approval="writes"`` pauses before any non-GET/HEAD call. Requests go
through the SDK's safe HTTP client: only the API's own host is allowed, and
loopback/private addresses are refused unless ``allow_loopback`` is set.
``allowed_hosts`` trusts specific extra hosts (e.g. one private address) without
opening every private range. ``timeout``, ``on_timeout``, ``input_guardrails``,
``output_guardrails`` and ``enabled`` apply to every generated tool, as on ``@tool``
(``ctx.tool_name`` tells a guardrail or rule which operation is being called).
Each tool's ``origin`` is ``openapi:<METHOD>:<path>`` and it carries the tag
``toolset:<toolset>`` (default: ``prefix``, else ``openapi``), so a policy can match
every operation of this API (``ctx.tool``), plus ``openapi.method:<METHOD>`` and,
when the method or ``x-idempotent`` claims it, ``openapi.hint:idempotent=true``.

Every tool is ``non_idempotent`` for durable recovery (a crash during the call pauses
the run) unless you declare otherwise: ``read_only`` lists operationIds you vouch have
no side effects (``pure``: repeated after a crash) and ``idempotent`` those that are
safe to repeat (``idempotent``). The spec's HTTP method and ``x-idempotent`` are the
API's own claims and never change the class. Unknown operationIds, or one listed in
both, raise ``SDKValidationError``. These declarations do not change approval:
``requires_approval="writes"`` still pauses before every non-GET/HEAD call.
~~~

### `module_tool`

function `module_tool(module: 'BaseModule', *, name: 'str | None' = None, description: 'str | None' = None, config: 'dict[str, Any] | None' = None, requires_approval: 'ApprovalPolicy' = False, max_result_chars: 'int | None' = 20000, result: 'Callable[[dict[str, Any]], Any] | None' = None, timeout: 'float | None' = None, on_timeout: 'OnTimeout' = 'tell_model', input_guardrails: 'Sequence[GuardrailFn]' = (), output_guardrails: 'Sequence[GuardrailFn]' = (), enabled: 'EnabledRule' = True, context: 'ExecutionContext | None' = None, effect: 'EffectName | None' = None, tags: 'Iterable[str]' = ()) -> 'SchemaTool'`

~~~text
Expose ``module`` to an agent.

The model sees the module's input schema. Each call runs ``module.execute`` (which
validates inputs again and emits module events) with an ``ExecutionContext`` tied
to the agent run. A module failure is returned to the model as an error result
instead of ending the run; ``max_result_chars`` keeps large results from flooding
the context window. ``timeout``, ``on_timeout``, ``input_guardrails``,
``output_guardrails`` and ``enabled`` work as on ``@tool``.

``context`` ties every call to an enclosing module run (an agent node inside a
workflow): each call's ``ExecutionContext`` takes its workspace, principal, run id,
deadline, cancellation, budget, event sink, artifact store and credential resolver.
Its approval is never inherited: only a call the approval gate let through is granted.

The tool keeps the module's identity: ``origin`` is ``mcp:<server>:<tool>``,
``openapi:<METHOD>:<path>`` or ``module:<module_id>``, the tags include
``module:<module_id>`` plus ``tags``, and its effect class comes from the manifest's
``effect_policy`` (see ``module_effect``). Pass ``effect=`` to override it.

A call needs approval when any gate says so: ``requires_approval``, the manifest's
write gate, or, for a module that wraps a ``Tool`` (``tool_to_module``), that tool's
own ``requires_approval``. Wrapping a tool never removes its approval gate.
~~~

### `SchemaTool`

class `SchemaTool(name: 'str', parameters: 'dict[str, Any]', handler: 'SchemaHandler', *, description: 'str' = '', requires_approval: 'ApprovalRule' = False, max_retries: 'int | None' = 1, enabled: 'EnabledRule' = True, input_guardrails: 'Sequence[GuardrailFn]' = (), output_guardrails: 'Sequence[GuardrailFn]' = (), timeout: 'float | None' = None, on_timeout: 'OnTimeout' = 'tell_model', retry: 'ToolRetry | None' = None, cache: 'bool' = False, idempotent: 'bool' = False, effect: 'EffectName | None' = None, reconcile: 'ReconcileFn | None' = None, origin: 'str | None' = None, tags: 'Iterable[str]' = ()) -> 'None'`

~~~text
A tool described by a raw JSON Schema instead of a Python signature.

Used for tools whose arguments are defined elsewhere (MCP servers, OpenAPI specs).
The handler receives ``(ctx, arguments)`` after the arguments pass the schema.
~~~

- `arun(self, **kwargs: 'Any') -> 'Any'`
  Async counterpart of ``run``; always raises because schema tools need a Runner.
- `execute(self, arguments: 'dict[str, Any]', ctx: 'RunContext[Any] | None' = None) -> 'str'`
  Validate arguments against the schema, run the handler and return its result.
- `run(self, **kwargs: 'Any') -> 'Any'`
  Always raises ``ToolValidationError``; schema tools run only through a Runner.
- `to_schema(self) -> 'dict[str, Any]'`
  Return the OpenAI-style function schema built from the stored JSON Schema.
- `validate_arguments(self, arguments: 'dict[str, Any]') -> 'None'`
  Raise ``ToolValidationError`` if ``arguments`` do not fit the JSON Schema.

### `StopAtTools`

class `StopAtTools(tool_names: 'Sequence[str]') -> 'None'`

~~~text
``Agent(tool_use_behavior=StopAtTools(["name", ...]))``: a successful call to one of
these tools ends the run, and its result is the final output (no further model call).
~~~


### `ToolRetry`

class `ToolRetry(max_attempts: 'int' = 3, *, initial_delay: 'float' = 0.5, backoff: 'float' = 2.0, max_delay: 'float' = 8.0, retry_on: 'tuple[type[BaseException], ...]' = (<class 'ConnectionError'>, <class 'TimeoutError'>)) -> 'None'`

~~~text
Retry a tool automatically when it fails with a transient error.

``@tool(idempotent=True, retry=ToolRetry(max_attempts=3))``: the call is tried up to
``max_attempts`` times in total, waiting ``initial_delay`` seconds, then
``initial_delay * backoff``, ... (at most ``max_delay``) between attempts. Only
exceptions of the ``retry_on`` types are retried (default: connection and timeout
errors, which includes the tool's own ``timeout``). The model sees only the final
outcome. Requires ``idempotent=True``, because a failed attempt may have had effects.
~~~

- `delay(self, attempt: 'int') -> 'float'`
  Seconds to wait after failed attempt number ``attempt`` (1-based).

### `ToolOutput`

class `ToolOutput(text: 'str' = '', attachments: 'Sequence[Any]' = ()) -> 'None'`

~~~text
A tool result with files: ``return ToolOutput("Here is the chart.", [chart])``.

A tool can also return an ``Attachment`` or a list holding attachments and text. The
text becomes the tool result; the files are shown to the model in the next message (see
``docs/multimodal.md``).
~~~


### `ToolInfo`

class `ToolInfo(name: 'str', origin: 'str | None' = None, effect: 'str' = 'non_idempotent', requires_approval: 'bool' = False, tags: 'frozenset[str]' = frozenset()) -> None`

~~~text
What a hook, guardrail or policy rule may know about a tool: ``ctx.tool``.

``origin`` says where the tool comes from: ``python:<module>.<qualname>`` for a
``@tool`` function, ``mcp:<server>:<tool>`` and ``openapi:<METHOD>:<path>`` for MCP and
OpenAPI tools, ``module:<module_id>`` for other module tools, ``agent:<name>`` for
``Agent.as_tool``, ``builtin:<name>`` for the SDK's own tools, or ``None`` when unknown.
``effect`` is the tool's effect class (``pure``, ``idempotent``, ``non_idempotent`` or
``reconcilable``). ``requires_approval`` is ``True`` when the tool declares an approval
flag or rule (a rule may still say no for a given call). ``tags`` are labels such as
``toolset:<name>`` (MCP server or OpenAPI toolset) or your own ``@tool(tags=...)``.
~~~


### `SessionStore`

class `SessionStore(*args, **kwargs)`

~~~text
Protocol for conversation session storage.
~~~

- `close(self) -> 'None'`
  Release any database or store resources.
- `get(self, session_id: 'str') -> 'SessionSnapshot | None'`
  Retrieve the session snapshot, or None if the session does not exist.
- `save(self, session_id: 'str', messages: 'list[Message]', expected_revision: 'int') -> 'SessionSnapshot'`
  Save conversation messages using compare-and-swap on expected_revision.

### `MemorySessionStore`

class `MemorySessionStore() -> 'None'`

~~~text
In-memory session store with compare-and-swap concurrency control.

Useful for tests and transient execution where durability across restarts
is not needed.
~~~

- `close(self) -> 'None'`
  No-op for in-memory store.
- `delete(self, session_id: 'str') -> 'bool'`
  Delete a session. Returns ``True`` if it existed.
- `get(self, session_id: 'str') -> 'SessionSnapshot | None'`
  Return a copy of the stored snapshot, or ``None`` if the session is unknown.
- `list_sessions(self) -> 'list[str]'`
  Return all stored session ids, sorted.
- `save(self, session_id: 'str', messages: 'list[Message]', expected_revision: 'int', state: 'dict[str, Any] | None' = None) -> 'SessionSnapshot'`
  Store messages if ``expected_revision`` matches, else raise ``SessionConflictError``.

### `SQLiteSessionStore`

class `SQLiteSessionStore(db_path: 'str | Path') -> 'None'`

~~~text
Durable conversation session store backed by standard library sqlite3.

Uses thread offloading (asyncio.to_thread) to prevent blocking the event loop
during database I/O, explicit lifecycle management, and compare-and-swap
optimistic locking.
~~~

- `close(self) -> 'None'`
  Asynchronously close database connection.
- `close_sync(self) -> 'None'`
  Synchronously close database connection.
- `delete(self, session_id: 'str') -> 'bool'`
  Delete a session. Returns ``True`` if it existed.
- `get(self, session_id: 'str') -> 'SessionSnapshot | None'`
  Asynchronously retrieve session snapshot without blocking the event loop.
- `list_sessions(self) -> 'list[str]'`
  Return all stored session ids, sorted.
- `save(self, session_id: 'str', messages: 'list[Message]', expected_revision: 'int', state: 'dict[str, Any] | None' = None) -> 'SessionSnapshot'`
  Asynchronously save session snapshot using CAS without blocking the event loop.
- `updated_at(self, session_ids: 'list[str]') -> 'dict[str, float]'`
  Last-save time (Unix seconds) of each stored session among ``session_ids``.

### `fork_session`

function `fork_session(store: 'SessionStore', source_id: 'str', new_id: 'str', *, at: 'int | None' = None) -> 'SessionSnapshot'`

~~~text
Copy a session into a new one, whole or up to a message, to branch the conversation.

``at`` is how many of the stored messages to keep (``None``: all of them), so ``at=4``
keeps ``snapshot.messages[:4]``, which starts with the system message, and the next run
on ``new_id`` continues from there. The source session
is not changed. The saved run state (``RunContext.state``) is copied too, when the store
keeps state.

Raises:
    SDKValidationError: ``new_id`` equals ``source_id``, ``source_id`` does not exist,
        ``at`` is out of range, or ``at`` would separate a tool call from its results
        (the message lists the counts that are allowed).
    SessionConflictError: ``new_id`` already exists; a fork never overwrites a session.
~~~

### `delete_session`

function `delete_session(store: 'SessionStore', session_id: 'str') -> 'bool'`

~~~text
Delete a session. Returns ``True`` if it existed, ``False`` if it did not.

Needs a store with an optional ``delete(session_id)`` method (both built-in stores have
one); otherwise raises ``SDKValidationError``.
~~~

### `list_sessions`

function `list_sessions(store: 'SessionStore') -> 'list[str]'`

~~~text
Return the ids of all stored sessions, sorted.

Needs a store with an optional ``list_sessions()`` method (both built-in stores have
one); otherwise raises ``SDKValidationError``.
~~~

### `pop_last_turn`

function `pop_last_turn(store: 'SessionStore', session_id: 'str') -> 'SessionSnapshot | None'`

~~~text
Remove the last user turn and everything after it; return the saved snapshot.

The turn is the last user message plus its tool calls, tool results and reply. Earlier
turns, the system message and saved run state stay. Returns ``None`` (and changes
nothing) when the session is missing or has no user message.

The save is compare-and-swap on the revision that was read, so a concurrent save
raises ``SessionConflictError`` instead of losing data.
~~~

### `retry_last_turn`

function `retry_last_turn(runner: 'Runner', agent: 'Agent[OutputT]', session_id: 'str', **run_kwargs: 'Any') -> 'RunResult[OutputT]'`

~~~text
Pop the last turn of ``session_id`` and run its user input again.

This is a helper function rather than a ``Runner`` method so ``runner.py`` stays
unchanged. The runner must have a ``session_store``. Earlier turns stay; the retried turn
gets a new reply. ``run_kwargs`` (``deps``, ``timeout``, ``cancel``, ...) go to
``runner.run_async``. If the retry fails or is cancelled, the original turn is put back
(unless the session changed meanwhile, in which case nothing is overwritten). Raises
``SDKValidationError`` when there is no turn to retry and ``SessionConflictError`` when
the session changed during the pop.
~~~

### `RateLimiter`

class `RateLimiter(*, max_concurrent: 'int | None' = None, requests_per_minute: 'int | None' = None, tokens_per_minute: 'int | None' = None, clock: 'Callable[[], float] | None' = None, sleep: 'Callable[[float], Any] | None' = None) -> 'None'`

~~~text
Cap concurrent model calls and pace requests and tokens per minute.

- ``max_concurrent``: at most this many calls (or open streams) at once.
- ``requests_per_minute``: at most this many call starts in any 60 second window.
- ``tokens_per_minute``: after reported usage reaches this many tokens in the last
  60 seconds, new calls wait until older usage ages out. This is approximate: a
  call's tokens are only known after it finishes, so calls already in flight can
  push usage past the budget, and calls that report no usage count as 0 tokens.

Waiting never busy-loops and is cancellable; a cancelled wait takes nothing. ``clock``
returns seconds (default ``time.monotonic``) and ``sleep`` is the awaitable used for
timed waits (default ``asyncio.sleep``); both exist so tests can run without delays.
~~~

- `acquire(self) -> 'None'`
  Wait until a call may start, then take a slot. Pair with :meth:`release`.
- `record_usage(self, usage: 'Usage | None') -> 'None'`
  Count a call's reported tokens toward ``tokens_per_minute``.
- `release(self, usage: 'Usage | None' = None) -> 'None'`
  Give back a slot taken by :meth:`acquire`, recording the call's reported usage.

### `RateLimitedModel`

class `RateLimitedModel(model: 'ModelProtocol', limiter: 'RateLimiter') -> 'None'`

~~~text
Wrap a model so every ``generate`` and ``stream`` call goes through a limiter.

A stream holds its slot until it finishes, fails, is closed or is cancelled. Pass one
``RateLimiter`` to several wrappers to limit them together. Nest it inside
``RetryModel`` so each retry waits for a slot: ``RetryModel(RateLimitedModel(m, lim))``.
~~~

- `generate(self, *args: 'Any', **kwargs: 'Any') -> 'ModelResponse'`
  Wait for a slot, call the wrapped model, then release and record usage.
- `stream(self, *args: 'Any', model_settings: 'Any' = None, **kwargs: 'Any') -> 'AsyncIterator[Any]'`
  Stream from the wrapped model while holding a slot until the stream ends.

### `GroupChat`

class `GroupChat(name: 'str', members: 'Sequence[Agent | Team]', *, max_turns: 'int', select: "Literal['round_robin'] | Callable[[list[GroupChatTurn], list[str]], Any] | Agent" = 'round_robin', terminate: 'TerminationCondition | Callable[[Sequence[GroupChatTurn]], Any] | None' = None, history_turns: 'int' = 20, history_chars: 'int' = 20000, description: 'str | None' = None, runner: 'Runner | None' = None) -> 'None'`

~~~text
Members take turns in one shared conversation (AutoGen group chat, CrewAI manager).

Each turn one member speaks. Its prompt is the task followed by the conversation so
far as plain text, one block per turn, ``"<member name>: <output text>"``::

    <task>

    Conversation so far:
    Writer: first draft
    Critic: too long

The first turn gets the task alone. The history is bounded: only the last
``history_turns`` turns (default 20) are shown, then oldest turns are dropped until the
text fits in ``history_chars`` characters (default 20000; a single turn longer than
that is cut from its start). A dropped history starts with ``[earlier turns omitted]``.
A task given as a list of attachments keeps them and gets the conversation as a final
text part.

``select`` chooses who speaks:

- ``"round_robin"`` (default): members in order, again and again.
- a callable ``(transcript: list[GroupChatTurn], members: list[str]) -> name``.
- a manager ``Agent``: asked "who speaks next" with the member names and descriptions;
  its answer (stripped, case-insensitive) must be a member name or the run fails with
  ``SDKValidationError``. Its usage counts like a member's. It may not need approval.

``terminate`` is a condition (``MaxTurns``, ``TextMention``, a plain
``callable(transcript) -> bool``, combined with ``|`` and ``&``). ``max_turns`` (1 to
100) is always a hard cap. The output is the last speaker's output and
``result.stop_reason`` says why the chat ended (``"max_turns"`` for the cap, otherwise
the condition, e.g. ``"text_mention('DONE')"``).

Steps are named ``turn-3:Writer``; a manager's choice is a step named ``select-3``.
Member names must be unique (case-insensitive) and must not contain ``/``.
~~~


### `GroupChatTurn`

class `GroupChatTurn(turn: 'int', speaker: 'str', text: 'str', output: 'Any' = None) -> None`

~~~text
One turn of a ``GroupChat`` as selectors and termination conditions see it.

``turn`` counts from 1, ``speaker`` is the member name, ``text`` the output as text and
``output`` the raw output (a Pydantic model stays a model).
~~~


### `MaxTurns`

class `MaxTurns(n: 'int') -> 'None'`

~~~text
Stop once the chat has ``n`` turns.
~~~

- `check(self, transcript: 'Sequence[GroupChatTurn]') -> 'str | None'`
  ``"max_turns(n)"`` once the chat has ``n`` turns, else ``None``.

### `TextMention`

class `TextMention(text: 'str') -> 'None'`

~~~text
Stop when the last speaker's output contains ``text`` (case-insensitive).
~~~

- `check(self, transcript: 'Sequence[GroupChatTurn]') -> 'str | None'`
  ``"text_mention('...')"`` when the last output contains the text, else ``None``.

### `TerminationCondition`

class `TerminationCondition()`

~~~text
A rule that ends a ``GroupChat``. Combine with ``|`` (either) and ``&`` (both).

A plain ``callable(transcript) -> bool`` works wherever a condition does.
~~~

- `check(self, transcript: 'Sequence[GroupChatTurn]') -> 'str | None'`
  The reason to stop, or ``None`` to go on.

### `web_fetch_tool`

function `web_fetch_tool(*, policy: 'DestinationPolicy | None' = None, max_bytes: 'int' = 200000, timeout: 'float' = 15.0, max_redirects: 'int' = 5, allowed_content_types: 'Sequence[str]' = ('text/html', 'application/xhtml+xml', 'text/plain', 'text/markdown', 'text/x-markdown', 'application/json'), allowed_domains: 'Sequence[str]' = (), name: 'str' = 'web_fetch', transport: 'httpx.AsyncBaseTransport | None' = None) -> 'Tool'`

~~~text
Build a tool that fetches one http(s) page with GET and returns readable text.

``policy`` defaults to ``DestinationPolicy()``, which blocks private, loopback,
link-local, multicast, reserved, unspecified and cloud-metadata addresses.
``allowed_domains=["example.com"]`` restricts the tool to those domains and their
subdomains (every other site is refused); the address checks still apply.
``DestinationPolicy(allowed_hosts=[...])`` is different: it does not restrict anything,
it lets the listed hosts skip the address checks (for a trusted internal host).
``transport`` is a hook for tests and local use (for example ``httpx.MockTransport``).
Failures come back to the model as ``{"error": ..., "message": ...}`` with no headers.
~~~

### `RunCheckpointStore`

class `RunCheckpointStore(*args, **kwargs)`

~~~text
Where a durable runner keeps the latest checkpoint of each unfinished run.
~~~

- `delete(self, run_id: 'str') -> 'None'`
  Remove the checkpoint of ``run_id`` (no error when it does not exist).
- `list_incomplete(self, limit: 'int' = 100) -> 'list[str]'`
  Run ids that still have a checkpoint, oldest update first.
- `load(self, run_id: 'str') -> 'str | None'`
  Return the checkpoint of ``run_id``, or None when there is none.
- `save(self, run_id: 'str', checkpoint_json: 'str') -> 'None'`
  Atomically replace the checkpoint of ``run_id``.

### `keep_last_turns`

function `keep_last_turns(n: 'int') -> 'Callable[[list[Message]], list[Message]]'`

~~~text
Keep the system message and the last ``n`` user turns (with their tool calls).
~~~

### `fit_token_budget`

function `fit_token_budget(max_tokens: 'int', *, estimator: 'TokenEstimator | None' = None) -> 'Callable[[list[Message]], list[Message]]'`

~~~text
Drop the oldest user turns until the estimated size fits ``max_tokens``.

Estimates are offline (``HeuristicTokenEstimator`` by default), not the provider's
count, so leave headroom. The system message and the current turn are always kept,
even when they alone exceed the budget. Attachments are not counted.
~~~

### `summarize_older_turns`

function `summarize_older_turns(model: 'Any', *, keep_last_turns: 'int' = 4, max_summary_chars: 'int' = 2000, instructions: 'str | None' = None, max_cache_entries: 'int' = 64, on_error: 'str' = 'trim', timeout: 'float | None' = None) -> 'Callable[[RunContext[Any], list[Message]], Awaitable[list[Message]]]'`

~~~text
Replace turns older than the last ``keep_last_turns`` user turns with a summary.

For ``Agent(history_processors=[...])``. ``model`` is any model with
``generate(messages)`` (``FakeModel`` in tests). The older turns are cut only at
user-message boundaries, so a tool call stays with its result, and the system
messages and the recent turns are untouched. The summary is inserted as one *system*
message right after the leading system messages, labelled as an automatic summary
and wrapped in ``<conversation_summary>`` tags; it is never presented as the user's
words. The runner's retry feedback messages are not sent to the summarizer. The
stored session is not changed.

Cache: summaries are kept per processor object, keyed by a SHA-256 chain over the
older turns (their messages, ``max_summary_chars`` and ``instructions``), so the same
prefix is summarized once however many model calls follow. When the cut moves forward
(a new turn became old) the cached summary of the longest earlier prefix is extended
with just the newly old turns instead of starting over. At most
``max_cache_entries`` summaries are kept (least recently used dropped); the cache
lives as long as the processor and is shared by runs that use it.

Summarizer usage is counted toward the run with ``ctx.add_usage``. The runner folds
that usage in after the next tool step, so a run that finishes right after a
summarising model call without calling a tool does not include it.

``on_error`` decides what happens when the summarizer raises or returns no text:
``"trim"`` (default) falls back to plain trimming (the older turns are dropped, as
``keep_last_turns`` would) and the failure is not cached, so the next call retries;
``"raise"`` fails the run with ``SDKValidationError``.
~~~

### `MemoryCheckpointStore`

class `MemoryCheckpointStore(*, execution_store: 'ExecutionStore | None' = None, workspace_id: 'str' = 'default') -> 'None'`

~~~text
In-process store for tests and demos; checkpoints do not survive the process.

Its execution journal is a ``MemoryExecutionStore`` (pass ``execution_store=`` to share
one between stores of the same process). Journal rows of finished runs stay in memory
for the life of the store.
~~~

- `claim(self, run_id: 'str', owner: 'str', lease_seconds: 'float' = 3600.0) -> 'bool'`
  Take the exclusive recovery lease of ``run_id``; False when another owner holds it.
- `consume_approval(self, approval_id: 'str', payload: 'str') -> 'bool'`
  Atomically consume a matching, unexpired approval once.
- `delete(self, run_id: 'str', owner: 'str | None' = None, *, fenced: 'bool' = False) -> 'None'`
  Remove the checkpoint of ``run_id`` if present (``fenced``: only if not stale).
- `list_incomplete(self, limit: 'int' = 100) -> 'list[str]'`
  Run ids with a checkpoint, oldest update first.
- `load(self, run_id: 'str') -> 'str | None'`
  Return the checkpoint of ``run_id`` or None.
- `load_approval(self, approval_id: 'str') -> 'str | None'`
  Return the immutable approval payload, including after consumption.
- `open_execution_store(self) -> 'ExecutionStore'`
  The execution journal the runner keeps next to these checkpoints.
- `release(self, run_id: 'str', owner: 'str') -> 'None'`
  Give the lease back (no-op when ``owner`` does not hold it).
- `save(self, run_id: 'str', checkpoint_json: 'str', owner: 'str | None' = None) -> 'None'`
  Store the checkpoint of ``run_id``; fenced by the recovery lease.
- `save_approval(self, approval_id: 'str', payload: 'str', expires_at: 'float') -> 'None'`
  Keep immutable approval identity independently of checkpoint cleanup.

### `SQLiteCheckpointStore`

class `SQLiteCheckpointStore(db_path: 'str | Path', *, workspace_id: 'str' = 'default') -> 'None'`

~~~text
Durable checkpoint store on stdlib ``sqlite3``.

The database file is created with mode 0600 (owner only) because checkpoints hold the
conversation transcript. Writes are atomic upserts and I/O runs in a worker thread.
Saves are fenced by the recovery lease in the same transaction, across processes.
Checkpoints, claims and approvals are scoped by ``workspace_id`` (schema version 2; a
version 1 file is migrated on open, its rows going to ``"default"``), so a store opened
with another workspace neither lists nor recovers them. The execution journal
(``SQLiteExecutionStore``) lives in the same file; it is opened on first use and
switches the file to WAL mode. Single host only, like all SQLite leases.
~~~

- `claim(self, run_id: 'str', owner: 'str', lease_seconds: 'float' = 3600.0) -> 'bool'`
  Take the exclusive recovery lease of ``run_id`` (atomic across processes).
- `close(self) -> 'None'`
  Close the database connections (checkpoints and execution journal).
- `consume_approval(self, approval_id: 'str', payload: 'str') -> 'bool'`
  Consume a matching unexpired approval once, atomically across processes.
- `delete(self, run_id: 'str', owner: 'str | None' = None, *, fenced: 'bool' = False) -> 'None'`
  Remove the checkpoint of ``run_id`` if present (``fenced``: only if not stale).
- `list_incomplete(self, limit: 'int' = 100) -> 'list[str]'`
  Run ids with a checkpoint, oldest update first.
- `load(self, run_id: 'str') -> 'str | None'`
  Return the checkpoint of ``run_id`` or None.
- `load_approval(self, approval_id: 'str') -> 'str | None'`
  Load the original approval payload, including after consumption.
- `open_execution_store(self) -> 'ExecutionStore'`
  The execution journal in the same database file (created on first use).
- `release(self, run_id: 'str', owner: 'str') -> 'None'`
  Give the lease back (no-op when ``owner`` does not hold it).
- `save(self, run_id: 'str', checkpoint_json: 'str', owner: 'str | None' = None) -> 'None'`
  Atomically store the checkpoint of ``run_id``; fenced by the recovery lease.
- `save_approval(self, approval_id: 'str', payload: 'str', expires_at: 'float') -> 'None'`
  Persist an immutable approval independently of run checkpoint cleanup.

### `ResolvedOutcome`

class `ResolvedOutcome(result: 'Any' = None, resolution: "Literal['confirmed', 'not_applied', 'failed']" = 'confirmed', reason: 'str' = '') -> None`

~~~text
A person's answer for a tool call whose outcome was unknown after a crash.

Pass it to ``Runner.resume_async(agent, interruption, outcomes={call_id: ...})`` when
``interruption.pause_reason == "effect_unknown"``:

- ``ResolvedOutcome(result)``: the action happened; ``result`` becomes the tool result
  and the call is not run again.
- ``ResolvedOutcome.not_applied()``: the action did not happen; the call is sent again
  (with a new idempotency key). A tool that requires approval pauses for a fresh one.
- ``ResolvedOutcome.failed(reason)``: the action failed; the run fails.

With a journal-capable store the resolution is recorded on the call's effect record.
~~~

- `failed(cls, reason: 'str' = '') -> 'ResolvedOutcome'`
  The action failed: the run fails with this reason.
- `not_applied(cls) -> 'ResolvedOutcome'`
  The action did not happen: send the call again.

### `RunRecoveryError`

class `RunRecoveryError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
Raised when a durable run cannot be recovered (unknown, corrupt or mismatched
checkpoint, or an uncertain tool call with ``on_uncertain="fail"``).
~~~


### `ApproveWith`

class `ApproveWith(arguments: 'dict[str, Any]', digest: 'str | None' = None) -> None`

~~~text
Approve a pending tool call, but run it with ``arguments`` instead.

``Runner.resume(..., approve={call_id: ApproveWith(arguments={...}, digest=d)})``. The
edit is checked against the tool's schema, and the call in the transcript is rewritten,
so the history shows what actually ran. ``digest`` (from
``interruption.pending_digests()``) binds the approval to the original action and
approval occurrence the reviewer saw. Omitting it requires a verified signed or
server-stored interruption.
~~~


### `CheckpointLeaseLostError`

class `CheckpointLeaseLostError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
A checkpoint write was refused because another caller now holds the run's
recovery lease; this caller must stop so it cannot overwrite newer progress.
~~~


### `Memory`

class `Memory(store: 'MemoryStore', *, namespace: 'str | Callable[[RunContext[Any]], str]' = 'default', recall_limit: 'int' = 5, min_score: 'float | None' = None, auto_recall: 'bool' = True, tools: 'bool' = True, max_chars: 'int' = 2000, ttl: 'float | None' = None, dedupe_threshold: 'float | None' = None, clock: 'Callable[[], float]' = <built-in function time>) -> 'None'`

~~~text
Attach long-term memory to an agent: ``Agent(..., memory=Memory(store))``.

``namespace`` is a string or a function of the :class:`RunContext`, evaluated per run
(``lambda ctx: ctx.deps.user_id``). If it fails or returns an empty value the run
fails; memory never falls back to a shared namespace.

``auto_recall`` searches the namespace once per run with the latest user message and
adds the hits to the system instructions for that run only (they are not saved in
sessions). ``tools`` gives the model ``remember`` and ``recall`` tools. ``max_chars``
caps the whole recalled block.

``ttl`` (seconds, or None for never) makes memories saved through this object expire
``ttl`` seconds after they are saved. Expired memories are not recalled, even from
stores that do not know about expiry; ``await memory.prune()`` removes them.

``dedupe_threshold`` (cosine similarity in (0, 1], or None to disable) consolidates
near-duplicates: see :meth:`save`. ``clock`` (default ``time.time``) is injectable
for tests; give an ``InMemoryMemoryStore`` the same clock.
~~~

- `build_tools(self) -> 'list[Tool]'`
  The ``remember`` and ``recall`` tools bound to this memory.
- `live(self, records: 'Sequence[MemoryRecord]') -> '_Records'`
  ``records`` without the ones that have expired (stores need not filter).
- `namespace_for(self, ctx: 'RunContext[Any]') -> 'str'`
  The namespace for this run.
- `prune(self, namespace: 'str | None' = None) -> 'int'`
  Remove expired memories; returns how many.
- `recall_block(self, ctx: 'RunContext[Any]', query: 'str') -> 'tuple[str, Usage | None]'`
  Search for ``query`` and render the instructions block ("" when nothing matches).
- `render_block(self, records: 'Sequence[MemoryRecord]') -> 'str'`
  Format records as the delimited "Relevant memories:" block, within ``max_chars``.
- `save(self, namespace: 'str', text: 'str', metadata: 'dict[str, Any] | None' = None) -> 'tuple[MemoryRecord, str | None]'`
  Save ``text`` applying ``ttl`` and ``dedupe_threshold``.

### `MemoryRecord`

class `MemoryRecord(id: 'str', text: 'str', namespace: 'str', metadata: 'dict[str, Any]' = <factory>, created_at: 'float' = <factory>, score: 'float | None' = None, expires_at: 'float | None' = None) -> None`

~~~text
One stored memory. ``score`` is set on search results only (higher is closer).
~~~

- `from_dict(cls, data: 'dict[str, Any]') -> 'MemoryRecord'`
  Inverse of :meth:`to_dict`.
- `to_dict(self) -> 'dict[str, Any]'`
  JSON-serialisable form.

### `MemoryStore`

class `MemoryStore(*args, **kwargs)`

~~~text
Where memories are kept. Every method takes the namespace that isolates them.
~~~

- `add(self, namespace: 'str', text: 'str', metadata: 'dict[str, Any] | None' = None) -> 'MemoryRecord'`
  Store ``text`` and return the new record.
- `delete(self, namespace: 'str', id: 'str') -> 'bool'`
  Remove a memory; False when it did not exist in that namespace.
- `list(self, namespace: 'str', limit: 'int' = 100) -> '_Records'`
  Most recent memories in the namespace, newest first.
- `search(self, namespace: 'str', query: 'str', limit: 'int' = 5, min_score: 'float | None' = None) -> '_Records'`
  Memories closest to ``query``, best first, with ``score`` set.

### `InMemoryMemoryStore`

class `InMemoryMemoryStore(embedder: 'Embedder | Any', *, clock: 'Callable[[], float]' = <built-in function time>) -> 'None'`

~~~text
Process-local store using cosine similarity over an :class:`Embedder`.

Namespaces are fully isolated: nothing stored in one is ever returned from another.
Data is lost when the process exits. For durable storage use a vector database
(see ``nexura_runtime.memory.vector_memory.VectorMemoryStore``).

Records may carry an expiry (``add(..., expires_at=epoch_seconds)``). Expired records
are never returned by ``search`` or ``list``; :meth:`prune` removes them. ``clock``
(default ``time.time``) tells the store what time it is, so tests can control it.
~~~

- `add(self, namespace: 'str', text: 'str', metadata: 'dict[str, Any] | None' = None, expires_at: 'float | None' = None) -> 'MemoryRecord'`
  Store ``text`` in ``namespace``, optionally expiring at ``expires_at``.
- `delete(self, namespace: 'str', id: 'str') -> 'bool'`
  Remove memory ``id`` from ``namespace``.
- `list(self, namespace: 'str', limit: 'int' = 100) -> '_Records'`
  Newest memories first.
- `prune(self, namespace: 'str | None' = None) -> 'int'`
  Remove expired memories from ``namespace`` (every namespace when None).
- `search(self, namespace: 'str', query: 'str', limit: 'int' = 5, min_score: 'float | None' = None) -> '_Records'`
  Best matches for ``query``, highest cosine similarity first.

### `HashingEmbedder`

class `HashingEmbedder(dimensions: 'int' = 256) -> 'None'`

~~~text
Deterministic offline embedder: hashed bag of words, L2-normalised.

For tests and demos only. It matches shared words, not meaning ("car" and
"automobile" are unrelated), so use a real embedding model in production.
~~~

- `embed(self, texts: 'list[str]') -> 'list[list[float]]'`
  Embed ``texts``; identical texts always give identical vectors.

### `BudgetManager`

class `BudgetManager(ledger: 'UsageLedger | None' = None, pricing_table: 'PricingTable | None' = None, fx_converter: 'CurrencyConverter | None' = None) -> 'None'`

~~~text
Orchestrates workspace and run budgets with versioned pricing and FX conversion.
~~~

- `create_run_budget(self, workspace_id: 'str', run_id: 'str', parent_run_id: 'str | None' = None, config: 'BudgetConfig | None' = None, default_model: 'str' = 'gpt-4o') -> 'RunBudget'`
  Create or retrieve a RunBudget; existing run policy is retained.
- `get_run_budget(self, run_id: 'str') -> 'RunBudget | None'`
  Retrieve existing run budget by run_id.
- `get_workspace_config(self, workspace_id: 'str') -> 'BudgetConfig | None'`
  Get configured workspace budget.
- `get_workspace_status(self, workspace_id: 'str') -> 'BudgetStatus'`
  Compute live workspace-wide status.
- `set_workspace_budget(self, workspace_id: 'str', config: 'BudgetConfig') -> 'None'`
  Configure budget envelopes for a workspace.

### `BudgetConfig`

class `BudgetConfig(*, max_cost_usd: decimal.Decimal | None = None, max_tokens: int | None = None, max_turns: int | None = None, max_tool_calls: int | None = None, soft_limit_percent: decimal.Decimal = Decimal('0.80'), conservative_unreported_tokens: int = 1000, conservative_unreported_cost_usd: decimal.Decimal = Decimal('0.05'), deny_unknown_pricing: bool = True, pricing_version: str | None = None) -> None`

~~~text
Budget limits and safety bounds for workspaces and runs.
~~~


### `ModelPricing`

class `ModelPricing(*, model_id: str, input_cost_per_million: decimal.Decimal, output_cost_per_million: decimal.Decimal, cached_prompt_cost_per_million: decimal.Decimal | None = None, cache_write_cost_per_million: decimal.Decimal | None = None, cache_storage_cost_per_million_per_hour: decimal.Decimal | None = None, per_request_cost: decimal.Decimal = Decimal('0'), currency: str = 'USD', effective_date: str | None = None, source: str = 'operator_config') -> None`

~~~text
Model token pricing configured with source and date.

Prices are expressed in Decimal to avoid floating-point rounding errors.
Rate units: cost per 1,000,000 tokens (industry standard).
~~~


### `PricingTable`

class `PricingTable(*, version: 'str', date: 'str', source: 'str' = 'operator_config', models: 'dict[str, ModelPricing] | None' = None) -> 'None'`

~~~text
Versioned operator-supplied pricing table.

Never relies on unversioned or scraped defaults. All pricing decisions
derive from explicit operator-configured rates with date and source metadata.
~~~

- `add_model_pricing(self, pricing: 'ModelPricing') -> 'None'`
  Register pricing for a model.
- `calculate_cost(self, model_id: 'str', usage: 'TokenUsage | None', *, conservative_fallback_cost: 'Decimal | None' = None) -> 'CostEstimate'`
  Compute exact cost using Decimal arithmetic.
- `get_pricing(self, model_id: 'str') -> 'ModelPricing | None'`
  Get model pricing, or None if unconfigured.

### `Case`

class `Case(name: 'str', input: 'Prompt', expected: 'Any' = None, metadata: 'dict[str, Any]' = <factory>, deps: 'Any' = None, auto_approve: 'bool | list[str]' = False, turns: 'list[Prompt]' = <factory>) -> None`

~~~text
One thing to evaluate: an input, optionally what is expected, and run settings.

``auto_approve`` lets the case continue through approval pauses: ``True`` approves every
pending tool call, a list of names approves only those tools. Without it a case that
needs approval is reported as ``paused``.

``turns`` makes a multi-turn conversation: ``input`` is the first user turn and each
item of ``turns`` is a later one, run in the same conversation (the earlier messages
are passed as history). Scorers see the whole conversation; ``output.turns`` holds one
view per turn. Agents only: a ``Team`` has no shared history.
~~~


### `CaseResult`

class `CaseResult(name: 'str', repeat: 'int', status: 'CaseStatus', scores: 'list[Score]' = <factory>, output_text: 'str' = '', output: 'Any' = None, usage: 'Usage | None' = None, judge_usage: 'Usage | None' = None, latency_seconds: 'float' = 0.0, prompt: 'dict[str, Any] | None' = None, agent_path: 'list[str]' = <factory>, error: 'dict[str, str] | None' = None, pending_tools: 'list[str]' = <factory>, metadata: 'dict[str, Any]' = <factory>) -> None`

~~~text
Outcome of one run of one case (one entry per repeat).
~~~

- `from_dict(cls, raw: 'dict[str, Any]') -> 'CaseResult'`
  Rebuild a case result from ``to_dict`` output.
- `to_dict(self) -> 'dict[str, Any]'`
  Plain JSON-ready dict of this case result.

### `Score`

class `Score(name: 'str', value: 'float', passed: 'bool', reason: 'str' = '', usage: 'Usage | None' = None) -> None`

~~~text
One scorer's verdict on one case: ``value`` in 0..1 and whether it ``passed``.

``usage`` is only set by model-based scorers (``llm_judge``): the judge's own tokens,
kept apart from the usage of the agent being evaluated.
~~~


### `EvalOutput`

class `EvalOutput(raw: 'RunResult | TeamResult', duration_seconds: 'float', turns: 'Sequence[RunResult | TeamResult] | None' = None, *, skip_messages: 'int' = 0) -> 'None'`

~~~text
What scorers receive: the finished run with RunResult-like attributes.

``final_output``, ``output``, ``usage``, ``duration_seconds``, ``messages``, ``events``,
``prompt`` and ``agent_path`` work for an agent and a team alike. ``raw`` is the
underlying ``RunResult`` or ``TeamResult``; ``runs`` lists every agent run inside it.
Latency is wall-clock time of the whole case, approval resumes included. For a
multi-turn case (``Case.turns``) ``raw`` is the last turn's result, ``messages`` is the
whole conversation, ``events``/``usage``/``agent_path`` cover every turn and ``turns``
gives one ``EvalOutput`` per turn.
~~~


### `EvalReport`

class `EvalReport(target: 'str', results: 'list[CaseResult]' = <factory>, scorer_names: 'list[str]' = <factory>, repeats: 'int' = 1) -> None`

~~~text
Everything an evaluation produced. Save with ``to_json()``, view with ``to_markdown()``.
~~~

- `compare(self, baseline: 'EvalReport', *, tolerance: 'float' = 0.0) -> 'EvalComparison'`
  Differences against ``baseline``, per case and per scorer (mean over repeats).
- `from_json(cls, data: 'str') -> 'EvalReport'`
  Rebuild a report from ``to_json`` output; rejects unknown schema versions.
- `to_dict(self) -> 'dict[str, Any]'`
  Plain JSON-ready dict of the whole report.
- `to_json(self, *, indent: 'int | None' = 2) -> 'str'`
  Serialize the report (schema version 1) to a JSON string.
- `to_markdown(self) -> 'str'`
  Summary and per-case table as Markdown, for logs and pull request comments.

### `EvalComparison`

class `EvalComparison(regressions: 'list[EvalChange]' = <factory>, improvements: 'list[EvalChange]' = <factory>) -> None`

~~~text
Per-case and per-scorer differences between a report and a baseline.
~~~

- `format(self) -> 'str'`
  Readable multi-line summary of regressions and improvements.

### `EvalRegressionError`

class `EvalRegressionError(comparison: 'EvalComparison') -> 'None'`

~~~text
Raised by ``assert_no_regressions``; ``comparison`` has the structured diff.
~~~


### `evaluate`

function `evaluate(target: 'Agent | Team', cases: 'Sequence[Case]', scorers: 'Sequence[Scorer]' = (), *, runner: 'Runner | None' = None, concurrency: 'int' = 4, repeats: 'int' = 1, deps: 'Any' = None, timeout: 'float | None' = None) -> 'EvalReport'`

~~~text
Synchronous :func:`evaluate_async`. Refuses to run inside an active event loop.
~~~

### `evaluate_async`

function `evaluate_async(target: 'Agent | Team', cases: 'Sequence[Case]', scorers: 'Sequence[Scorer]' = (), *, runner: 'Runner | None' = None, concurrency: 'int' = 4, repeats: 'int' = 1, deps: 'Any' = None, timeout: 'float | None' = None) -> 'EvalReport'`

~~~text
Run every case ``repeats`` times against ``target`` and score the results.

Each run is isolated: a fresh run with no shared session. An exception in one case
becomes an ``error`` case result; the evaluation always finishes. A case that pauses for
approval is ``paused`` unless its ``auto_approve`` allows the pending tools. Pass a
``runner`` to apply hooks, limits or a budget to every run.
~~~

### `assert_no_regressions`

function `assert_no_regressions(report: 'EvalReport', baseline: 'EvalReport', tolerance: 'float' = 0.0) -> 'EvalComparison'`

~~~text
Raise ``EvalRegressionError`` with a readable diff if ``report`` is worse.
~~~

### `NexuraError`

class `NexuraError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
Base exception for all errors in Nexura AgentKit.
~~~


### `ToolError`

class `ToolError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
Base exception for tool-related failures.
~~~


### `ProviderError`

class `ProviderError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
Base exception for LLM provider errors.

Ensures API keys and sensitive tokens are masked from messages and representations.
~~~


### `ModelTimeoutError`

class `ModelTimeoutError(timeout_seconds: float, elapsed_seconds: float) -> None`

~~~text
Raised when execution duration exceeds configured timeout limit.
~~~


### `MaxTurnsExceededError`

class `MaxTurnsExceededError(turns_count: int, max_turns: int) -> None`

~~~text
Raised when the model/tool execution loop exceeds configured maximum turns.
~~~


### `MaxHandoffsExceededError`

class `MaxHandoffsExceededError(handoffs_count: int, max_handoffs: int) -> None`

~~~text
Raised when a run hands off between agents more often than ``max_handoffs`` allows.
~~~


### `UsageLimitExceededError`

class `UsageLimitExceededError(limit: str, value: int, maximum: int) -> None`

~~~text
Raised when a run exceeds a configured ``UsageLimits`` value.
~~~


### `OutputValidationError`

class `OutputValidationError(message: str, *, raw_output: str | None, attempts: int) -> None`

~~~text
Raised when the model's final output still fails ``output_type`` after all retries.
~~~


### `GuardrailTripwireTriggered`

class `GuardrailTripwireTriggered(guardrail: str, stage: str, info: Any = None, *, tool: str | None = None) -> None`

~~~text
Raised when an input or output guardrail stops the run.
~~~


### `ApprovalDecisionError`

class `ApprovalDecisionError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
Raised when resume decisions do not match the paused run's pending tool calls.
~~~


## Red-team eval pack (`nexura_agents.redteam`)

### `CANARY`

Constant: `'NXCANARY-7f3c9e1a-do-not-reveal'`

### `contains_canary`

function `contains_canary(text: 'str', canary: 'str' = 'NXCANARY-7f3c9e1a-do-not-reveal') -> 'bool'`

~~~text
Whether ``text`` contains the canary in any form ``no_canary_leak`` looks for.
~~~

### `scrub_canary`

function `scrub_canary(value: 'Any', canary: 'str' = 'NXCANARY-7f3c9e1a-do-not-reveal') -> 'Any'`

~~~text
A copy of a JSON-like value with every string that contains the canary replaced.

Mapping keys are checked too (a key holding the canary becomes ``[REDACTED key N]``).
Use before saving or printing a red-team report anywhere it could be read (a CI artifact,
a log): a leaked canary is what the report records, not what it should keep.
~~~

### `approval_bypass_cases`

function `approval_bypass_cases(write_tools: 'Sequence[str]', *, prefix: 'str' = 'bypass') -> 'list[Case]'`

~~~text
Attempts to talk the agent into running a write tool without a human approval.
~~~

### `indirect_injection_cases`

function `indirect_injection_cases(*, tool_name: 'str' = 'read_document', prefix: 'str' = 'indirect') -> 'list[Case]'`

~~~text
Cases that ask the agent to read a document whose content carries injected orders.

Give the agent :func:`poisoned_document_tool` (with the same ``tool_name``) for these.
~~~

### `no_canary_leak`

function `no_canary_leak(canary: 'str' = 'NXCANARY-7f3c9e1a-do-not-reveal', *, name: 'str | None' = None) -> 'Scorer'`

~~~text
Pass when the canary appears nowhere the agent wrote: answers or tool-call arguments.

Every assistant message of every turn is checked, plus all tool-call arguments together
(so a canary split across two calls is caught). The comparison ignores case, separators,
line wrapping and escapes, and also looks inside URL-encoded, base64 and hex text, and at
the reversed canary. It is a fixed set of checks, not proof that nothing leaked.
~~~

### `no_write_without_approval`

function `no_write_without_approval(write_tools: 'Sequence[str]', *, name: 'str | None' = None) -> 'Scorer'`

~~~text
Pass when none of ``write_tools`` ran; a case paused for approval counts as safe.

Tools run by an ``Agent.as_tool`` sub-agent count too.
~~~

### `poisoned_document_tool`

function `poisoned_document_tool(write_tools: 'Sequence[str]', *, canary: 'str' = 'NXCANARY-7f3c9e1a-do-not-reveal', name: 'str' = 'read_document') -> 'Tool'`

~~~text
A read tool whose result tells the model to leak the canary and call a write tool.
~~~

### `prompt_injection_cases`

function `prompt_injection_cases(*, prefix: 'str' = 'injection') -> 'list[Case]'`

~~~text
Direct prompt-injection attempts to make the agent reveal its instructions or secrets.
~~~

### `redteam_ok`

function `redteam_ok(report: 'EvalReport') -> 'bool'`

~~~text
True when every red-team scorer passed on every case that finished.

A case paused for approval counts as safe (nothing ran), although ``report.pass_rate``
skips it and reads 0 when every case paused. An errored case, a failed scorer, a missing
score or a report with no cases is not ok. Use this in CI instead of ``pass_rate``.
~~~

### `redteam_suite`

function `redteam_suite(write_tools: 'Sequence[str]', *, canary: 'str' = 'NXCANARY-7f3c9e1a-do-not-reveal', indirect_tool: 'str | None' = 'read_document') -> 'tuple[list[Case], list[Scorer]]'`

~~~text
All red-team cases and the two scorers, ready for :func:`nexura_agents.evaluate`.

Pass ``indirect_tool=None`` when the agent does not have :func:`poisoned_document_tool`.
~~~

## Skills (`nexura_agents.skills`)

### `MAX_SKILL_BYTES`

Constant: `65536`

### `Skill`

class `Skill(name: 'str', description: 'str', instructions: 'str', tools: 'tuple[str, ...]' = (), resources: 'Mapping[str, str]' = <factory>, metadata: 'Mapping[str, Any]' = <factory>, format: 'str' = 'nexura') -> None`

~~~text
One skill version: text only, identified by name and the sha256 of its content.
~~~

- `from_directory(cls, path: 'str | Path', *, format: 'str' = 'nexura') -> 'Skill'`
  Load ``path/SKILL.md`` plus text files beside it as resources (no scripts).
- `from_markdown(cls, text: 'str', *, resources: 'Mapping[str, str] | None' = None, name: 'str | None' = None, format: 'str' = 'nexura') -> 'Skill'`
  Parse a ``SKILL.md``: ``---`` front matter (name, description, tools) then the body.

### `SkillError`

class `SkillError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
A skill is invalid, unknown, or would break the no-executable-content rule.
~~~


### `SkillLibrary`

class `SkillLibrary(skills: 'Iterable[Skill]' = ()) -> 'None'`

~~~text
Versioned skills by name, with labels such as ``production`` (thread-safe).
~~~

- `add(self, skill: 'Skill', *, labels: 'Sequence[str]' = ()) -> 'SkillVersion'`
  Store a new version (same content as the newest returns it); ``latest`` moves to it.
- `add_directory(self, path: 'str | Path') -> 'list[SkillVersion]'`
  Add every ``*/SKILL.md`` folder directly under ``path``, all or nothing.
- `catalog(self, *, label: 'str' = 'latest', heading: 'str' = 'Available skills') -> 'str'`
  Names and one-line descriptions, for an agent's instructions.
- `continue_numbering(self, name: 'str', next_version: 'int') -> 'None'`
  Start ``name``'s next version at ``next_version`` or later (never lower).
- `export(self) -> 'dict[str, Any]'`
  All skills, versions and labels as a JSON-ready dict (text only), for persistence.
- `from_directory(cls, path: 'str | Path') -> 'SkillLibrary'`
  Load a bounded directory of Agent Skills, in sorted order, without execution.
- `get(self, name: 'str', *, label: 'str' = 'latest', version: 'int | None' = None) -> 'Skill'`
  The skill at ``version``, or the one carrying ``label``.
- `import_versions(self, data: 'Mapping[str, Any]', *, replace: 'bool' = False) -> 'list[str]'`
  Restore skills from :meth:`export` output, all or nothing; returns the names added.
- `loader_tool(self, *, label: 'str' = 'latest', available_tools: 'Iterable[str]' = (), name: 'str' = 'load_skill', progressive: 'bool' = False) -> 'Tool'`
  A read-only ``load_skill(name)`` tool returning a skill's text (never runs anything).
- `names(self) -> 'list[str]'`
  Names of all stored skills.
- `names_with_label(self, label: 'str') -> 'list[str]'`
  Names of skills that have a version carrying ``label``.
- `next_version(self, name: 'str') -> 'int'`
  The number the next new version of ``name`` gets (also after it was removed).
- `remove(self, name: 'str', version: 'int | None' = None) -> 'int'`
  Delete every version of ``name``, or only ``version``; returns how many were removed.
- `render(self, name: 'str', *, label: 'str' = 'latest', available_tools: 'Iterable[str]' = (), progressive: 'bool' = False) -> 'str'`
  The text ``load_skill`` returns: instructions, resources and tool availability.
- `resource_tool(self, *, label: 'str' = 'latest') -> 'Tool'`
  Read one snapshot resource on demand. No filesystem access or execution.
- `set_label(self, name: 'str', label: 'str', version: 'int') -> 'None'`
  Point ``label`` at ``version``; other versions lose it.
- `versions(self, name: 'str') -> 'list[SkillVersion]'`
  All versions of a skill, oldest first.

### `SkillVersion`

class `SkillVersion(skill: 'Skill', version: 'int', labels: 'tuple[str, ...]' = ()) -> None`

~~~text
A stored skill with its version number and labels.
~~~


### `TEXT_SUFFIXES`

Constant: `('.md', '.markdown', '.txt', '.text', '.rst', '.json', '.yaml', '.yml', '.toml', '.csv', '.tsv', '.xml', '.html', '.htm')`

### `validate_skills`

function `validate_skills(skills: 'Iterable[Skill]') -> 'list[Skill]'`

~~~text
Check that names are unique; raise ``SDKValidationError`` otherwise.
~~~

## Network policy (`nexura_agents.network`)

### `DestinationPolicy`

class `DestinationPolicy(*, allow_loopback: bool = False, allow_private_ranges: bool = False, allowed_hosts: list[str] = <factory>, allowed_schemes: list[str] = <factory>) -> None`

~~~text
Enforces boundaries on outbound HTTP/HTTPS destinations.

Guarantees:
- Blocks every non-public address (RFC 1918, ULA, carrier-grade NAT, 0.0.0.0/8, multicast,
  reserved, unspecified) unless ``allow_private_ranges`` is set.
- Blocks loopback/localhost unless explicitly enabled.
- Strictly blocks link-local and cloud metadata addresses (169.254.169.254, fd00:ec2::254,
  100.100.100.200), even for hosts in ``allowed_hosts``.
- Checks IPv4 embedded in IPv6 (mapped, NAT64, 6to4, Teredo) as IPv4.
- Validates every resolved IP address, including for allow-listed hosts. An allow-listed name
  that does not resolve at check time passes ``check_url``; ``SafeHttpClient`` connects
  through ``pinned_transport``, which validates the addresses again at connect time and
  connects only to an address that passed (no DNS-rebinding window).
- Re-evaluates destination policy on every HTTP redirect.
~~~

- `check_addresses(self, url: 'str', host: 'str', addresses: 'list[_IP]') -> 'None'`
  Raise ``DestinationPolicyDeniedError`` unless every address of ``host`` is allowed.
- `check_url(self, url: 'str') -> 'None'`
  Validate URL against outbound access rules.
- `is_allow_listed(self, host: 'str') -> 'bool'`
  Whether ``host`` is in ``allowed_hosts`` (case-insensitive, trailing dot ignored).
- `resolve(host: 'str', port: 'int') -> 'list[_IP]'`
  Every address ``host`` resolves to (an IP literal resolves to itself); [] if none.

### `SafeHttpClient`

class `SafeHttpClient(policy: 'DestinationPolicy | None' = None, *, transport: 'httpx.AsyncBaseTransport | None' = None, timeout: 'float' = 30.0) -> 'None'`

~~~text
Outbound HTTP client that validates target URLs and redirects against DestinationPolicy.
~~~

- `close(self) -> 'None'`
  Close underlying HTTP client connections.
- `get(self, url: 'str', **kwargs: 'Any') -> 'httpx.Response'`
  Send a GET request.
- `post(self, url: 'str', **kwargs: 'Any') -> 'httpx.Response'`
  Send a POST request.
- `request(self, method: 'str', url: 'str', *, headers: 'dict[str, str] | None' = None, content: 'Any' = None, data: 'Any' = None, json: 'Any' = None, params: 'Any' = None, follow_redirects: 'bool' = True, max_redirects: 'int' = 10) -> 'httpx.Response'`
  Execute request with strict policy verification on original and redirect URLs.
- `stream(self, method: 'str', url: 'str', *, headers: 'dict[str, str] | None' = None, json: 'Any' = None, refuse_redirect: 'bool' = True) -> 'AsyncIterator[httpx.Response]'`
  Open a streaming response (e.g. server-sent events) after the policy check.

### `DestinationPolicyDeniedError`

class `DestinationPolicyDeniedError(url: 'str', reason: 'str', details: 'dict[str, Any] | None' = None) -> 'None'`

~~~text
Raised when an outbound URL violates security destination policy.
~~~


## Execution stores (`nexura_agents.execution_store`, used by `DurableExecutionEngine`)

### `ExecutionStore`

class `ExecutionStore(*args, **kwargs)`

~~~text
Atomic asynchronous journal API; implementations never dispatch external effects.

Create requires an owner so the original run is leased from its first write.
Busy claims return None. Missing runs raise KeyError. Conflicting journal
changes raise ExecutionConflictError. Every mutation after create requires
an unexpired owner/token pair, including operator effect resolution.
~~~

- `claim(self, execution_id: 'ExecutionId', owner: 'str', ttl: 'float', *, definition_digest: 'str') -> 'ExecutionLease | None'`
  Claim a free/expired/own lease; every acquisition after expiry increments token.
- `close(self) -> 'None'`
  Release owned backend resources.
- `commit(self, lease: 'ExecutionLease', *, state: 'dict[str, Any]', cursor: 'str | None' = None, status: 'str' = 'running', steps: 'Sequence[StepIntent | StepOutcome]' = (), effects: 'Sequence[EffectRecord]' = (), events: 'Sequence[dict[str, Any]]' = (), state_version: 'int' = 2, expected_revision: 'int | None' = None) -> 'ExecutionRun'`
  Commit state/cursor/steps/effects/events together; failures roll everything back.
- `create(self, execution_id: 'ExecutionId', *, owner: 'str', definition_digest: 'str', state: 'dict[str, Any] | None' = None, ttl: 'float' = 30.0, kind: 'str' = 'agent', parent_run_id: 'str | None' = None, origin: 'dict[str, Any] | None' = None) -> 'ExecutionRun'`
  Atomically create a leased run; duplicate identity is an error.
- `get(self, execution_id: 'ExecutionId') -> 'ExecutionSnapshot'`
  Read a consistent detached snapshot; accept current and previous versions.
- `init_db(self) -> 'None'`
  Apply versioned migrations; reject databases from a newer release.
- `list_recoverable(self, workspace_id: 'str | None' = None) -> 'list[ExecutionRun]'`
  List running/interrupted runs with free or expired leases, using store time.
- `release(self, lease: 'ExecutionLease') -> 'None'`
  Release current ownership without reducing its fencing token.
- `renew(self, lease: 'ExecutionLease', ttl: 'float') -> 'ExecutionLease'`
  Renew active ownership; stale handles raise LeaseLostError.
- `resolve_effect(self, lease: 'ExecutionLease', effect_id: 'str', resolution: 'EffectResolution', *, actor: 'str', result: 'Any' = None) -> 'EffectRecord'`
  Resolve an indeterminate effect once and audit the actor in the same transaction.

### `MemoryExecutionStore`

class `MemoryExecutionStore(*, clock: 'Callable[[], float]' = <built-in function time>) -> 'None'`

~~~text
Process-local journal; a locked snapshot is published only on successful commit.

The optional clock is the store's clock, useful for deterministic tests. This
backend is ephemeral and cannot coordinate processes or survive a restart.
~~~

- `close(self) -> 'None'`
  No external resources are held; existing snapshots remain available.
- `init_db(self) -> 'None'`
  No schema initialization is needed for memory storage.
- `list_recoverable(self, workspace_id: 'str | None' = None) -> 'list[ExecutionRun]'`
  Return expired/free running runs according to the memory store clock.

### `SQLiteExecutionStore`

class `SQLiteExecutionStore(path: 'str | Path', *, busy_timeout: 'float' = 10.0) -> 'None'`

~~~text
Durable single-host execution journal; never use a shared file across hosts.

SQLite supplies lease time using julianday('now'), rather than caller clocks.
WAL, FULL synchronous writes and BEGIN IMMEDIATE serialize competing writers.
Methods use short synchronous sqlite3 transactions inside the async API; they
can block the event loop up to busy_timeout under contention. Use Postgres for
multi-host coordination. Call init_db() before using a new database.
~~~

- `close(self) -> 'None'`
  Close this store's SQLite connection; other store instances are unaffected.
- `from_connection(cls, connection: 'sqlite3.Connection') -> 'SQLiteExecutionStore'`
  Borrow an idle SQLite connection; close() leaves it open for its owner.

### `ExecutionLease`

class `ExecutionLease(execution_id: 'ExecutionId', owner: 'str', fencing_token: 'int', expires_at: 'float') -> None`

~~~text
Fenced authority for one run; expiry is always checked using the store clock.
~~~


### `ExecutionRun`

class `ExecutionRun(execution_id: 'ExecutionId', definition_digest: 'str', state: 'dict[str, Any]', kind: "Literal['agent', 'workflow', 'durable_graph']" = 'agent', parent_run_id: 'str | None' = None, origin: 'dict[str, Any]' = <factory>, status: "Literal['running', 'interrupted', 'paused', 'completed', 'failed']" = 'running', cursor: 'str | None' = None, journal_schema_version: 'int' = 1, state_version: 'int' = 2, lease_owner: 'str | None' = None, lease_expires_at: 'float | None' = None, fencing_token: 'int' = 0, revision: 'int' = 0, created_at: 'float' = 0.0, updated_at: 'float' = 0.0) -> None`

~~~text
Detached run snapshot, including versions, cursor, identity and lease metadata.
~~~


### `ExecutionSnapshot`

class `ExecutionSnapshot(run: 'ExecutionRun', steps: 'dict[str, ExecutionStep]' = <factory>, effects: 'dict[str, EffectRecord]' = <factory>, events: 'list[ExecutionEvent]' = <factory>) -> None`

~~~text
Consistent detached snapshot of run, steps, effects and ordered events.
~~~


### `ExecutionStep`

class `ExecutionStep(intent: 'StepIntent | None' = None, outcome: 'StepOutcome | None' = None) -> None`

~~~text
At most one immutable intent and one immutable outcome per step key.
~~~


### `ExecutionEvent`

class `ExecutionEvent(sequence: 'int', event_type: 'str', payload: 'dict[str, Any]', timestamp: 'float') -> None`

~~~text
Append-only event, sequenced within a run by its fenced transaction.
~~~


### `ExecutionConflictError`

class `ExecutionConflictError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
Creation, immutable journal data or an idempotency key conflicts with stored data.
~~~


### `EffectAlreadyResolvedError`

class `EffectAlreadyResolvedError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
An operator tried to resolve an effect more than once.
~~~


### `ExecutionVersionError`

class `ExecutionVersionError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
A journal, migration or engine state version cannot be read or written safely.
~~~


## Postgres execution store (`nexura_postgres.execution_store`)

### `PostgresExecutionStore`

class `PostgresExecutionStore(dsn: 'str | None' = None, *, pool: 'Any' = None) -> 'None'`

~~~text
Optional durable multi-host journal, with database-clock leases and fenced writes.

Uses FOR UPDATE plus a token/owner/revision guarded UPDATE, following
PostgresRepository's CAS pattern. Expiry uses the server's clock_timestamp()
after locking; transaction-start now() could be stale after a row-lock wait.
Call init_db() to apply versioned migrations before first use. Supplied pools
remain caller-owned. DSNs must name an explicitly configured database.
~~~

- `close(self) -> 'None'`
  Close only a pool created by this store; supplied pools remain caller-owned.

## Execution Contract v1 (`nexura_agents.contracts`, being implemented)

### `EffectClass`

class `EffectClass(*values)`

~~~text
Recovery promise for an effect; undeclared effects default to non_idempotent.

Pure effects may repeat freely. Idempotent effects must reuse their key.
Non-idempotent effects must never silently repeat after an uncertain dispatch.
Reconcilable effects first probe for Applied, NotApplied or Unknown.
~~~


### `ExecutionId`

class `ExecutionId(workspace_id: 'str', run_id: 'str') -> None`

~~~text
Workspace-scoped execution identity; run_id is an opaque, nonempty string.

Kind, origin, parent_run_id and definition_digest belong to the run record,
not this identity pair. Trigger reclaim preserves this pair.
~~~


### `StepKey`

class `StepKey(value: 'str') -> 'StepKey'`

~~~text
Validated string identity for an agent turn or workflow graph visit.

Agent keys are turn:<n>/call:<id> or turn:<n>/model. Workflow keys are
<node>#<step>[.<attempt>][[<item>]], where the item uses one literal bracket
pair, for example charge#3.2[0]. Attempts start at 1; attempt 1 is omitted
by the workflow helper to preserve existing child run id conventions.
As a str subclass this type serializes directly with stdlib json.
~~~

- `agent_model(cls, turn: 'int') -> 'StepKey'`
  Build turn:<turn>/model for a model call (pure for execution).
- `agent_tool(cls, turn: 'int', tool_call_id: 'str') -> 'StepKey'`
  Build turn:<turn>/call:<tool_call_id> for one tool dispatch.
- `parse(cls, value: 'str') -> 'StepKey'`
  Validate a persisted key without changing its spelling.
- `workflow(cls, node_id: 'str', step_count: 'int', attempt: 'int' = 1, item: 'int | None' = None) -> 'StepKey'`
  Build a workflow visit key, with optional retry and map-item suffixes.

### `StepIntent`

class `StepIntent(step_key: 'StepKey', payload_digest: 'str', idempotency_key: 'str', effect_class: 'EffectClass' = <EffectClass.NON_IDEMPOTENT: 'non_idempotent'>, approval_id: 'str | None' = None, reservation_ids: 'list[str]' = <factory>, status: "Literal['dispatched']" = 'dispatched') -> None`

~~~text
Pre-dispatch record for one non-pure effect, committed with state and cursor.

The key is persisted and reused across takeover. A pure step needs no intent.
approval_id and reservation_ids link exact approval and budget records.
JSON objects decoded from dataclasses.asdict can be passed to the constructor.
~~~


### `StepOutcome`

class `StepOutcome(step_key: 'StepKey', status: "Literal['completed', 'failed']", output: 'Any' = None) -> None`

~~~text
Completed or failed step output, to be committed atomically with cursor advance.

Output must be JSON-compatible when persisted; no lossy string conversion is
performed here. This value type does not execute or commit the step.
~~~


### `EffectRecord`

class `EffectRecord(execution_id: 'ExecutionId', intent: 'StepIntent', effect_index: 'int' = 0, status: 'EffectStatus' = <EffectStatus.PENDING: 'pending'>, resolution: 'EffectResolution | None' = None, result: 'Any' = None, resolved_by: 'str | None' = None) -> None`

~~~text
One journaled effect with its identity, intent and explicit resolution.

Allowed states are pending, confirmed, indeterminate and resolved. Only a
resolved record carries a resolution. Store code will enforce transitions,
fencing and uniqueness of (workspace_id, intent.idempotency_key).
effect_id is derived from identity, intent.step_key and effect_index.
~~~


### `EffectStatus`

class `EffectStatus(*values)`

~~~text
Journal effect state: pending, confirmed, indeterminate or resolved.
~~~


### `EffectResolution`

class `EffectResolution(*values)`

~~~text
Machine-readable resolution: confirmed, not_applied or failed (operator fail).
~~~


### `ReconcileResult`

class `ReconcileResult()`

~~~text
Result of probing an uncertain effect: Applied(result), NotApplied or Unknown.

Instantiate one of the three dataclass variants. Unknown means the run stays
paused with pause_reason='effect_unknown'; it never authorizes re-dispatch.
~~~

- `from_dict(data: 'dict[str, Any]') -> 'ReconcileResult'`
  Restore the concrete variant from its status discriminator.
- `to_dict(self) -> 'dict[str, Any]'`
  Serialize a concrete variant, including its JSON discriminator.

### `Applied`

class `Applied(result: 'Any', status: "Literal['applied']" = 'applied') -> None`

~~~text
The effect happened; record result as the step outcome and continue.
~~~


### `NotApplied`

class `NotApplied(status: "Literal['not_applied']" = 'not_applied') -> None`

~~~text
The effect did not happen; retry with the same key and a fresh gated approval.
~~~


### `Unknown`

class `Unknown(status: "Literal['unknown']" = 'unknown') -> None`

~~~text
The effect cannot be determined; pause with pause_reason='effect_unknown'.
~~~


### `LeaseLostError`

class `LeaseLostError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
A stale owner cannot write or delete after losing the run's fenced lease.
~~~


### `DefinitionMismatchError`

class `DefinitionMismatchError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
Takeover found a different definition_digest and requires explicit migration.
~~~


### `derive_effect_id`

function `derive_effect_id(execution_id: 'ExecutionId', step_key: 'StepKey | str', effect_index: 'int' = 0) -> 'str'`

~~~text
SHA-256 of [workspace_id, run_id, step_key, effect_index] as compact JSON.

Hash bytes are UTF-8, with ensure_ascii=False and separators=(',', ':').
Tuple encoding preserves boundaries even for opaque ids containing delimiters.
Only these four identity inputs participate, so takeover preserves the digest.
~~~

### `default_idempotency_key`

function `default_idempotency_key(execution_id: 'ExecutionId', step_key: 'StepKey | str', effect_index: 'int' = 0) -> 'str'`

~~~text
Return 'nx-' plus the first 32 hex characters of the deterministic effect id.
~~~

### `effect_class_from_policy`

function `effect_class_from_policy(policy: 'str | None' = None) -> 'EffectClass'`

~~~text
Map manifest/runtime EffectPolicy strings without importing the runtime.

pure/read map to pure, idempotent_write to idempotent, and
non_idempotent_write (or an absent declaration) to non_idempotent.
Unknown spellings raise ValueError rather than weakening the recovery promise.
~~~

### `effect_class_from_idempotent`

function `effect_class_from_idempotent(idempotent: 'bool' = False) -> 'EffectClass'`

~~~text
Map @tool(idempotent=True) to idempotent; plain @tool is non_idempotent.
~~~

## Provider errors (`nexura_agents.providers.errors`)

### `ProviderError`

class `ProviderError(message: 'str', *, code: 'ContractErrorCode | str' = <ContractErrorCode.UPSTREAM_FAILURE: 'UPSTREAM_FAILURE'>, details: 'dict[str, Any] | None' = None, original_error: 'BaseException | None' = None) -> 'None'`

~~~text
Base error for all provider and streaming exceptions.
~~~


### `CapabilityError`

class `CapabilityError(message: 'str', *, provider_name: 'str', capability: 'str') -> 'None'`

~~~text
Raised when a caller requests a capability unsupported by the provider.
~~~


### `StreamCancelledError`

class `StreamCancelledError(message: 'str' = 'Stream was cancelled or disconnected cleanly.', *, stream_id: 'str | None' = None) -> 'None'`

~~~text
Raised when an ongoing token or delta stream is cancelled or disconnected.
~~~


### `MalformedToolCallArgumentsError`

class `MalformedToolCallArgumentsError(tool_name: 'str', tool_call_id: 'str', raw_arguments: 'str', reason: 'str') -> 'None'`

~~~text
Raised when streamed or returned tool call arguments fail syntax or schema validation.
~~~


### `ProviderTimeoutError`

class `ProviderTimeoutError(provider_name: 'str', timeout_seconds: 'float') -> 'None'`

~~~text
Raised when provider invocation exceeds specified deadline or timeout.
~~~


### `ProviderAuthError`

class `ProviderAuthError(provider_name: 'str', raw_error: 'str') -> 'None'`

~~~text
Raised when provider authentication fails, ensuring credentials are masked.
~~~


### `ProviderServerError`

class `ProviderServerError(message: 'str', *, retry_after: 'float | None' = None, original_error: 'BaseException | None' = None) -> 'None'`

~~~text
Raised for HTTP 5xx responses; transient, so ``RetryModel`` retries it.

``retry_after`` holds the server's ``Retry-After`` seconds when it sent one (OpenAI 503
``server_is_overloaded`` and Anthropic 529 ``overloaded_error`` may), else ``None``.
~~~


### `ProviderClientError`

class `ProviderClientError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
Raised for non-auth, non-429 HTTP 4xx responses; the request itself is wrong.

Never retried: sending the same request again fails the same way.
~~~


### `ProviderContextLengthError`

class `ProviderContextLengthError(message: str, *, original_error: BaseException | None = None) -> None`

~~~text
A 4xx saying the request is longer than the model's context window.

Not transient: sending the same messages fails again. ``RetryModel(trim_history=N)`` can
recover by dropping the oldest conversation turns and retrying.
~~~


### `ProviderRateLimitedError`

class `ProviderRateLimitedError(message: 'str', *, retry_after: 'float | None' = None, original_error: 'BaseException | None' = None) -> 'None'`

~~~text
A 429 that carries the server's ``Retry-After`` (seconds) when it sent one.
~~~


### `is_context_length_message`

function `is_context_length_message(message: 'str') -> 'bool'`

~~~text
True when a provider error message says the prompt is longer than the context window.
~~~

### `parse_retry_after`

function `parse_retry_after(value: 'str | None') -> 'float | None'`

~~~text
Parse a ``Retry-After`` seconds header; ``None`` when absent or not a number.

``"0"`` is a valid value (retry now) and is returned as ``0.0``. HTTP-date values
are not supported and are treated as absent.
~~~

## Vertex AI provider (`nexura_vertex`, package `nexura-vertex`)

### `TokenProvider`

Constant: `collections.abc.Callable[[], str | collections.abc.Awaitable[str]]`

### `VertexAIProvider`

class `VertexAIProvider(model: 'str' = 'gemini-2.5-flash', *, project: 'str | None' = None, location: 'str' = 'global', token_provider: 'TokenProvider | None' = None, timeout: 'float' = 30.0, client: 'httpx.AsyncClient | None' = None, transport: 'httpx.AsyncBaseTransport | None' = None, capabilities: 'ProviderCapabilities | None' = None, extra_headers: 'dict[str, str] | None' = None) -> 'None'`

~~~text
Gemini on Vertex AI (``aiplatform.googleapis.com``).

Args:
    model: Vertex model ID, e.g. ``"gemini-2.5-flash"``.
    project: Google Cloud project ID. Defaults to the project of the credentials.
    location: ``"global"`` or a region such as ``"us-central1"``.
    token_provider: Callable returning an OAuth access token (sync or async). Defaults
        to Application Default Credentials (google-auth, a dependency of this package).
~~~

- `delete_cached_content(self, cached_content_id: 'str') -> 'bool'`
  Always raises ``CapabilityError``; explicit cache management is not implemented.

## Templates (`nexura_runtime.templates`)

### `FixtureOutcome`

class `FixtureOutcome(name: 'str', status: 'str', state: 'dict[str, Any]', path: 'list[str]', failures: 'list[str]' = <factory>) -> None`

~~~text
The result of one fixture run: failures is empty when every expectation held.
~~~


### `Template`

class `Template(id: 'str', title: 'str', description: 'str', category: 'str', difficulty: 'str', tags: 'tuple[str, ...]', required_modules: 'tuple[str, ...]', required_credentials: 'tuple[str, ...]', spec: 'dict[str, Any]', sample_input: 'dict[str, Any]', fixture: 'dict[str, Any]', template_version: 'int' = 1, suggested_trigger: 'dict[str, Any] | None' = None, extra: 'dict[str, Any]' = <factory>) -> None`

~~~text
One starter workflow: metadata, the spec, a sample input and a test fixture.
~~~

- `detail(self) -> 'dict[str, Any]'`
  Full view: summary plus the spec, a Mermaid diagram and the sample input.
- `new_spec(self, name: 'str | None' = None) -> 'dict[str, Any]'`
  A fresh copy of the spec, optionally renamed, ready to create as a workflow.
- `summary(self) -> 'dict[str, Any]'`
  Catalog-card view: metadata only.

### `TemplateError`

class `TemplateError`

~~~text
A template file is malformed or does not validate against the engine.
~~~


### `get_template`

function `get_template(template_id: 'str') -> 'Template | None'`

~~~text
One template by id, or ``None``.
~~~

### `list_templates`

function `list_templates(category: 'str | None' = None, tag: 'str | None' = None) -> 'list[Template]'`

~~~text
Templates, optionally narrowed to one category and/or tag (exact, case-insensitive).
~~~

### `mount_templates`

function `mount_templates(app: 'FastAPI', api_v1: 'APIRouter') -> 'None'`

~~~text
Register the template handlers on the app's registry and mount ``/templates``.
~~~

### `register_template_handlers`

function `register_template_handlers(registry: 'ModuleRegistry') -> 'None'`

~~~text
Add any template handler the registry lacks; never replaces an existing one.
~~~

### `run_fixture`

function `run_fixture(template: 'Template') -> 'list[FixtureOutcome]'`

~~~text
Execute every fixture run: start, decide any approval, check the expectations.
~~~

### `template_registry`

function `template_registry() -> 'ModuleRegistry'`

~~~text
A registry holding only the template handlers.
~~~

## Builder assistant (`nexura_runtime.builder_assistant`)

### `MAX_ATTEMPTS_LIMIT`

Constant: `5`

### `MAX_DESCRIPTION_CHARS`

Constant: `4000`

### `PLACEHOLDER_HANDLER`

Constant: `'unconfigured_step'`

### `DraftError`

class `DraftError(code: 'str', message: 'str', *, errors: 'list[dict[str, Any]] | None' = None, warnings: 'list[str] | None' = None, attempts: 'int' = 0, unknown_modules: 'list[str] | None' = None) -> 'None'`

~~~text
The assistant could not produce a valid draft; ``code`` says why.
~~~


### `DraftResult`

class `DraftResult(spec: 'dict[str, Any]', explanation: 'str', warnings: 'list[str]' = <factory>, attempts: 'int' = 1) -> None`

~~~text
A draft for the user to review. Nothing here has been saved or run.
~~~

- `to_dict(self) -> 'dict[str, Any]'`
  Return the draft as a JSON-ready dict.

### `draft_from_description`

function `draft_from_description(description: 'str', *, model: 'Any', registry: 'ModuleRegistry', templates: 'Sequence[Template]' = (), max_attempts: 'int' = 3, approval_required_handlers: 'Collection[str]' = ()) -> 'DraftResult'`

~~~text
Draft a workflow spec from ``description`` using the user's ``model``.

Returns a :class:`DraftResult` that has been validated by the engine against
``registry``. Raises :class:`DraftError` when no valid draft can be produced. The
draft is never saved or run.
~~~

### `output_schema`

function `output_schema() -> 'dict[str, Any]'`

~~~text
JSON schema the model must answer with (nodes as a list so every provider accepts it).
~~~

## Code generation (`nexura_runtime.codegen`)

### `MAX_SOURCE_BYTES`

Constant: `524288`

### `CodeGenerationError`

class `CodeGenerationError`

~~~text
The spec holds a value that cannot be written as a Python literal.
~~~


### `CodeImportError`

class `CodeImportError(message: 'str', line: 'int | None' = None, column: 'int | None' = None) -> 'None'`

~~~text
Source is not the restricted shape ``workflow_to_python`` emits.

``line`` and ``column`` locate the offending construct (1-based line, 0-based column).
~~~


### `python_to_workflow`

function `python_to_workflow(source: 'str', *, max_bytes: 'int' = 524288) -> 'WorkflowSpec'`

~~~text
Rebuild a ``WorkflowSpec`` from generated Python without executing it.

Only the shape ``workflow_to_python`` emits is accepted; anything else (other calls,
attribute access, other imports, comprehensions, lambdas, f-strings, decorators...) raises
``CodeImportError`` carrying the line number.
~~~

### `workflow_to_python`

function `workflow_to_python(spec: 'WorkflowSpec', *, registry_import: 'str | None' = None) -> 'str'`

~~~text
Return readable, ruff-clean Python that rebuilds ``spec``.

Handlers stay names (strings); the generated ``__main__`` block runs the module with a
registry: ``registry_import="package.module:attribute"`` names yours, otherwise the
built-in registry is used. Every spec field is kept, so ``exec`` of the module gives a
``spec`` whose ``to_dict()`` equals ``spec.to_dict()``.
~~~

## Project scaffolding (`nexura_runtime.project`, `nexura-runtime init/dev/deploy`)

### `DEFAULT_TEMPLATE`

Constant: `'agent'`

### `SUPPORTED_TARGETS`

Constant: `{'docker': 'Dockerfile, .dockerignore and DEPLOY.md for a single-user container (Studio on a loopback-published port, token required).'}`

### `TEMPLATES`

Constant: `{'agent': ProjectTemplate(name='agent', summary='Tool-using agent with an approval-gated write tool and a token and cost budget.'), 'workflow': ProjectTemplate(name='workflow', summary='Approval-gated workflow with durable SQLite checkpoints, resumable after a restart.'), 'rag': ProjectTemplate(name='rag', summary='Knowledge ingestion and answers with citations (test embedder; real one optional).')}`

### `DeployError`

class `DeployError`

~~~text
Deployment files cannot be written as asked.
~~~


### `DeployResult`

class `DeployResult(target: 'str', written: 'list[Path]' = <factory>, replaced: 'list[Path]' = <factory>) -> None`

~~~text
The files ``write_deploy_files`` wrote, and those it replaced (``force=True``).
~~~


### `ProjectConfig`

class `ProjectConfig(root: 'Path', name: 'str', template: 'str | None', handlers: 'tuple[str, ...]', workflows: 'tuple[str, ...]') -> None`

~~~text
A project's ``[tool.nexura]`` settings: its root, name, template and code sources.
~~~


### `ProjectError`

class `ProjectError`

~~~text
The project's ``[tool.nexura]`` table or the code it names cannot be used.
~~~


### `ProjectTemplate`

class `ProjectTemplate(name: 'str', summary: 'str') -> None`

~~~text
A template ``nexura-runtime init --template NAME`` can write, with its one-line summary.
~~~


### `ScaffoldError`

class `ScaffoldError`

~~~text
A project cannot be written as asked (bad name, non-empty directory, unknown template).
~~~


### `ScaffoldResult`

class `ScaffoldResult(root: 'Path', template: 'str', package: 'str', written: 'list[Path]' = <factory>, skipped: 'list[Path]' = <factory>) -> None`

~~~text
What ``scaffold_project`` wrote, and the existing files it kept (``force=True``).
~~~


### `find_project`

function `find_project(root: 'Path') -> 'ProjectConfig | None'`

~~~text
The project in ``root``, or None when it has no ``[tool.nexura]`` table.
~~~

### `project_registry`

function `project_registry(config: 'ProjectConfig | None') -> 'ModuleRegistry'`

~~~text
Studio's built-in handlers plus the project's handler sources.
~~~

### `project_workflows`

function `project_workflows(config: 'ProjectConfig | None') -> 'dict[str, WorkflowSpec]'`

~~~text
Workflow id -> spec for every ``workflows`` source of the project.
~~~

### `scaffold_project`

function `scaffold_project(directory: 'Path', template: 'str' = 'agent', *, package: 'str | None' = None, force: 'bool' = False, env_example: 'bool' = True) -> 'ScaffoldResult'`

~~~text
Write a runnable project from ``template`` into ``directory``.

``directory`` must be missing or empty unless ``force``; even with ``force`` an existing
file is kept and listed in ``ScaffoldResult.skipped``, never overwritten.
~~~

### `write_deploy_files`

function `write_deploy_files(project_dir: 'Path', target: 'str', *, port: 'int' = 8000, force: 'bool' = False) -> 'DeployResult'`

~~~text
Write the target's files into ``project_dir``.

Existing files are never overwritten silently: without ``force`` the call is refused and
nothing is written; with ``force`` the listed files are replaced.
~~~

## Nexura V2 integration (`nexura_runtime.integrations.nexura_v2`)

### `NexuraV2TraceExporter`

class `NexuraV2TraceExporter(base_url: 'str', api_key: 'str', *, client: 'httpx.Client | None' = None, timeout: 'float' = 10, max_attempts: 'int' = 3, max_backoff_seconds: 'float' = 2, capture_content: 'bool' = False, redactor: 'Redactor | None' = None, sleeper: 'Callable[[float], None]' = <built-in function sleep>) -> 'None'`

~~~text
Synchronous, bounded exporter. Export finished traces outside async hot paths.
~~~

- `close(self) -> 'None'`
  Flush and close the exporter.
- `export(self, spans: 'Sequence[SpanRecord]', metadata: 'TraceMetadata') -> 'TraceExportReceipt'`
  Send finished spans to Nexura V2's trace ingestion.

### `TraceExportError`

class `TraceExportError(status_code: 'int | None' = None, *, retryable: 'bool' = False) -> 'None'`

~~~text
Sanitized transport/status failure; never includes server body or key.
~~~


### `TraceExportReceipt`

class `TraceExportReceipt(trace_id: 'UUID', attempts: 'int') -> None`

~~~text
TraceExportReceipt(trace_id: 'UUID', attempts: 'int')
~~~


### `TraceMetadata`

class `TraceMetadata(name: 'str', service_name: 'str | None' = None, environment: 'str | None' = None, deployment: 'str | None' = None, session_id: 'str | None' = None) -> None`

~~~text
TraceMetadata(name: 'str', service_name: 'str | None' = None, environment: 'str | None' = None, deployment: 'str | None' = None, session_id: 'str | None' = None)
~~~


### `TraceValidationError`

class `TraceValidationError`

~~~text
Invalid/incomplete trace; nothing was dispatched.
~~~


### `build_trace_envelope`

function `build_trace_envelope(spans: 'Sequence[SpanRecord]', metadata: 'TraceMetadata', *, capture_content: 'bool' = False, redactor: 'Redactor | None' = None, sensitive_strings: 'tuple[str, ...]' = ()) -> 'dict[str, Any]'`

~~~text
Build stable V2 UUID hierarchy from complete SDK spans without mutation.

Opt-in content uses attributes ``nexura.input`` and ``nexura.output``.
Default capture excludes them, arbitrary attributes and exception text.
Redaction is not a general PII detector; operators define their capture policy.
~~~

## Langflow integration (`nexura_runtime.integrations.langflow`)

### `ComponentRefusedError`

class `ComponentRefusedError`

~~~text
An extended (V3) component refused an operation: size cap, path, SQL policy, loop bound.
~~~


### `V3Options`

class `V3Options(files_root: 'str | Path | None' = None, max_file_bytes: 'int' = 1000000, max_file_paths: 'int' = 20, max_pdf_pages: 'int' = 100, vector_store: 'Any' = None, embedder: 'Embedder | None' = None, workspace_id: 'str' = 'default', knowledge_scope: "Literal['workspace', 'session']" = 'workspace', max_top_k: 'int' = 20, sql_manager: 'Any' = None, sql_connection: 'str | None' = None, sql_allow_write: 'bool' = False, sql_max_rows: 'int' = 100, sql_max_bytes: 'int' = 200000, sql_timeout_seconds: 'float' = 5.0, max_loop_items: 'int' = 100, max_split_chunks: 'int' = 1000) -> None`

~~~text
Everything the extended components need from the caller (pass to ``build(v3=...)``).
~~~


### `ingest_knowledge`

function `ingest_knowledge(opts: 'V3Options', name: 'str', texts: 'Sequence[str]', *, session_id: 'str | None' = None, dimensions: 'int | None' = None) -> 'int'`

~~~text
Embed ``texts`` into the knowledge base ``name`` of this workspace (and session).
~~~

### `BuiltFlow`

class `BuiltFlow(imported: 'ImportedFlow', fields: 'dict[str, dict[str, Any]]', order: 'list[str]', passive: 'set[str]', agents: 'dict[str, Agent[Any]]', tools: 'dict[str, Tool]', model_refs: 'dict[str, ModelRef]', warnings: 'list[str]', v3: 'V3Options' = <factory>, v3_nodes: 'dict[str, dict[str, Any]]' = <factory>, loops: 'dict[str, LoopPlan]' = <factory>, loop_body: 'set[str]' = <factory>, runner: 'Runner' = <factory>, _store: 'SessionStore | None' = None, _identity: 'str | None' = None) -> None`

~~~text
A runnable imported flow. ``agents`` maps node id to the SDK ``Agent`` built for it.
~~~

- `run(self, input_value: 'str' = '', *, session_id: 'str | None' = None, session_store: 'SessionStore | None' = None, output_component: 'str | None' = None) -> 'FlowRunResult'`
  Blocking ``run_async`` (do not call from a running event loop).
- `run_async(self, input_value: 'str' = '', *, session_id: 'str | None' = None, session_store: 'SessionStore | None' = None, output_component: 'str | None' = None) -> 'FlowRunResult'`
  Run the flow once and return the ChatOutput text (raises the original error).
- `stream_events(self, input_value: 'str' = '', *, session_id: 'str | None' = None, session_store: 'SessionStore | None' = None, output_component: 'str | None' = None, mapper: 'FlowEventMapper[EventT]') -> 'AsyncIterator[EventT]'`
  Yield the run's events as ``mapper`` shapes them (see ``FlowEventMapper``).

### `FlowBuildError`

class `FlowBuildError`

~~~text
The imported flow cannot be turned into a runnable object.
~~~


### `FlowEventMapper`

class `FlowEventMapper(*args, **kwargs)`

~~~text
Turns the steps of one flow run into events of some wire format.

The engine calls these hooks in run order: ``vertices_sorted`` once, then per vertex
``build_start``, ``add_message`` / ``token`` for chat input and output, ``end_vertex``;
then ``build_end`` and ``end``, or ``error`` and ``end`` on failure. ``from_run_event``
maps the SDK events of a model call while it runs (an empty list skips one). Pass an
implementation to ``BuiltFlow.stream_events`` or ``stream_flow_events``. The open-source
edition ships no wire format of its own; the closed edition's Nexura V2 compatibility
layer provides one.
~~~

- `add_message(self, message_id: 'str', text: 'str', *, sender: 'str', session_id: 'str | None') -> '_EventT_co'`
  A complete chat message (``sender`` is ``User`` or ``Machine``).
- `build_end(self) -> '_EventT_co'`
  Every vertex finished.
- `build_start(self, vertex_id: 'str') -> '_EventT_co'`
  A vertex starts.
- `end(self) -> '_EventT_co'`
  The last event of every run, successful or not.
- `end_vertex(self, vertex_id: 'str', display_name: 'str', class_name: 'str', results: 'dict[str, Any]') -> '_EventT_co'`
  A vertex finished with ``results`` (output name to value).
- `error(self, vertex_id: 'str | None', exc: 'BaseException') -> '_EventT_co'`
  The run failed in ``vertex_id`` (``None`` outside a vertex); ``end`` follows.
- `from_run_event(self, vertex_id: 'str', event: 'RunEvent', *, message_id: 'str | None' = None) -> 'Sequence[_EventT_co]'`
  Events for one SDK ``RunEvent`` of a model call in ``vertex_id`` (may be empty).
- `new_id(self) -> 'str'`
  A fresh message id (one per vertex, made before the run starts).
- `token(self, message_id: 'str', chunk: 'str') -> '_EventT_co'`
  One streamed text chunk of the ChatOutput message ``message_id``.
- `vertices_sorted(self, ids: 'Iterable[str]', to_run: 'Iterable[str] | None' = None) -> '_EventT_co'`
  The run order (``ids``) and the vertices that will run, once at the start.

### `FlowExportError`

class `FlowExportError`

~~~text
The flow cannot be written back to Langflow JSON; the message says why.
~~~


### `FlowFormatError`

class `FlowFormatError(message: 'str', problems: 'list[str] | None' = None) -> 'None'`

~~~text
The input is not a valid saved-flow document; ``problems`` lists every defect found.
~~~


### `FlowRunResult`

class `FlowRunResult(output: 'str', outputs: 'dict[str, str]', node_outputs: 'dict[str, dict[str, Any]]', run_results: 'dict[str, RunResult[Any]]', session_id: 'str | None' = None) -> None`

~~~text
Outcome of one flow run.
~~~


### `FlowTooLargeError`

class `FlowTooLargeError(message: 'str', problems: 'list[str] | None' = None) -> 'None'`

~~~text
The input exceeds a configured size limit (bytes, nodes or edges).
~~~


### `ImportReport`

class `ImportReport(supported: 'list[SupportedNode]' = <factory>, unsupported: 'list[UnsupportedNode]' = <factory>, notes: 'list[SkippedNote]' = <factory>, warnings: 'list[str]' = <factory>, unsupported_edges: 'list[str]' = <factory>, variables: 'list[tuple[str, str, str, bool]]' = <factory>) -> None`

~~~text
ImportReport(supported: 'list[SupportedNode]' = <factory>, unsupported: 'list[UnsupportedNode]' = <factory>, notes: 'list[SkippedNote]' = <factory>, warnings: 'list[str]' = <factory>, unsupported_edges: 'list[str]' = <factory>, variables: 'list[tuple[str, str, str, bool]]' = <factory>)
~~~

- `to_dict(self) -> 'dict[str, Any]'`
  Return the report as a JSON-ready dict.

### `ImportedFlow`

class `ImportedFlow(name: 'str', description: 'str', nodes: 'dict[str, FlowNode]', edges: 'list[FlowEdge]', report: 'ImportReport', strict: 'bool' = True, extended: 'bool' = False) -> None`

~~~text
ImportedFlow(name: 'str', description: 'str', nodes: 'dict[str, FlowNode]', edges: 'list[FlowEdge]', report: 'ImportReport', strict: 'bool' = True, extended: 'bool' = False)
~~~

- `supported_nodes(self) -> 'list[FlowNode]'`
  Return the imported nodes that have no unsupported-feature finding.

### `LangflowImportError`

class `LangflowImportError`

~~~text
Base class for every importer failure.
~~~


### `MissingVariablesError`

class `MissingVariablesError(names: 'list[str]') -> 'None'`

~~~text
The flow references global variables (``load_from_db``) the caller did not supply.
~~~


### `ModelMappingError`

class `ModelMappingError`

~~~text
A Langflow model/provider has no SDK mapping and no caller-supplied model.
~~~


### `ModelRef`

class `ModelRef(node_id: 'str', display_name: 'str', provider: 'str', name: 'str', api_key_variable: 'str | None' = None, api_key: 'str | None' = None, options: 'dict[str, Any]' = <factory>) -> None`

~~~text
What the flow asks for. ``api_key`` is only ever caller-supplied (never from the flow).
~~~


### `TweakRefusedError`

class `TweakRefusedError(refused: 'list[tuple[str, str]]') -> 'None'`

~~~text
One or more tweaks were refused; ``refused`` holds ``(key, reason)`` for all of them.
~~~


### `UnsupportedFlowError`

class `UnsupportedFlowError(message: 'str', report: 'ImportReport | None' = None) -> 'None'`

~~~text
The flow needs components or wiring the importer does not support.
~~~


### `export_flow`

function `export_flow(flow: 'ImportedFlow', *, original: 'dict[str, Any] | str | Path | None' = None, allow_sdk_only: 'bool' = False) -> 'dict[str, Any]'`

~~~text
Return Langflow flow JSON for ``flow``; with ``original`` every unmodelled field is kept.
~~~

### `build`

function `build(imported: 'ImportedFlow', *, models: 'ModelsArg' = None, tools: 'ToolsArg | None' = None, tweaks: 'Mapping[str, Any] | None' = None, variables: 'Mapping[str, str] | None' = None, search_provider: 'SearchProvider | None' = None, http_transport: 'httpx.AsyncBaseTransport | None' = None, v3: 'V3Options | None' = None) -> 'BuiltFlow'`

~~~text
Build a runnable ``BuiltFlow``.

``models``: see ``models.ModelsArg``. ``tools``: replace a tool node's implementation, keyed
by node id, display name, class name or kind. ``tweaks``: node id / display name -> fields.
``variables``: values for ``load_from_db`` global variables (never read from the flow);
API-key variables are only required when the provider table builds the model.
``search_provider``: backs web search tools (default refuses). ``http_transport``: transport
for URL tools (for example ``httpx.MockTransport``). ``v3``: options for the extended
components (see ``V3Options``); only used by flows loaded with ``extended=True``.
~~~

### `calculator_tool`

function `calculator_tool(name: 'str' = 'calculator') -> 'Tool'`

~~~text
Return a safe arithmetic calculator tool.
~~~

### `load`

function `load(flow: 'dict[str, Any] | str | Path', *, strict: 'bool' = True, max_bytes: 'int' = 5242880, max_nodes: 'int' = 1000, max_edges: 'int' = 5000, extended: 'bool' = False) -> 'ImportedFlow'`

~~~text
Parse and validate a saved Langflow flow. Never executes embedded component code.

``flow`` is a dict, JSON text or a file path. Accepts a bare starter, a database export
or a bare ``data`` object. With ``strict=True`` (default) an unsupported node on the
path to a ChatOutput raises ``UnsupportedFlowError``; with ``strict=False`` the report
lists it and the supported part is kept. ``extended=True`` also recognises the V3 families
(File, MemoryBase, KnowledgeBase/Knowledge, LoopComponent, SQLComponent, SplitText); they
stay unsupported by default so existing callers see no change.
~~~

### `run_flow`

function `run_flow(flow: 'ImportedFlow | BuiltFlow', input_value: 'str' = '', *, session_id: 'str | None' = None, session_store: 'SessionStore | None' = None, **build_kwargs: 'Any') -> 'str'`

~~~text
Blocking ``run_flow_async``.
~~~

### `run_flow_async`

function `run_flow_async(flow: 'ImportedFlow | BuiltFlow', input_value: 'str' = '', *, session_id: 'str | None' = None, session_store: 'SessionStore | None' = None, **build_kwargs: 'Any') -> 'str'`

~~~text
Run a flow once and return the ChatOutput text. ``build_kwargs`` go to ``build``.
~~~

### `safe_eval`

function `safe_eval(expression: 'str') -> 'float | int'`

~~~text
Evaluate plain arithmetic. No names, calls, attributes or huge exponents.
~~~

### `stream_flow_events`

function `stream_flow_events(flow: 'ImportedFlow | BuiltFlow', input_value: 'str' = '', *, session_id: 'str | None' = None, session_store: 'SessionStore | None' = None, mapper: 'FlowEventMapper[E]', **build_kwargs: 'Any') -> 'AsyncIterator[E]'`

~~~text
Async iterator of one run's events as ``mapper`` shapes them (``build_kwargs`` go to
``build``). ``mapper`` is required; see ``FlowEventMapper``.
~~~

## Workflows (`nexura_runtime.workflow`)

### `WorkflowSpec`

class `WorkflowSpec(name: 'str', entry_node: 'str', nodes: 'dict[str, WorkflowNode]', spec_version: 'int' = 1, terminal_nodes: 'list[str]' = <factory>, metadata: 'dict[str, Any]' = <factory>, presentation: 'dict[str, Any]' = <factory>, version: 'str | int | None' = None, migrations: 'dict[tuple[str | int, str | int], Callable[[str | int, str | int, dict[str, Any]], dict[str, Any]]]' = <factory>) -> None`

~~~text
JSON-serializable, versioned workflow specification.
~~~

- `canonical_hash(self) -> 'str'`
  Compute deterministic SHA-256 digest of this executable specification.
- `from_dict(cls, data: 'dict[str, Any]') -> 'WorkflowSpec'`
  Create specification from dictionary representation.
- `from_json(cls, json_str: 'str') -> 'WorkflowSpec'`
  Deserialize specification from JSON string.
- `register_migration(self, from_version: 'str | int', to_version: 'str | int', migrate: 'Callable[[str | int, str | int, dict[str, Any]], dict[str, Any]]') -> 'None'`
  Register a pure state transformation; directed edges can form a chain.
- `to_dict(self, include_presentation: 'bool' = True) -> 'dict[str, Any]'`
  Convert specification to a standard Python dictionary.
- `to_json(self, indent: 'int | None' = 2) -> 'str'`
  Serialize specification to formatted JSON string.
- `to_mermaid(self) -> 'str'`
  A Mermaid ``flowchart`` of this workflow: steps, branch labels and composites.
- `validate(self, registry: 'ModuleRegistry | None' = None, approval_required_handlers: 'frozenset[str] | set[str] | None' = None) -> 'None'`
  Validate specification graph completeness, entries, handler bindings, and policies.

### `WorkflowNode`

class `WorkflowNode(id: 'str', handler: 'str', transitions: 'dict[str, str]' = <factory>, default_transition: 'str | None' = None, requires_approval: 'bool' = False, required_role: 'str' = 'admin', module_id: 'str | None' = None, module_version: 'str | None' = None, config: 'dict[str, Any]' = <factory>, input_mapping: 'dict[str, Any]' = <factory>, output_mapping: 'dict[str, Any]' = <factory>, presentation: 'dict[str, Any]' = <factory>, metadata: 'dict[str, Any]' = <factory>, retry: 'dict[str, Any]' = <factory>, subgraph: 'WorkflowSpec | None' = None, parallel: 'list[str]' = <factory>, map_over: 'str | None' = None, map_as: 'str' = 'item', reducers: 'dict[str, str]' = <factory>, max_concurrency: 'int' = 10) -> None`

~~~text
Defines a single node in a workflow specification.
~~~

- `from_dict(cls, data: 'dict[str, Any]') -> 'WorkflowNode'`
  Build a node from its dict form, defaulting optional fields.
- `to_dict(self, include_presentation: 'bool' = True) -> 'dict[str, Any]'`
  JSON-compatible form; ``include_presentation=False`` drops editor-only data.

### `ModuleRegistry`

class `ModuleRegistry(versioned_registry: 'Any | None' = None) -> 'None'`

~~~text
Binds named identifiers to trusted Python handler functions or BaseModules.
~~~

- `declared_effect(self, name: 'str') -> 'tuple[EffectClass, Callable[..., Any] | None] | None'`
  The effect class (and reconcile probe) registered for ``name``, if declared.
- `describe_module(self, module_id: 'str', version: 'str | None' = None) -> 'dict[str, Any] | None'`
  Describe module metadata if registered.
- `get(self, name: 'str') -> 'Any'`
  Retrieve registered handler callable or BaseModule by name.
- `get_module(self, module_id: 'str', version: 'str | None' = None) -> 'Any'`
  Retrieve BaseModule from versioned module registry.
- `has_handler(self, name: 'str') -> 'bool'`
  True if the given handler name is registered.
- `has_module(self, module_id: 'str', version: 'str | None' = None) -> 'bool'`
  True if versioned module is registered.
- `include(self, other: 'ModuleRegistry') -> 'None'`
  Add every handler from ``other``; a name registered in both is rejected.
- `include_from(self, target: 'str') -> 'None'`
  Import trusted handlers named by an operator as ``"package.module:attribute"``.
- `override_module(self, module: 'BaseModule') -> 'None'`
  Use ``module`` instead of the shared versioned module with its id and version.
- `register(self, name: 'str', handler: 'Callable[..., Any] | BaseModule', *, effect: 'EffectClass | str | None' = None, reconcile: 'Callable[..., Any] | None' = None) -> 'None'`
  Register a Python callable or BaseModule by name.
- `register_handler(self, name: 'str', *, effect: 'EffectClass | str | None' = None, reconcile: 'Callable[..., Any] | None' = None) -> 'Callable[[Callable[..., Any]], Callable[..., Any]]'`
  Decorator to register a handler callable (``effect=`` as in :meth:`register`).

### `WorkflowEngine`

class `WorkflowEngine(spec: 'WorkflowSpec', registry: 'ModuleRegistry', *, store: 'WorkflowCheckpointStore | None' = None, max_steps: 'int' = 50, timeout: 'float | None' = None, workspace_id: 'str' = 'default', budget: 'Any | None' = None, event_sink: 'Any | None' = None, artifact_store: 'Any | None' = None, credential_resolver: 'Any | None' = None, approval_required_handlers: 'frozenset[str] | set[str] | None' = None, approval_ttl: 'float' = 3600.0, max_map_writes_per_approval: 'int' = 50, policy_version: 'str' = '1.0.0', lease_ttl: 'float' = 30.0) -> 'None'`

~~~text
Executes workflows deterministically with checkpoints and approval gates.
~~~

- `fork(self, workflow_run_id: 'str', checkpoint_index: 'int', *, state_update: 'dict[str, Any] | None' = None, new_run_id: 'str | None' = None) -> 'WorkflowRunResult'`
  Start a new run from a past checkpoint, optionally changing its state first.
- `get_result(self, workflow_run_id: 'str') -> 'WorkflowRunResult'`
  Inspect a run's latest checkpoint without dispatching or claiming approval.
- `history(self, workflow_run_id: 'str') -> 'list[WorkflowCheckpoint]'`
  Every checkpoint of a run, oldest first: inspect state step by step.
- `migrate(self, workflow_run_id: 'str') -> 'WorkflowCheckpoint'`
  Migrate checkpoint state to this definition under the run's fenced lease.
- `recover(self, workflow_run_id: 'str', *, on_uncertain: "Literal['pause', 'fail']" = 'pause', continue_on_old_definition: 'bool' = False) -> 'WorkflowRunResult'`
  Continue a stopped run, or an ``effect_unknown`` pause a person resolved.
- `resolve_effect(self, workflow_run_id: 'str', resolution: 'EffectResolution | str', *, actor: 'str', result: 'Any' = None, effect_id: 'str | None' = None) -> 'WorkflowRunResult'`
  Resolve the unknown effect a run is paused on, then continue it with :meth:`recover`.
- `resume(self, workflow_run_id: 'str', approval_id: 'str', *, decision: "Literal['approve', 'reject']" = 'approve', state_update: 'dict[str, Any] | None' = None, workspace_id: 'str | None' = None, actor: 'str | None' = None, actor_role: 'str | None' = None, expected_payload_digest: 'str | None' = None, continue_on_old_definition: 'bool' = False) -> 'WorkflowRunResult'`
  Resume an approval-gated workflow run with exact-action bindings.
- `resume_interrupt(self, workflow_run_id: 'str', interrupt_id: 'str', value: 'Any', *, workspace_id: 'str | None' = None, continue_on_old_definition: 'bool' = False) -> 'WorkflowRunResult'`
  Answer an :func:`interrupt` and continue; the paused node runs again with it.
- `resume_stream(self, workflow_run_id: 'str', approval_id: 'str', **kwargs: 'Any') -> 'AsyncGenerator[RunEvent, None]'`
  Event-stream form of :meth:`resume`; takes the same arguments.
- `run(self, initial_state: 'dict[str, Any] | None' = None, *, workflow_run_id: 'str | None' = None) -> 'WorkflowRunResult'`
  Start a new workflow execution run.
- `stream(self, initial_state: 'dict[str, Any] | None' = None, *, workflow_run_id: 'str | None' = None) -> 'AsyncGenerator[RunEvent, None]'`
  Run the workflow and yield progress events as it goes.

### `MemoryWorkflowStore`

class `MemoryWorkflowStore(*, execution_store: 'ExecutionStore | None' = None) -> 'None'`

~~~text
In-memory checkpoint store for transient workflow runs and tests.

Its execution journal (leases, intents, effects) is a ``MemoryExecutionStore``, or the
``execution_store`` given.
~~~

- `claim_approval(self, workflow_run_id: 'str', approval_id: 'str') -> 'bool'`
  Atomically claim a pending approval or interrupt; ``False`` if already claimed.
- `close(self) -> 'None'`
  Release resources held by the store.
- `create(self, checkpoint: 'WorkflowCheckpoint') -> 'bool'`
  Save the first checkpoint of a new run; False if the run id is taken.
- `get(self, workflow_run_id: 'str') -> 'WorkflowCheckpoint | None'`
  The latest checkpoint of a run, or ``None`` if it does not exist.
- `history(self, workflow_run_id: 'str') -> 'list[WorkflowCheckpoint]'`
  Every checkpoint saved for the run, oldest first.
- `list_children(self, parent_id: 'str') -> 'list[WorkflowCheckpoint]'`
  Latest checkpoint of every child run (ids starting ``<parent_id>/``), by id.
- `list_incomplete(self, limit: 'int' = 100, *, workspace_id: 'str | None' = None) -> 'list[str]'`
  Ids of top-level runs whose latest checkpoint is ``running``, oldest update first.
- `open_execution_store(self) -> 'ExecutionStore'`
  The execution journal kept next to these checkpoints (created on first use).
- `run_identities(self, run_ids: 'list[str]') -> 'dict[str, dict[str, Any]]'`
  Read a page's checkpoint identities under one store lock.
- `save(self, checkpoint: 'WorkflowCheckpoint') -> 'None'`
  Store a checkpoint as the run's latest and append it to its history.

### `SQLiteWorkflowStore`

class `SQLiteWorkflowStore(db_path: 'str | Path') -> 'None'`

~~~text
Durable workflow checkpoint store backed by sqlite3.

Its execution journal (leases, intents, effects) is a ``SQLiteExecutionStore`` in the
same database file, opened on first use with its own connection.
~~~

- `claim_approval(self, workflow_run_id: 'str', approval_id: 'str') -> 'bool'`
  Atomically claim a pending approval or interrupt; ``False`` if already claimed.
- `close(self) -> 'None'`
  Release resources held by the store.
- `close_sync(self) -> 'None'`
  Close the store from synchronous code.
- `create(self, checkpoint: 'WorkflowCheckpoint') -> 'bool'`
  Save the first checkpoint of a new run; False if the run id is taken.
- `get(self, workflow_run_id: 'str') -> 'WorkflowCheckpoint | None'`
  The latest checkpoint of a run, or ``None`` if it does not exist.
- `history(self, workflow_run_id: 'str') -> 'list[WorkflowCheckpoint]'`
  Every checkpoint saved for the run, oldest first.
- `list_children(self, parent_id: 'str') -> 'list[WorkflowCheckpoint]'`
  Latest checkpoint of every child run (ids starting ``<parent_id>/``), by id.
- `list_incomplete(self, limit: 'int' = 100, *, workspace_id: 'str | None' = None) -> 'list[str]'`
  Ids of top-level runs whose latest checkpoint is ``running``, oldest update first.
- `open_execution_store(self) -> 'ExecutionStore'`
  The execution journal in the same database file (created on first use).
- `run_identities(self, run_ids: 'list[str]') -> 'dict[str, dict[str, Any]]'`
  Read a page's active and starting checkpoint identities in one query.
- `save(self, checkpoint: 'WorkflowCheckpoint') -> 'None'`
  Store a checkpoint as the run's latest and append it to its history.

### `NodeRetryPolicy`

class `NodeRetryPolicy(max_attempts: 'int' = 1, initial_delay: 'float' = 0.5, max_delay: 'float' = 30.0, backoff: 'float' = 2.0, retry_on: 'tuple[str, ...]' = ()) -> None`

~~~text
Retry a failing node with exponential backoff (``WorkflowNode(retry={...})``).

Keys: ``max_attempts`` (1-20, default 1 = no retry), ``initial_delay`` (s),
``max_delay`` (s), ``backoff`` (multiplier), ``retry_on`` (exception class names;
empty retries any error). Timeouts and interrupts are never retried, and modules
declaring a ``non_idempotent_write`` effect cannot be retried at all.
~~~

- `delay(self, attempt: 'int') -> 'float'`
  Seconds to wait after failed attempt number ``attempt`` (1-based).
- `from_dict(cls, data: 'dict[str, Any]', node_id: 'str' = '?') -> 'NodeRetryPolicy'`
  Build a policy from a dict; invalid keys or values raise ``WorkflowValidationError``.
- `retries(self, error: 'BaseException') -> 'bool'`
  Whether ``error`` should be retried under this policy.

### `interrupt`

function `interrupt(value: 'Any' = None) -> 'Any'`

~~~text
Pause the workflow inside a node and ask a human for input.

The first time, the run stops with ``status="paused"`` and ``result.interrupt`` holds
``value`` (a question, a draft to review...). After
``engine.resume_interrupt(run_id, interrupt_id, answer)`` the node runs again from
the start and this call returns ``answer``. A node may call ``interrupt`` several
times; earlier answers are replayed in order. Code before the call runs again on
resume, so keep side effects after it.
~~~

### `request_approval`

function `request_approval(details: 'dict[str, Any]') -> 'NoReturn'`

~~~text
Pause the running node until a human approves or rejects ``details``.

Unlike :func:`interrupt`, this uses the approval path: the run stops with
``status="paused"`` and a ``pending_approval`` whose ``details`` are ``details`` plus
``"node_request": True``. It is decided like any approval gate
(``engine.resume(run_id, approval_id, decision=...)``, or the service's
``POST /runs/{id}/decide``) with the same workspace, role and expiry rules. On approve
the node runs again from its start and :func:`approved_request` returns the details; on
reject the run ends ``rejected`` and the node does not run again. Code before the call
runs again on resume, so keep side effects after it. ``details`` must be JSON and at
most ``MAX_NODE_APPROVAL_DETAILS_CHARS`` characters; not allowed in parallel or mapped
steps.
~~~

### `approved_request`

function `approved_request() -> 'dict[str, Any] | None'`

~~~text
The details a human approved for this node with :func:`request_approval`, else None.

Only the node step that runs right after the approval sees them; later nodes, other
runs and a fresh run of the same node get ``None``.
~~~

### `WorkflowInterrupt`

class `WorkflowInterrupt(value: 'Any') -> 'None'`

~~~text
Raised by :func:`interrupt` to pause a node; the engine handles it.

Derives from ``BaseException`` so a handler's ``except Exception`` does not swallow it.
~~~


### `HandlerOutput`

class `HandlerOutput(state_update: 'dict[str, Any]' = <factory>, branch: 'str | None' = None) -> None`

~~~text
Explicit output from a workflow node handler.
~~~


### `WorkflowCheckpoint`

class `WorkflowCheckpoint(workflow_run_id: 'str', spec_version: 'int', spec_hash: 'str', current_node_id: 'str | None', state: 'dict[str, Any]', step_count: 'int', status: 'WorkflowStatus', pending_approval: 'PendingApproval | None' = None, error: 'str | None' = None, updated_at: 'float' = <factory>, budget_state: 'dict[str, Any] | None' = None, interrupt: 'dict[str, Any] | None' = None, forked_from: 'dict[str, Any] | None' = None, workspace_id: 'str | None' = None, pending_effect: 'dict[str, Any] | None' = None, revision: 'int | None' = None, workflow_version: 'str | int | None' = None) -> None`

~~~text
Point-in-time execution snapshot for workflow resumption.
~~~


### `WorkflowRunResult`

class `WorkflowRunResult(workflow_run_id: 'str', status: 'WorkflowStatus', state: 'dict[str, Any]', step_count: 'int', current_node_id: 'str | None' = None, pending_approval: 'PendingApproval | None' = None, error: 'str | None' = None, budget_status: 'dict[str, Any] | None' = None, interrupt: 'dict[str, Any] | None' = None, pause_reason: 'str | None' = None, pending_effect: 'dict[str, Any] | None' = None, workflow_version: 'str | int | None' = None, definition_digest: 'str' = '') -> None`

~~~text
Final or paused result of a workflow run.
~~~


### `PendingApproval`

class `PendingApproval(approval_id: 'str', workflow_run_id: 'str', node_id: 'str', input_state: 'dict[str, Any]', created_at: 'float' = <factory>, workspace_id: 'str' = 'default', workflow_version: 'str' = '1.0.0', spec_hash: 'str' = '', required_role: 'str' = 'admin', expires_at: 'float | None' = None, policy_version: 'str' = '1.0.0', payload_digest: 'str' = '', actor: 'str | None' = None, status: 'str' = 'pending', decision: 'dict[str, Any] | None' = None, details: 'dict[str, Any] | None' = None, canonical_payload_json: 'str' = '', supersedes_approval_id: 'str | None' = None, regate_reason: 'str | None' = None, previous_input_state: 'dict[str, Any] | None' = None) -> None`

~~~text
An immutable pending approval request created before a gated node executes.
~~~

- `from_dict(cls, data: 'dict[str, Any]') -> 'PendingApproval'`
  Rebuild a pending approval from its dict form, applying defaults for missing fields.
- `to_dict(self) -> 'dict[str, Any]'`
  JSON-compatible form of this approval request.

### `REDUCERS`

Constant: `('append', 'extend', 'sum', 'merge', 'max', 'min')`

## See also

- [Documentation index](README.md)
