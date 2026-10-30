# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Portable skill snapshots and explicit rejection of unsupported disk workspaces."""

from __future__ import annotations

import os
import platform
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nexura_agents import ScopedFilesystem, Skill, SkillLibrary
from nexura_agents.errors import SDKValidationError
from nexura_agents.skills import MAX_SKILL_BYTES, SkillError


def skill_folder(tmp_path: Path) -> Path:
    root = tmp_path / "skills" / "review"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review.\n---\nBODY", encoding="utf-8"
    )
    (root / "references").mkdir()
    (root / "references" / "note.md").write_text("REFERENCE", encoding="utf-8")
    return root


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_skill_loading_needs_neither_landlock_nor_no_follow_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str
) -> None:
    import nexura_agents._filesystem_sandbox as sandbox

    root = skill_folder(tmp_path)
    (root / "scripts").mkdir()
    sentinel = tmp_path / "executed"
    (root / "scripts" / "run.py").write_text(
        f"from pathlib import Path; Path({str(sentinel)!r}).touch()", encoding="utf-8"
    )
    (root / "scripts" / "not-a-reference.md").write_text("SCRIPT TEXT", encoding="utf-8")
    (root / "asset.bin").write_bytes(b"\xff\xfe")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Skills must not initialize a scoped filesystem or Landlock")

    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(sandbox, "restrict_worker", forbidden)
    monkeypatch.setattr(ScopedFilesystem, "__init__", forbidden)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    library = SkillLibrary.from_directory(root.parent)
    loaded = library.get("review")
    assert loaded.instructions == "BODY"
    assert loaded.resources == {"references/note.md": "REFERENCE"}
    assert not sentinel.exists()
    # Snapshot, not a live on-demand host file read.
    (root / "references" / "note.md").write_text("CHANGED", encoding="utf-8")
    assert loaded.resources["references/note.md"] == "REFERENCE"


@pytest.mark.parametrize("target", ["ancestor", "library", "skill", "definition", "resource"])
def test_reparse_points_are_rejected_at_every_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    root = skill_folder(tmp_path)
    paths = {
        "ancestor": tmp_path,
        "library": root.parent,
        "skill": root,
        "definition": root / "SKILL.md",
        "resource": root / "references" / "note.md",
    }
    original = os.lstat

    def reparse(path: Any, *args: Any, **kwargs: Any) -> Any:
        info = original(path, *args, **kwargs)
        if Path(path) == paths[target]:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return info

    monkeypatch.setattr(os, "lstat", reparse)
    with pytest.raises(SkillError, match="reparse"):
        SkillLibrary.from_directory(root.parent)


@pytest.mark.parametrize("target", ["ancestor", "library", "skill", "definition", "resource"])
def test_real_symlinks_are_refused_during_skill_discovery(tmp_path: Path, target: str) -> None:
    root = skill_folder(tmp_path)
    path = {
        "ancestor": tmp_path / "alias",
        "library": root.parent,
        "skill": root,
        "definition": root / "SKILL.md",
        "resource": root / "references" / "note.md",
    }[target]
    if target == "ancestor":
        destination = tmp_path
        library_path = path / "skills"
    else:
        destination = path.with_name(path.name + "-original")
        path.rename(destination)
        library_path = root.parent
    try:
        path.symlink_to(destination, target_is_directory=destination.is_dir())
    except OSError as exc:
        pytest.skip(f"This platform cannot create test symlinks: {exc}")
    with pytest.raises(SkillError):
        SkillLibrary.from_directory(library_path)


@pytest.mark.parametrize("nofollow", [False, True])
def test_opened_skill_identity_must_match_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nofollow: bool
) -> None:
    root = skill_folder(tmp_path)
    decoy = tmp_path / "replacement"
    decoy.write_text("---\nname: review\ndescription: Changed.\n---\nOTHER", encoding="utf-8")
    original = os.open
    observed: list[int] = []

    def changed(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        observed.append(flags)
        if Path(path) == root / "SKILL.md":
            os.replace(decoy, path)
        return original(path, flags, *args, **kwargs)

    if not nofollow:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(os, "open", changed)
    with pytest.raises(SkillError, match="identity"):
        Skill.from_directory(root, format="agent-skills")
    access_mode = getattr(os, "O_ACCMODE", os.O_WRONLY | os.O_RDWR)
    assert observed and all((flags & access_mode) == os.O_RDONLY for flags in observed)
    assert all(not flags & (os.O_CREAT | os.O_TRUNC) for flags in observed)
    if nofollow and hasattr(os, "O_NOFOLLOW"):
        assert all(flags & os.O_NOFOLLOW for flags in observed)


def test_fstat_reparse_point_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = skill_folder(tmp_path)
    original = os.fstat

    def reparse(fd: int) -> Any:
        info = original(fd)
        return SimpleNamespace(
            st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
        )

    monkeypatch.setattr(os, "fstat", reparse)
    with pytest.raises(SkillError, match="reparse"):
        SkillLibrary.from_directory(root.parent)


@pytest.mark.parametrize("kind", ["file", "total", "entries", "library-entries", "depth"])
def test_portable_skill_caps_include_ignored_scripts_and_binary_files(
    tmp_path: Path, kind: str
) -> None:
    root = skill_folder(tmp_path)
    if kind == "file":
        (root / "asset.bin").write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
    elif kind == "total":
        for i in range(5):
            (root / f"asset{i}.bin").write_bytes(b"x" * MAX_SKILL_BYTES)
    elif kind in ("entries", "library-entries"):
        directory = root if kind == "entries" else root.parent
        for i in range(257):
            (directory / f"ignored{i}.bin").touch()
    else:
        directory = root
        for _ in range(17):
            directory /= "nested"
            directory.mkdir()
    with pytest.raises(SkillError, match="cap|256"):
        SkillLibrary.from_directory(root.parent)


def test_portable_skills_refuse_special_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = skill_folder(tmp_path)
    path = root / "device"
    path.touch()
    original = os.lstat

    def special(name: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(name) == path:
            return SimpleNamespace(st_mode=stat.S_IFIFO, st_file_attributes=0)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", special)
    with pytest.raises(SkillError, match="ordinary"):
        SkillLibrary.from_directory(root.parent)


def test_disk_unavailable_is_explicit_and_memory_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nexura_agents._filesystem_sandbox as sandbox

    def unavailable(root_fd: int) -> None:
        raise SDKValidationError(
            "Disk workspaces require Linux Landlock ABI >= 3; use memory mode."
        )

    monkeypatch.setattr(sandbox, "restrict_worker", unavailable)
    with pytest.raises(SDKValidationError, match="memory mode"):
        ScopedFilesystem(tmp_path)
    with ScopedFilesystem() as memory:
        memory.write("note", "portable")
        assert memory.read("note") == "portable"
