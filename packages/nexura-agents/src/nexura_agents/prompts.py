# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Versioned prompts managed outside code, plugged straight into agents.

    registry = PromptRegistry(FilePromptStore("prompts"))
    registry.register("support", "You help {{ user.name }} with {{ product }}.")
    registry.set_label("support", "production", 1)
    agent = Agent(name="support", model=model, instructions=registry.instructions("support", ...))

Templates use ``{{ variable }}`` only. Dotted names such as ``{{ user.name }}`` look up
keys of mappings; there is no attribute access, no calls, no filters and no expressions,
so a template (even one edited by a non-engineer) cannot reach into Python objects.
Substituted values are inserted verbatim in a single pass: braces inside a value are never
interpreted again.

Resolution happens on every run, so moving a label such as ``production`` takes effect on
the next run of an existing agent. The resolved prompt is recorded on ``RunContext.prompt``,
on ``RunResult.prompt`` and, when tracing is instrumented, on the run span as
``gen_ai.prompt.name`` and ``gen_ai.prompt.version`` (the OpenTelemetry GenAI
conventions' prompt attributes).
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import functools
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexura_agents.errors import NexuraError, SDKValidationError

if TYPE_CHECKING:
    from nexura_agents.run_context import RunContext

__all__ = [
    "FilePromptStore",
    "InMemoryPromptStore",
    "PromptError",
    "PromptNotFoundError",
    "PromptRegistry",
    "PromptStore",
    "PromptSyntaxError",
    "PromptVariableError",
    "PromptVersion",
]

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_SEGMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PromptError(NexuraError):
    """Base class for prompt registry errors."""


class PromptSyntaxError(PromptError):
    """A template has an unbalanced or invalid ``{{ ... }}`` tag."""


class PromptVariableError(PromptError):
    """Rendering failed because variables are missing or cannot be looked up."""

    def __init__(self, message: str, *, missing: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing = missing


class PromptNotFoundError(PromptError, LookupError):
    """The prompt name, version or label does not exist."""


def _check_name(value: str, what: str) -> str:
    if (
        not isinstance(value, str)
        or not _NAME_RE.fullmatch(value)
        or value.startswith(".")
        or ".." in value
    ):
        raise SDKValidationError(
            f"Invalid prompt {what} {value!r}: use 1-100 characters from A-Za-z0-9_.- "
            "and do not start with '.' or contain '..'."
        )
    return value


_MAX_TEMPLATE_CHARS = 200_000


def _scan_tags(template: str) -> list[str | tuple[str, ...]]:
    """Linear scan (``str.find`` only) into literals and variable paths."""
    parts: list[str | tuple[str, ...]] = []
    buf: list[str] = []
    pos = 0
    n = len(template)
    opener = closer = -2  # cached next positions; -1 means none left
    while pos < n:
        if opener != -1 and opener < pos:
            opener = template.find("{{", pos)
        if closer != -1 and closer < pos:
            closer = template.find("}}", pos)
        if opener == -1 and closer == -1:
            break
        is_open = closer == -1 or (opener != -1 and opener < closer)
        at = opener if is_open else closer
        token = "{{" if is_open else "}}"
        if at > pos and template[at - 1] == "\\":
            buf.append(template[pos : at - 1] + token)
            pos = at + 2
            continue
        if not is_open:
            raise PromptSyntaxError(
                f"Unbalanced '}}}}' near offset {at}; write '\\}}}}' for a literal."
            )
        end = template.find("}}", at + 2)
        if end == -1:
            raise PromptSyntaxError(
                f"Unclosed '{{{{' near offset {at}; write '\\{{{{' for a literal."
            )
        raw = template[at + 2 : end]
        expr = raw.strip()
        segments = expr.split(".")
        if not expr or not all(_SEGMENT_RE.fullmatch(s) for s in segments):
            raise PromptSyntaxError(
                f"Invalid tag '{{{{{raw[:80]}}}}}': only {{{{ name }}}} or "
                "{{ name.key }} are allowed (no calls, filters or expressions)."
            )
        if any(s.startswith("__") for s in segments):
            raise PromptSyntaxError(f"Invalid tag '{expr}': names may not start with '__'.")
        buf.append(template[pos:at])
        literal = "".join(buf)
        buf = []
        if literal:
            parts.append(literal)
        parts.append(tuple(segments))
        pos = end + 2
    buf.append(template[pos:])
    literal = "".join(buf)
    if literal:
        parts.append(literal)
    return parts


@functools.lru_cache(maxsize=256)
def _parse_cached(template: str) -> tuple[str | tuple[str, ...], ...]:
    return tuple(_scan_tags(template))


def _parse(template: str) -> tuple[str | tuple[str, ...], ...]:
    """Split into literal strings and variable paths; raise ``PromptSyntaxError``.

    ``\\{{`` and ``\\}}`` write literal braces. Results are cached per template.
    """
    if not isinstance(template, str):
        raise PromptSyntaxError("Prompt template must be a string.")
    if len(template) > _MAX_TEMPLATE_CHARS:
        raise PromptSyntaxError(f"Prompt template exceeds {_MAX_TEMPLATE_CHARS} characters.")
    return _parse_cached(template)


def _sha(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptVersion:
    """One immutable version of a named prompt template."""

    name: str
    version: int
    template: str
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    sha256: str = ""

    def __post_init__(self) -> None:
        _check_name(self.name, "name")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise SDKValidationError("Prompt version must be an integer >= 1.")
        _parse(self.template)
        digest = _sha(self.template)
        if not self.sha256:
            object.__setattr__(self, "sha256", digest)
        elif self.sha256 != digest:
            raise PromptError(
                f"Prompt {self.name!r} v{self.version} does not match its recorded sha256."
            )
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(UTC).isoformat())
        object.__setattr__(self, "labels", tuple(self.labels))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def variables(self) -> tuple[str, ...]:
        """Required variable names (dotted paths included), in order of first use."""
        seen: dict[str, None] = {}
        for part in _parse(self.template):
            if isinstance(part, tuple):
                seen.setdefault(".".join(part), None)
        return tuple(seen)

    def render(self, variables: Mapping[str, Any] | None = None) -> str:
        """Substitute variables in one pass; extra variables are ignored."""
        values: Mapping[str, Any] = variables or {}
        out: list[str] = []
        missing: list[str] = []
        for part in _parse(self.template):
            if isinstance(part, str):
                out.append(part)
                continue
            current: Any = values
            ok = True
            for segment in part:
                if isinstance(current, Mapping) and segment in current:
                    current = current[segment]
                else:
                    ok = False
                    break
            if not ok:
                path = ".".join(part)
                if path not in missing:
                    missing.append(path)
            else:
                out.append(str(current))
        if missing:
            raise PromptVariableError(
                f"Prompt {self.name!r} v{self.version} is missing variables: "
                f"{', '.join(missing)}. Dotted names only look up mapping keys.",
                missing=tuple(missing),
            )
        return "".join(out)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict."""
        return {
            "name": self.name,
            "version": self.version,
            "template": self.template,
            "labels": list(self.labels),
            "metadata": copy.deepcopy(self.metadata),
            "created_at": self.created_at,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PromptVersion:
        """Rebuild from ``to_dict`` output; verifies the recorded sha256."""
        try:
            return cls(
                name=data["name"],
                version=data["version"],
                template=data["template"],
                labels=tuple(data.get("labels", ())),
                metadata=dict(data.get("metadata") or {}),
                created_at=data.get("created_at", ""),
                sha256=data.get("sha256", ""),
            )
        except KeyError as exc:
            raise PromptError(f"Prompt record is missing field {exc}.") from exc


@runtime_checkable
class PromptStore(Protocol):
    """Persistence for prompt versions. ``lock`` serialises read-modify-write cycles."""

    lock: AbstractContextManager[Any]

    def names(self) -> list[str]: ...

    def load(self, name: str) -> list[PromptVersion]: ...

    def save(self, name: str, versions: list[PromptVersion]) -> None: ...


class InMemoryPromptStore:
    """Process-local store, for tests and prototypes."""

    def __init__(self) -> None:
        self.lock: AbstractContextManager[Any] = threading.RLock()
        self._data: dict[str, list[PromptVersion]] = {}

    def names(self) -> list[str]:
        """Sorted prompt names in this store."""
        with self.lock:
            return sorted(self._data)

    def load(self, name: str) -> list[PromptVersion]:
        """All versions of ``name``, oldest first; empty if unknown."""
        with self.lock:
            return list(self._data.get(name, []))

    def save(self, name: str, versions: list[PromptVersion]) -> None:
        """Replace all versions of ``name``."""
        with self.lock:
            self._data[name] = list(versions)


try:  # POSIX advisory file locks; elsewhere only the in-process lock applies
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_DIR_LOCKS: dict[str, _DirLock] = {}
_DIR_LOCKS_GUARD = threading.Lock()


class _DirLock:
    """Reentrant lock shared by every store on one directory, plus an OS file lock."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    def __enter__(self) -> _DirLock:
        self._rlock.acquire()
        try:
            if self._depth == 0 and fcntl is not None:
                self._directory.mkdir(parents=True, exist_ok=True)
                fd = os.open(self._directory / ".prompts.lock", os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fd, self._fd = self._fd, None
            with contextlib.suppress(OSError):
                os.close(fd)  # closing releases the flock
        self._rlock.release()


def _dir_lock(directory: Path) -> _DirLock:
    key = str(directory.resolve())
    with _DIR_LOCKS_GUARD:
        lock = _DIR_LOCKS.get(key)
        if lock is None:
            lock = _DIR_LOCKS[key] = _DirLock(directory)
        return lock


class FilePromptStore:
    """One JSON file per prompt name (``<directory>/<name>.json``) holding all versions.

    Writes are atomic (temp file in the same directory, then ``os.replace``). The lock is shared by
    every store on the same directory in this process and adds an OS file lock
    (``.prompts.lock``, POSIX) so registrations from several processes serialise too.
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)
        self.lock = _dir_lock(self.directory)

    def _path(self, name: str) -> Path:
        return self.directory / f"{_check_name(name, 'name')}.json"

    def names(self) -> list[str]:
        """Sorted prompt names in this store."""
        if not self.directory.is_dir():
            return []
        found = []
        for path in self.directory.glob("*.json"):
            if _NAME_RE.fullmatch(path.stem) and not path.stem.startswith("."):
                found.append(path.stem)
        return sorted(found)

    def load(self, name: str) -> list[PromptVersion]:
        """All versions of ``name``, oldest first; empty if unknown."""
        path = self._path(name)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            data = json.loads(raw)
            versions = [PromptVersion.from_dict(item) for item in data["versions"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise PromptError(f"Prompt file {path.name} is corrupt: {exc}") from exc
        return sorted(versions, key=lambda v: v.version)

    def save(self, name: str, versions: list[PromptVersion]) -> None:
        """Replace all versions of ``name``."""
        path = self._path(name)
        payload = json.dumps(
            {"name": name, "versions": [v.to_dict() for v in versions]},
            ensure_ascii=False,
            indent=2,
        )
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise


class PromptRegistry:
    """Register, version, label and resolve prompts."""

    def __init__(self, store: PromptStore | None = None) -> None:
        self.store: PromptStore = store if store is not None else InMemoryPromptStore()
        self._lock: AbstractContextManager[Any] = (
            getattr(self.store, "lock", None) or threading.RLock()
        )

    def register(
        self,
        name: str,
        template: str,
        *,
        labels: tuple[str, ...] | list[str] = ("latest",),
        metadata: Mapping[str, Any] | None = None,
    ) -> PromptVersion:
        """Add a new version. The same template as the newest version returns that version.

        The ``latest`` label always moves to the new version; other labels listed here move
        to it as well (a label points at one version per prompt). When the template is a
        duplicate, nothing changes and ``labels`` and ``metadata`` are ignored.
        """
        _check_name(name, "name")
        _parse(template)
        wanted = [_check_name(label, "label") for label in labels]
        if "latest" not in wanted:
            wanted.append("latest")
        with self._lock:
            existing = self.store.load(name)
            if existing and existing[-1].template == template:
                return existing[-1]
            number = existing[-1].version + 1 if existing else 1
            moved = [
                dataclasses.replace(v, labels=tuple(x for x in v.labels if x not in wanted))
                for v in existing
            ]
            created = PromptVersion(
                name=name,
                version=number,
                template=template,
                labels=tuple(dict.fromkeys(wanted)),
                metadata=dict(metadata or {}),
            )
            self.store.save(name, [*moved, created])
            return created

    def versions(self, name: str) -> list[PromptVersion]:
        """All versions of a prompt, oldest first."""
        found = self.store.load(_check_name(name, "name"))
        if not found:
            raise PromptNotFoundError(f"Unknown prompt {name!r}.")
        return found

    def names(self) -> list[str]:
        """Names of all registered prompts."""
        return self.store.names()

    def get(
        self, name: str, *, version: int | None = None, label: str | None = None
    ) -> PromptVersion:
        """Fetch by ``version`` or ``label`` (default label ``latest``)."""
        if version is not None and label is not None:
            raise SDKValidationError("Pass version or label, not both.")
        found = self.versions(name)
        if version is not None:
            for item in found:
                if item.version == version:
                    return item
            raise PromptNotFoundError(f"Prompt {name!r} has no version {version}.")
        wanted = label if label is not None else "latest"
        for item in found:
            if wanted in item.labels:
                return item
        raise PromptNotFoundError(f"Prompt {name!r} has no label {wanted!r}.")

    def set_label(self, name: str, label: str, version: int) -> PromptVersion:
        """Point ``label`` (for example ``production``) at ``version``; other versions lose it."""
        _check_name(label, "label")
        with self._lock:
            found = self.versions(name)
            if not any(v.version == version for v in found):
                raise PromptNotFoundError(f"Prompt {name!r} has no version {version}.")
            updated = []
            for item in found:
                labels = tuple(x for x in item.labels if x != label)
                if item.version == version:
                    labels = (*labels, label)
                updated.append(dataclasses.replace(item, labels=labels))
            self.store.save(name, updated)
            return next(v for v in updated if v.version == version)

    def instructions(
        self,
        name: str,
        *,
        label: str | None = None,
        version: int | None = None,
        variables: Mapping[str, Any] | Callable[[RunContext[Any]], Mapping[str, Any]] | None = None,
        pin: bool = False,
    ) -> Callable[[RunContext[Any]], str]:
        """A callable for ``Agent(instructions=...)``.

        Without ``version`` it resolves ``label`` (default ``production``) on every run, so
        promotion applies to the next run. ``pin=True`` resolves once now instead.
        """
        if version is not None and label is not None:
            raise SDKValidationError("Pass version or label, not both.")
        effective_label = None if version is not None else (label or "production")
        pinned = self.get(name, version=version, label=effective_label) if pin else None

        def render(ctx: RunContext[Any]) -> str:
            prompt = pinned or self.get(name, version=version, label=effective_label)
            values = variables(ctx) if callable(variables) else variables
            text = prompt.render(values)
            ctx.prompt = {"name": prompt.name, "version": prompt.version, "sha256": prompt.sha256}
            return text

        return render
