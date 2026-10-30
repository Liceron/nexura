# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""``llms.txt``, ``llms-full.txt`` and the coding-agent skill stay correct and current."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
REPO_URL = "https://github.com/Liceron/nexura/blob/main/"
LLMS = ROOT / "llms.txt"
SKILL = ROOT / "skills" / "nexura" / "SKILL.md"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load("gen_llms_full")


def _links() -> list[str]:
    return re.findall(r"\]\(([^)\s]+)\)", LLMS.read_text(encoding="utf-8"))


def test_llms_txt_follows_the_convention() -> None:
    lines = LLMS.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# Nexura"
    assert lines[2].startswith("> "), "the summary is a blockquote after the H1"
    assert [ln for ln in lines if ln.startswith("# ")] == ["# Nexura"]
    assert any(ln.startswith("## ") for ln in lines)


def test_every_llms_txt_link_is_an_absolute_github_url_to_an_existing_file() -> None:
    links = _links()
    assert len(links) >= 30
    relative = [link for link in links if not link.startswith(REPO_URL)]
    assert not relative, f"llms.txt links must be absolute GitHub URLs: {relative}"
    missing = [link for link in links if not (ROOT / link.removeprefix(REPO_URL)).is_file()]
    assert not missing, f"llms.txt links to missing files: {missing}"


def test_llms_txt_links_every_page_of_the_docs_index() -> None:
    linked = {link.removeprefix(REPO_URL) for link in _links()}
    pages = {p.relative_to(ROOT).as_posix() for p in gen.pages()}
    assert not pages - linked, f"pages in docs/README.md missing from llms.txt: {pages - linked}"


def test_llms_full_txt_is_up_to_date() -> None:
    page = ROOT / "llms-full.txt"
    assert page.read_text(encoding="utf-8") == gen.render(), (
        "llms-full.txt is stale; run: uv run python scripts/gen_llms_full.py"
    )


def test_llms_full_txt_holds_every_index_page_once() -> None:
    text = gen.render()
    for path in gen.pages():
        rel = path.relative_to(ROOT).as_posix()
        assert text.count(f"Source: {REPO_URL}{rel}\n") == 1, rel


def test_skill_has_front_matter_with_name_and_description() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = text.split("---\n", 2)[1]
    fields = dict(
        line.split(":", 1) for line in front.splitlines() if line and not line.startswith(" ")
    )
    assert fields["name"].strip() == "nexura"
    assert len(fields["description"].strip()) > 40


def test_skill_python_blocks_run_offline(tmp_path: Path) -> None:
    import subprocess
    import sys

    blocks = re.findall(r"```python\n(.*?)```", SKILL.read_text(encoding="utf-8"), re.DOTALL)
    assert blocks
    for index, code in enumerate(blocks):
        script = tmp_path / f"skill_{index}.py"
        script.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, f"SKILL.md block {index} failed:\n{proc.stderr[-3000:]}"


def test_open_source_export_includes_llms_files_and_skills() -> None:
    export = _load("export_oss_tree")
    wanted = ["llms.txt", "llms-full.txt", "skills/nexura/SKILL.md", "scripts/gen_llms_full.py"]
    assert export.select(wanted) == wanted
