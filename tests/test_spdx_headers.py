# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Every first-party open-source source file carries the MIT SPDX header."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "add_spdx_headers.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("add_spdx_headers", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_mode_passes_on_the_tree() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 missing, 0 conflicting" in proc.stdout


def test_check_mode_fails_when_a_file_lacks_the_header(tmp_path: Path) -> None:
    mod = _load_script()
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "good.py").write_text("# SPDX-License-Identifier: MIT\n# Copyright (c) 2026 Liceron\n")
    (pkg / "bad.py").write_text('"""No header."""\n')
    assert mod.run(check=True, root=tmp_path) == 1
    # --check never writes.
    assert (pkg / "bad.py").read_text() == '"""No header."""\n'
    assert mod.run(check=False, root=tmp_path) == 0
    assert mod.run(check=True, root=tmp_path) == 0


def test_conflicting_identifier_is_reported_and_left_alone(tmp_path: Path) -> None:
    mod = _load_script()
    pkg = tmp_path / "src"
    pkg.mkdir()
    original = "# SPDX-License-Identifier: Apache-2.0\nx = 1\n"
    (pkg / "other.py").write_text(original)
    assert mod.run(check=False, root=tmp_path) == 1
    assert (pkg / "other.py").read_text() == original
    assert mod.run(check=True, root=tmp_path) == 1


def test_header_keeps_docstring_future_import_shebang_and_encoding() -> None:
    mod = _load_script()
    source = (
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n"
        '"""Module docstring."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "X: int = 1\n"
    )
    updated = mod.add_header(source, "#")
    lines = updated.splitlines()
    assert lines[0] == "#!/usr/bin/env python3"
    assert lines[1] == "# -*- coding: utf-8 -*-"
    assert lines[2] == "# SPDX-License-Identifier: MIT"
    assert lines[3] == "# Copyright (c) 2026 Liceron"
    tree = ast.parse(updated)
    assert ast.get_docstring(tree) == "Module docstring."
    compile(updated, "<header>", "exec")  # from __future__ is still first statement
    assert mod.inspect(updated, "#") == "ok"
    assert mod.inspect(source, "#") == "missing"


def test_typescript_header_and_missing_copyright_line() -> None:
    mod = _load_script()
    updated = mod.add_header("import x from 'y';\n", "//")
    assert updated.startswith("// SPDX-License-Identifier: MIT\n// Copyright (c) 2026 Liceron\n")
    assert mod.inspect(updated, "//") == "ok"
    partial = "// SPDX-License-Identifier: MIT\nexport const a = 1;\n"
    assert mod.inspect(partial, "//") == "missing"
    fixed = mod.add_header(partial, "//")
    assert fixed.count("SPDX-License-Identifier") == 1
    assert mod.inspect(fixed, "//") == "ok"
