"""The guides in ``docs/`` are executed, not just read.

Every ```python block runs offline in a fresh interpreter; blocks fenced as
```python notest need live credentials or a running service and are skipped here.
Relative links between pages must point at files that exist.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
PAGES = sorted(DOCS.rglob("*.md"))
BLOCKS = [
    pytest.param(page, index, code, id=f"{page.relative_to(DOCS)}#{index}")
    for page in PAGES
    for index, code in enumerate(
        re.findall(r"```python\n(.*?)```", page.read_text(encoding="utf-8"), flags=re.DOTALL)
    )
]
LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)#\s]+)(?:#[^)]*)?\)")


def test_docs_exist_and_have_examples() -> None:
    assert (DOCS / "README.md").exists()
    assert len(PAGES) >= 10 and len(BLOCKS) >= 20


@pytest.mark.parametrize(("page", "index", "code"), BLOCKS)
def test_docs_python_block_runs_offline(page: Path, index: int, code: str, tmp_path: Path) -> None:
    script = tmp_path / f"block_{index}.py"
    script.write_text(code, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"{page.name} block {index} failed:\n{proc.stderr[-3000:]}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(DOCS)))
def test_docs_relative_links_resolve(page: Path) -> None:
    broken = [
        target
        for target in LINK.findall(page.read_text(encoding="utf-8"))
        if not (page.parent / target).resolve().exists()
    ]
    assert not broken, f"{page.name} links to missing files: {broken}"
