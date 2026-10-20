"""Shared pytest setup.

The shared-profile startup refusal (P9) is covered by tests/test_orgs_and_secrets.py.

``_local_client_helper`` points in-process test clients at 127.0.0.1 and sends the local
server's per-session token the way Studio does (P28 step 7 review, R1-R3). The repository's
top-level ``conftest.py`` installs the same helper for tests outside ``tests/``.

``_contain_tempfile_dirs`` sends ``tempfile.mkdtemp()`` directories into pytest's own temporary
directory, which pytest prunes to the last three runs. Several tests never removed theirs, and
on a RAM-backed /tmp the leftovers filled it and crashed the release gate.
"""

import importlib.util
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")


def _local_client_helper():  # type: ignore[no-untyped-def]
    name = "nexura_tests_local_client_helper"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("_local_client_helper.py")
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


_local_client_helper().install()


@pytest.fixture(scope="session", autouse=True)
def _contain_tempfile_dirs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    previous = tempfile.tempdir
    tempfile.tempdir = str(tmp_path_factory.mktemp("tempfile"))
    try:
        yield
    finally:
        tempfile.tempdir = previous
