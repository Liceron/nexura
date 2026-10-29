# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Child process of the two-process budget test (``test_secrets_usage_contract.py``).

Arguments: a go-file path, threads, cap (USD), cost per reservation (USD). The child opens its
ledger, prints ``ready`` and waits (up to 120 s) until the go-file exists, so every child is
running before any reserves; a 10 ms pause after each grant keeps the children overlapping.
The ledger target comes from ``NEXURA_WP5_CHILD_TARGET`` (a SQLite path or a Postgres URL; never
printed) and ``NEXURA_WP5_CHILD_BACKEND``. Every thread reserves against one workspace cap until it
is refused. Prints one JSON line: reservations granted, and unexpected errors (their class names).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from nexura_agents.usage.errors import BudgetExceededError
from nexura_agents.usage.models import BudgetConfig


def _ledger() -> Any:
    target = os.environ["NEXURA_WP5_CHILD_TARGET"]
    if os.environ["NEXURA_WP5_CHILD_BACKEND"] == "sqlite":
        from nexura_agents.usage.ledger import UsageLedger

        return UsageLedger(target)
    from nexura_postgres import Database, PostgresUsageLedger

    return PostgresUsageLedger(Database(target))


def main() -> None:
    go, threads, cap, cost = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
    ledger = _ledger()
    print("ready", flush=True)
    deadline = time.time() + 120
    while not go.exists() and time.time() < deadline:
        time.sleep(0.005)
    config = BudgetConfig(max_cost_usd=Decimal(cap))
    granted = 0
    errors: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        nonlocal granted
        while True:
            try:
                ledger.reserve(
                    workspace_id="ws",
                    run_id=f"run-{os.getpid()}",
                    estimated_cost_usd=Decimal(cost),
                    workspace_config=config,
                )
            except BudgetExceededError:
                return
            except Exception as exc:  # reported by class only
                with lock:
                    errors.append(type(exc).__name__)
                return
            with lock:
                granted += 1
            # A short pause between reservations: without it the first child to see the
            # go-file (polled every 5 ms) can take the whole cap before the others start.
            time.sleep(0.01)

    workers = [threading.Thread(target=work) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(180)
    database = getattr(ledger, "database", None)
    if database is not None:
        database.close()
    print(json.dumps({"granted": granted, "errors": len(errors), "error_types": errors}))


if __name__ == "__main__":
    main()
