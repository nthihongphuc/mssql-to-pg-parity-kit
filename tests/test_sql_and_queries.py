"""SQL AST, read-only query catalog, and PostgreSQL migration_audit schema alignment tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from src.report import render_audit_sql
from src.snapshot.models import (
    CheckResult,
    CheckStatus,
    ParityLayer,
    ParityReport,
    Severity,
    Snapshot,
)
from src.snapshot.queries import (
    ALL_SNAPSHOT_QUERIES,
    MSSQL_QUERIES,
    POSTGRES_QUERIES,
    SnapshotQuery,
    UnsafeSnapshotQueryError,
    validate_read_only_sql,
    validate_snapshot_query,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# =============================================================================
# 1. Read-Only Snapshot Query Catalog Tests (src/snapshot/queries.py)
# =============================================================================


@pytest.mark.parametrize("query", ALL_SNAPSHOT_QUERIES, ids=lambda q: q.name)
def test_snapshot_query_catalog_parses_cleanly_and_is_strictly_read_only(query: SnapshotQuery) -> None:
    """Every catalog query must parse with sqlglot (strict) and pass read-only AST guards."""
    validate_snapshot_query(query)

    # Secondary defense-in-depth: forbid write/DDL/DCL verbs as standalone keywords outside comments.
    upper_sql = query.sql.upper()
    for forbidden_kw in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE ", "ALTER ", "GRANT ", "REVOKE "):
        assert forbidden_kw not in upper_sql, f"Unexpected keyword {forbidden_kw!r} in {query.name}"


def test_snapshot_query_catalog_covers_required_mssql_and_postgres_system_catalogs() -> None:
    """Verify production catalog queries reference all required SQL Server & PostgreSQL metadata views."""
    mssql_blob = "\n".join(q.sql.lower() for q in MSSQL_QUERIES.values())
    for required_catalog in (
        "sys.tables",
        "sys.columns",
        "sys.indexes",
        "sys.index_columns",
        "sys.foreign_keys",
        "sys.objects",
        "sys.database_permissions",
        "sys.dm_db_partition_stats",
    ):
        assert required_catalog in mssql_blob, f"Missing SQL Server catalog: {required_catalog}"

    # Verify NVARCHAR UTF-16 byte-to-character length normalization preserving -1 for MAX
    assert "c.max_length / 2" in MSSQL_QUERIES["mssql_tables_and_columns"].sql.lower()

    pg_blob = "\n".join(q.sql.lower() for q in POSTGRES_QUERIES.values())
    for required_catalog in (
        "pg_class",
        "pg_attribute",
        "pg_constraint",
        "pg_index",
        "pg_proc",
        "information_schema.role_table_grants",
        "pg_stat_user_tables",
    ):
        assert required_catalog in pg_blob, f"Missing PostgreSQL catalog: {required_catalog}"

    # Verify PostgreSQL index query filters out invalid/unready CONCURRENTLY indexes
    assert "indisvalid" in POSTGRES_QUERIES["pg_indexes"].sql.lower()


@pytest.mark.parametrize(
    ("dialect", "unsafe_sql"),
    [
        ("postgres", "DELETE FROM public.orders WHERE order_id = 1"),
        ("postgres", "UPDATE public.orders SET order_status = 'VOID'"),
        ("postgres", "DROP TABLE public.orders"),
        ("postgres", "SELECT 1; DROP TABLE public.orders"),
        ("postgres", "WITH d AS (DELETE FROM public.orders RETURNING *) SELECT * FROM d"),
        ("postgres", "SELECT * FROM public.orders FOR UPDATE"),
        ("postgres", "SELECT * INTO public.backup_orders FROM public.orders"),
        ("tsql", "SELECT * INTO dbo.BackupOrders FROM dbo.Orders"),
        ("tsql", "TRUNCATE TABLE dbo.Orders"),
        ("tsql", "GRANT SELECT ON dbo.Orders TO app_retail_rw"),
    ],
)
def test_read_only_guard_rejects_mutating_stacked_and_locking_sql(dialect: str, unsafe_sql: str) -> None:
    with pytest.raises(UnsafeSnapshotQueryError):
        validate_read_only_sql(unsafe_sql, dialect=dialect)


# =============================================================================
# 2. Repository SQL Files Parse Cleanly with sqlglot
# =============================================================================


def test_migration_audit_schema_sql_parses_cleanly_as_postgres() -> None:
    sql_path = REPO_ROOT / "sql" / "migration_audit_schema.sql"
    content = sql_path.read_text(encoding="utf-8")
    stmts = [
        s
        for s in sqlglot.parse(content, read="postgres", error_level=sqlglot.ErrorLevel.RAISE)
        if s is not None and not isinstance(s, exp.Semicolon)
    ]
    assert len(stmts) >= 9
    assert not any(isinstance(s, exp.Command) for s in stmts)


def test_demo_mssql_init_sql_parses_cleanly_as_tsql() -> None:
    sql_path = REPO_ROOT / "demo" / "mssql_init.sql"
    content = sql_path.read_text(encoding="utf-8")
    batches = [b.strip() for b in re.split(r"(?im)^\s*GO\s*$", content) if b.strip()]
    parsed_count = 0
    for batch in batches:
        stmts = [
            s
            for s in sqlglot.parse(batch, read="tsql")
            if s is not None and not isinstance(s, exp.Semicolon)
        ]
        assert len(stmts) >= 1
        parsed_count += len(stmts)
    assert parsed_count >= 25


def test_demo_pg_init_sql_parses_cleanly_as_postgres() -> None:
    sql_path = REPO_ROOT / "demo" / "pg_init.sql"
    content = sql_path.read_text(encoding="utf-8")
    stmts = [
        s
        for s in sqlglot.parse(content, read="postgres")
        if s is not None and not isinstance(s, exp.Semicolon)
    ]
    # Only the anonymous DO $$ role bootstrap block falls back to Command; all DDL/DML/GRANT/SELECT parse to typed ASTs.
    command_stmts = [s for s in stmts if isinstance(s, exp.Command)]
    assert len(command_stmts) == 1
    assert str(command_stmts[0].this).upper() == "DO"
    assert len(stmts) >= 28


# =============================================================================
# 3. 100% Column Alignment & Quote Escaping Between report.py and DDL Schema
# =============================================================================


def _extract_ddl_table_spec(ddl_sql: str) -> dict[str, dict[str, set[str]]]:
    """Parse PostgreSQL CREATE TABLE statements via sqlglot to extract columns, required NOT NULLs, and FKs."""
    specs: dict[str, dict[str, set[str]]] = {}
    for stmt in sqlglot.parse(ddl_sql, read="postgres", error_level=sqlglot.ErrorLevel.RAISE):
        if not isinstance(stmt, exp.Create) or not isinstance(stmt.this, exp.Schema):
            continue
        table_expr = stmt.this.this
        if not isinstance(table_expr, exp.Table):
            continue
        full_name = f"{table_expr.db}.{table_expr.name}".lower()
        all_cols: set[str] = set()
        required_cols: set[str] = set()
        fk_cols: set[str] = set()

        for col_def in stmt.this.expressions:
            if not isinstance(col_def, exp.ColumnDef):
                continue
            col_name = col_def.name.lower()
            all_cols.add(col_name)
            col_type_sql = col_def.kind.sql(dialect="postgres").upper() if col_def.kind else ""
            is_serial = "SERIAL" in col_type_sql
            has_not_null = False
            has_default = False
            for constraint in col_def.constraints:
                kind = constraint.kind
                if isinstance(kind, exp.NotNullColumnConstraint) and not kind.args.get("allow_null"):
                    has_not_null = True
                if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                    has_not_null = True
                if isinstance(kind, exp.DefaultColumnConstraint):
                    has_default = True
                if isinstance(kind, exp.Reference):
                    fk_cols.add(col_name)
            if has_not_null and not has_default and not is_serial:
                required_cols.add(col_name)

        specs[full_name] = {
            "columns": all_cols,
            "required_not_null": required_cols,
            "fk_columns": fk_cols,
        }
    return specs


def test_render_audit_sql_matches_migration_audit_schema_100_percent_and_escapes_quotes() -> None:
    """Every INSERT column in render_audit_sql must align with migration_audit_schema.sql and escape quotes."""
    ddl_sql = (REPO_ROOT / "sql" / "migration_audit_schema.sql").read_text(encoding="utf-8")
    ddl_specs = _extract_ddl_table_spec(ddl_sql)

    assert "migration_audit.migration_run" in ddl_specs
    assert "migration_audit.check_result" in ddl_specs
    assert "run_id" in ddl_specs["migration_audit.check_result"]["fk_columns"]

    run_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    src_snap = Snapshot(
        engine="mssql",
        server_name="sql-src-01's",
        database_name="o'brien_retail",
        schema_name="dbo's",
        captured_at="2026-02-15T02:00:00Z",
    )
    tgt_snap = Snapshot(
        engine="postgresql",
        server_name="pg-target-01",
        database_name="o'brien_retail",
        schema_name="public",
        captured_at="2026-02-15T02:00:04Z",
    )
    report = ParityReport(
        run_id=run_id,
        source_snapshot=src_snap,
        target_snapshot=tgt_snap,
        generated_at="2026-02-15T02:05:00Z",
        results=[
            CheckResult(
                layer=ParityLayer.STRUCTURE,
                check_code="COLUMN_NULLABILITY_MISMATCH",
                status=CheckStatus.FAIL,
                severity=Severity.HIGH,
                object_type="COLUMN",
                source_object="Customers.O'BrienNote",
                target_object="customers.o'brien_note",
                source_value="NOT NULL DEFAULT 'O''Brien'\x00",
                target_value="NULL",
                message="Column 'O'BrienNote' loosened from NOT NULL to NULL.",
                remediation="ALTER TABLE \"public\".\"customers\" ALTER COLUMN \"o'brien_note\" SET NOT NULL;",
            ),
            CheckResult(
                layer=ParityLayer.DATA,
                check_code="DATA_STATS_MISSING",
                status=CheckStatus.SKIP,
                severity=Severity.LOW,
                object_type="TABLE",
                source_object="StagingLog",
                target_object="staging_log",
                source_value=None,
                target_value=None,
                message="Skipped empty staging stats.",
                remediation=None,
            ),
        ],
    )

    generated_sql = render_audit_sql(report)
    assert "\x00" not in generated_sql
    # Escaped single quote inside JSONB metadata literal (schema_name = "dbo's")
    assert "dbo''s" in generated_sql
    # Escaped single quote inside TEXT message/remediation literal
    assert "O''BrienNote" in generated_sql

    stmts = [
        s
        for s in sqlglot.parse(generated_sql, read="postgres", error_level=sqlglot.ErrorLevel.RAISE)
        if s is not None and not isinstance(s, exp.Semicolon)
    ]
    assert isinstance(stmts[0], exp.Transaction)
    assert isinstance(stmts[-1], exp.Commit)

    insert_stmts = [s for s in stmts if isinstance(s, exp.Insert)]
    assert len(insert_stmts) == 3  # 1 migration_run + 2 check_result rows

    parent_run_ids: set[str] = set()
    child_run_ids: list[str] = []

    for ins in insert_stmts:
        schema_expr = ins.this
        assert isinstance(schema_expr, exp.Schema)
        tbl = schema_expr.this
        assert isinstance(tbl, exp.Table)
        full_table = f"{tbl.db}.{tbl.name}".lower()
        assert full_table in ddl_specs

        inserted_cols = [c.name.lower() for c in schema_expr.expressions]
        spec = ddl_specs[full_table]

        # 1. Every inserted column must exist in migration_audit_schema.sql
        unknown_cols = set(inserted_cols) - spec["columns"]
        assert not unknown_cols, f"Unknown columns inserted into {full_table}: {unknown_cols}"

        # 2. Every NOT NULL column without default must be populated
        missing_required = spec["required_not_null"] - set(inserted_cols)
        assert not missing_required, f"Missing NOT NULL columns for {full_table}: {missing_required}"

        # 3. Value tuple length matches column list length & run_id links cleanly
        values_expr = ins.expression
        assert isinstance(values_expr, exp.Values)
        row_tuple = values_expr.expressions[0]
        assert len(row_tuple.expressions) == len(inserted_cols)

        run_id_idx = inserted_cols.index("run_id")
        run_id_cast = row_tuple.expressions[run_id_idx]
        assert isinstance(run_id_cast, exp.Cast)
        run_id_val = run_id_cast.this.name
        if full_table == "migration_audit.migration_run":
            parent_run_ids.add(run_id_val)
            # Verify counts sum to total_checks
            col_to_val = dict(zip(inserted_cols, row_tuple.expressions))
            total_c = int(col_to_val["total_checks"].name)
            sum_c = (
                int(col_to_val["pass_count"].name)
                + int(col_to_val["warn_count"].name)
                + int(col_to_val["fail_count"].name)
                + int(col_to_val["skip_count"].name)
            )
            assert total_c == sum_c == 2
        else:
            child_run_ids.append(run_id_val)

    assert parent_run_ids == {run_id}
    assert child_run_ids == [run_id, run_id]


def test_render_audit_sql_rejects_invalid_run_id() -> None:
    snap = Snapshot("mssql", "sql-src-01", "retail_demo", "dbo", "2026-02-15T00:00:00Z")
    pg_snap = Snapshot("postgresql", "pg-target-01", "retail_demo", "public", "2026-02-15T00:00:02Z")
    bad_report = ParityReport(
        run_id="not-a-valid-uuid'; DROP TABLE migration_audit.migration_run; --",
        source_snapshot=snap,
        target_snapshot=pg_snap,
        generated_at="2026-02-15T00:05:00Z",
    )
    with pytest.raises(ValueError):
        render_audit_sql(bad_report)
