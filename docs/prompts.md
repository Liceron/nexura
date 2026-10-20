# Versioned prompts

Keep prompts outside your code, give every edit a version number, and promote a version
to `production` without redeploying. A registry plugs straight into
`Agent(instructions=...)`.

```python
from nexura_agents import Agent, FakeModel, PromptRegistry, Runner

registry = PromptRegistry()  # in memory; see FilePromptStore below
registry.register("support", "You help {{ user.name }} with {{ product }}.")
registry.set_label("support", "production", 1)

model = FakeModel(responses=["Sure.", "Of course."])
agent = Agent(
    name="support",
    model=model,
    instructions=registry.instructions(
        "support", variables={"user": {"name": "Ada"}, "product": "Nexura"}
    ),
)

first = Runner().run(agent, "Hello")
print(first.prompt["name"], first.prompt["version"])  # support 1
assert model.calls[0]["messages"][0].content == "You help Ada with Nexura."

# Promote a new version: the same agent object uses it on its next new conversation.
registry.register("support", "Be brief, {{ user.name }}. Product: {{ product }}.")
registry.set_label("support", "production", 2)
second = Runner().run(agent, "Hello again")
assert second.prompt["version"] == 2
```

## Template syntax

Only `{{ variable }}` is supported. `{{ user.name }}` looks up the key `name` in the mapping
`user`. There is no attribute access, no calls, no filters and no expressions, so
`{{ obj.__class__ }}` is rejected when you register it. Single braces are plain text.

```python
from nexura_agents import PromptRegistry
from nexura_agents.prompts import PromptSyntaxError, PromptVariableError

registry = PromptRegistry()
v = registry.register("greet", "Hi {{ user.name }}, ticket {{ id }}")
print(v.variables)  # ('user.name', 'id')

try:
    v.render({"user": {"name": "Ada"}})
except PromptVariableError as exc:
    print(exc.missing)  # ('id',)

# Values are inserted as str() in one pass; braces in a value are not expanded again.
print(v.render({"user": {"name": "{{ id }}"}, "id": 7}))  # Hi {{ id }}, ticket 7

try:
    registry.register("bad", "{{ a.__class__ }}")
except PromptSyntaxError as exc:
    print("rejected:", exc)
```

Extra variables are ignored. Missing ones raise `PromptVariableError` naming all of them.

## Versions and labels

`register` appends a new immutable version (1, 2, 3, ...). Registering the same template as
the newest version returns that version instead of a duplicate. The `latest` label always
follows the newest version; any label points at one version per prompt.

```python
from nexura_agents import PromptRegistry

registry = PromptRegistry()
registry.register("p", "one")
registry.register("p", "two", metadata={"author": "sam"})
registry.set_label("p", "production", 1)
print(registry.get("p").version)  # 2 (label "latest")
print(registry.get("p", label="production").version)  # 1
print(registry.get("p", version=2).metadata)  # {'author': 'sam'}
print([v.version for v in registry.versions("p")], registry.names())
```

`registry.instructions(name, label="production")` re-resolves on every run. Use
`version=N` to fix a version, or `pin=True` to resolve once when the agent is built.
`variables` can be a mapping or a function of the `RunContext`:
`variables=lambda ctx: {"run": ctx.run_id}`.

## Files

`FilePromptStore(directory)` keeps one `<name>.json` file per prompt holding all its
versions, so prompts can live in a Git repository. Writes are atomic (temp file then
rename). Names must match `[A-Za-z0-9_.-]{1,100}`, must not start with `.` or contain `..`.
Registration is serialised by a lock shared by every store on the same directory in a process, plus an OS file lock (`.prompts.lock`, POSIX) across processes.

```python
import tempfile
from nexura_agents import FilePromptStore, PromptRegistry

with tempfile.TemporaryDirectory() as directory:
    PromptRegistry(FilePromptStore(directory)).register("p", "Hello {{ name }}")
    reopened = PromptRegistry(FilePromptStore(directory))
    print(reopened.get("p").render({"name": "Ada"}))  # Hello Ada
```

## Debugging and tracing

The resolved prompt is recorded as `ctx.prompt` (inside the instructions and tools),
as `RunResult.prompt` (`{"name", "version", "sha256"}`, `None` for plain-string
instructions; a run resumed after an approval pause does not carry it), and, once
`nexura_agents.tracing.instrument()` is on, as `gen_ai.prompt.name` and
`gen_ai.prompt.version` on the `invoke_agent` span. The OpenTelemetry GenAI conventions
define no prompt-registry attributes, so these follow their `gen_ai.*` naming.

## Literal braces

Write `\{{` or `\}}` for a literal `{{` or `}}`, for example `Reply as {"a": {"b": 1\}}`.
Templates are limited to 200,000 characters.

When the history or session already contains a system message, the prompt text is not
sent, so `RunResult.prompt` and the span attributes are left empty for that run.

In practice this means a promoted version applies to **new** conversations. A conversation
stored in a session keeps the system prompt it started with, so its later turns are not
changed mid-way.
