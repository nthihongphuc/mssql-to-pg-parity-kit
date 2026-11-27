"""Unit tests for the strictly read-only live snapshot capture engine (src/snapshot/capturer.py)."""

from __future__ import annotations

import builtins
import json
import subprocess
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pytest

from src.cli import main, run_comparison
from src.snapshot.capturer import (
    MissingDriverError,
    PsqlSubprocessQueryRunner,
    PyodbcQueryRunner,
    SnapshotCaptureError,
    _PsycopgBaseQueryRunner,
    build_mssql_table_aggregate_sql,
    build_postgres_table_aggregate_sql,
    capture_mssql_snapshot,
    capture_postgres_snapshot,
    create_postgres_runner,
    enforce_mssql_readonly_conn_str,
    mssql_quote_ident,
    resolve_dsn,
    write_snapshot,
)
from src.snapshot.loader import load_name_overrides, load_snapshot, load_snapshot_pair, load_type_map
from src.snapshot.models import CheckStatus, ColumnSchema, TableSchema
from src.snapshot.queries import (
    MSSQL_QUERIES,
    POSTGRES_QUERIES,
    SnapshotQuery,
    UnsafeSnapshotQueryError,
    validate_read_only_sql,
)


class MockQueryRunner:
    """Deterministic in-memory QueryRunner recording every executed query."""

    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self._responses = responses
        self.calls: list[tuple[Optional[str], str, Optional[dict[str, Any]]]] = []

    def fetch_all(
        self,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        *,
        query_name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((query_name, sql, params))
        if query_name not in self._responses:
            raise KeyError(f"Unexpected query_name in MockQueryRunner: {query_name!r}")
        return list(self._responses[query_name])


def _pg_clean_mock_responses() -> dict[str, list[dict[str, Any]]]:
    return {
        "pg_tables_and_columns": [
            {
                "schema_name": "public",
                "table_name": "customers",
                "column_name": "customer_id",
                "data_type": "bigint",
                "max_length": None,
                "numeric_precision": None,
                "numeric_scale": None,
                "is_nullable": False,
                "ordinal_position": 1,
                "is_identity": True,
                "default_value": None,
            },
            {
                "schema_name": "public",
                "table_name": "customers",
                "column_name": "email_address",
                "data_type": "character varying",
                "max_length": 255,
                "numeric_precision": None,
                "numeric_scale": None,
                "is_nullable": False,
                "ordinal_position": 2,
                "is_identity": False,
                "default_value": None,
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "column_name": "order_id",
                "data_type": "bigint",
                "max_length": None,
                "numeric_precision": None,
                "numeric_scale": None,
                "is_nullable": False,
                "ordinal_position": 1,
                "is_identity": True,
                "default_value": None,
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "column_name": "customer_id",
                "data_type": "bigint",
                "max_length": None,
                "numeric_precision": None,
                "numeric_scale": None,
                "is_nullable": False,
                "ordinal_position": 2,
                "is_identity": False,
                "default_value": None,
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "column_name": "total_amount",
                "data_type": "numeric",
                "max_length": None,
                "numeric_precision": 18,
                "numeric_scale": 2,
                "is_nullable": False,
                "ordinal_position": 3,
                "is_identity": False,
                "default_value": None,
            },
        ],
        "pg_constraints": [
            {
                "schema_name": "public",
                "table_name": "customers",
                "constraint_name": "customers_pkey",
                "constraint_type": "PRIMARY KEY",
                "key_ordinal": 1,
                "column_name": "customer_id",
                "referenced_schema": None,
                "referenced_table": None,
                "referenced_column": None,
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "constraint_name": "orders_pkey",
                "constraint_type": "PRIMARY KEY",
                "key_ordinal": 1,
                "column_name": "order_id",
                "referenced_schema": None,
                "referenced_table": None,
                "referenced_column": None,
            },
            # Intentionally unsorted key_ordinal rows to prove ordering is preserved
            {
                "schema_name": "public",
                "table_name": "orders",
                "constraint_name": "orders_customer_id_fkey",
                "constraint_type": "FOREIGN KEY",
                "key_ordinal": 1,
                "column_name": "customer_id",
                "referenced_schema": "public",
                "referenced_table": "customers",
                "referenced_column": "customer_id",
            },
        ],
        "pg_indexes": [
            {
                "schema_name": "public",
                "table_name": "customers",
                "index_name": "customers_pkey",
                "index_type": "BTREE",
                "is_unique": True,
                "is_primary": True,
                "filter_predicate": None,
                "key_ordinal": 1,
                "is_included_column": False,
                "column_name": "customer_id",
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "index_name": "orders_pkey",
                "index_type": "BTREE",
                "is_unique": True,
                "is_primary": True,
                "filter_predicate": None,
                "key_ordinal": 1,
                "is_included_column": False,
                "column_name": "order_id",
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "index_name": "idx_orders_customer_total",
                "index_type": "BTREE",
                "is_unique": False,
                "is_primary": False,
                "filter_predicate": "(total_amount > (0)::numeric)",
                "key_ordinal": 2,
                "is_included_column": True,
                "column_name": "total_amount",
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "index_name": "idx_orders_customer_total",
                "index_type": "BTREE",
                "is_unique": False,
                "is_primary": False,
                "filter_predicate": "(total_amount > (0)::numeric)",
                "key_ordinal": 1,
                "is_included_column": False,
                "column_name": "customer_id",
            },
        ],
        "pg_routines": [
            {
                "schema_name": "public",
                "routine_name": "fn_get_customer_orders",
                "routine_type": "FUNCTION",
                "parameter_count": 1,
                "return_type": "record",
                "definition_hash": "0123456789abcdef0123456789abcdef",
            }
        ],
        "pg_security_grants": [
            {
                "principal": "app_retail_rw",
                "object_type": "TABLE",
                "object_name": "customers",
                "privilege": "SELECT",
                "is_grantable": False,
                "is_superuser": False,
            },
            # Duplicate row from second grantor should be deduplicated
            {
                "principal": "app_retail_rw",
                "object_type": "TABLE",
                "object_name": "customers",
                "privilege": "SELECT",
                "is_grantable": False,
                "is_superuser": False,
            },
            {
                "principal": "app_retail_rw",
                "object_type": "TABLE",
                "object_name": "orders",
                "privilege": "SELECT",
                "is_grantable": False,
                "is_superuser": False,
            },
        ],
        "pg_row_counts": [
            {"schema_name": "public", "table_name": "customers", "estimated_row_count": 2},
            {"schema_name": "public", "table_name": "orders", "estimated_row_count": 3},
        ],
        "postgresql_table_aggregates:customers": [
            {
                "row_count": 2,
                "col_0_sum": "3",
                "col_0_min": "1",
                "col_0_max": "2",
                "col_0_nulls": 0,
            }
        ],
        "postgresql_table_aggregates:orders": [
            {
                "row_count": 3,
                "col_0_sum": "6",
                "col_0_min": "1",
                "col_0_max": "3",
                "col_0_nulls": 0,
                "col_1_sum": "4",
                "col_1_min": "1",
                "col_1_max": "2",
                "col_1_nulls": 0,
                "col_2_sum": "500.50",
                "col_2_min": "100.00",
                "col_2_max": "250.50",
                "col_2_nulls": 0,
            }
        ],
    }


def _mssql_clean_mock_responses() -> dict[str, list[dict[str, Any]]]:
    return {
        "mssql_tables_and_columns": [
            {
                "schema_name": "dbo",
                "table_name": "Customers",
                "column_name": "CustomerID",
                "data_type": "bigint",
                "max_length": 8,  # SQL Server reports byte length 8; capturer must strip for non-char types
                "numeric_precision": None,
                "numeric_scale": None,
                "is_nullable": 0,
                "ordinal_position": 1,
                "is_identity": 1,
                "default_value": None,
            },
            {
                "schema_name": "dbo",
                "table_name": "Customers",
                "column_name": "EmailAddress",
                "data_type": "nvarchar",
                "max_length": 255,
                "numeric_precision": None,
                "numeric_scale": None,
                "is_nullable": 0,
                "ordinal_position": 2,
                "is_identity": 0,
                "default_value": None,
            },
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "column_name": "OrderID",
                "data_type": "bigint",
                "max_length": 8,
                "numeric_precision": None,
                "numeric_scale": None,
                "is_nullable": 0,
                "ordinal_position": 1,
                "is_identity": 1,
                "default_value": None,
            },
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "column_name": "CustomerID",
                "data_type": "bigint",
                "max_length": 8,
                "numeric_precision": None,
                "numeric_scale": None,
                "is_nullable": 0,
                "ordinal_position": 2,
                "is_identity": 0,
                "default_value": None,
            },
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "column_name": "TotalAmount",
                "data_type": "decimal",
                "max_length": 9,
                "numeric_precision": 18,
                "numeric_scale": 2,
                "is_nullable": 0,
                "ordinal_position": 3,
                "is_identity": 0,
                "default_value": None,
            },
        ],
        "mssql_key_constraints": [
            {
                "schema_name": "dbo",
                "table_name": "Customers",
                "constraint_name": "PK_Customers",
                "constraint_type": "PRIMARY KEY",
                "key_ordinal": 1,
                "column_name": "CustomerID",
            },
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "constraint_name": "PK_Orders",
                "constraint_type": "PRIMARY KEY",
                "key_ordinal": 1,
                "column_name": "OrderID",
            },
        ],
        "mssql_foreign_keys": [
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "constraint_name": "FK_Orders_Customers",
                "key_ordinal": 1,
                "column_name": "CustomerID",
                "referenced_schema": "dbo",
                "referenced_table": "Customers",
                "referenced_column": "CustomerID",
            }
        ],
        "mssql_indexes": [
            {
                "schema_name": "dbo",
                "table_name": "Customers",
                "index_name": "PK_Customers",
                "index_type": "CLUSTERED",
                "is_unique": 1,
                "is_primary": 1,
                "filter_predicate": None,
                "key_ordinal": 1,
                "is_included_column": 0,
                "column_name": "CustomerID",
            },
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "index_name": "PK_Orders",
                "index_type": "CLUSTERED",
                "is_unique": 1,
                "is_primary": 1,
                "filter_predicate": None,
                "key_ordinal": 1,
                "is_included_column": 0,
                "column_name": "OrderID",
            },
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "index_name": "IX_Orders_CustomerID",
                "index_type": "NONCLUSTERED",
                "is_unique": 0,
                "is_primary": 0,
                "filter_predicate": "([TotalAmount]>(0))",
                "key_ordinal": 1,
                "is_included_column": 0,
                "column_name": "CustomerID",
            },
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "index_name": "IX_Orders_CustomerID",
                "index_type": "NONCLUSTERED",
                "is_unique": 0,
                "is_primary": 0,
                "filter_predicate": "([TotalAmount]>(0))",
                "key_ordinal": 0,
                "is_included_column": 1,
                "column_name": "TotalAmount",
            },
        ],
        "mssql_routines": [
            {
                "schema_name": "dbo",
                "routine_name": "fn_GetCustomerOrders",
                "routine_type": "FUNCTION",
                "parameter_count": 1,
                "definition_hash": "ABCDEF0123456789",
            }
        ],
        "mssql_security_grants": [
            {
                "principal": "app_retail_rw",
                "object_type": "TABLE",
                "object_name": "Customers",
                "privilege": "SELECT",
                "is_grantable": 0,
                "is_superuser": 0,
            },
            {
                "principal": "app_retail_rw",
                "object_type": "TABLE",
                "object_name": "Orders",
                "privilege": "SELECT",
                "is_grantable": 0,
                "is_superuser": 0,
            },
        ],
        "mssql_row_counts": [
            {"schema_name": "dbo", "table_name": "Customers", "row_count": 2},
            {"schema_name": "dbo", "table_name": "Orders", "row_count": 3},
        ],
        "mssql_table_aggregates:Customers": [
            {
                "row_count": 2,
                "col_0_sum": Decimal("3"),
                "col_0_min": Decimal("1"),
                "col_0_max": Decimal("2"),
                "col_0_nulls": 0,
            }
        ],
        "mssql_table_aggregates:Orders": [
            {
                "row_count": 3,
                "col_0_sum": Decimal("6"),
                "col_0_min": Decimal("1"),
                "col_0_max": Decimal("3"),
                "col_0_nulls": 0,
                "col_1_sum": Decimal("4"),
                "col_1_min": Decimal("1"),
                "col_1_max": Decimal("2"),
                "col_1_nulls": 0,
                "col_2_sum": Decimal("500.50"),
                "col_2_min": Decimal("100.00"),
                "col_2_max": Decimal("250.50"),
                "col_2_nulls": 0,
            }
        ],
    }


# =============================================================================
# 1. PostgreSQL & SQL Server Mock Capture + Exact JSON Round-Trip + Compare
# =============================================================================


def test_capture_postgres_and_mssql_exact_json_roundtrip_and_zero_drift_compare(tmp_path: Path) -> None:
    pg_runner = MockQueryRunner(_pg_clean_mock_responses())
    ms_runner = MockQueryRunner(_mssql_clean_mock_responses())

    pg_snap = capture_postgres_snapshot(
        dsn=None,
        schema="public",
        server_name="pg-target-01",
        database_name="retail_demo",
        include_data_stats=True,
        runner=pg_runner,
        captured_at="2026-03-01T12:00:02Z",
    )
    ms_snap = capture_mssql_snapshot(
        conn_str="Driver={ODBC Driver 18 for SQL Server};Server=127.0.0.1;Database=retail_demo;",
        schema="dbo",
        server_name="sql-src-01",
        database_name="retail_demo",
        include_data_stats=True,
        runner=ms_runner,
        captured_at="2026-03-01T12:00:00Z",
    )

    # Confirm EVERY registered catalog query ran
    executed_pg_names = {call[0] for call in pg_runner.calls}
    assert set(POSTGRES_QUERIES.keys()).issubset(executed_pg_names)
    executed_ms_names = {call[0] for call in ms_runner.calls}
    assert set(MSSQL_QUERIES.keys()).issubset(executed_ms_names)

    # Verify non-char SQL Server byte max_length was stripped while nvarchar(255) was preserved
    ms_cust = ms_snap.table_map()["Customers"].column_map()
    assert ms_cust["CustomerID"].max_length is None
    assert ms_cust["EmailAddress"].max_length == 255

    # Verify PostgreSQL INCLUDE index & filter predicate captured properly
    pg_orders_idx = {i.name: i for i in pg_snap.table_map()["orders"].indexes}
    assert pg_orders_idx["idx_orders_customer_total"].columns == ("customer_id",)
    assert pg_orders_idx["idx_orders_customer_total"].include_columns == ("total_amount",)
    assert pg_orders_idx["idx_orders_customer_total"].filter_predicate == "(total_amount > (0)::numeric)"

    # Verify duplicate security grant deduplicated
    assert len(pg_snap.security_grants) == 2

    # Write snapshots to nested run directory and assert pure LF UTF-8 JSON + exact round-trip
    run_dir = tmp_path / "captured_run"
    pg_file = write_snapshot(pg_snap, run_dir / "pg" / "snapshot.json")
    ms_file = write_snapshot(ms_snap, run_dir / "mssql" / "snapshot.json")

    raw_pg_bytes = pg_file.read_bytes()
    assert b"\r" not in raw_pg_bytes
    assert raw_pg_bytes.endswith(b"\n")

    reloaded_ms, reloaded_pg = load_snapshot_pair(run_dir)
    assert reloaded_pg == pg_snap
    assert reloaded_ms == ms_snap

    # Zero-drift 5-layer comparison across all layers
    type_map = load_type_map("config/type_map.yaml")
    overrides = load_name_overrides(None)
    report = run_comparison(reloaded_ms, reloaded_pg, type_map, overrides)
    assert report.overall_status == CheckStatus.PASS
    assert report.fail_count == 0
    assert report.warn_count == 0
    assert report.pass_count > 10


def test_capture_roundtrip_detects_mutated_drift_across_structure_and_data(tmp_path: Path) -> None:
    ms_runner = MockQueryRunner(_mssql_clean_mock_responses())
    mutated_pg_data = _pg_clean_mock_responses()

    # Introduce 3 realistic migration drifts on PG target:
    # 1. Loosen customers.email_address to NULLABLE
    mutated_pg_data["pg_tables_and_columns"][1]["is_nullable"] = True
    # 2. Drop secondary index on orders
    mutated_pg_data["pg_indexes"] = [
        r for r in mutated_pg_data["pg_indexes"] if r["index_name"] != "idx_orders_customer_total"
    ]
    # 3. Simulate missing row on orders (row_count 2 instead of 3, total_amount sum 350.50 vs 500.50)
    mutated_pg_data["postgresql_table_aggregates:orders"][0]["row_count"] = 2
    mutated_pg_data["postgresql_table_aggregates:orders"][0]["col_2_sum"] = "350.50"

    pg_runner = MockQueryRunner(mutated_pg_data)

    ms_snap = capture_mssql_snapshot(None, "dbo", "sql-src-01", "retail_demo", runner=ms_runner)
    pg_snap = capture_postgres_snapshot(None, "public", "pg-target-01", "retail_demo", runner=pg_runner)

    run_dir = tmp_path / "drifted_run"
    write_snapshot(ms_snap, run_dir / "mssql" / "snapshot.json")
    write_snapshot(pg_snap, run_dir / "pg" / "snapshot.json")

    reloaded_ms, reloaded_pg = load_snapshot_pair(run_dir)
    report = run_comparison(reloaded_ms, reloaded_pg, load_type_map("config/type_map.yaml"), load_name_overrides(None))

    fail_codes = {r.check_code for r in report.results if r.status == CheckStatus.FAIL}
    assert report.overall_status == CheckStatus.FAIL
    assert "COLUMN_NULLABILITY_MISMATCH" in fail_codes
    assert "INDEX_MISSING" in fail_codes
    assert "ROW_COUNT_DRIFT" in fail_codes
    assert "COLUMN_AGGREGATE_DRIFT" in fail_codes


# =============================================================================
# 2. Read-Only AST Guard Blocks Tampered Queries BEFORE Execution
# =============================================================================


@pytest.mark.parametrize(
    "tampered_sql",
    [
        "DELETE FROM public.orders WHERE order_id = 1",
        "UPDATE public.orders SET order_status = 'VOID'",
        "DROP TABLE public.orders",
        "SELECT 1; DROP TABLE public.orders",
        "WITH d AS (DELETE FROM public.orders RETURNING *) SELECT * FROM d",
        "SELECT * FROM public.orders FOR UPDATE",
    ],
)
def test_postgres_capture_blocks_tampered_mutating_query_before_execution(tampered_sql: str) -> None:
    runner = MockQueryRunner(_pg_clean_mock_responses())
    tampered_query = SnapshotQuery(
        name="pg_tables_and_columns",
        engine="postgresql",
        layer="structure",
        description="Tampered unsafe query",
        sql=tampered_sql,
    )
    with pytest.raises(UnsafeSnapshotQueryError):
        capture_postgres_snapshot(
            dsn=None,
            schema="public",
            server_name="pg-target-01",
            database_name="retail_demo",
            runner=runner,
            queries={"pg_tables_and_columns": tampered_query},
        )
    # Guard MUST fire BEFORE runner.fetch_all is ever invoked
    assert runner.calls == []


@pytest.mark.parametrize(
    "tampered_sql",
    [
        "DELETE FROM dbo.Orders",
        "TRUNCATE TABLE dbo.Orders",
        "SELECT * INTO dbo.BackupOrders FROM dbo.Orders",
        "GRANT SELECT ON dbo.Orders TO app_retail_rw",
        "SELECT 1; DROP TABLE dbo.Orders",
    ],
)
def test_mssql_capture_blocks_tampered_mutating_query_before_execution(tampered_sql: str) -> None:
    runner = MockQueryRunner(_mssql_clean_mock_responses())
    tampered_query = SnapshotQuery(
        name="mssql_tables_and_columns",
        engine="mssql",
        layer="structure",
        description="Tampered unsafe T-SQL query",
        sql=tampered_sql,
    )
    with pytest.raises(UnsafeSnapshotQueryError):
        capture_mssql_snapshot(
            conn_str=None,
            schema="dbo",
            server_name="sql-src-01",
            database_name="retail_demo",
            runner=runner,
            queries={"mssql_tables_and_columns": tampered_query},
        )
    assert runner.calls == []


# =============================================================================
# 3. Identifier Quoting Safety & Empty-Table None Aggregates & Skip Data Stats
# =============================================================================


def test_aggregate_query_builders_safely_quote_tricky_identifiers_and_reject_nul() -> None:
    pg_table = TableSchema(
        name='weird "order"; DROP TABLE x',
        schema_name='my "schema"',
        columns=(
            ColumnSchema(name='amount "usd"; --', data_type="numeric", is_nullable=True, ordinal_position=1),
            ColumnSchema(name="select", data_type="bigint", is_nullable=False, ordinal_position=2),
            ColumnSchema(name="notes", data_type="text", is_nullable=True, ordinal_position=3),
        ),
    )
    pg_sql, pg_cols = build_postgres_table_aggregate_sql(pg_table)
    assert pg_cols == ('amount "usd"; --', "select")
    assert '"my ""schema"""."weird ""order""; DROP TABLE x"' in pg_sql
    assert '"amount ""usd""; --"' in pg_sql
    assert '"select"' in pg_sql
    validate_read_only_sql(pg_sql, dialect="postgres")

    ms_table = TableSchema(
        name="Weird [Orders]; DROP TABLE x",
        schema_name="dbo",
        columns=(
            ColumnSchema(name="Amount [USD]", data_type="decimal", is_nullable=True, ordinal_position=1),
            ColumnSchema(name="Qty [Units]", data_type="int", is_nullable=False, ordinal_position=2),
        ),
    )
    ms_sql, ms_cols = build_mssql_table_aggregate_sql(ms_table)
    assert ms_cols == ("Amount [USD]", "Qty [Units]")
    assert "[dbo].[Weird [Orders]]; DROP TABLE x]" in ms_sql
    assert "[Amount [USD]]]" in ms_sql
    assert "CAST([Qty [Units]]] AS DECIMAL(38, 0))" in ms_sql
    validate_read_only_sql(ms_sql, dialect="tsql")

    # NUL byte identifiers must be rejected immediately
    bad_table = TableSchema(
        name="orders\x00evil",
        schema_name="public",
        columns=(ColumnSchema(name="id", data_type="bigint", is_nullable=False, ordinal_position=1),),
    )
    with pytest.raises(UnsafeSnapshotQueryError):
        build_postgres_table_aggregate_sql(bad_table)
    with pytest.raises(UnsafeSnapshotQueryError):
        mssql_quote_ident("col\x00bad")


def test_empty_table_preserves_none_aggregates_and_skip_data_stats_runs_zero_table_scans(tmp_path: Path) -> None:
    empty_pg = {
        "pg_tables_and_columns": [
            {
                "schema_name": "public",
                "table_name": "empty_ledger",
                "column_name": "amount",
                "data_type": "numeric",
                "max_length": None,
                "numeric_precision": 18,
                "numeric_scale": 2,
                "is_nullable": True,
                "ordinal_position": 1,
                "is_identity": False,
                "default_value": None,
            }
        ],
        "pg_constraints": [],
        "pg_indexes": [],
        "pg_routines": [],
        "pg_security_grants": [],
        "pg_row_counts": [{"schema_name": "public", "table_name": "empty_ledger", "estimated_row_count": 0}],
        "postgresql_table_aggregates:empty_ledger": [
            {
                "row_count": 0,
                "col_0_sum": None,
                "col_0_min": None,
                "col_0_max": None,
                "col_0_nulls": 0,
            }
        ],
    }

    snap = capture_postgres_snapshot(
        dsn=None,
        schema="public",
        server_name="pg-target-01",
        database_name="retail_demo",
        include_data_stats=True,
        runner=MockQueryRunner(empty_pg),
    )
    assert len(snap.data_stats) == 1
    agg = snap.data_stats[0].column_aggregates[0]
    assert snap.data_stats[0].row_count == 0
    assert agg.sum_value is None
    assert agg.min_value is None
    assert agg.max_value is None
    assert agg.null_count == 0

    # Round-trip through JSON MUST keep None as JSON null -> Python None (never "0" or "None")
    out_file = write_snapshot(snap, tmp_path / "empty_snap.json")
    reloaded = load_snapshot(out_file)
    reloaded_agg = reloaded.data_stats[0].column_aggregates[0]
    assert reloaded_agg.sum_value is None
    assert reloaded_agg.min_value is None
    assert reloaded_agg.max_value is None

    # --skip-data-stats (include_data_stats=False) skips pg_row_counts and table aggregate queries
    skip_runner = MockQueryRunner(empty_pg)
    meta_only = capture_postgres_snapshot(
        dsn=None,
        schema="public",
        server_name="pg-target-01",
        database_name="retail_demo",
        include_data_stats=False,
        runner=skip_runner,
    )
    assert meta_only.data_stats == ()
    called_names = [c[0] for c in skip_runner.calls]
    assert "pg_row_counts" not in called_names
    assert not any((name or "").startswith("postgresql_table_aggregates:") for name in called_names)


# =============================================================================
# 4. Read-Only Session Hardening, Driver Adapters & Missing Driver Errors
# =============================================================================


def test_enforce_mssql_readonly_conn_str_appends_and_overrides_readwrite() -> None:
    base = "Driver={ODBC Driver 18 for SQL Server};Server=127.0.0.1,1433;Database=retail_demo"
    enforced = enforce_mssql_readonly_conn_str(base)
    assert "ApplicationIntent=ReadOnly;" in enforced
    assert "Server=127.0.0.1,1433" in enforced

    mutated = (
        "Driver={ODBC Driver 18 for SQL Server};Server=127.0.0.1;ApplicationIntent=ReadWrite;"
        "Database=retail_demo;applicationintent=readwrite"
    )
    hardened = enforce_mssql_readonly_conn_str(mutated)
    assert "ReadWrite" not in hardened
    assert "readwrite" not in hardened
    assert hardened.lower().count("applicationintent=readonly") == 1

    with pytest.raises(ValueError):
        enforce_mssql_readonly_conn_str("   ")


def test_psycopg_base_runner_enforces_readonly_transaction_timeout_and_rollback() -> None:
    executed: list[tuple[str, Any]] = []

    class FakeCursor:
        def __init__(self) -> None:
            self.closed = False
            self.description = [("val",)]

        def execute(self, sql: str, params: Any = None) -> None:
            executed.append((sql, params))

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [(42,)]

        def close(self) -> None:
            self.closed = True

    class FakeConn:
        def __init__(self) -> None:
            self.closed = False
            self.cur = FakeCursor()

        def cursor(self) -> FakeCursor:
            return self.cur

        def close(self) -> None:
            self.closed = True

    conn = FakeConn()
    with _PsycopgBaseQueryRunner(conn, statement_timeout_ms=15_000) as runner:
        # Parameterized query binds $1 -> %(schema_name)s
        rows1 = runner.fetch_all("SELECT 1 WHERE n = $1 AND m = $1", {"schema_name": "public"})
        # Non-parameterized query with literal % in identifier must NOT pass params dict
        rows2 = runner.fetch_all('SELECT COUNT(*) FROM "public"."margin_%"', None)

    assert rows1 == [{"val": 42}]
    assert rows2 == [{"val": 42}]
    assert executed[0] == ("BEGIN TRANSACTION READ ONLY;", None)
    assert executed[1] == ("SET LOCAL statement_timeout = '15s';", None)
    assert executed[2] == ("SELECT 1 WHERE n = %(schema_name)s AND m = %(schema_name)s", {"schema_name": "public"})
    assert executed[3] == ('SELECT COUNT(*) FROM "public"."margin_%"', None)
    assert executed[-1] == ("ROLLBACK;", None)
    assert conn.cur.closed is True
    assert conn.closed is True


def test_psql_subprocess_runner_wraps_readonly_txn_and_never_leaks_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_cmd: list[str] = []
    captured_input: list[str] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        captured_input.append(kwargs["input"])
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout='[{"table_name":"orders","row_count":4}]\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    sentinel_token = "CHANGE_ME_PSQL_TEST_TOKEN"
    secret_dsn = f"postgresql://user:{sentinel_token}@127.0.0.1:5432/retail_demo"
    runner = PsqlSubprocessQueryRunner(secret_dsn, statement_timeout_ms=15_000, psql_binary="/usr/bin/psql")
    rows = runner.fetch_all(
        "SELECT table_name FROM pg_tables WHERE schemaname = $1",
        {"schema_name": "public"},
        query_name="pg_tables_test",
    )

    assert rows == [{"table_name": "orders", "row_count": 4}]
    assert "-v" in captured_cmd
    assert "schema_name=public" in captured_cmd
    assert "BEGIN TRANSACTION READ ONLY;" in captured_input[0]
    assert "SET LOCAL statement_timeout = '15s';" in captured_input[0]
    assert ":'schema_name'" in captured_input[0]
    assert "ROLLBACK;" in captured_input[0]

    # Failure path must raise SnapshotCaptureError WITHOUT leaking DSN
    def fake_fail(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="ERROR: permission denied")

    monkeypatch.setattr(subprocess, "run", fake_fail)
    with pytest.raises(SnapshotCaptureError) as exc_info:
        runner.fetch_all("SELECT 1", None, query_name="failing_q")
    assert sentinel_token not in str(exc_info.value)


def test_pyodbc_runner_enforces_readonly_isolation_and_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    connect_calls: list[tuple[str, dict[str, Any]]] = []
    executed: list[tuple[str, Any]] = []
    rolled_back = False

    class FakePyodbcCursor:
        description = [("cnt",)]

        def execute(self, sql: str, params: Any = None) -> None:
            executed.append((sql, params))

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [(7,)]

        def close(self) -> None:
            return None

    class FakePyodbcConn:
        def cursor(self) -> FakePyodbcCursor:
            return FakePyodbcCursor()

        def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True

        def close(self) -> None:
            return None

    fake_pyodbc = types.ModuleType("pyodbc")

    def fake_connect(conn_str: str, **kwargs: Any) -> FakePyodbcConn:
        connect_calls.append((conn_str, kwargs))
        return FakePyodbcConn()

    fake_pyodbc.connect = fake_connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)

    with PyodbcQueryRunner(
        "Driver={ODBC Driver 18 for SQL Server};Server=127.0.0.1;ApplicationIntent=ReadWrite;",
        lock_timeout_ms=5000,
    ) as runner:
        rows = runner.fetch_all("SELECT 1 WHERE s = @schema_name", {"schema_name": "dbo"})

    assert rows == [{"cnt": 7}]
    assert len(connect_calls) == 1
    assert "ApplicationIntent=ReadOnly;" in connect_calls[0][0]
    assert "ReadWrite" not in connect_calls[0][0]
    assert connect_calls[0][1] == {"readonly": True, "autocommit": False}
    assert executed[0] == ("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;", None)
    assert executed[1] == ("SET LOCK_TIMEOUT 5000;", None)
    assert executed[2] == ("SELECT 1 WHERE s = ?", ["dbo"])
    assert rolled_back is True


def test_missing_drivers_raise_clear_missing_driver_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {"psycopg", "psycopg2", "pyodbc"}:
            raise ImportError(f"Simulated missing module {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    monkeypatch.setattr("shutil.which", lambda _: None)

    with pytest.raises(MissingDriverError, match="No PostgreSQL client driver available"):
        create_postgres_runner("postgresql://127.0.0.1/retail_demo", driver="auto")

    with pytest.raises(MissingDriverError, match="pyodbc"):
        PyodbcQueryRunner("Driver={ODBC Driver 18 for SQL Server};Server=127.0.0.1;")


# =============================================================================
# 5. --dsn-env Resolution & CLI capture / validate Subcommands
# =============================================================================


def test_resolve_dsn_and_cli_capture_env_var_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel_token = "CHANGE_ME_CLI_TEST_TOKEN"
    secret_dsn = f"postgresql://readonly_audit:{sentinel_token}@127.0.0.1:5432/retail_demo"
    monkeypatch.setenv("PG_CAPTURE_DSN_TEST", secret_dsn)

    assert resolve_dsn(dsn_env="PG_CAPTURE_DSN_TEST") == secret_dsn
    with pytest.raises(ValueError):
        resolve_dsn(dsn="x", dsn_env="PG_CAPTURE_DSN_TEST")
    with pytest.raises(SnapshotCaptureError, match="UNSET_DSN_ENV_VAR"):
        resolve_dsn(dsn_env="UNSET_DSN_ENV_VAR")

    # Patch capture_postgres_snapshot inside src.cli to use our mock runner
    def fake_capture_pg(**kwargs: Any) -> Any:
        assert kwargs["dsn"] == secret_dsn
        assert kwargs["schema"] == "public"
        assert kwargs["include_data_stats"] is True
        return capture_postgres_snapshot(
            dsn=None,
            schema=kwargs["schema"],
            server_name=kwargs["server_name"],
            database_name=kwargs["database_name"],
            include_data_stats=kwargs["include_data_stats"],
            runner=MockQueryRunner(_pg_clean_mock_responses()),
            captured_at="2026-03-01T12:00:00Z",
        )

    monkeypatch.setattr("src.cli.capture_postgres_snapshot", fake_capture_pg)
    out_file = tmp_path / "snapshots" / "pg" / "snapshot.json"

    rc = main(
        [
            "capture",
            "--engine",
            "postgres",
            "--dsn-env",
            "PG_CAPTURE_DSN_TEST",
            "--schema",
            "public",
            "--server",
            "pg-target-01",
            "--database",
            "retail_demo",
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.exists()

    captured_io = capsys.readouterr()
    assert "[OK] Captured postgresql snapshot" in captured_io.out
    assert sentinel_token not in captured_io.out
    assert sentinel_token not in captured_io.err

    # Validate single captured file via CLI validate --snapshot-file
    val_rc = main(["validate", "--snapshot-file", str(out_file)])
    assert val_rc == 0
    val_io = capsys.readouterr()
    assert "[OK] Validated snapshot:" in val_io.out

    # Missing env var returns clean exit code 1 with error on stderr
    err_rc = main(
        [
            "capture",
            "--engine",
            "postgres",
            "--dsn-env",
            "NON_EXISTENT_CAPTURE_ENV",
            "--schema",
            "public",
            "--server",
            "pg-target-01",
            "--database",
            "retail_demo",
            "--out",
            str(tmp_path / "never_written.json"),
        ]
    )
    assert err_rc == 1
    err_io = capsys.readouterr()
    assert "[ERROR] Snapshot capture failed:" in err_io.err
    assert not (tmp_path / "never_written.json").exists()
