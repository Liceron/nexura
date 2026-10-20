"""Round trip between workflow specs and plain Python (P5), and the safe import path."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.cli.main import main as cli_main
from nexura_runtime.codegen import (
    CodeGenerationError,
    CodeImportError,
    python_to_workflow,
    workflow_to_python,
)
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

CHILD = WorkflowSpec(
    name="child",
    entry_node="leaf",
    nodes={"leaf": WorkflowNode(id="leaf", handler="synthetic_router")},
    terminal_nodes=["leaf"],
    metadata={"owner": "team-a"},
)


def _specs() -> dict[str, WorkflowSpec]:
    plain = WorkflowSpec(
        name='plain "quoted" \u00e9 name',
        entry_node="a",
        nodes={
            "a": WorkflowNode(
                id="a",
                handler="h1",
                transitions={"yes": "b", "no": "done"},
                default_transition="done",
                module_id="mod.x",
                module_version="1.2.0",
                config={"temperature": 0.2, "tags": ["x", "y"], "neg": -3, "none": None},
                input_mapping={"q": "state.question"},
                output_mapping={"answer": "text"},
                presentation={"x": 10, "y": -20},
                metadata={"note": "hello", "nested": {"k": [1, 2.5, True]}},
            ),
            "b": WorkflowNode(id="b", handler="h2"),
        },
        terminal_nodes=["b", "done"],
        metadata={"purpose": "test", "long": [f"item-{i}" for i in range(30)]},
        presentation={"viewport": {"zoom": 1.5}},
    )
    approval = WorkflowSpec(
        name="approval",
        entry_node="pay",
        nodes={
            "pay": WorkflowNode(
                id="pay", handler="charge", requires_approval=True, required_role="admin"
            )
        },
        terminal_nodes=["pay"],
    )
    retry = WorkflowSpec(
        name="retry",
        entry_node="call",
        nodes={
            "call": WorkflowNode(
                id="call",
                handler="flaky",
                retry={"max_attempts": 3, "initial_delay": 0.1, "retry_on": ["TimeoutError"]},
            )
        },
        terminal_nodes=["call"],
    )
    parallel = WorkflowSpec(
        name="parallel",
        entry_node="fan",
        nodes={
            "fan": WorkflowNode(
                id="fan",
                handler="",
                parallel=["a", "b"],
                reducers={"out": "append", "total": "sum"},
                max_concurrency=2,
            )
        },
        terminal_nodes=["fan"],
    )
    mapped = WorkflowSpec(
        name="map",
        entry_node="each",
        nodes={
            "each": WorkflowNode(
                id="each",
                handler="h",
                map_over="rows",
                map_as="row",
                reducers={"out": "extend"},
                max_concurrency=4,
            )
        },
        terminal_nodes=["each"],
    )
    sub = WorkflowSpec(
        name="sub",
        entry_node="run",
        nodes={
            "run": WorkflowNode(
                id="run",
                handler="",
                subgraph=CHILD,
                input_mapping={"x": "y"},
                default_transition="end",
            ),
            "end": WorkflowNode(id="end", handler="h"),
        },
        terminal_nodes=["end"],
    )
    nested = WorkflowSpec(
        name="nested",
        entry_node="outer",
        nodes={
            "outer": WorkflowNode(
                id="outer",
                handler="",
                subgraph=sub,
                map_over="items",
                reducers={"r": "merge"},
                retry={"max_attempts": 2},
                requires_approval=True,
            )
        },
        terminal_nodes=["outer"],
    )
    empty = WorkflowSpec(name="empty", entry_node="x", nodes={})
    return {
        "plain": plain,
        "approval": approval,
        "retry": retry,
        "parallel": parallel,
        "map": mapped,
        "subgraph": sub,
        "nested": nested,
        "empty": empty,
    }


SPECS = _specs()


@pytest.mark.parametrize("key", sorted(SPECS))
def test_exec_round_trip_matches_original(key: str) -> None:
    spec = SPECS[key]
    source = workflow_to_python(spec)
    ast.parse(source)
    namespace: dict[str, Any] = {"__name__": "generated"}
    exec(compile(source, "<generated>", "exec"), namespace)  # tests only, fake data
    assert namespace["spec"].to_dict() == spec.to_dict()
    assert namespace["spec"].canonical_hash() == spec.canonical_hash()


@pytest.mark.parametrize("key", sorted(SPECS))
def test_safe_import_matches_original_without_executing(key: str) -> None:
    spec = SPECS[key]
    for registry in (None, "my_pkg.handlers:registry", "my_pkg.handlers:reg"):
        source = workflow_to_python(spec, registry_import=registry)
        assert python_to_workflow(source).to_dict() == spec.to_dict()


def test_comments_explain_composites_approvals_and_retries() -> None:
    source = workflow_to_python(SPECS["nested"])
    assert "Approval gate" in source
    assert "Retries: up to 2" in source
    assert "Composite step: runs the nested workflow" in source
    assert "once per item" in source
    assert "in parallel" in workflow_to_python(SPECS["parallel"])


def test_registry_import_shapes_the_main_block() -> None:
    assert "from my_pkg.handlers import registry\n" in workflow_to_python(
        SPECS["plain"], registry_import="my_pkg.handlers:registry"
    )
    assert "import reg as registry" in workflow_to_python(
        SPECS["plain"], registry_import="my_pkg.handlers:reg"
    )
    assert "get_default_registry()" in workflow_to_python(SPECS["plain"])
    for bad in ("nocolon", "a b:c", "os;rm:x", "a.b:"):
        with pytest.raises(ValueError, match="registry_import"):
            workflow_to_python(SPECS["plain"], registry_import=bad)


def test_non_literal_values_are_refused_on_generation() -> None:
    spec = WorkflowSpec(
        name="x",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="h", config={"v": float("nan")})},
    )
    with pytest.raises(CodeGenerationError):
        workflow_to_python(spec)


@pytest.mark.parametrize("key", sorted(SPECS))
def test_generated_code_is_ruff_clean(key: str, tmp_path: Path) -> None:
    target = tmp_path / "wf.py"
    target.write_text(workflow_to_python(SPECS[key], registry_import="pkg.mod:registry"))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--line-length",
            "100",
            "--select",
            "E,F,I,UP,B,SIM",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if "No module named ruff" in proc.stderr:
        pytest.skip("ruff is not installed in this interpreter")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generated_main_block_runs_with_the_callers_registry(tmp_path: Path) -> None:
    (tmp_path / "myhandlers.py").write_text(
        "from nexura_runtime.workflow import ModuleRegistry\n"
        "registry = ModuleRegistry()\n"
        "@registry.register_handler('inc')\n"
        "def inc(state):\n"
        "    return {'n': state.get('n', 0) + 1}\n"
    )
    spec = WorkflowSpec(
        name="run",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="inc")},
        terminal_nodes=["a"],
    )
    (tmp_path / "wf.py").write_text(workflow_to_python(spec, registry_import="myhandlers:registry"))
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "wf.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(tmp_path) + ":" + ":".join(sys.path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "status: completed" in proc.stdout
    assert '"n": 1' in proc.stdout


# --- malicious / unsupported sources -------------------------------------------------


def _module(node_kwargs: str = 'id="a", handler="h"', extra: str = "") -> str:
    return (
        "from nexura_runtime.workflow import WorkflowNode, WorkflowSpec\n"
        f"{extra}"
        'spec = WorkflowSpec(name="x", entry_node="a", '
        f'nodes={{"a": WorkflowNode({node_kwargs})}})\n'
    )


def _marker(tmp_path: Path) -> Path:
    return tmp_path / "pwned"


def _touch(tmp_path: Path) -> str:
    return f"open({str(_marker(tmp_path))!r}, 'w').write('x')"


def _cases(tmp_path: Path) -> dict[str, tuple[str, int]]:
    touch = _touch(tmp_path)
    marker = str(_marker(tmp_path))
    return {
        "dunder_import": (_module(f'id="a", handler=__import__("os").system("touch {marker}")'), 4),
        "os_system": (
            "import os\n" + _module(f'id="a", handler=os.system("touch {marker}")'),
            1,
        ),
        "getattr_trick": (
            _module(f'id="a", handler=getattr(__builtins__, "exec")("{touch}")'),
            3,
        ),
        "fstring_call": (_module(f'id="a", handler=f"{{{touch}}}"'), 3),
        "plain_fstring": (_module('id="a", handler=f"abc"'), 3),
        "decorator": (
            "from nexura_runtime.workflow import WorkflowNode, WorkflowSpec\n"
            f"@(lambda f: {touch})\n"
            "def f():\n    pass\n",
            2,
        ),
        "nested_lambda": (_module('id="a", handler=(lambda: (lambda: 1))()'), 3),
        "lambda": (_module('id="a", handler=lambda: 1'), 3),
        "comprehension": (_module('id="a", handler=[x for x in "ab"]'), 3),
        "set_comprehension": (_module('id="a", config={"k": {x for x in "ab"}}'), 3),
        "attribute": (_module('id="a", handler="h".upper'), 3),
        "name_ref": (_module('id="a", handler=h'), 3),
        "function_def": ("def evil():\n    open('x')\n", 1),
        "class_def": ("class A:\n    x = 1\n", 1),
        "exec_stmt": ("exec('1')\n", 1),
        "star_import": ("from nexura_runtime.workflow import *\n", 1),
        "other_import": ("import subprocess\n", 1),
        "aliased_import": ("from nexura_runtime.workflow import WorkflowNode as W\n", 1),
        "relative_import": ("from . import x\n", 1),
        "other_module_import": ("from os import system\n", 1),
        "with_stmt": ("with open('x') as f:\n    pass\n", 1),
        "kwargs_unpack": (_module("id='a', handler='h', **{'x': 1}"), 3),
        "unknown_field": (_module('id="a", handler="h", callback="x"'), 3),
        "wrong_type": (_module('id="a", handler=5'), 3),
        "bytes": (_module('id="a", handler=b"x"'), 3),
        "main_with_extra_code": (
            _module() + f'if __name__ == "__main__":\n    {touch}\n',
            4,
        ),
        "bad_if": (_module() + "if True:\n    pass\n", 4),
        "syntax_error": ("spec = (\n", 1),
    }


def test_malicious_sources_are_rejected_without_running_anything(tmp_path: Path) -> None:
    for name, (source, _line) in _cases(tmp_path).items():
        with pytest.raises(CodeImportError) as excinfo:
            python_to_workflow(source)
        assert excinfo.value.line is not None, name
        assert f"line {excinfo.value.line}" in str(excinfo.value), name
        assert excinfo.value.line >= 1, name
        expected = {"dunder_import": 2, "os_system": 1, "star_import": 1, "function_def": 1}
        if name in expected:
            assert excinfo.value.line == expected[name], (name, str(excinfo.value))
    assert not _marker(tmp_path).exists()


def test_error_messages_name_the_problem() -> None:
    with pytest.raises(CodeImportError, match="calls are not allowed"):
        python_to_workflow(_module('id="a", handler=os.system("x")'))
    with pytest.raises(CodeImportError, match="f-strings"):
        python_to_workflow(_module('id="a", handler=f"{1}"'))
    with pytest.raises(CodeImportError, match="no `spec"):
        python_to_workflow('"""doc"""\n')
    with pytest.raises(CodeImportError, match="assigned more than once"):
        python_to_workflow(_module() + _module().split("\n", 1)[1])


def test_size_and_depth_caps() -> None:
    with pytest.raises(CodeImportError, match="larger than"):
        python_to_workflow("#" * 200, max_bytes=100)
    deep = "[" * 400 + "]" * 400
    with pytest.raises(CodeImportError):
        python_to_workflow(_module(f'id="a", handler="h", config={{"k": {deep}}}'))
    with pytest.raises(CodeImportError):
        python_to_workflow("x = '\x00'\n")


def test_generated_main_block_tampering_is_rejected() -> None:
    source = workflow_to_python(SPECS["plain"])
    with pytest.raises(CodeImportError, match="__main__"):
        python_to_workflow(source.replace("result = await engine.run({})", "result = 1"))
    assert python_to_workflow(source).to_dict() == SPECS["plain"].to_dict()


# --- CLI --------------------------------------------------------------------------------


def test_cli_export_python_and_import_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = SPECS["nested"]
    spec_json = tmp_path / "spec.json"
    spec_json.write_text(spec.to_json())
    out = tmp_path / "wf.py"
    assert (
        cli_main(["workflow", "export", str(spec_json), "--format", "python", "-o", str(out)]) == 0
    )
    assert out.read_text().startswith('"""')
    back = tmp_path / "back.json"
    assert cli_main(["workflow", "import", str(out), "--out", str(back)]) == 0
    assert WorkflowSpec.from_json(back.read_text()).to_dict() == spec.to_dict()
    capsys.readouterr()
    assert cli_main(["workflow", "import", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "nested"


def test_cli_import_rejects_unsafe_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(f"import os\n{_touch(tmp_path)}\n")
    assert cli_main(["workflow", "import", str(bad)]) == 1
    assert "line 1" in capsys.readouterr().err
    assert not _marker(tmp_path).exists()
    assert cli_main(["workflow", "import", str(tmp_path / "missing.py")]) == 1


def test_cli_export_bundle_still_needs_out(tmp_path: Path) -> None:
    spec_json = tmp_path / "spec.json"
    spec_json.write_text(SPECS["plain"].to_json())
    assert cli_main(["workflow", "export", str(spec_json)]) == 1
