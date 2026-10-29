"""Local demo files; action values belong to the approval database and fake ledger."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

WORKSPACE = "crossframework-demo"


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Append and sync before returning; failures prevent further dispatch."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(json.dumps({"timestamp": time.time(), **record}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
