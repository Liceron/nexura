"""Negative AST self-tests promoted from the independent round 8 review.

Synthetic source is parsed, never executed. The platform.libc_ver non-spawn
control from the review is deliberately excluded from the negative cases.
"""

from pathlib import Path

import pytest
from test_round7_contract import _VALID_LAUNCH_SOURCE as BASE
from test_round7_contract import _process_launch_violations as check

from nexura_runtime import retention

# Keep the original review probes, including forms already rejected in round 7.
FORMS = {
    "importlib.import_module": (
        "import importlib\nimportlib.import_module('subprocess').call(['x'])"
    ),
    "dunder-import-attr": "__import__('os').system('x')",
    "sys.modules": "sys.modules['os'].system('x')",
    "sys.modules-get": "sys.modules.get('subprocess').run(['x'])",
    "globals": "globals()['os'].system('x')",
    "ctypes-libc-system": "import ctypes\nctypes.CDLL(None).system(b'x')",
    "ctypes-libc-execv": "import ctypes\nctypes.CDLL(None).execv(b'/bin/sh', None)",
    "asyncio.subprocess-submodule": "asyncio.subprocess.create_subprocess_exec('x')",
    "event-loop-subprocess_exec": "asyncio.get_event_loop().subprocess_exec(None, 'x')",
    "concurrent.futures-process-pool": (
        "import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor().submit(print)"
    ),
    "os.fork-then-exec-alias-free": "pid = os.fork()",
    "pty": "import pty\npty.spawn(['x'])",
    "multiprocessing": "import multiprocessing\nmultiprocessing.Process().start()",
    "multiprocessing-submodule-from": (
        "from multiprocessing import context\ncontext.Process().start()"
    ),
    "webbrowser": "import webbrowser\nwebbrowser.open('file:///x')",
    "os.posix_spawn-via-getattr-string": "getattr(os, 'posix_spawn')('x', [], {})",
}


@pytest.mark.parametrize("name", sorted(FORMS))
def test_guard_reports_form(name):
    assert check(BASE + "\n" + FORMS[name]), f"guard bypass: {name}"


@pytest.mark.parametrize(
    "extra",
    [
        "import importlib",
        "import importlib.util as util",
        "from importlib import import_module",
        "import concurrent.futures as futures",
        "from concurrent import futures",
        "from concurrent.futures import ProcessPoolExecutor",
        "import webbrowser as browser",
        "from webbrowser import open as launch",
        "sys.modules['unrelated']",
        "sys.modules.get('unrelated')",
        "globals()['unrelated']",
        "locals()['os'].system('x')",
        "vars()['os'].system('x')",
        "asyncio.subprocess.run(['x'])",
        "os.path.system('x')",
        "launch = asyncio.subprocess.create_subprocess_exec",
        "loop.subprocess_exec(None, 'x')",
        "loop.subprocess_shell(None, 'x')",
        "launch = loop.subprocess_shell",
        "custom.create_subprocess_exec('x')",
        "custom.create_subprocess_shell('x')",
        "custom.create_subprocess_unknown('x')",
        "import ctypes.util",
        "from ctypes import CDLL",
        "import ctypes as native",
        "import ctypes\nctypes.cdll.LoadLibrary(None).system(b'x')",
        "import ctypes\nctypes.CDLL(None, use_errno=True).system(b'x')",
        "import ctypes\nctypes.CDLL(None, use_errno=True).syscall(59, b'x', None, None)",
        "import ctypes\nctypes.pythonapi.PyRun_SimpleString(b'x')",
        "import ctypes\nctypes.CFUNCTYPE(None)(1)()",
    ],
)
def test_guard_rejects_reflective_imports_chains_and_native_calls(extra):
    # Preserve both canonical launches so cardinality cannot mask a bypass.
    assert check(BASE + "\n" + extra)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("libc.syscall(444, None, 0, 1)", "libc.system(b'x')"),
        ("libc.syscall(444, None, 0, 1)", "libc.execv(b'/bin/sh', None)"),
        ("libc.syscall(444, None, 0, 1)", "libc.syscall(59, b'/bin/sh', None, None)"),
        ("libc.prctl(38, 1, 0, 0, 0)", "libc.prctl(0, 1, 0, 0, 0)"),
        ("libc.syscall(446, fd, 0)", "getattr(libc, 'system')(b'x')"),
        ("libc.syscall(446, fd, 0)", "(launch := libc).system(b'x')"),
        ("libc.syscall(446, fd, 0)", "(launch := libc.system)(b'x')"),
        ("ctypes.CDLL(None, use_errno=True)", "ctypes.CDLL(None)"),
        ("ctypes.CDLL(None, use_errno=True)", "ctypes.CDLL('/tmp/lib.so', use_errno=True)"),
        ("libc = ctypes.CDLL", "other = ctypes.CDLL"),
        ("def _deny_filesystem_writes()", "def unreviewed_native_setup()"),
    ],
)
def test_guard_restricts_real_landlock_ctypes_uses(original, replacement):
    source = Path(retention.__file__).read_text()
    assert original in source
    assert check(source.replace(original, replacement, 1))
