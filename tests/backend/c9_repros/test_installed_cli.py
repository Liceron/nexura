"""Installed console script, offline, from a disposable consumer directory."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from nexura_runtime.studio.store import StudioStore


def test_installed_cli_requires_apply(tmp_path):
    db = tmp_path / "consumer.sqlite3"
    store = StudioStore(db)
    store._close_sync()
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        with conn:
            conn.execute(
                "INSERT INTO nexura_studio_runs (run_id,spec_hash,spec_json,status,"
                "input_state_json,current_state_json,created_at,updated_at) "
                "VALUES ('cli','h','{}','completed','{}','{}',?,?)",
                (time.time() - 60 * 86400,) * 2,
            )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        command = [
            str(Path(sys.executable).parent / "nexura-runtime"),
            "retention",
            "--older-than-days",
            "30",
            "--kinds",
            "runs",
            "--db",
            str(db),
        ]
        preview = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
        )
        assert preview.returncode == 0, preview.stderr
        assert "DRY RUN" in preview.stdout and "Runs: 1 run records" in preview.stdout
        assert conn.execute("SELECT COUNT(*) FROM nexura_studio_runs").fetchone() == (1,)
        applied = subprocess.run(
            [*command, "--apply"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
        )
        assert applied.returncode == 0, applied.stderr
        assert "APPLIED" in applied.stdout and "Runs: 1 run records" in applied.stdout
        assert conn.execute("SELECT COUNT(*) FROM nexura_studio_runs").fetchone() == (0,)
        probe = tmp_path / "import_probe.py"
        probe.write_text(
            "import json, sys, nexura_runtime\n"
            "print(json.dumps({'executable': sys.executable, "
            "'package': nexura_runtime.__file__}))\n"
        )
        imported = subprocess.run(
            [sys.executable, str(probe)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert imported.returncode == 0, imported.stderr
        evidence = {
            "consumer_cwd": str(tmp_path),
            "import": json.loads(imported.stdout),
            "preview": preview.stdout,
            "apply": applied.stdout,
        }
        (tmp_path / "installed-cli-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        conn.close()
