"""Repository-wide pytest setup for tests outside ``tests/`` (for example review repros).

Installs the same central in-process test-client helper as ``tests/conftest.py``: clients
reach the local server as ``http://127.0.0.1`` from a loopback peer and send its per-session
token the way Studio does (P28 step 7 review, R1-R3). See ``tests/_local_client_helper.py``.
"""

import importlib.util
import sys
from pathlib import Path

pytest_plugins = ["nexura_runtime.testing.pytest_backend"]

_NAME = "nexura_tests_local_client_helper"
if _NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _NAME, Path(__file__).parent / "tests" / "_local_client_helper.py"
    )
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_NAME] = _module
    _spec.loader.exec_module(_module)
sys.modules[_NAME].install()
