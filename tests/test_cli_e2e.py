"""End-to-end tests for CLI execution, snapshot validation, Markdown reporting, and audit SQL generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cli import main
from src.snapshot.loader import SnapshotValidationError, load_snapshot


def test_cli_validate_passes_on_retail_demo_fixture() -> None:
    exit_code = main(["validate", "--snapshot-dir", "fixtures/retail_demo_run"])
    assert exit_code == 0


def test_cli_compare_e2e_generates_markdown_and_audit_sql(tmp_path: Path) -> None:
    report_path = tmp_path / "drift_report.md"
    sql_path = tmp_path / "audit_inserts.sql"
    run_id = "11111111-2222-3333-4444-555555555555"

    exit_code = main(
        [
            "compare",
            "--snapshot-dir",
            "fixtures/retail_demo_run",
            "--out",
            str(report_path),
            "--sql-out",
            str(sql_path),
            "--run-id",
            run_id,
        ]
    )
    assert exit_code == 0
    assert report_path.exists()
    assert sql_path.exists()

    md = report_path.read_text(encoding="utf-8")
    # Executive header & summary checks
    assert f"`{run_id}`" in md
    assert "Overall Cutover Gate:** **FAIL**" in md
    # Intentional drifts in retail_demo_run fixture must be detected across all 5 layers
    assert "COLUMN_MISSING_TARGET" in md
    assert "Orders.PromoChannelCode" in md
    assert "COLUMN_NULLABILITY_MISMATCH" in md
    assert "Customers.EmailAddress" in md
    assert "INDEX_MISSING" in md
    assert "IX_Orders_CustomerID_OrderDateUTC" in md
    assert "ROW_COUNT_DRIFT" in md
    assert "COLUMN_AGGREGATE_DRIFT" in md
    assert "TABLE_CHECKSUM_DRIFT" in md
    assert "ROUTINE_MISSING_TARGET" in md
    assert "usp_ArchiveOldOrders" in md
    assert "SECURITY_PUBLIC_GRANT" in md
    assert "SECURITY_EXCESSIVE_ALL_PRIVILEGES" in md

    sql = sql_path.read_text(encoding="utf-8")
    assert "BEGIN;" in sql
    assert "INSERT INTO migration_audit.migration_run" in sql
    assert "INSERT INTO migration_audit.check_result" in sql
    assert f"'{run_id}'::uuid" in sql
    assert "COMMIT;" in sql


def test_cli_strict_mode_exits_nonzero_when_fail_findings_exist(tmp_path: Path) -> None:
    report_path = tmp_path / "strict_report.md"
    exit_code = main(
        [
            "compare",
            "--snapshot-dir",
            "fixtures/retail_demo_run",
            "--out",
            str(report_path),
            "--strict",
        ]
    )
    assert exit_code == 1


def test_cli_strict_mode_exits_zero_on_zero_drift_snapshot_pair(tmp_path: Path) -> None:
    snap_root = tmp_path / "clean_run"
    (snap_root / "mssql").mkdir(parents=True)
    (snap_root / "pg").mkdir(parents=True)

    mssql_snap = {
        "engine": "mssql",
        "server_name": "sql-src-01",
        "database_name": "clean_demo",
        "schema_name": "dbo",
        "captured_at": "2026-02-15T00:00:00Z",
        "tables": [
            {
                "name": "Products",
                "columns": [
                    {"name": "ProductID", "data_type": "bigint", "is_nullable": False, "ordinal_position": 1},
                    {"name": "SKUCode", "data_type": "varchar", "max_length": 64, "is_nullable": False, "ordinal_position": 2},
                ],
                "constraints": [{"name": "PK_Products", "constraint_type": "PRIMARY KEY", "columns": ["ProductID"]}],
                "indexes": [{"name": "PK_Products", "columns": ["ProductID"], "is_unique": True, "is_primary": True}],
            }
        ],
        "data_stats": [
            {
                "table_name": "Products",
                "row_count": 10,
                "aggregate_checksum": "00ff11ee",
                "column_aggregates": [
                    {"column_name": "ProductID", "sum_value": "55", "min_value": "1", "max_value": "10", "null_count": 0}
                ],
            }
        ],
    }
    pg_snap = {
        "engine": "postgresql",
        "server_name": "pg-target-01",
        "database_name": "clean_demo",
        "schema_name": "public",
        "captured_at": "2026-02-15T00:00:02Z",
        "tables": [
            {
                "name": "products",
                "columns": [
                    {"name": "product_id", "data_type": "bigint", "is_nullable": False, "ordinal_position": 1},
                    {"name": "sku_code", "data_type": "character varying", "max_length": 64, "is_nullable": False, "ordinal_position": 2},
                ],
                "constraints": [{"name": "products_pkey", "constraint_type": "PRIMARY KEY", "columns": ["product_id"]}],
                "indexes": [{"name": "products_pkey", "columns": ["product_id"], "is_unique": True, "is_primary": True}],
            }
        ],
        "data_stats": [
            {
                "table_name": "products",
                "row_count": 10,
                "aggregate_checksum": "00ff11ee",
                "column_aggregates": [
                    {"column_name": "product_id", "sum_value": "55", "min_value": "1", "max_value": "10", "null_count": 0}
                ],
            }
        ],
    }

    (snap_root / "mssql" / "snapshot.json").write_text(json.dumps(mssql_snap), encoding="utf-8")
    (snap_root / "pg" / "snapshot.json").write_text(json.dumps(pg_snap), encoding="utf-8")

    report_path = tmp_path / "clean_report.md"
    exit_code = main(
        [
            "compare",
            "--snapshot-dir",
            str(snap_root),
            "--out",
            str(report_path),
            "--no-overrides",
            "--strict",
        ]
    )
    assert exit_code == 0
    assert "Zero drift detected." in report_path.read_text(encoding="utf-8")


def test_snapshot_loader_rejects_invalid_engine_and_negative_row_count(tmp_path: Path) -> None:
    bad_engine = tmp_path / "bad_engine.json"
    bad_engine.write_text(
        json.dumps(
            {
                "engine": "mysql",
                "server_name": "s1",
                "database_name": "db",
                "schema_name": "public",
                "captured_at": "2026-02-15T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SnapshotValidationError):
        load_snapshot(bad_engine)

    neg_rows = tmp_path / "neg_rows.json"
    neg_rows.write_text(
        json.dumps(
            {
                "engine": "postgresql",
                "server_name": "pg-target-01",
                "database_name": "db",
                "schema_name": "public",
                "captured_at": "2026-02-15T00:00:00Z",
                "data_stats": [{"table_name": "orders", "row_count": -5}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SnapshotValidationError):
        load_snapshot(neg_rows)
