"""Round 7 directory trust, neutral previews and fixed worker launch contracts."""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from nexura_runtime import retention
from nexura_runtime.cli.main import main
from nexura_runtime.studio.store import StudioStore


def _directory_info(mode, uid):
    return os.stat_result((stat.S_IFDIR | mode, 1, 1, 1, uid, 1, 0, 0, 0, 0))


@pytest.mark.parametrize(
    ("mode", "uid", "final", "allowed"),
    [
        (0o775, 0, False, False),
        (0o777, os.geteuid(), True, False),
        (0o1777, 0, False, True),
        (0o1777, 0, True, False),
        (0o1777, 12345, False, False),
        (0o755, os.geteuid(), True, True),
        (0o755, 0, True, True),
        (0o755, 12345, True, False),
        (0o755, 12345, False, False),
    ],
)
def test_database_directory_permission_contract(mode, uid, final, allowed):
    path = Path("/operator/database")
    if allowed:
        retention._check_directory_permissions(path, _directory_info(mode, uid), final=final)
    else:
        with pytest.raises(ValueError) as failure:
            retention._check_directory_permissions(path, _directory_info(mode, uid), final=final)
        assert str(path) in str(failure.value) and "chmod" in str(failure.value)


def test_unsafe_directory_refuses_before_connect(tmp_path, monkeypatch):
    root = tmp_path / "unsafe"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    db = root / "service.sqlite3"
    service = StudioStore(db)
    service._close_sync()
    before = db.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe directory reached SQLite connect")

    monkeypatch.setattr(retention.sqlite3, "connect", forbidden)
    for dry_run in (True, False):
        with pytest.raises(retention.RetentionError) as failure:
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=dry_run)
        assert failure.value.report.errors
        assert failure.value.report.runs_deleted == failure.value.report.events_deleted == 0
        assert "directory" in str(failure.value) and "chmod" in str(failure.value)
        assert db.read_bytes() == before


@pytest.mark.parametrize("uid", [0, os.geteuid(), 12345])
def test_sticky_directory_cannot_adopt_another_users_sidecar(uid):
    parent = _directory_info(0o1777, 0)
    entry = os.stat_result((stat.S_IFREG | 0o600, 1, 1, 1, uid, 1, 0, 0, 0, 0))
    if uid in (0, os.geteuid()):
        retention._check_sticky_entry_owner(Path("/tmp/db-wal"), parent, entry)
    else:
        with pytest.raises(ValueError, match="Untrusted entry owner"):
            retention._check_sticky_entry_owner(Path("/tmp/db-wal"), parent, entry)


def _process_launch_violations(source):
    """Closed canonical launch contract, including references/import aliases.

    Module/function aliases and reflective module use cannot hide extra calls.
    Only Popen type annotations may reference a launcher without calling it.
    """
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    modules = {"os", "subprocess", "asyncio", "sys", "ctypes"}
    tracked_names = modules | {"libc"}
    forbidden_imports = {"multiprocessing", "pty", "importlib", "concurrent", "webbrowser"}
    issues = []
    # Only the existing Landlock setup may use ctypes/libc. Pin arguments too:
    # allowing arbitrary syscall numbers would permit execve through libc.
    reviewed_native_calls = {
        ast.dump(ast.parse(expression, mode="eval").body)
        for expression in (
            "ctypes.CDLL(None, use_errno=True)",
            "ctypes.c_uint64((1 << 1) | sum(1 << bit for bit in range(4, 15)))",
            "ctypes.byref(access)",
            "ctypes.sizeof(access)",
            "libc.syscall(444, None, 0, 1)",
            "libc.syscall(444, ctypes.byref(access), ctypes.sizeof(access), 0)",
            "libc.prctl(38, 1, 0, 0, 0)",
            "libc.syscall(446, fd, 0)",
        )
    }
    seen_native_calls = set()
    reviewed = {
        "Popen": (
            "from nexura_runtime.retention import _serve_database; _serve_database()",
            {"stdin", "stdout", "stderr", "text", "close_fds", "cwd", "env"},
        ),
        "run": (
            "from nexura_runtime.retention import _inspect_export_worker; _inspect_export_worker()",
            {"input", "capture_output", "text", "timeout", "check", "close_fds", "cwd", "env"},
        ),
    }
    launches = []

    def literal(node, expected):
        try:
            value = ast.literal_eval(node)
            return type(value) is type(expected) and value == expected
        except (ValueError, TypeError):
            return False

    def process_api(module, attribute):
        if module == "subprocess":
            return attribute not in {"PIPE", "DEVNULL", "TimeoutExpired", "SubprocessError"}
        if module == "os":
            return attribute in {
                "system",
                "popen",
                "startfile",
                "fork",
                "forkpty",
                "vfork",
            } or attribute.lstrip("_").startswith(("exec", "spawn", "posix_spawn"))
        return module == "asyncio" and (
            attribute == "subprocess" or attribute.startswith("create_subprocess_")
        )

    def attribute_root(node):
        while isinstance(node, (ast.Attribute, ast.Call)):
            node = node.value if isinstance(node, ast.Attribute) else node.func
        return node.id if isinstance(node, ast.Name) else None

    def landlock_scope(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return isinstance(node, ast.FunctionDef) and node.name == "_deny_filesystem_writes"
        return False

    def libc_assignment(node):
        return (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "libc"
            and ast.dump(node.value)
            == ast.dump(ast.parse("ctypes.CDLL(None, use_errno=True)", mode="eval").body)
            and landlock_scope(node)
        )

    def annotation(node):
        while node in parents:
            parent = parents[node]
            if isinstance(parent, (ast.arg, ast.AnnAssign)) and parent.annotation is node:
                return True
            if (
                isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                and parent.returns is node
            ):
                return True
            if not isinstance(parent, (ast.Subscript, ast.Tuple, ast.BinOp, ast.Attribute)):
                return False
            node = parent
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                root = item.name.split(".")[0]
                binding = item.asname or root
                if (
                    (root in modules and (item.asname or item.name != root))
                    or (binding in tracked_names and binding != item.name)
                    or root in forbidden_imports | {"libc"}
                ):
                    issues.append("unreviewed module/alias import")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in tracked_names | forbidden_imports or any(
                (item.asname or item.name) in tracked_names for item in node.names
            ):
                issues.append("unreviewed function/alias import")
        elif isinstance(node, ast.Name) and node.id in tracked_names:
            parent = parents.get(node)
            if not (
                isinstance(node.ctx, ast.Load)
                and isinstance(parent, ast.Attribute)
                and parent.value is node
            ) and not (node.id == "libc" and libc_assignment(parent)):
                issues.append("aliased or reflective process module")
        elif (isinstance(node, ast.arg) and node.arg in tracked_names) or (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in tracked_names
        ):
            issues.append("shadowed process module")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            issues.append("reflective module import")
        if isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and attribute_root(value) == "sys"
                and value.attr == "modules"
            ) or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in {"globals", "locals", "vars"}
            ):
                issues.append("reflective module lookup")
        if not isinstance(node, ast.Attribute):
            continue
        module = attribute_root(node)
        if (
            module == "sys"
            and node.attr == "get"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "modules"
        ):
            issues.append("reflective module lookup")
        if node.attr in {"subprocess_exec", "subprocess_shell"} or node.attr.startswith(
            "create_subprocess_"
        ):
            issues.append("unreviewed async process API")
        if module in {"ctypes", "libc"}:
            parent = parents.get(node)
            signature = ast.dump(parent) if isinstance(parent, ast.Call) else None
            if not (
                isinstance(node.value, ast.Name)
                and isinstance(parent, ast.Call)
                and parent.func is node
                and landlock_scope(node)
                and signature in reviewed_native_calls
                and signature not in seen_native_calls
                and (node.attr != "CDLL" or libc_assignment(parents.get(parent)))
            ):
                issues.append("unreviewed ctypes/libc use")
            seen_native_calls.add(signature)
            continue
        if not process_api(module, node.attr):
            continue
        parent = parents.get(node)
        if annotation(node) and module == "subprocess" and node.attr == "Popen":
            continue
        if not (isinstance(parent, ast.Call) and parent.func is node):
            issues.append("aliased process function")
            continue
        if (
            module != "subprocess"
            or not isinstance(node.value, ast.Name)
            or node.attr not in reviewed
        ):
            issues.append("unreviewed process launch")
            continue
        launches.append(node.attr)
        entry, expected_keywords = reviewed[node.attr]
        call = parent
        if len(call.args) != 1 or not isinstance(call.args[0], ast.List):
            issues.append("unreviewed argv")
            continue
        argv = call.args[0].elts
        if (
            len(argv) != 5
            or ast.dump(argv[0]) != ast.dump(ast.parse("sys.executable", mode="eval").body)
            or not all(
                literal(arg, value)
                for arg, value in zip(argv[1:], ["-I", "-B", "-c", entry], strict=False)
            )
        ):
            issues.append("unreviewed argv")
        keywords = {kw.arg: kw.value for kw in call.keywords}
        if set(keywords) != expected_keywords or len(keywords) != len(call.keywords):
            issues.append("unreviewed launch keywords")
            continue
        expected_values = {"cwd": "/", "env": {}, "close_fds": True, "text": True}
        if node.attr == "run":
            expected_values.update({"timeout": 35, "check": False, "capture_output": True})
        if not all(literal(keywords[key], value) for key, value in expected_values.items()):
            issues.append("unreviewed launch configuration")
        if node.attr == "Popen" and any(
            ast.unparse(keywords[key]) != value
            for key, value in {
                "stdin": "subprocess.PIPE",
                "stdout": "subprocess.PIPE",
                "stderr": "subprocess.DEVNULL",
            }.items()
        ):
            issues.append("unreviewed process pipes")
    if sorted(launches) != ["Popen", "run"]:
        issues.append("expected exactly the two reviewed launches")
    return issues


def test_worker_launches_are_isolated_constant_entries():
    assert not _process_launch_violations(Path(retention.__file__).read_text())


_VALID_LAUNCH_SOURCE = """
import os, sys, subprocess, asyncio
subprocess.Popen([sys.executable, "-I", "-B", "-c",
"from nexura_runtime.retention import _serve_database; _serve_database()"],
stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
text=True, close_fds=True, cwd="/", env={})
subprocess.run([sys.executable, "-I", "-B", "-c",
"from nexura_runtime.retention import _inspect_export_worker; _inspect_export_worker()"],
input="data", capture_output=True, text=True, timeout=35, check=False,
close_fds=True, cwd="/", env={})
"""


def test_launch_checker_accepts_only_its_reviewed_baseline():
    assert not _process_launch_violations(_VALID_LAUNCH_SOURCE)


@pytest.mark.parametrize(
    "extra",
    [
        "os.system('true')",
        "os.popen('true')",
        "os.execv('x', [])",
        "os.execl('x', 'x')",
        "os.spawnv(0, 'x', [])",
        "os.spawnlp(0, 'x', 'x')",
        "os.posix_spawn('x', [], {})",
        "os.posix_spawnp('x', [], {})",
        "os.fork()",
        "subprocess.call(['x'])",
        "subprocess.check_call(['x'])",
        "subprocess.check_output(['x'])",
        "subprocess.getoutput('x')",
        "subprocess.getstatusoutput('x')",
        "asyncio.create_subprocess_exec('x')",
        "asyncio.create_subprocess_shell('x')",
        "from subprocess import Popen as launch\nlaunch(['x'])",
        "from os import system as launch\nlaunch('x')",
        "from os import execv\nexecv('x', [])",
        "import subprocess as sp\nsp.run(['x'])",
        "import os as o\no.system('x')",
        "from asyncio import create_subprocess_exec as launch\nlaunch('x')",
        "launch = subprocess.run\nlaunch(['x'])",
        "launch = os.system\nlaunch('x')",
        "module = os\nmodule.system('x')",
        "getattr(os, 'system')('x')",
        "import pty\npty.spawn(['x'])",
        "import multiprocessing\nmultiprocessing.Process()",
    ],
)
def test_launch_checker_rejects_other_process_apis_and_aliases(extra):
    # Start with the TWO valid launches: cardinality alone cannot pass this test.
    assert _process_launch_violations(_VALID_LAUNCH_SOURCE + "\n" + extra)


@pytest.mark.parametrize(
    "keyword",
    ["shell=False", "shell=True", "executable='x'", "preexec_fn=None", "pass_fds=()", "**{}"],
)
def test_launch_checker_rejects_additional_keywords(keyword):
    source = _VALID_LAUNCH_SOURCE.replace('cwd="/", env={})', f'cwd="/", env={{}}, {keyword})', 1)
    assert _process_launch_violations(source)


@pytest.mark.parametrize(
    "name", sorted(name for name in dir(os) if name.startswith(("exec", "spawn", "posix_spawn")))
)
def test_launch_checker_rejects_every_available_os_exec_spawn_form(name):
    assert _process_launch_violations(_VALID_LAUNCH_SOURCE + f"\nos.{name}()")


def test_launch_checker_rejects_extra_fully_reviewed_launches():
    assert _process_launch_violations(_VALID_LAUNCH_SOURCE * 2)


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "target", ["absent.sqlite3", "file:/tmp/f.sqlite3", "postgresql://db/x", ":memory:"]
)
def test_invalid_database_is_an_error(tmp_path, target, dry_run):
    path = tmp_path / target if target == "absent.sqlite3" else target
    with pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=path, dry_run=dry_run)
    assert failure.value.report.errors
    assert failure.value.report.runs_deleted == failure.value.report.events_deleted == 0


@pytest.mark.parametrize("mode", ["wal", "delete"])
def test_read_only_preview_preserves_all_database_bytes(tmp_path, mode):
    db = tmp_path / "service.sqlite3"
    service = StudioStore(db)
    if mode == "delete":
        service._close_sync()
        producer = sqlite3.connect(db)
        assert producer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    else:
        producer = service._conn
    try:
        paths = [db, *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
        before = {path: path.read_bytes() if path.exists() else None for path in paths}
        report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
        assert report.dry_run and not report.errors
        assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    finally:
        producer.close()


@pytest.mark.parametrize("mode", ["wal", "delete"])
def test_preexisting_journal_is_never_read_or_recovered(tmp_path, mode):
    db = tmp_path / "service.sqlite3"
    service = StudioStore(db)
    service._close_sync()
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.close()
    journal = Path(str(db) + "-journal")
    journal.write_bytes(bytes.fromhex("d9d505f920a163d7") + bytes(1024))
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    for dry_run in (True, False):
        with pytest.raises(retention.RetentionError) as failure:
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=dry_run)
        assert "journal" in str(failure.value)
        assert failure.value.report.errors and failure.value.report.runs_deleted == 0
        assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_dry_run_with_real_hot_journal_preserves_every_byte(tmp_path):
    db = tmp_path / "hot.sqlite3"
    # Force a real rollback journal and dirty cache spill, then lose the writer
    # without SQLite cleanup. Recovery would write the main DB and remove journal.
    code = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('CREATE TABLE payload(value BLOB)')
c.execute('INSERT INTO payload VALUES (zeroblob(1048576))')
c.commit()
c.execute('PRAGMA cache_size=1')
c.execute('BEGIN IMMEDIATE')
c.execute("UPDATE payload SET value=randomblob(1048576)")
os._exit(0)
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(db)], check=False)
    assert result.returncode == 0
    journal = Path(str(db) + "-journal")
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
    assert "journal" in str(failure.value) and failure.value.report.errors
    assert failure.value.report.runs_deleted == failure.value.report.events_deleted == 0
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_standalone_rollback_journal_actor(tmp_path):
    script = Path(__file__).with_name("run_round7_rollback_journal_paths.py")
    result = subprocess.run(
        [sys.executable, "-B", str(script), str(tmp_path / "standalone")],
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
        umask=0o077,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["swapped"]
    assert evidence["errors"] and evidence["runs_deleted"] == 0
    assert not evidence["foreign_changed"]


def _inject_worker(monkeypatch, code):
    popen = retention.subprocess.Popen

    def launch(argv, *args, **kwargs):
        if any("_serve_database" in part for part in argv):
            argv = list(argv)
            argv[4] = "import nexura_runtime.retention as retention\n" + code + "\n" + argv[4]
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(retention.subprocess, "Popen", launch)


@pytest.mark.parametrize("phase", ["write", "commit"])
def test_missing_rollback_journal_descriptor_prevents_commit(tmp_path, monkeypatch, phase):
    db = tmp_path / "service.sqlite3"
    service = StudioStore(db)
    with service._conn:
        service._conn.execute(
            "INSERT INTO nexura_studio_runs "
            "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
            "created_at,updated_at) VALUES ('r','h','{}','completed','{}','{}',0,0)"
        )
    service._close_sync()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    marker = tmp_path / "unverified-commit"
    _inject_worker(
        monkeypatch,
        f"phase = {phase!r}\njournal_name = {str(db) + '-journal'!r}\nmarker = {str(marker)!r}\n"
        + """
import json, os
execute = retention._RetentionConnection.execute
commit = retention._RetentionConnection.commit
probe = retention._writable_file_descriptors
loads = json.loads
unprovable = False
deleted = False
def tracked_execute(self, statement, parameters=()):
    global deleted, unprovable
    cursor = execute(self, statement, parameters)
    if statement.startswith('DELETE '):
        deleted = True
        if phase == 'write':
            unprovable = True
    return cursor
def tracked_loads(value, *args, **kwargs):
    global unprovable
    request = loads(value, *args, **kwargs)
    if phase == 'commit' and deleted and request.get('operation') == 'commit':
        unprovable = True
    return request
def tracked_probe():
    descriptors = probe()
    if unprovable:
        info = os.stat(journal_name)
        identity = (info.st_dev, info.st_ino)
        return {fd: item for fd, item in descriptors.items() if item != identity}
    return descriptors
def tracked_commit(self):
    if unprovable:
        with open(marker, 'w') as out:
            out.write('unverified COMMIT')
    return commit(self)
retention._RetentionConnection.execute = tracked_execute
retention._RetentionConnection.commit = tracked_commit
retention._writable_file_descriptors = tracked_probe
json.loads = tracked_loads
""",
    )
    with pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
    assert "journal descriptor" in str(failure.value)
    assert failure.value.report.errors
    assert failure.value.report.runs_deleted == failure.value.report.events_deleted == 0
    assert not marker.exists()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("r",)]
    finally:
        conn.close()


def test_preview_refuses_missing_landlock_enforcement(tmp_path, monkeypatch):
    db = tmp_path / "service.sqlite3"
    service = StudioStore(db)
    paths = [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]
    before = {path: path.read_bytes() for path in paths}
    _inject_worker(
        monkeypatch,
        """
class NoTruncateProtection:
    def syscall(self, *args):
        return 2  # Landlock ABI 2 cannot deny truncate.
retention.ctypes.CDLL = lambda *args, **kwargs: NoTruncateProtection()
""",
    )
    try:
        with pytest.raises(retention.RetentionError) as failure:
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
        assert "Landlock ABI >= 3" in str(failure.value)
        assert failure.value.report.errors and failure.value.report.runs_deleted == 0
        assert {path: path.read_bytes() for path in paths} == before
    finally:
        service._close_sync()


@pytest.mark.parametrize("apply", [True, False])
@pytest.mark.parametrize("target", ["absent.sqlite3", "postgresql://db/x"])
def test_invalid_cli_target_never_reports_applied(tmp_path, capsys, target, apply):
    path = str(tmp_path / target) if target == "absent.sqlite3" else target
    code = main(
        ["retention", "--older-than-days", "30", "--db", path, *(["--apply"] if apply else [])]
    )
    assert code == 1
    output = capsys.readouterr().out
    assert "Error:" in output and "[APPLIED]" not in output
