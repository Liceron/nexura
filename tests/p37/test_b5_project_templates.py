# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P37 B5: ``nexura-runtime init`` templates, ``dev`` wiring and ``deploy --target docker``.

Each template is generated into tmp_path and its own offline test suite runs in a subprocess.
``dev`` is checked with ``serve_all`` replaced, so no server ever listens. ``deploy`` only
writes files; these tests fail if anything tries to run a process.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.cli.main import main
from nexura_runtime.project import TEMPLATES, ScaffoldError, scaffold_project
from nexura_runtime.project import dev as dev_module
from nexura_runtime.project.dev import DevServer, seed_workflows
from nexura_runtime.project.scaffold import files_root
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_NAMES = ("agent", "workflow", "rag")


@pytest.fixture(autouse=True)
def _restore_import_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Generated packages are imported by ``dev``; keep them out of other tests."""
    path, modules = list(sys.path), set(sys.modules)
    for name in ("NEXURA_MODEL", "NEXURA_EMBEDDINGS", "NEXURA_STUDIO_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield
    sys.path[:] = path
    for name in set(sys.modules) - modules:
        if name.startswith("b5_"):
            del sys.modules[name]


def _offline_env() -> dict[str, str]:
    drop = {"PYTHONPATH", "NEXURA_MODEL", "NEXURA_EMBEDDINGS", "NEXURA_STUDIO_TOKEN"}
    return {k: v for k, v in os.environ.items() if k not in drop and not k.endswith("_API_KEY")}


# --- init ---------------------------------------------------------------------------------


def test_init_list_names_every_template(capsys: pytest.CaptureFixture[str]) -> None:
    assert set(TEMPLATES) >= set(TEMPLATE_NAMES)
    assert main(["init", "--list"]) == 0
    out = capsys.readouterr().out
    for name in TEMPLATE_NAMES:
        assert any(line.startswith(name + " ") for line in out.splitlines()), out


@pytest.mark.parametrize("template", TEMPLATE_NAMES)
def test_template_project_runs_its_own_offline_tests(
    template: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / f"demo-{template}-app"
    assert main(["init", str(project), "--template", template]) == 0
    out = capsys.readouterr().out
    package = f"demo_{template}_app"
    assert f"package {package}" in out and "nexura-runtime dev" in out

    for rel in (
        "pyproject.toml",
        "README.md",
        ".gitignore",
        ".env.example",
        f"{package}/__init__.py",
        f"{package}/__main__.py",
        f"{package}/studio.py",
    ):
        assert (project / rel).is_file(), rel
    assert list((project / "tests").glob("test_*.py"))
    assert not list(project.rglob("*.tmpl"))

    text = (project / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = tomllib.loads(text)
    deps = pyproject["project"]["dependencies"]
    assert any(d.startswith("nexura-runtime") for d in deps)
    assert "not on PyPI yet" in text and "--find-links" in text
    assert pyproject["tool"]["nexura"] == {
        "template": template,
        "handlers": [f"{package}.studio:registry"],
        "workflows": [f"{package}.studio:workflows"],
    }
    env_lines = [
        line
        for line in (project / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert env_lines and all(line.endswith("=") for line in env_lines), env_lines
    ignore = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore and ".nexura/" in ignore
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert "## Next steps" in readme and "pytest" in readme and "nexura-runtime dev" in readme
    assert "{{" not in readme and "}}" not in text

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        env=_offline_env(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert " passed" in proc.stdout and "failed" not in proc.stdout

    if importlib.util.find_spec("ruff") is not None:
        for args in (["check", "."], ["format", "--check", "."]):
            lint = subprocess.run(
                [sys.executable, "-m", "ruff", *args, "--no-cache"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert lint.returncode == 0, lint.stdout + lint.stderr


def test_rag_template_documents_the_test_embedder(tmp_path: Path) -> None:
    project = tmp_path / "kb"
    scaffold_project(project, "rag")
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert "deterministic test embedder" in readme and "non-semantic" in readme
    assert "NEXURA_EMBEDDINGS" in readme
    assert "NEXURA_EMBEDDINGS=" in (project / ".env.example").read_text(encoding="utf-8")
    assert sorted(p.name for p in (project / "docs").iterdir()) == ["refunds.md", "shipping.md"]


def test_init_refuses_a_non_empty_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "busy"
    project.mkdir()
    (project / "notes.txt").write_text("mine", encoding="utf-8")
    assert main(["init", str(project)]) == 2
    assert "not empty" in capsys.readouterr().err
    assert [p.name for p in project.iterdir()] == ["notes.txt"]
    assert (project / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_init_force_never_overwrites_existing_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "busy"
    project.mkdir()
    (project / "README.md").write_text("my readme", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname = 'mine'\n", encoding="utf-8")
    assert main(["init", str(project), "--force", "--template", "workflow"]) == 0
    captured = capsys.readouterr()
    assert (project / "README.md").read_text(encoding="utf-8") == "my readme"
    assert (project / "pyproject.toml").read_text(encoding="utf-8") == "[project]\nname = 'mine'\n"
    assert "kept existing README.md" in captured.err
    assert "kept existing pyproject.toml" in captured.err
    assert (project / "busy" / "workflow.py").is_file()


def test_init_refuses_a_file_and_bad_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    assert main(["init", str(target)]) == 2
    assert main(["init", str(tmp_path / "1st")]) == 2
    assert main(["init", str(tmp_path / "json")]) == 2  # would shadow the stdlib
    assert main(["init", str(tmp_path / "ok"), "--name", "Bad-Name"]) == 2
    assert main(["init"]) == 2
    err = capsys.readouterr().err
    assert "not a directory" in err and "--name" in err
    assert not (tmp_path / "1st").exists() and not (tmp_path / "ok").exists()


def test_init_name_and_no_env_example(tmp_path: Path) -> None:
    result = scaffold_project(
        tmp_path / "Some Dir", "agent", package="b5_custom", env_example=False
    )
    assert result.package == "b5_custom"
    assert (tmp_path / "Some Dir" / "b5_custom" / "agent.py").is_file()
    assert not (tmp_path / "Some Dir" / ".env.example").exists()
    with pytest.raises(ScaffoldError, match="unknown template"):
        scaffold_project(tmp_path / "x", "nope")


def test_init_never_writes_through_a_link_out_of_the_project(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "b5_linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ScaffoldError, match="leaves the project"):
        scaffold_project(project, "agent", package="b5_linked", force=True)
    assert list(outside.iterdir()) == []
    assert [p.name for p in project.iterdir()] == ["b5_linked"]


def test_every_template_file_is_used_and_has_no_stray_placeholders() -> None:
    root = files_root()
    groups = {child.name for child in root.iterdir() if child.is_dir()}
    assert groups == {"common", "docker", *TEMPLATE_NAMES}
    known = {
        "package",
        "project_name",
        "slug",
        "title",
        "template",
        "description",
        "nexura_version",
        "port",
    }
    import re

    for path in Path(str(root)).rglob("*"):
        if path.is_file():
            assert path.name.endswith(".tmpl"), path
            found = set(re.findall(r"\{\{([a-z_]+)\}\}", path.read_text(encoding="utf-8")))
            assert found <= known, (path, found - known)


# --- dev ----------------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.servers: list[DevServer] = []
        self.workflows: dict[str, list[str]] = {}
        self.handlers: dict[str, set[str]] = {}

    async def __call__(self, servers: list[DevServer]) -> None:
        self.servers = list(servers)
        for server in servers:
            stored = await server.app.state.store.list_workflows()
            self.workflows[server.label] = sorted(w["workflow_id"] for w in stored)
            self.handlers[server.label] = set(server.app.state.registry._handlers)


def _project(tmp_path: Path, template: str, package: str) -> Path:
    project = tmp_path / package
    scaffold_project(project, template, package=package)
    return project


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(dev_module, "serve_all", rec)
    return rec


def test_dev_serves_service_and_studio_on_loopback_with_the_project(
    tmp_path: Path, recorder: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, "workflow", "b5_dev_wf")
    data = tmp_path / "data"
    code = main(
        [
            "dev",
            "--project",
            str(project),
            "--data-dir",
            str(data),
            "--port",
            "18765",
            "--studio-port",
            "18001",
        ]
    )
    assert code == 0
    assert [(s.label, s.host, s.port) for s in recorder.servers] == [
        ("Service", "127.0.0.1", 18765),
        ("Studio", "127.0.0.1", 18001),
    ]
    out = capsys.readouterr().out
    assert "http://127.0.0.1:18001" in out and "http://127.0.0.1:18765" in out
    assert "Loopback only" in out
    for label in ("Service", "Studio"):
        assert "b5-dev-wf-refund-reply" in recorder.workflows[label]
        assert {"b5_dev_wf.draft", "b5_dev_wf.send"} <= recorder.handlers[label]
    assert (data / "service.sqlite3").is_file() and (data / "studio.sqlite3").is_file()

    # A second start finds the workflow up to date instead of adding another copy.
    assert main(["dev", "--project", str(project), "--data-dir", str(data)]) == 0
    assert "is up to date" in capsys.readouterr().out
    assert recorder.workflows["Studio"].count("b5-dev-wf-refund-reply") == 1


@pytest.mark.parametrize(("template", "suffix"), [("agent", "refund-request"), ("rag", "ask")])
def test_dev_registers_other_templates(
    tmp_path: Path, recorder: _Recorder, template: str, suffix: str
) -> None:
    package = f"b5_dev_{template}"
    project = _project(tmp_path, template, package)
    assert main(["dev", "--project", str(project), "--data-dir", str(tmp_path / "d")]) == 0
    slug = package.replace("_", "-")
    assert f"{slug}-{suffix}" in recorder.workflows["Studio"]


def test_dev_defaults_to_the_current_directory_and_project_data_dir(
    tmp_path: Path, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, "workflow", "b5_dev_cwd")
    monkeypatch.chdir(project)
    assert main(["dev", "--no-studio"]) == 0
    assert [(s.label, s.host, s.port) for s in recorder.servers] == [("Service", "127.0.0.1", 8765)]
    assert (project / ".nexura" / "service.sqlite3").is_file()


def test_dev_without_a_project_layout_starts_with_built_ins(
    tmp_path: Path, recorder: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["dev", "--project", str(tmp_path), "--data-dir", str(tmp_path / "d")]) == 0
    assert "No [tool.nexura] table" in capsys.readouterr().err
    assert [s.label for s in recorder.servers] == ["Service", "Studio"]


def test_dev_without_the_studio_package_serves_the_service(
    tmp_path: Path,
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(dev_module, "_studio_factory", lambda: None)
    assert main(["dev", "--project", str(tmp_path), "--data-dir", str(tmp_path / "d")]) == 0
    assert [s.label for s in recorder.servers] == ["Service"]
    assert "pip install nexura-studio" in capsys.readouterr().err


def test_dev_container_mode_needs_a_token_and_serves_studio_only(
    tmp_path: Path,
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path, "workflow", "b5_dev_box")
    argv = ["dev", "--container", "--project", str(project), "--data-dir", str(tmp_path / "d")]
    assert main(argv) == 2
    assert "NEXURA_STUDIO_TOKEN" in capsys.readouterr().err
    assert recorder.servers == []

    monkeypatch.setenv("NEXURA_STUDIO_TOKEN", "t" * 40)
    assert main(argv) == 0
    assert [(s.label, s.host) for s in recorder.servers] == [("Studio", "0.0.0.0")]
    assert "#token=" in capsys.readouterr().out


def test_dev_has_no_host_option() -> None:
    from nexura_runtime.cli.main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["dev", "--host", "0.0.0.0"])


def test_dev_rejects_broken_project_config(
    tmp_path: Path, recorder: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nexura]\nhandlers = ["no_such_b5_module:registry"]\n', encoding="utf-8"
    )
    assert main(["dev", "--project", str(tmp_path), "--data-dir", str(tmp_path / "d")]) == 2
    assert "no_such_b5_module" in capsys.readouterr().err
    assert recorder.servers == []


def test_seed_workflows_creates_then_updates_a_changed_spec(tmp_path: Path) -> None:
    def spec(handler: str) -> WorkflowSpec:
        return WorkflowSpec(
            name="seeded",
            entry_node="a",
            nodes={"a": WorkflowNode(id="a", handler=handler)},
            terminal_nodes=["a"],
        )

    async def scenario() -> tuple[list[str], list[str], dict[str, Any] | None]:
        store = StudioStore(tmp_path / "studio.sqlite3")
        try:
            first = await seed_workflows(store, {"b5-seeded": spec("synthetic_router")})
            second = await seed_workflows(store, {"b5-seeded": spec("synthetic_order_lookup")})
            return first, second, await store.get_workflow("b5-seeded")
        finally:
            await store.close()

    first, second, record = asyncio.run(scenario())
    assert first == ["registered workflow 'b5-seeded'"]
    assert second == ["updated workflow 'b5-seeded'"]
    assert record is not None and record["revision"] == 2


# --- deploy -------------------------------------------------------------------------------


@pytest.fixture
def no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"deploy must not run processes: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)


def test_deploy_docker_writes_files_with_the_security_caveats(
    tmp_path: Path, no_processes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, "agent", "b5_deploy")
    assert main(["deploy", "--target", "docker", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "Nothing was built or run" in out

    dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("# syntax=docker/dockerfile:1\n")
    assert "FROM python:3.12-slim" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "-p 127.0.0.1:8000:8000" in dockerfile
    assert "NEXURA_STUDIO_TOKEN" in dockerfile
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    assert '"dev", "--container"' in cmd and "--insecure" not in cmd
    assert "-p 0.0.0.0" not in dockerfile and "PIP_FIND_LINKS" in dockerfile
    assert "{{" not in dockerfile

    deploy_md = (project / "DEPLOY.md").read_text(encoding="utf-8")
    for needle in (
        "single user, this machine only",
        "127.0.0.1:8000:8000",
        "NEXURA_STUDIO_TOKEN",
        "never add",
        "--insecure-allow-unauthenticated-network",
        "external security review",
        "not on PyPI",
    ):
        assert needle in deploy_md, needle

    ignore = (project / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", ".nexura/", ".venv/", ".git/"} <= set(ignore)


def test_deploy_never_overwrites_without_force(
    tmp_path: Path, no_processes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, "workflow", "b5_deploy_twice")
    (project / "Dockerfile").write_text("FROM mine\n", encoding="utf-8")
    assert main(["deploy", "--target", "docker", "--project", str(project)]) == 2
    assert "refusing to overwrite Dockerfile" in capsys.readouterr().err
    assert (project / "Dockerfile").read_text(encoding="utf-8") == "FROM mine\n"
    assert not (project / "DEPLOY.md").exists()

    argv = ["deploy", "--target", "docker", "--project", str(project), "--port", "9000"]
    assert main([*argv, "--force"]) == 0
    assert "replaced Dockerfile" in capsys.readouterr().err
    assert "-p 127.0.0.1:9000:9000" in (project / "Dockerfile").read_text(encoding="utf-8")


def test_deploy_other_targets_print_what_is_supported(
    tmp_path: Path, no_processes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, "workflow", "b5_deploy_k8s")
    for target in ("kubernetes", "helm", "lambda"):
        assert main(["deploy", "--target", target, "--project", str(project)]) == 2
        err = capsys.readouterr().err
        assert "Supported deploy targets" in err and "docker" in err
    assert not (project / "Dockerfile").exists()


def test_deploy_needs_a_project(
    tmp_path: Path, no_processes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["deploy", "--target", "docker", "--project", str(tmp_path)]) == 2
    assert "not a Nexura project" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


# --- entry points and packaging -----------------------------------------------------------


def test_project_entry_point_runs_the_same_parser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from importlib.metadata import entry_points

    commands = {ep.name: ep for ep in entry_points(group="nexura.commands")}
    project = commands["project"].load()
    assert project(["init", "--list"]) == 0
    assert "workflow" in capsys.readouterr().out
    assert project(["init", str(tmp_path / "b5_ep"), "--template", "rag"]) == 0
    assert (tmp_path / "b5_ep" / "b5_ep" / "rag.py").is_file()
    assert commands["dev"].load()(["--help"]) == 0
    assert "--studio-port" in capsys.readouterr().out
    assert project(["deploy", "--target", "nope", "--project", str(tmp_path)]) == 2


def test_templates_are_in_the_export_allow_list() -> None:
    spec = importlib.util.spec_from_file_location(
        "export_oss_tree", ROOT / "scripts" / "export_oss_tree.py"
    )
    assert spec is not None and spec.loader is not None
    export = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export)
    files = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "src/nexura_runtime/project/files").rglob("*.tmpl")
    )
    assert any(f.endswith("agent/env.example.tmpl") for f in files)
    assert export.select(files) == files


def test_built_wheel_contains_the_templates(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=300,
    )
    wheel = next(tmp_path.glob("nexura_runtime-*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    expected = {
        p.relative_to(ROOT / "src").as_posix()
        for p in (ROOT / "src/nexura_runtime/project/files").rglob("*.tmpl")
    }
    assert len(expected) > 20
    assert expected <= names, sorted(expected - names)
