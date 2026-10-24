# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Skills: reusable, versioned instruction bundles an agent loads only when it needs them.

A skill is plain text: a name, a one-line description, markdown instructions and optional
named reference texts. It never contains code that runs: the library stores and returns
text, and a skill cannot add tools to an agent (it may only *name* the tools it expects,
and the loader reports which of those the agent actually has). This keeps skills inside the
platform's "no arbitrary code in definitions" rule; tools a skill uses keep their own
``requires_approval`` and guardrails.

    library = SkillLibrary()
    library.add(Skill.from_markdown(open("refunds/SKILL.md").read()))
    agent = Agent(
        name="support",
        model=model,
        instructions="Help customers.\\n\\n" + library.catalog(),
        tools=[library.loader_tool(), issue_refund],
    )

The agent sees the catalog (names and descriptions only) and calls ``load_skill(name)`` to
read a skill's full instructions on demand, so long instructions cost tokens only when used.
``SKILL.md`` files use the common front-matter layout::

    ---
    name: refunds
    description: How to handle refund requests.
    tools: [issue_refund, lookup_order]
    ---
    # Refunds
    ...
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nexura_agents.errors import NexuraError, SDKValidationError
from nexura_agents.tools import Tool, tool

MAX_SKILL_BYTES = 64 * 1024
MAX_DESCRIPTION_CHARS = 300
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_RESOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
#: The only resource file types a skill may carry: plain text formats, never code.
TEXT_SUFFIXES = (
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".htm",
)
# Any character Python's str.splitlines() treats as a line break.
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")


EXPORT_FORMAT = "nexura-skills/1"
_BLOCK_MARKER_RE = re.compile(r"[>|][+-]?[0-9]?")


class SkillError(NexuraError):
    """A skill is invalid, unknown, or would break the no-executable-content rule."""


def _freeze(value: Any) -> Any:
    """Deep read-only copy: mappings become proxies, lists become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    """Plain JSON-style copy of a frozen value (dicts and lists)."""
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise SkillError(
            f"Invalid skill name {name!r}: use 1-64 lowercase letters, digits, '-' or '_'."
        )
    return name


@dataclass(frozen=True)
class Skill:
    """One skill version: text only, identified by name and the sha256 of its content."""

    name: str
    description: str
    instructions: str
    tools: tuple[str, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    format: str = "nexura"

    def __post_init__(self) -> None:
        _check_name(self.name)
        if self.format not in ("nexura", "agent-skills"):
            raise SkillError("Skill format must be 'nexura' or 'agent-skills'.")
        standard = self.format == "agent-skills"
        if standard and (
            not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.name) or len(self.name) > 64
        ):
            raise SkillError(
                "Agent Skills names must use lowercase alphanumerics and single hyphens."
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise SkillError(f"Skill {self.name!r} needs a one-line description.")
        description_limit = 1024 if standard else MAX_DESCRIPTION_CHARS
        if len(self.description) > description_limit or any(
            ch in _LINE_BREAKS for ch in self.description
        ):
            raise SkillError(
                f"Skill {self.name!r}: the description must be one line of at most "
                f"{description_limit} characters."
            )
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise SkillError(f"Skill {self.name!r} has no instructions.")
        if isinstance(self.tools, str):  # one name, not a sequence of characters
            object.__setattr__(self, "tools", (self.tools,))
        elif isinstance(self.tools, bytes) or not isinstance(self.tools, Iterable):
            raise SkillError(
                f"Skill {self.name!r}: tools must be a list of tool names, not {self.tools!r}."
            )
        object.__setattr__(self, "tools", tuple(self.tools))
        for name in self.tools:
            if not isinstance(name, str) or not name.strip():
                raise SkillError(f"Skill {self.name!r} lists an empty tool name.")
        resources = dict(self.resources)
        for key, value in resources.items():
            if (
                not isinstance(key, str)
                or not all(
                    _RESOURCE_RE.fullmatch(part) for part in (key.split("/") if standard else [key])
                )
                or any(part in (".", "..") for part in key.split("/"))
            ):
                raise SkillError(f"Skill {self.name!r}: invalid resource name {key!r}.")
            if key.lower() == "skill.md":
                raise SkillError(
                    f"Skill {self.name!r}: a resource cannot be named {key!r} (the skill itself)."
                )
            if not key.lower().endswith(TEXT_SUFFIXES):
                raise SkillError(
                    f"Skill {self.name!r}: resource {key!r} is not a text file (allowed: "
                    f"{', '.join(TEXT_SUFFIXES)}); skills hold text only, so code that "
                    "looks like a script must become a tool instead."
                )
            if not isinstance(value, str):
                raise SkillError(f"Skill {self.name!r}: resource {key!r} must be text.")
            if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
                raise SkillError(
                    f"Skill {self.name!r}: resource {key!r} contains control characters; "
                    "skills hold plain text only."
                )
            if value.lstrip("\ufeff").startswith("#!"):
                raise SkillError(
                    f"Skill {self.name!r}: resource {key!r} starts with '#!' (a script); skills "
                    "hold text only."
                )
        object.__setattr__(self, "resources", MappingProxyType(resources))
        object.__setattr__(self, "metadata", _freeze(dict(self.metadata)))
        if self.size_bytes > MAX_SKILL_BYTES:
            raise SkillError(
                f"Skill {self.name!r} is {self.size_bytes} bytes; the limit is {MAX_SKILL_BYTES}."
            )

    @property
    def size_bytes(self) -> int:
        """Total UTF-8 size of everything the skill holds (text, names, tools, metadata)."""
        return len(self._canonical().encode("utf-8"))

    def _canonical(self) -> str:
        """Unambiguous serialisation used for the size limit and the content digest."""
        return json.dumps(
            {
                "name": self.name,
                "description": self.description,
                "instructions": self.instructions,
                "tools": list(self.tools),
                "resources": dict(sorted(self.resources.items())),
                "metadata": _thaw(self.metadata),
                **({"format": self.format} if self.format != "nexura" else {}),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    @property
    def sha256(self) -> str:
        """Content digest; two skills with the same content have the same digest."""
        return hashlib.sha256(self._canonical().encode("utf-8")).hexdigest()

    @classmethod
    def from_markdown(
        cls,
        text: str,
        *,
        resources: Mapping[str, str] | None = None,
        name: str | None = None,
        format: str = "nexura",
    ) -> Skill:
        """Parse a ``SKILL.md``: ``---`` front matter (name, description, tools) then the body.

        Only ``key: value`` lines, ``[a, b]`` lists and ``- item`` block lists are read from
        the front matter; nothing in it is evaluated. YAML folded or literal blocks (``>`` and
        ``|``) are refused. Keys other than name, description and tools are kept in
        ``metadata`` as text (or a tuple of text for block lists).
        """
        if not isinstance(text, str):
            raise SkillError("SKILL.md content must be text.")
        if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            raise SkillError(f"SKILL.md is larger than {MAX_SKILL_BYTES} bytes.")
        if format == "agent-skills":
            from nexura_agents._skill_frontmatter import parse_frontmatter

            try:
                fields, instructions = parse_frontmatter(text)
            except ValueError as exc:
                raise SkillError(f"Invalid Agent Skills front matter: {exc}") from exc
            compatibility = fields.get("compatibility")
            if compatibility is not None and not 1 <= len(compatibility) <= 500:
                raise SkillError("compatibility must contain 1-500 characters.")
            return cls(
                name=name or fields.get("name", ""),
                description=" ".join(fields.get("description", "").splitlines()),
                instructions=instructions,
                resources=resources or {},
                metadata={k: v for k, v in fields.items() if k not in ("name", "description")},
                format=format,
            )
        if format != "nexura":
            raise SkillError("Skill format must be 'nexura' or 'agent-skills'.")
        front: dict[str, str | tuple[str, ...]] = {}
        body = text
        lines = text.lstrip("\ufeff").splitlines()
        if lines and lines[0].strip() == "---":
            current: str | None = None
            items: list[str] = []
            for index, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    body = "\n".join(lines[index + 1 :])
                    break
                if line[:1] in (" ", "\t") and line.strip():
                    item = line.strip()
                    if (
                        current is None
                        or front.get(current) not in ("", tuple(items))
                        or not item.startswith("-")
                    ):
                        raise SkillError(
                            f"SKILL.md front matter line {line.strip()!r} is not supported: "
                            "use single-line 'key: value', '[a, b]' or '- item' lists."
                        )
                    items.append(item[1:].strip().strip("'\""))
                    front[current] = tuple(items)
                    continue
                key, sep, value = line.partition(":")
                if sep and key.strip():
                    current = key.strip().lower()
                    items = []
                    value = value.strip()
                    if _BLOCK_MARKER_RE.fullmatch(value):
                        raise SkillError(
                            f"SKILL.md front matter key {current!r} uses a YAML block marker "
                            f"({value!r}), which is not supported; write the value on one line."
                        )
                    front[current] = value
            else:
                raise SkillError("SKILL.md front matter has no closing '---'.")

        def text_of(key: str) -> str:
            value = front.get(key, "")
            if not isinstance(value, str):
                raise SkillError(f"SKILL.md front matter {key!r} must be a single line of text.")
            return value.strip("'\"")

        tools_raw = front.get("tools", "")
        if isinstance(tools_raw, tuple):
            tools = tools_raw
        else:
            tools = tuple(
                t.strip().strip("'\"") for t in tools_raw.strip("[]").split(",") if t.strip()
            )
        return cls(
            name=name or text_of("name"),
            description=text_of("description"),
            instructions=body.strip(),
            tools=tools,
            resources=dict(resources or {}),
            metadata={k: v for k, v in front.items() if k not in ("name", "description", "tools")},
        )

    @classmethod
    def from_directory(cls, path: str | Path, *, format: str = "nexura") -> Skill:
        """Load ``path/SKILL.md`` plus text files beside it as resources (no scripts).

        Files with a script suffix are refused, not skipped, so a copied skill that ships
        code is noticed. Subdirectories and files over the size limit are refused too.
        """
        root = Path(path)
        if format == "agent-skills":
            from nexura_agents.filesystem import ScopedFilesystem

            # Snapshot bounded text only. Scripts/binary assets remain inaccessible;
            # no metadata value can install a tool or grant execution permission.
            try:
                with ScopedFilesystem(root, max_total_bytes=MAX_SKILL_BYTES * 4) as files:
                    standard_resources = {
                        p: files.read(p)
                        for p in files.list()
                        if p != "SKILL.md"
                        and not p.startswith("scripts/")
                        and p.lower().endswith(TEXT_SUFFIXES)
                    }
                    skill = cls.from_markdown(
                        files.read("SKILL.md"), resources=standard_resources, format=format
                    )
            except (OSError, UnicodeError, SDKValidationError) as exc:
                raise SkillError(f"Cannot load skill directory: {exc}") from exc
            if skill.name != root.name:
                raise SkillError("Agent Skills name must match its directory name.")
            return skill
        if format != "nexura":
            raise SkillError("Skill format must be 'nexura' or 'agent-skills'.")
        skill_md = root / "SKILL.md"
        if skill_md.is_symlink() or not skill_md.is_file():
            raise SkillError(f"{root} has no SKILL.md (a symlinked SKILL.md is refused).")
        resources: dict[str, str] = {}
        for entry in sorted(root.iterdir()):
            if entry.name == "SKILL.md":
                continue
            if entry.name.startswith("."):
                raise SkillError(f"{entry}: hidden files are not allowed in a skill; remove it.")
            if entry.is_symlink() or not entry.is_file():
                raise SkillError(f"{entry}: skills may contain only plain text files.")
            if not entry.name.lower().endswith(TEXT_SUFFIXES):
                raise SkillError(
                    f"{entry}: scripts are not allowed in a skill; only text files "
                    f"({', '.join(TEXT_SUFFIXES)}) are, and code must become a tool."
                )
            if entry.stat().st_size > MAX_SKILL_BYTES:
                raise SkillError(f"{entry} is larger than {MAX_SKILL_BYTES} bytes.")
            try:
                resources[entry.name] = entry.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise SkillError(f"{entry} is not UTF-8 text.") from None
        return cls.from_markdown(skill_md.read_text(encoding="utf-8"), resources=resources)


@dataclass(frozen=True)
class SkillVersion:
    """A stored skill with its version number and labels."""

    skill: Skill
    version: int
    labels: tuple[str, ...] = ()


class SkillLibrary:
    """Versioned skills by name, with labels such as ``production`` (thread-safe)."""

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._versions: dict[str, list[SkillVersion]] = {}
        self._next: dict[str, int] = {}  # next version number per name (never reused)
        self._lock = threading.RLock()
        for skill in skills:
            self.add(skill)

    @classmethod
    def from_directory(cls, path: str | Path) -> SkillLibrary:
        """Load a bounded directory of Agent Skills, in sorted order, without execution.

        Each immediate subdirectory contains SKILL.md. Unrelated files are ignored;
        symlinks are refused. No home-directory discovery or network access occurs.
        """
        root = Path(path)
        if root.is_symlink():
            raise SkillError("A skill library root cannot be a symlink.")
        entries: list[Path] = []
        for entry in root.iterdir():
            if len(entries) >= 256:
                raise SkillError("A skill library may contain at most 256 entries.")
            if entry.is_symlink():
                raise SkillError("A skill library cannot contain symlinks.")
            entries.append(entry)
        return cls(
            Skill.from_directory(entry, format="agent-skills")
            for entry in sorted(entries)
            if entry.is_dir() and (entry / "SKILL.md").exists()
        )

    def add(self, skill: Skill, *, labels: Sequence[str] = ()) -> SkillVersion:
        """Store a new version (same content as the newest returns it); ``latest`` moves to it."""
        wanted = list(dict.fromkeys([*labels, "latest"]))
        for label in wanted:
            _check_name(label)
        with self._lock:
            existing = self._versions.get(skill.name, [])
            if existing and existing[-1].skill.sha256 == skill.sha256:
                if set(wanted) <= set(existing[-1].labels):
                    return existing[-1]
                # Same content, new labels: move them onto the existing newest version.
                newest = existing[-1]
                kept = [
                    SkillVersion(v.skill, v.version, tuple(x for x in v.labels if x not in wanted))
                    for v in existing[:-1]
                ]
                updated = SkillVersion(
                    newest.skill, newest.version, tuple(dict.fromkeys([*newest.labels, *wanted]))
                )
                self._versions[skill.name] = [*kept, updated]
                return updated
            kept = [
                SkillVersion(v.skill, v.version, tuple(x for x in v.labels if x not in wanted))
                for v in existing
            ]
            number = max(self._next.get(skill.name, 1), len(existing) + 1)
            created = SkillVersion(skill, number, tuple(wanted))
            self._versions[skill.name] = [*kept, created]
            self._next[skill.name] = number + 1
            return created

    def next_version(self, name: str) -> int:
        """The number the next new version of ``name`` gets (also after it was removed)."""
        with self._lock:
            return max(self._next.get(name, 1), len(self._versions.get(name, [])) + 1)

    def continue_numbering(self, name: str, next_version: int) -> None:
        """Start ``name``'s next version at ``next_version`` or later (never lower).

        For stores that keep a removed skill's counter elsewhere, so a re-created skill does
        not reuse the numbers of the versions it had before.
        """
        if not isinstance(next_version, int) or isinstance(next_version, bool) or next_version < 1:
            raise SkillError("next_version must be a positive integer.")
        with self._lock:
            self._next[name] = max(self._next.get(name, 1), next_version)

    def add_directory(self, path: str | Path) -> list[SkillVersion]:
        """Add every ``*/SKILL.md`` folder directly under ``path``, all or nothing.

        Every folder is loaded and validated first; if any is invalid, or ``path`` is not a
        readable directory, ``SkillError`` is raised and the library is unchanged.
        """
        root = Path(path)
        try:
            children = sorted(root.iterdir())
            folders = [c for c in children if c.is_dir() and (c / "SKILL.md").is_file()]
        except OSError as exc:
            raise SkillError(
                f"Cannot read skills directory {root}: {exc.strerror or exc}."
            ) from None
        loaded = [Skill.from_directory(child) for child in folders]
        with self._lock:
            return [self.add(skill) for skill in loaded]

    def remove(self, name: str, version: int | None = None) -> int:
        """Delete every version of ``name``, or only ``version``; returns how many were removed.

        Removing the newest version moves ``latest`` to the one before it. Version numbers
        are not reused. Other labels on a removed version are dropped. Raises ``SkillError``
        for an unknown skill or version.
        """
        with self._lock:
            found = self.versions(name)
            if version is None:
                del self._versions[name]
                return len(found)
            if not any(v.version == version for v in found):
                raise SkillError(f"Skill {name!r} has no version {version}.")
            rest = [v for v in found if v.version != version]
            if not rest:
                del self._versions[name]
                return 1
            if not any("latest" in v.labels for v in rest):
                last = rest[-1]
                rest[-1] = SkillVersion(last.skill, last.version, (*last.labels, "latest"))
            self._versions[name] = rest
            return 1

    def export(self) -> dict[str, Any]:
        """All skills, versions and labels as a JSON-ready dict (text only), for persistence.

        Save it with ``json.dumps`` and restore it with :meth:`import_versions`.
        """
        with self._lock:
            return {
                "format": EXPORT_FORMAT,
                "skills": {
                    name: {
                        "next_version": self._next.get(name, len(versions) + 1),
                        "versions": [
                            {
                                "version": v.version,
                                "labels": list(v.labels),
                                "skill": {
                                    "name": v.skill.name,
                                    "description": v.skill.description,
                                    "instructions": v.skill.instructions,
                                    "tools": list(v.skill.tools),
                                    "resources": dict(v.skill.resources),
                                    "metadata": _thaw(v.skill.metadata),
                                    **(
                                        {"format": v.skill.format}
                                        if v.skill.format != "nexura"
                                        else {}
                                    ),
                                },
                            }
                            for v in versions
                        ],
                    }
                    for name, versions in sorted(self._versions.items())
                },
            }

    def import_versions(self, data: Mapping[str, Any], *, replace: bool = False) -> list[str]:
        """Restore skills from :meth:`export` output, all or nothing; returns the names added.

        Everything is validated first (skill rules, labels, version numbers). A name already in
        the library raises ``SkillError`` unless ``replace=True``, which overwrites it.
        """
        parsed: dict[str, tuple[list[SkillVersion], int]] = {}
        try:
            if data.get("format") != EXPORT_FORMAT:
                raise SkillError(f"Unsupported skills export format {data.get('format')!r}.")
            for name, entry in dict(data["skills"]).items():
                _check_name(name)
                versions: list[SkillVersion] = []
                seen_labels: set[str] = set()
                for raw in entry["versions"]:
                    spec = raw["skill"]
                    skill = Skill(
                        name=spec["name"],
                        description=spec["description"],
                        instructions=spec["instructions"],
                        tools=spec.get("tools", ()),
                        resources=spec.get("resources", {}),
                        metadata=spec.get("metadata", {}),
                        format=spec.get("format", "nexura"),
                    )
                    number = raw["version"]
                    labels = tuple(raw.get("labels", ()))
                    if skill.name != name:
                        raise SkillError(f"Export entry {name!r} holds skill {skill.name!r}.")
                    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                        raise SkillError(f"Skill {name!r}: invalid version {number!r}.")
                    if versions and number <= versions[-1].version:
                        raise SkillError(f"Skill {name!r}: versions must be strictly ascending.")
                    for label in labels:
                        _check_name(label)
                        if label in seen_labels:
                            raise SkillError(f"Skill {name!r}: label {label!r} is on two versions.")
                        seen_labels.add(label)
                    versions.append(SkillVersion(skill, number, labels))
                if not versions:
                    raise SkillError(f"Skill {name!r} has no versions.")
                if "latest" not in seen_labels or "latest" not in versions[-1].labels:
                    raise SkillError(f"Skill {name!r}: 'latest' must be on the newest version.")
                following = entry.get("next_version", versions[-1].version + 1)
                if (
                    isinstance(following, bool)
                    or not isinstance(following, int)
                    or following <= versions[-1].version
                ):
                    raise SkillError(f"Skill {name!r}: invalid next_version {following!r}.")
                parsed[name] = (versions, following)
        except (KeyError, TypeError, AttributeError) as exc:
            raise SkillError(f"Malformed skills export: {exc!r}.") from None
        with self._lock:
            clash = sorted(n for n in parsed if n in self._versions)
            if clash and not replace:
                raise SkillError(f"Skills already in the library: {', '.join(clash)}.")
            for name, (versions, following) in parsed.items():
                self._versions[name] = versions
                self._next[name] = following
        return sorted(parsed)

    def names(self) -> list[str]:
        """Names of all stored skills."""
        with self._lock:
            return sorted(self._versions)

    def names_with_label(self, label: str) -> list[str]:
        """Names of skills that have a version carrying ``label``."""
        with self._lock:
            return sorted(
                n for n, vs in self._versions.items() if any(label in v.labels for v in vs)
            )

    def versions(self, name: str) -> list[SkillVersion]:
        """All versions of a skill, oldest first."""
        with self._lock:
            found = self._versions.get(_check_name(name))
            if not found:
                raise SkillError(f"Unknown skill {name!r}.")
            return list(found)

    def get(self, name: str, *, label: str = "latest", version: int | None = None) -> Skill:
        """The skill at ``version``, or the one carrying ``label``."""
        found = self.versions(name)
        if version is not None:
            for item in found:
                if item.version == version:
                    return item.skill
            raise SkillError(f"Skill {name!r} has no version {version}.")
        for item in found:
            if label in item.labels:
                return item.skill
        raise SkillError(f"Skill {name!r} has no label {label!r}.")

    def set_label(self, name: str, label: str, version: int) -> None:
        """Point ``label`` at ``version``; other versions lose it.

        ``latest`` is reserved for the newest version, so pointing it elsewhere is refused.
        """
        _check_name(label)
        with self._lock:
            found = self.versions(name)
            if not any(v.version == version for v in found):
                raise SkillError(f"Skill {name!r} has no version {version}.")
            if label == "latest" and version != found[-1].version:
                raise SkillError(
                    f"Skill {name!r}: 'latest' always points at the newest version "
                    f"({found[-1].version}); use another label to pin version {version}."
                )
            self._versions[name] = [
                SkillVersion(
                    v.skill,
                    v.version,
                    (
                        *(x for x in v.labels if x != label),
                        *((label,) if v.version == version else ()),
                    ),
                )
                for v in found
            ]

    def catalog(self, *, label: str = "latest", heading: str = "Available skills") -> str:
        """Names and one-line descriptions, for an agent's instructions."""
        lines = [f"{heading} (call load_skill(name) to read one before using it):"]
        for name in self.names_with_label(label):
            try:
                skill = self.get(name, label=label)
            except SkillError:
                continue
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)

    def render(
        self,
        name: str,
        *,
        label: str = "latest",
        available_tools: Iterable[str] = (),
        progressive: bool = False,
    ) -> str:
        """The text ``load_skill`` returns: instructions, resources and tool availability."""
        skill = self.get(name, label=label)
        have = set(available_tools)
        parts = [f"# Skill: {skill.name}", skill.instructions]
        if skill.tools:
            ok = [t for t in skill.tools if t in have]
            missing = [t for t in skill.tools if t not in have]
            line = f"Tools this skill uses: {', '.join(ok) or 'none available'}."
            if missing:
                line += (
                    f" Not available to you: {', '.join(missing)} (do not pretend to call them)."
                )
            parts.append(line)
        for key, value in skill.resources.items():
            parts.append(
                f"Resource available via read_skill_resource: {key}"
                if progressive
                else f"## Resource: {key}\n{value}"
            )
        return "\n\n".join(parts)

    def loader_tool(
        self,
        *,
        label: str = "latest",
        available_tools: Iterable[str] = (),
        name: str = "load_skill",
        progressive: bool = False,
    ) -> Tool:
        """A read-only ``load_skill(name)`` tool returning a skill's text (never runs anything)."""
        have = tuple(available_tools)

        @tool(
            name=name,
            description="Load the full instructions of a named skill.",
            effect="pure" if progressive else None,
        )
        def load_skill(skill: str) -> str:
            try:
                return self.render(
                    skill, label=label, available_tools=have, progressive=progressive
                )
            except SkillError:
                known = ", ".join(self.names_with_label(label)) or "none"
                return f"Unknown skill {skill!r}. Known skills: {known}."

        if progressive:
            load_skill.origin = "harness:skills"
        return load_skill

    def resource_tool(self, *, label: str = "latest") -> Tool:
        """Read one snapshot resource on demand. No filesystem access or execution."""

        @tool(effect="pure")
        def read_skill_resource(skill: str, path: str) -> str:
            """Read a named text resource listed by load_skill."""
            found = self.get(skill, label=label)
            if path not in found.resources:
                raise SkillError(f"Unknown skill resource {path!r}.")
            return found.resources[path]

        read_skill_resource.origin = "harness:skills"
        return read_skill_resource


def validate_skills(skills: Iterable[Skill]) -> list[Skill]:
    """Check that names are unique; raise ``SDKValidationError`` otherwise."""
    seen: set[str] = set()
    out = []
    for skill in skills:
        if skill.name in seen:
            raise SDKValidationError(f"Duplicate skill name {skill.name!r}.")
        seen.add(skill.name)
        out.append(skill)
    return out


__all__ = [
    "MAX_SKILL_BYTES",
    "Skill",
    "SkillError",
    "SkillLibrary",
    "SkillVersion",
    "TEXT_SUFFIXES",
    "validate_skills",
]
