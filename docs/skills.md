# Skills

A skill is a reusable bundle of instructions an agent reads only when it needs it: refund
policy, a house style, the steps for a support process. The agent's instructions carry a short
catalog (each skill's name and one-line description). The agent calls `load_skill(name)` to
read the full text, so long instructions cost tokens only when they are used.

Skills are text only. A skill never contains code that runs, and it cannot add tools to an
agent. It may name the tools it expects, and the loader tells the agent which of those it
actually has. Tools a skill relies on keep their own `requires_approval` and guardrails.

## Write a skill

A skill is a `SKILL.md` file with a small front-matter header, plus optional text files beside
it as resources:

```text
refunds/
  SKILL.md
  policy.md
```

```markdown
---
name: refunds
description: How to handle refund requests.
tools: [issue_refund, lookup_order]
---
# Refunds

1. Look up the order.
2. Refund only within 30 days; see policy.md for exceptions.
```

Only `key: value` lines, `[a, b]` lists and `- item` block lists are read from the front
matter, and nothing in it is evaluated. YAML folded and literal blocks (`description: >` or
`|`) are refused with a `SkillError`; write the value on one line. Keys other than `name`,
`description` and `tools` (such as `version` or `license`) are kept in `Skill.metadata` as
text. In Python, `Skill(tools=...)` takes a list of names; a bare string counts as one name.

## Validation rules

- **Names:** 1 to 64 characters: lowercase letters, digits, `-` or `_`, starting with a
  letter or digit (so `-a` and `_a` are refused).
- **Description:** one line of at most 300 characters; any line-break character is refused, so
  a description cannot forge extra catalog entries.
- **Size:** at most 64 KiB in total, counting names, tools and metadata too.
- **Resources:** text files only (`.md`, `.txt`, `.json`, `.yaml`, `.csv`, `.html` and similar;
  the full list is `TEXT_SUFFIXES`). Anything else is refused, and so is text that starts with
  `#!`, text holding control characters other than tab and line breaks, and a resource named
  `SKILL.md`.
- **Immutable once stored:** `Skill.resources` and `Skill.metadata` are read-only, so a stored
  version and its `sha256` cannot change; build a new `Skill` for a new version.
- **Folder contents:** `Skill.from_directory` refuses non-text files, hidden files, symlinks
  (including a symlinked `SKILL.md`) and subdirectories, so a copied skill that ships code is
  noticed rather than silently trimmed.

## Use skills in an agent

```python
from nexura_agents import Agent, FakeModel, Skill, SkillLibrary, tool
from nexura_agents.types import ToolCall

library = SkillLibrary()
library.add(
    Skill.from_markdown(
        "---\nname: refunds\ndescription: How to handle refund requests.\n"
        "tools: [lookup_order]\n---\n# Refunds\nRefund only within 30 days."
    )
)


@tool
def lookup_order(order_id: str) -> str:
    """Find an order."""
    return "order 7, 3 days old"


def handler(messages, tools):
    if messages[-1].role == "tool":
        return "Within 30 days, so I can refund it."
    return [ToolCall(id="k", name="load_skill", arguments={"skill": "refunds"})]


agent = Agent(
    name="support",
    instructions="Help customers.\n\n" + library.catalog(),
    model=FakeModel(handler=handler),
    tools=[library.loader_tool(available_tools=["lookup_order"]), lookup_order],
)
print(agent.run("I want a refund for order 7").final_output)
```

## Versions and labels

`SkillLibrary.add` stores a new version when the content differs from the newest version.
Adding the same content again as the newest version returns that version; adding an older
version's content again stores it as a new version. The `latest` label always moves to the newest version, and
`set_label(name, "latest", n)` raises `SkillError` unless `n` is the newest version.
`set_label(name, "production", version)` pins a label to a version, and
`catalog(label=...)`, `render(..., label=...)` and `loader_tool(label=...)` read that label, so
you can promote a new version without changing the agent.
`SkillLibrary.add_directory(path)` loads every `*/SKILL.md` folder under `path`. One invalid
folder, or a missing `path`, raises `SkillError` and nothing is added.
`remove(name)` deletes a skill, and `remove(name, version)` one version (`latest` moves back to
the newest remaining version; version numbers are not reused).

## In the service

The service keeps a skills store per workspace in its own database (`nexura_service_skills`,
one row per skill holding the library's own export, so versions and labels behave exactly as
above). Skills survive restarts and one workspace never sees another's.

| Route | Does | Role |
| --- | --- | --- |
| `GET /api/v1/skills?label=production` | List skills, only those with a version carrying the label | any member |
| `GET /api/v1/skills/{name}?version=2` or `?label=production` | One version with its text (newest if neither) | any member |
| `GET /api/v1/skills/{name}/versions` | Every version with labels | any member |
| `POST /api/v1/skills` `{"markdown": "...", "resources": {"policy.md": "..."}, "label": "production"}` | Create from `SKILL.md` text and optional text resources; text identical to the newest version is not a new version | editor |
| `PUT /api/v1/skills/{name}/labels/{label}` `{"version": 1}` | Point a label at a version | editor |
| `DELETE /api/v1/skills/{name}` (`?version=2` for one version) | Remove | admin |

Viewers read only. Everything `Skill` refuses is refused here with `400 SKILL_INVALID`:
scripts and non-text resources, `#!` text, bad names, multi-line or long descriptions,
anything over 64 KiB. Request bodies are capped (70,000 characters of Markdown, 50
resources), a workspace holds at most 200 skills, a skill at most 100 versions and 20 labels. Unknown
names, versions and labels answer `404 SKILL_NOT_FOUND`. API keys limited to specific
resources (`workflow:`, `knowledge:`) cannot call these routes (`403 RESOURCE_SCOPE_DENIED`).

### Attach skills to an agent node

An agent node takes two config fields:

```json
{"id": "ask", "handler": "agent", "config": {"skills": ["refunds"], "skills_label": "production"}}
```

`skills` is a list of skill names and `skills_label` (default `production`) picks the version
of each. When the node runs, the engine adds the catalog to each agent's instructions and gives
it the read-only `load_skill` tool, built from the workspace store, so promoting a new
version with `PUT .../labels/production` changes what the saved workflow reads without
editing it. A skill or label that does not exist in the run's workspace fails the node
instead of being skipped; a non-list `skills`, or more than 20 names, is refused when the
workflow is saved.

The node is `AgentCoordinatorModule` (`nexura_runtime.runtime.agents`). The built-in
`nexura.agents.coordinator` module is one, and an agent module you register in the registry
you pass to `create_api_app` works the same way: the service connects each to its own skills
store at start (`module.bind_skill_source(store)` does the same by hand). The built-in module
is shared by every app in the process, so each app gets its own copy bound to its store
(`ModuleRegistry.override_module`); two apps never read each other's skills. A skill name
listed twice on a node is attached once.

## In Studio

The **Skills** page (sidebar) lists the workspace's skills with their labels and a label
filter. Select one to read the instructions, resources and each version's text, create a new
skill or version from a pasted `SKILL.md`, point a label (for example `production`) at the
selected version, or remove the skill (a second click confirms). Viewers can read but the
create and label controls are disabled; only admins see Remove.

Skill text is shown as plain text in preformatted blocks, never as HTML or Markdown, so
markup inside a skill is displayed, not run. In the node inspector, an agent node shows a
skills picker (a checkbox per workspace skill plus the label field) instead of raw JSON
fields. Studio shows text only; skills have no file upload.

## Limits

- **Storage:** in the SDK alone, skills live in memory in the `SkillLibrary`. `library.export()` returns every
  version and label as a JSON-ready dict (text only) and `library.import_versions(data)`
  restores it all or nothing (`replace=True` overwrites existing names). The service stores
  skills for you (see below); keeping the `SKILL.md` files in your repository also works.
- **Not a trust boundary:** a skill's text can still steer the model, so treat skills from
  other people like any other prompt you did not write.
