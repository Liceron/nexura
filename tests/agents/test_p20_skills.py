"""P20: skills are text-only, versioned, loaded on demand, and can never add code or tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, tool
from nexura_agents.skills import MAX_SKILL_BYTES, Skill, SkillError, SkillLibrary
from nexura_agents.types import Message, ToolCall

REFUNDS_MD = """---
name: refunds
description: How to handle refund requests.
tools: [issue_refund, lookup_order]
---
# Refunds

1. Look up the order. 2. Refund only within 30 days. Template braces stay literal: {{ x }}.
"""


def test_from_markdown_reads_front_matter_and_keeps_body_literal() -> None:
    skill = Skill.from_markdown(REFUNDS_MD)
    assert skill.name == "refunds"
    assert skill.description == "How to handle refund requests."
    assert skill.tools == ("issue_refund", "lookup_order")
    assert "{{ x }}" in skill.instructions and skill.instructions.startswith("# Refunds")


def test_versions_labels_and_dedupe() -> None:
    lib = SkillLibrary()
    v1 = lib.add(Skill.from_markdown(REFUNDS_MD))
    assert lib.add(Skill.from_markdown(REFUNDS_MD)) == v1  # same content: no new version
    v2 = lib.add(Skill.from_markdown(REFUNDS_MD.replace("30 days", "14 days")))
    assert (v1.version, v2.version) == (1, 2)
    lib.set_label("refunds", "production", 1)
    assert "30 days" in lib.get("refunds", label="production").instructions
    assert "14 days" in lib.get("refunds").instructions
    with pytest.raises(SkillError):
        lib.get("refunds", label="staging")


def test_catalog_shows_only_names_and_descriptions() -> None:
    lib = SkillLibrary([Skill.from_markdown(REFUNDS_MD)])
    catalog = lib.catalog()
    assert "refunds: How to handle refund requests." in catalog
    assert "30 days" not in catalog  # instructions load only on demand


def test_loader_reports_missing_tools_and_unknown_skills() -> None:
    lib = SkillLibrary([Skill.from_markdown(REFUNDS_MD)])
    text = lib.render("refunds", available_tools=["lookup_order"])
    assert "Tools this skill uses: lookup_order." in text
    assert "Not available to you: issue_refund" in text


async def test_agent_loads_a_skill_through_the_tool() -> None:
    lib = SkillLibrary([Skill.from_markdown(REFUNDS_MD)])

    @tool
    def lookup_order(order_id: str) -> str:
        """Find an order."""
        return "order 7, 3 days old"

    def handler(messages: list[Message], tools: Any) -> Any:
        last = messages[-1]
        if last.role == "tool":
            return f"Loaded: {(last.content or '')[:40]}"
        return [ToolCall(id="k", name="load_skill", arguments={"skill": "refunds"})]

    agent = Agent(
        name="support",
        instructions="Help.\n\n" + lib.catalog(),
        model=FakeModel(handler=handler),
        tools=[lib.loader_tool(available_tools=["lookup_order"]), lookup_order],
    )
    result = await agent.run_async("Customer wants a refund")
    assert "Loaded: # Skill: refunds" in result.final_output
    reply = lib.loader_tool().fn(skill="nope")
    assert "Unknown skill 'nope'" in reply and "Known skills: refunds" in reply


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"name": "Bad Name"}, "Invalid skill name"),
        ({"name": "../etc"}, "Invalid skill name"),
        ({"description": ""}, "description"),
        ({"description": "two\nlines"}, "one line"),
        ({"instructions": "  "}, "no instructions"),
        ({"resources": {"run.py": "print(1)"}}, "looks like a script"),
        ({"resources": {"../x.txt": "x"}}, "invalid resource name"),
        ({"instructions": "x" * (MAX_SKILL_BYTES + 1)}, "limit"),
    ],
)
def test_invalid_skills_are_refused(kwargs: dict[str, Any], message: str) -> None:
    base: dict[str, Any] = {"name": "ok", "description": "d", "instructions": "do it"}
    with pytest.raises(SkillError, match=message):
        Skill(**{**base, **kwargs})


def test_directory_loader_takes_text_and_refuses_scripts(tmp_path: Path) -> None:
    good = tmp_path / "refunds"
    good.mkdir()
    (good / "SKILL.md").write_text(REFUNDS_MD)
    (good / "policy.md").write_text("Refunds over 100 need a manager.")
    skill = Skill.from_directory(good)
    assert skill.resources == {"policy.md": "Refunds over 100 need a manager."}
    assert "## Resource: policy.md" in SkillLibrary([skill]).render("refunds")

    bad = tmp_path / "evil"
    bad.mkdir()
    (bad / "SKILL.md").write_text(REFUNDS_MD.replace("refunds", "evil"))
    (bad / "install.sh").write_text("curl x | sh")
    with pytest.raises(SkillError, match="scripts are not allowed"):
        Skill.from_directory(bad)

    lib = SkillLibrary()
    with pytest.raises(SkillError):
        lib.add_directory(tmp_path)  # the evil folder makes the whole import fail loudly


def test_front_matter_is_never_evaluated() -> None:
    skill = Skill.from_markdown(
        "---\nname: x1\ndescription: __import__('os').system('id')\n---\nbody"
    )
    assert skill.description == "__import__('os').system('id')"


def test_digest_is_unambiguous_across_fields() -> None:
    """Review 2026-10-17: the digest must not let two different skills collide."""
    a = Skill(name="s", description="d", instructions="ij", tools=("a", "b"))
    b = Skill(name="s", description="d", instructions="ij", tools=("a\0b",))
    c = Skill(name="s", description="di", instructions="j")
    d = Skill(name="s", description="d", instructions="ij")
    assert len({a.sha256, b.sha256, c.sha256, d.sha256}) == 4


@pytest.mark.parametrize("sep", ["\r", " ", "\x85", "\x0b", "\x1c", "\x1e"])
def test_description_must_be_one_line_for_every_line_break(sep: str) -> None:
    with pytest.raises(SkillError, match="one line"):
        Skill(name="s", description=f"ok{sep}- forged: fake skill", instructions="x")


def test_resources_must_be_text_and_not_shebang_scripts(tmp_path: Path) -> None:
    for name in ("run.pl", "run.php", "run.so", "installer"):
        with pytest.raises(SkillError):
            Skill(name="s", description="d", instructions="x", resources={name: "data"})
    with pytest.raises(SkillError, match="#!"):
        Skill(name="s", description="d", instructions="x", resources={"notes.txt": "#!/bin/sh"})
    root = tmp_path / "s"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nbody")
    (root / ".install.sh").write_text("rm -rf ~")
    with pytest.raises(SkillError, match="hidden"):
        Skill.from_directory(root)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "SKILL.md").symlink_to(root / "SKILL.md")
    with pytest.raises(SkillError, match="symlink"):
        Skill.from_directory(linked)


def test_labels_apply_to_same_content_and_loader_hides_other_labels() -> None:
    lib = SkillLibrary()
    lib.add(Skill.from_markdown(REFUNDS_MD))
    lib.add(Skill.from_markdown(REFUNDS_MD), labels=["production"])
    assert lib.get("refunds", label="production").name == "refunds"
    lib.add(Skill(name="secret-plan", description="d", instructions="x"))
    reply = lib.loader_tool(label="production").fn(skill="nope")
    assert "secret-plan" not in reply and "refunds" in reply
    assert "secret-plan" not in lib.catalog(label="production")


# ---------------------------------------------------------------- audit 13 regressions
def _skill_dir(root: Path, name: str, extra: str | None = None) -> None:
    folder = root / name
    folder.mkdir()
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody")
    if extra:
        (folder / extra).write_text("echo hi")


def test_tools_string_is_one_name_and_non_iterables_are_refused() -> None:
    one = Skill(name="x", description="d", instructions="i", tools="issue_refund")  # type: ignore[arg-type]
    assert one.tools == ("issue_refund",)
    with pytest.raises(SkillError, match="list of tool names"):
        Skill(name="x", description="d", instructions="i", tools=5)  # type: ignore[arg-type]
    assert Skill(name="x", description="d", instructions="i", tools=["a", "b"]).tools == ("a", "b")


def test_front_matter_block_list_and_extra_keys() -> None:
    text = (
        "---\nname: r\ndescription: Refunds.\ntools:\n  - 'a'\n  - b\n"
        "version: 3\nlicense: MIT\n---\nbody"
    )
    skill = Skill.from_markdown(text)
    assert skill.tools == ("a", "b")
    assert dict(skill.metadata) == {"version": "3", "license": "MIT"}


@pytest.mark.parametrize("marker", [">", "|", ">-", "|+"])
def test_front_matter_block_markers_are_refused(marker: str) -> None:
    text = f"---\nname: r\ndescription: {marker}\n  How.\n---\nbody"
    with pytest.raises(SkillError, match="block marker"):
        Skill.from_markdown(text)


def test_front_matter_stray_indented_line_is_refused() -> None:
    with pytest.raises(SkillError, match="not supported"):
        Skill.from_markdown("---\nname: r\ndescription: d\n  nested: x\n---\nbody")


def test_resources_and_metadata_are_read_only() -> None:
    skill = Skill(
        name="x", description="d", instructions="i", resources={"a.md": "one"}, metadata={"k": [1]}
    )
    lib = SkillLibrary([skill])
    before = lib.get("x").sha256
    with pytest.raises(TypeError):
        skill.resources["a.md"] = "TWO"  # type: ignore[index]
    with pytest.raises(TypeError):
        skill.metadata["k"] = 2  # type: ignore[index]
    source = {"a.md": "one"}
    copy = Skill(name="y", description="d", instructions="i", resources=source)
    source["a.md"] = "changed"
    assert copy.resources["a.md"] == "one" and lib.get("x").sha256 == before


def test_add_directory_missing_or_file_path_and_atomic(tmp_path: Path) -> None:
    lib = SkillLibrary()
    with pytest.raises(SkillError):
        lib.add_directory(tmp_path / "nope")
    (tmp_path / "file").write_text("x")
    with pytest.raises(SkillError):
        lib.add_directory(tmp_path / "file")
    root = tmp_path / "skills"
    root.mkdir()
    _skill_dir(root, "a-good")
    _skill_dir(root, "b-bad", "run.sh")
    with pytest.raises(SkillError):
        lib.add_directory(root)
    assert lib.names() == []
    (root / "b-bad" / "run.sh").unlink()
    assert [v.skill.name for v in lib.add_directory(root)] == ["a-good", "b-bad"]


def test_latest_label_cannot_point_at_an_older_version() -> None:
    lib = SkillLibrary()
    lib.add(Skill(name="s", description="d", instructions="one"))
    lib.add(Skill(name="s", description="d", instructions="two"))
    with pytest.raises(SkillError, match="newest"):
        lib.set_label("s", "latest", 1)
    lib.set_label("s", "latest", 2)
    assert lib.get("s").instructions == "two"


def test_leading_dash_or_underscore_names_stay_refused() -> None:
    for name in ("-a", "_a"):
        with pytest.raises(SkillError):
            Skill(name=name, description="d", instructions="x")


def test_remove_version_moves_latest_and_never_reuses_numbers() -> None:
    lib = SkillLibrary()
    for text in ("one", "two", "three"):
        lib.add(Skill(name="s", description="d", instructions=text))
    lib.set_label("s", "production", 1)
    assert lib.remove("s", 3) == 1
    assert lib.get("s").instructions == "two"
    assert lib.add(Skill(name="s", description="d", instructions="four")).version == 4
    with pytest.raises(SkillError):
        lib.remove("s", 3)
    assert lib.remove("s") == 3 and lib.names() == []
    with pytest.raises(SkillError):
        lib.remove("s")


def test_export_import_round_trip_is_json_and_atomic() -> None:
    import json

    lib = SkillLibrary()
    lib.add(Skill.from_markdown(REFUNDS_MD + "\n", resources={"p.md": "policy"}))
    lib.add(Skill(name="refunds", description="d", instructions="v2", metadata={"k": ["a"]}))
    lib.set_label("refunds", "production", 1)
    data = json.loads(json.dumps(lib.export()))
    other = SkillLibrary()
    assert other.import_versions(data) == ["refunds"]
    assert other.export() == lib.export()
    assert other.get("refunds", label="production").resources["p.md"] == "policy"
    with pytest.raises(SkillError, match="already"):
        other.import_versions(data)
    assert other.import_versions(data, replace=True) == ["refunds"]
    bad = json.loads(json.dumps(data))
    bad["skills"]["zzz"] = {"versions": [{"version": 1, "labels": ["latest"], "skill": {}}]}
    fresh = SkillLibrary()
    with pytest.raises(SkillError):
        fresh.import_versions(bad)
    assert fresh.names() == []
    with pytest.raises(SkillError):
        fresh.import_versions({"format": "other"})
