"""Standalone demonstration for Module B17: SQL Data Connector.

Runs 100% keylessly and offline:
  uv run python examples/backend/b17.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile

from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.integrations.sql import (
    CrossWorkspaceConnectionError,
    QueryTimeoutError,
    SqlConnectionConfig,
    SqlConnectionManager,
    SqlExecuteModule,
    SqlQueryModule,
    SqlReadOnlyViolationError,
    SqlSchemaInspectModule,
)


async def main() -> None:
    print("=== B17: SQL Data Connector Demonstration ===\n")

    # Create temporary database file for synthetic orders
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        db_path = tmp_db.name

    try:
        manager = SqlConnectionManager()
        # This script is the operator: it grants the human approval that SQL writes need
        # (in a workflow the engine sets this only after someone approves the step).
        ctx_workspace_a = ExecutionContext(
            workspace_id="ws_finance",
            run_id="run_b17_demo",
            node_id="sql_node",
            approval_granted=True,
        )
        ctx_workspace_b = ExecutionContext(
            workspace_id="ws_marketing",
            run_id="run_b17_demo_other",
            node_id="sql_node_other",
            approval_granted=True,
        )

        # 1. Setup disposable synthetic orders table via write connection
        print("1. Initializing synthetic orders database with sample records...")
        write_conn_cfg = SqlConnectionConfig(
            connection_id="orders_db_write",
            workspace_id="ws_finance",
            engine="sqlite",
            database_path=db_path,
            read_only=False,
            pool_size=3,
        )
        initial_schema_sql = """
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            item TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL
        );
        INSERT INTO orders (order_id, customer_name, item, amount, status) VALUES
            ('ORD-001', 'अरविन्द कुमार', 'Monitor 4K', 24999.00, 'delivered'),
            ('ORD-002', 'नेहा गुप्ता', 'Mechanical Keyboard', 4500.00, 'shipped'),
            ('ORD-003', 'John Doe', 'Wireless Mouse', 1250.00, 'processing'),
            ('ORD-004', 'प्रिया शर्मा', 'USB-C Dock', 8900.00, 'delivered');
        """
        manager.register_connection(write_conn_cfg, initial_sql=initial_schema_sql)

        # Register read-only connection endpoint for querying
        read_conn_cfg = SqlConnectionConfig(
            connection_id="orders_db_ro",
            workspace_id="ws_finance",
            engine="sqlite",
            database_path=db_path,
            read_only=True,
            statement_timeout_seconds=0.5,
            max_rows=10,
            pool_size=2,
        )
        manager.register_connection(read_conn_cfg)

        query_mod = SqlQueryModule(manager=manager)
        schema_mod = SqlSchemaInspectModule(manager=manager)
        exec_mod = SqlExecuteModule(manager=manager)

        # 2. Schema Inspection
        print("\n2. Inspecting table schema...")
        schema_res = await schema_mod.execute(
            ctx_workspace_a,
            {"connection_id": "orders_db_ro", "table_name": "orders"},
            {},
        )
        tables = schema_res.outputs["tables"]
        print(f"  Found {len(tables)} table(s). Table 'orders' columns:")
        for col in tables[0]["columns"]:
            pk_label = " [PK]" if col["primary_key"] else ""
            print(f"   - {col['name']} ({col['type']}){pk_label}")

        # 3. Parameterized Query with Injection-Looking Values
        print("\n3. Testing Parameterized Query with SQL Injection-Looking Input...")
        malicious_input = "' OR '1'='1"
        res_injection_test = await query_mod.execute(
            ctx_workspace_a,
            {
                "connection_id": "orders_db_ro",
                "query": "SELECT * FROM orders WHERE customer_name = ?",
                "parameters": [malicious_input],
            },
            {},
        )
        print(f"  Query returned {res_injection_test.outputs['row_count']} rows.")
        assert res_injection_test.outputs["row_count"] == 0
        print("  [SUCCESS] Injection input safely bound as literal string (0 rows returned).")

        # Legitimate parameterized query with Unicode Indic characters
        res_legit = await query_mod.execute(
            ctx_workspace_a,
            {
                "connection_id": "orders_db_ro",
                "query": "SELECT order_id, customer_name, amount, status "
                "FROM orders WHERE status = :st ORDER BY amount DESC",
                "parameters": {"st": "delivered"},
            },
            {},
        )
        print(f"\n  Delivered orders ({res_legit.outputs['row_count']} found):")
        for row in res_legit.outputs["rows"]:
            print(f"   - {row['order_id']}: {row['customer_name']} | ₹{row['amount']}")

        # 4. Security Enforcement: Database Read-Only Mode Blocks Writes & CTE Writes
        print("\n4. Verifying Read-Only Enforcement (Direct Writes & CTE Writes)...")
        try:
            await query_mod.execute(
                ctx_workspace_a,
                {
                    "connection_id": "orders_db_ro",
                    "query": "INSERT INTO orders VALUES "
                    "('ORD-999', 'Hacker', 'Bad Item', 0.0, 'cancelled')",
                },
                {},
            )
            print("  [ERROR] Write succeeded on read-only connection!")
        except SqlReadOnlyViolationError as exc:
            print(f"  [SUCCESS] Direct write blocked: {exc.message}")

        try:
            # CTE containing write attempt
            await query_mod.execute(
                ctx_workspace_a,
                {
                    "connection_id": "orders_db_ro",
                    "query": (
                        "WITH bad_cte AS (SELECT 'ORD-999' AS oid) "
                        "INSERT INTO orders (order_id, customer_name, item, amount, status) "
                        "SELECT oid, 'Hacker', 'Bad', 0.0, 'bad' FROM bad_cte"
                    ),
                },
                {},
            )
            print("  [ERROR] CTE write succeeded on read-only connection!")
        except SqlReadOnlyViolationError as exc:
            print(f"  [SUCCESS] CTE write blocked: {exc.message}")

        # 5. Cross-Workspace Isolation
        print("\n5. Verifying Cross-Workspace Access Isolation...")
        try:
            await query_mod.execute(
                ctx_workspace_b,
                {"connection_id": "orders_db_ro", "query": "SELECT * FROM orders"},
                {},
            )
            print("  [ERROR] Cross-workspace query allowed!")
        except CrossWorkspaceConnectionError as exc:
            print(f"  [SUCCESS] Cross-workspace connection rejected: {exc.message}")

        # 6. Slow Query Statement Timeout & Cancellation
        print("\n6. Verifying Query Timeout & Statement Cancellation...")
        try:
            # Run an exponential recursive CTE designed to exceed the 0.5s timeout
            await query_mod.execute(
                ctx_workspace_a,
                {
                    "connection_id": "orders_db_ro",
                    "query": (
                        "WITH RECURSIVE infinite_loop(n) AS ("
                        "VALUES(1) UNION ALL SELECT n + 1 FROM infinite_loop"
                        ") SELECT count(*) FROM infinite_loop"
                    ),
                },
                {},
            )
            print("  [ERROR] Infinite query was not cancelled!")
        except QueryTimeoutError as exc:
            print(f"  [SUCCESS] Slow query cancelled cleanly: {exc.message}")

        # 7. Connection Pool Integrity After Failure
        print("\n7. Verifying Pool Integrity & Connection Reclamation...")
        pool = await manager.get_pool("orders_db_ro", "ws_finance")
        initial_checked_out = pool.checked_out_count
        print(f"  Checked-out connections before query: {initial_checked_out}")

        # Execute deliberate bad query
        with contextlib.suppress(Exception):
            await query_mod.execute(
                ctx_workspace_a,
                {"connection_id": "orders_db_ro", "query": "SELECT * FROM non_existent_table"},
                {},
            )

        after_checked_out = pool.checked_out_count
        print(f"  Checked-out connections after failed query: {after_checked_out}")
        assert after_checked_out == initial_checked_out
        print("  [SUCCESS] Connection returned to pool after failure (zero pool leaks).")

        # 8. Modifying Operations via SqlExecuteModule
        print("\n8. Executing Gated Write Operation via SqlExecuteModule...")
        exec_res = await exec_mod.execute(
            ctx_workspace_a,
            {
                "connection_id": "orders_db_write",
                "statement": ("UPDATE orders SET status = ? WHERE order_id = ?"),
                "parameters": ["shipped", "ORD-003"],
            },
            {},
        )
        print(f"  Rows affected: {exec_res.outputs['rows_affected']}")

        # 9. CLI Execution via subprocess
        print("\n9. Executing SqlQueryModule via B01 CLI...")
        cli_payload = json.dumps(
            {
                "connection_id": "default_sqlite",
                "query": "SELECT * FROM sample_items ORDER BY price DESC",
            }
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "nexura_runtime.modules",
                "run",
                "nexura.sql.query",
                "--input",
                cli_payload,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        print(proc.stdout.strip())
        if proc.returncode == 0:
            print("\nCLI Execution: SUCCESS (code 0)")
        else:
            print(f"\nCLI Execution: FAILED (code {proc.returncode}): {proc.stderr}")

        print("\n=== B17 Example Completed Successfully ===")

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


if __name__ == "__main__":
    asyncio.run(main())
