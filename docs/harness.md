# Harness

`Agent(harness=Harness(...))` adds skills, context compaction, a task list and a scoped
filesystem. Each capability is opt-in. An empty harness adds no tools, instructions or
history processors. The SDK uses its existing runner, approvals, guardrails, budget
admission and durable execution journal.

## Skills from SKILL.md

The format is pinned to the [Agent Skills specification at
217be548739f21d6008915c29aefe320ea1a90af](https://github.com/agentskills/agentskills/blob/217be548739f21d6008915c29aefe320ea1a90af/docs/specification.mdx),
checked against the [public specification](https://agentskills.io/specification) on
2026-10-24. A skill directory contains YAML front matter (`name`, `description`) followed
by Markdown in `SKILL.md`; supporting files may live below that directory.

`SkillLibrary.from_directory(root)` scans immediate skill subdirectories in sorted order.
`Skill.from_directory(path, format="agent-skills")` loads just one. The existing default
`Skill.from_directory(path)` and `Skill.from_markdown(text)` retain their stricter Nexura
format. Both formats use the same versioned `SkillLibrary`, labels and export/import API.

The dependency-free front matter reader supports plain or quoted strings, comments,
folded/literal blocks and a one-level `metadata` string mapping. It rejects duplicate
keys, tags, anchors, aliases, flow collections and unsupported nesting. This is a safe
subset of YAML, not a general YAML parser. Standard names use 1–64 lowercase ASCII
letters/digits separated by single hyphens and match the directory; descriptions allow
up to 1024 characters, normalized to one line. Optional compatibility text is limited
to 500 characters. Optional fields are retained as metadata; `allowed-tools` grants
neither approval nor tools.

Only names/descriptions enter the system prompt. `load_skill(skill)` returns instructions
and resource names; `read_skill_resource(skill, path)` loads one text resource. Resources
are bounded snapshots taken during discovery, so file changes need an explicit reload.
The library admits at most 256 directory entries, and each skill workspace at most 256
entries/256 KiB with a 64 KiB file cap; the final skill's canonical text is capped at
64 KiB. Symlinks, hard links and special files are refused. Scripts under `scripts/` and
binary assets are not exposed as resource text. Text uses the suffix allowlist in
[skills.py](../packages/nexura-agents/src/nexura_agents/skills.py).

This example runs with an explicitly local fake model:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from nexura_agents import Agent, FakeModel, Harness, Message, SkillLibrary, ToolCall

with TemporaryDirectory() as temporary:
    root = Path(temporary) / "review"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review a proposed change.\n---\n"
        "Check the requirements before proposing a fix.\n", encoding="utf-8"
    )
    library = SkillLibrary.from_directory(temporary)
    model = FakeModel(responses=[
        Message("assistant", tool_calls=[ToolCall("s1", "load_skill", {"skill": "review"})]),
        "Requirements checked.",
    ])
    agent = Agent("Reviewer", "Help review changes.", model, harness=Harness(skills=library))
    result = agent.run("Review this change")
    assert result.output == "Requirements checked."
    assert "Check the requirements" not in model.calls[0]["messages"][0].content
```

Skills never execute scripts, import code or install tools. If an application needs script
execution, it must separately provide an explicit tool backed by a configured sandbox
(for the platform, see [sandbox backends](packages.md)), declare its effects,
and apply approvals and guardrails there. This harness supplies no execution tool or
automatic script-to-tool binding. Skill text remains untrusted prompt content.

## Context compaction

`compact_context(max_tokens, trigger_ratio=0.9, target_ratio=0.7)` is an async history
processor. At the trigger it removes oldest complete turns toward the target. Exchanges
that cross user-message boundaries are grouped together. Every system message, the
latest user turn and unanswered calls (including pending approvals) stay intact. A tool
call and its result are retained together or both removed. The stored conversation,
approval records and checkpoints remain complete.

Without `model`, compaction trims deterministically. With `model`, it summarizes removed
turns through the existing model abstraction and budget admission, adds reported usage
to the run, and caches by run id and removed content (64 entries). Budget refusals and
summary failures propagate. Summaries are bounded by `max_summary_chars` (default 2000),
labelled as automatic background, and included only when they fit alongside retained
content. A summary model is not called below the trigger.

```python
from nexura_agents import Agent, FakeModel, Harness, Message, Usage, compact_context

reported = Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)
summary_model = FakeModel(responses=["Earlier questions were discussed."], default_usage=reported)
agent = Agent(
    "Assistant", "Be helpful.", FakeModel(responses=["Ready."], default_usage=reported),
    harness=Harness(compaction=compact_context(600, model=summary_model)),
)
old = [m for i in range(6) for m in (
    Message("user", f"Question {i}: " + "x" * 1000), Message("assistant", "Answered.")
)]
result = agent.run("Latest question", history=old)
assert result.output == "Ready." and result.usage.total_tokens == 20
assert len(summary_model.calls) == 1
assert any(e.data.get("name") == "nexura.context_compacted" for e in result.events)
```

The custom event reports estimated before/after tokens, dropped message count, whether
a summary was included, and overflow. Protected content may exceed the target; it is
never cut to satisfy an estimate. Token estimates exclude attachments, recalled memory
and tool schemas; leave room for them and output. This is context selection, not a
provider context-window guarantee. You can also put this processor directly in
`Agent(history_processors=[...])`; see [history processors](agents.md#trimming-long-conversations-history-processors).

With a durable runner, completed summaries and a digest of their transcript and
configuration are saved in the execution checkpoint before the main model request.
Recovery rebuilds the compacted request from that saved summary, with its usage already
accounted, and inserts it once. The stored conversation stays complete. If the process
dies during summary generation or before its checkpoint commits, that unfinished step
can repeat; a completed summary is not regenerated when the main model call is retried.

## Plans and durable task lists

`Harness(todo=True)` installs `write_todos(items)` and `read_todos()`. Each item has a
unique `id` (1–64 characters), `text` (1–1000 characters), and `status`: `pending`,
`in_progress`, or `completed`. A plan holds at most 100 tasks. `write_todos` replaces the
whole list, so updates keep existing ids and change their status/text; `[]` clears it.
Applications may instead attach `todo_tools()` explicitly.

The plan lives in `RunContext.state["nexura.todos"]`, returned in `RunResult.state` and
saved with sessions, interruptions and checkpoints. Every replacement emits the custom
event `nexura.todos_updated`. Its `data` contains only an increasing integer `version`,
the total `count`, and `status_counts` with the fixed keys `pending`, `in_progress`,
and `completed`. Task ids, text, provider call ids and agent names are omitted; no
model- or user-controlled strings enter this notification, even with content capture
enabled. Live custom events precede tool/final-output checks. The full plan remains
authoritative run state; tool results use the ordinary
guardrail pipeline. With a checkpoint store configured
on the runner, a committed plan survives process recovery; if the process dies before
the result checkpoint, the effect journal safely replays the idempotent replacement.
Versions and per-run call receipts are saved in `nexura.todos.revision` alongside the
plan. A fresh replacement increases the version; replay of the same recorded call
keeps the current version and cannot revert a later plan. Progress events may repeat
after replay, with the same current version. Use sequential tool calls when ordering
multiple plan replacements matters. Without durable storage the plan lives only in the
run; no disk state is created automatically. See [durable runs](durable-runs.md).

## Sub-agents and a scoped filesystem

Pass the same `ScopedFilesystem` instance to parent and child harnesses, then delegate
with `child.as_tool()`. Each agent must opt in. A child gets the parent's policy hooks
and budget scopes through the existing G2 inheritance path. As with other sub-agent
tools, a child needing approval stops before the action and raises to the caller;
approval cannot be completed inside that tool call. Gate the delegation itself if a
reviewed subtask must be approved before it starts.

```python
from nexura_agents import Agent, FakeModel, Harness, Message, Runner, ScopedFilesystem, ToolCall

def call(identifier, name, **arguments):
    return Message("assistant", tool_calls=[ToolCall(identifier, name, arguments)])

with ScopedFilesystem() as files:  # explicitly in-memory; no host filesystem access
    child = Agent(
        "Reader", "Read the shared note.",
        FakeModel(responses=[call("r", "fs_read", path="notes/task.txt"), "Read it."]),
        harness=Harness(filesystem=files),
    )
    parent = Agent(
        "Planner", "Plan and delegate.",
        FakeModel(responses=[
            call("p", "write_todos", items=[{"id": "read", "text": "Read note"}]),
            call("w", "fs_write", path="notes/task.txt", content="Review requirements."),
            call("c", "reader", request="Read notes/task.txt"),
            call("u", "write_todos", items=[
                {"id": "read", "text": "Read note", "status": "completed"}
            ]), "Done.",
        ]),
        tools=[child.as_tool()], harness=Harness(todo=True, filesystem=files),
    )
    result = Runner().run(parent, "Start")
    assert result.state["nexura.todos"][0]["status"] == "completed"
    assert files.list() == ["notes/task.txt"]
```

The filesystem tools are `fs_read(path)`, `fs_list()`, `fs_write(path, content)` and
`fs_edit(path, old, new)`; edit requires exactly one non-empty literal match. Read/list
and skill/resource reads are `pure`; filesystem writes/edits are conservatively
`non_idempotent`. Todo replacement is `idempotent` by construction, and todo read is
`pure`. Tools have `harness:filesystem`, `harness:todo` or `harness:skills` origins for
policy inspection. `Harness(require_write_approval=True, ...)` gates todo and filesystem
writes. Hook approval votes and tool guardrails use the ordinary runner pipeline;
output policy and streaming checks continue through G1–G4.

For disk storage, supply an existing explicit directory: `ScopedFilesystem(root=path)`.
This backend requires Linux on x86-64 or AArch64, Landlock ABI 3 or later, POSIX `dir_fd`
and `O_NOFOLLOW`. It fails closed if isolation is unavailable; memory mode remains
available. A dedicated worker thread installs its own Landlock filesystem rules before
any disk operation. The caller's permissions and global process settings are unchanged.
Relative paths cannot
contain absolute prefixes, `..`, `.`, empty components, backslashes or colons, and have
at most 16 components/1024 characters. Descriptor traversal refuses symlinks throughout
the root path and workspace, special files and hard links. Disk writes replace files
atomically. Parent identity is re-resolved without symlinks before opens and replacements;
Landlock additionally checks containment in the kernel at each operation, including
both source and destination of replacements. Moving a traversed directory out of the
root cannot redirect those operations outside the directory capability. Close the
object or use a `with` block to stop the worker and release the root descriptor.

Defaults are 64 KiB per UTF-8 file, 1 MiB total and 256 entries including directories;
configure `max_file_bytes`, `max_total_bytes` and `max_entries` explicitly to adjust them.
Inventory/list/write enforce workspace caps; read enforces the individual file cap.
Use one filesystem instance per root so cap checks are serialized. External processes
are not coordinated for quota enforcement and can still change or delete files they
own. Isolation governs operations by this capability, not arbitrary host processes.
In-memory files are not checkpointed; reconstruct the same explicit
disk workspace after restart if shared artifacts must persist. An uncertain filesystem
write pauses on durable recovery under the normal effect contract.

Filesystem and todo decision digests bind the run, SDK-issued approval occurrence,
canonical arguments and the tool's effect scope. A disk scope identifies the normalized
root plus device/inode; a memory
scope identifies its instance. A reviewed mapping from one run cannot authorize an
identical pending write/edit in another run or a later approval occurrence within the
same run, even if the model repeats its call id. The interruption id is retained across
recovery of the same pause, so that occurrence still accepts its own reviewed decision.
The journal's stable effect hash is separate from this occurrence-bound decision.
Resuming
or recovering with a different scope fails before dispatch, even when tool names and
schemas match. Reconstructing the same disk root preserves its scope; recreating the
directory or memory instance does not. Scoped harness actions cannot use `ApproveWith`
to change reviewed arguments: start a fresh action for the changed path/task list.
