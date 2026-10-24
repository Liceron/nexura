"""Standalone F1 check outside pytest: reports which opening path ran.

Run: uv run --frozen python -B <this file> <empty scratch dir>
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import nexura_runtime.retention as retention
from nexura_runtime.studio.store import StudioStore

base = Path(sys.argv[1])
base.mkdir(parents=True, exist_ok=False)
db = base / "selected.sqlite3"
s = StudioStore(db)
with s._conn:
    s._conn.execute(
        "INSERT INTO nexura_studio_runs "
        "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
        "created_at,updated_at) "
        "VALUES ('selected-run','r','{}','completed','{}','{}',?,?)",
        (time.time() - 100 * 86400,) * 2,
    )
s._close_sync()
c = sqlite3.connect(db)
c.execute("PRAGMA journal_mode=DELETE")
c.close()
other = base / "elsewhere"
other.mkdir()
foreign = other / "foreign.sqlite3"
c = sqlite3.connect(foreign)
c.execute("CREATE TABLE f(v)")
c.execute("INSERT INTO f VALUES ('precious')")
c.commit()
c.close()
second = other / "second-name.sqlite3"
os.link(foreign, second)
expected = second.read_bytes()
workers = []
orig_popen = retention.subprocess.Popen


def popen(args, *a, **k):
    workers.append(any("_serve_database" in p for p in args))
    return orig_popen(args, *a, **k)


retention.subprocess.Popen = popen
orig = retention._protection
swapped = []


def hook(*a, **k):
    if not swapped:
        os.rename(foreign, str(db) + "-journal")
        swapped.append(True)
    return orig(*a, **k)


retention._protection = hook
try:
    report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
except retention.RetentionError as e:
    report = e.report
after = second.read_bytes()
print(
    json.dumps(
        {
            "isolated_worker_used": any(workers),
            "swapped": bool(swapped),
            "errors": report.errors,
            "runs_deleted": report.runs_deleted,
            "foreign_changed": after != expected,
            "foreign_bytes": [len(expected), len(after)],
            "foreign_magic_after": after[:8].hex(),
            "journal_name_exists": os.path.exists(str(db) + "-journal"),
        },
        indent=1,
    )
)
