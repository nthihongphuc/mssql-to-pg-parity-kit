"""Command-line interface for live snapshot capture and offline multi-layer MSSQL -> PostgreSQL parity."""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from src.comparators.data import compare_data
from src.comparators.routines import compare_routines
from src.comparators.security import compare_security
from src.comparators.structure import compare_structure
from src.report import render_audit_sql, render_markdown_report
from src.snapshot.capturer import (
    MissingDriverError,
    SnapshotCaptureError,
    capture_mssql_snapshot,
    capture_postgres_snapshot,
    resolve_dsn,
    write_snapshot,
)
from src.snapshot.loader import (
    load_name_overrides,
    load_snapshot,
    load_snapshot_pair,
    load_type_map,
)
from src.snapshot.models import CheckStatus, NameOverrides, ParityReport, Snapshot, TypeMapRule

DEFAULT_TYPE_MAP = Path("config/type_map.yaml")
DEFAULT_OVERRIDES = Path("config/name_overrides.yaml")


def run_comparison(
    source: Snapshot,
    target: Snapshot,
    type_map: dict[str, TypeMapRule],
    overrides: NameOverrides,
    run_id: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> ParityReport:
    """Execute all five parity verification layers against an immutable snapshot pair."""
    resolved_run_id = str(uuid.UUID(run_id)) if run_id else str(uuid.uuid4())
    resolved_ts = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Layers 1 & 2: Naming + Structure
    structure_out = compare_structure(source, target, type_map, overrides)

    # Layer 3: Data Parity
    data_results = compare_data(
        source=source,
        target=target,
        table_matches=structure_out.table_matches,
        column_matches_by_table=structure_out.column_matches_by_table,
        overrides=overrides,
    )

    # Layer 4: Routine Inventory Parity
    routine_results = compare_routines(source, target, overrides)

    # Layer 5: Security & Least-Privilege Parity
    security_results = compare_security(
        source=source,
        target=target,
        table_matches=structure_out.table_matches,
        overrides=overrides,
    )

    all_results = [
        *structure_out.results,
        *data_results,
        *routine_results,
        *security_results,
    ]

    return ParityReport(
        run_id=resolved_run_id,
        source_snapshot=source,
        target_snapshot=target,
        generated_at=resolved_ts,
        results=all_results,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mssql-to-pg-parity-kit",
        description="Offline immutable-snapshot parity & audit tool for SQL Server -> PostgreSQL migrations.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # compare subcommand
    cmp_parser = subparsers.add_parser(
        "compare",
        help="Compare frozen source (mssql) and target (pg) snapshots and emit Markdown + optional SQL audit trail.",
    )
    cmp_parser.add_argument(
        "--snapshot-dir",
        required=True,
        help="Directory containing mssql/snapshot.json and pg/snapshot.json.",
    )
    cmp_parser.add_argument(
        "--out",
        required=True,
        help="Output path for the Markdown drift report (e.g., out/report.md).",
    )
    cmp_parser.add_argument(
        "--sql-out",
        default=None,
        help="Optional output path for PostgreSQL migration_audit INSERT script (e.g., out/audit.sql).",
    )
    cmp_parser.add_argument(
        "--type-map",
        default=str(DEFAULT_TYPE_MAP),
        help="Path to SQL Server -> PostgreSQL type compatibility matrix YAML.",
    )
    cmp_parser.add_argument(
        "--overrides",
        default=None,
        help="Path to explicit name overrides YAML (defaults to config/name_overrides.yaml if present).",
    )
    cmp_parser.add_argument(
        "--no-overrides",
        action="store_true",
        help="Disable loading default name_overrides.yaml.",
    )
    cmp_parser.add_argument(
        "--run-id",
        default=None,
        help="Explicit UUID for deterministic audit runs.",
    )
    cmp_parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if any FAIL findings are detected (recommended for CI/CD cutover gates).",
    )

    # capture subcommand
    cap_parser = subparsers.add_parser(
        "capture",
        help="Capture a strictly read-only point-in-time snapshot JSON from live PostgreSQL or SQL Server.",
    )
    cap_parser.add_argument(
        "--engine",
        required=True,
        choices=["postgres", "postgresql", "mssql"],
        help="Source/target database engine to capture (postgres|mssql).",
    )
    dsn_group = cap_parser.add_mutually_exclusive_group(required=True)
    dsn_group.add_argument(
        "--dsn",
        default=None,
        help="Database connection DSN/ODBC string (prefer --dsn-env in production to avoid shell history exposure).",
    )
    dsn_group.add_argument(
        "--dsn-env",
        default=None,
        help="Environment variable name holding the connection DSN (e.g., PG_CAPTURE_DSN).",
    )
    cap_parser.add_argument(
        "--schema",
        default=None,
        help="Target schema to capture (defaults to 'public' for postgres, 'dbo' for mssql).",
    )
    cap_parser.add_argument(
        "--server",
        required=True,
        help="Logical server identifier recorded in snapshot metadata (e.g., pg-target-01).",
    )
    cap_parser.add_argument(
        "--database",
        required=True,
        help="Database name recorded in snapshot metadata (e.g., retail_demo).",
    )
    cap_parser.add_argument(
        "--out",
        required=True,
        help="Output path for the frozen snapshot JSON (e.g., snapshots/pg/snapshot.json).",
    )
    cap_parser.add_argument(
        "--skip-data-stats",
        action="store_true",
        help="Skip table row counts and numeric column aggregates (metadata-only capture, zero table scans).",
    )
    cap_parser.add_argument(
        "--driver",
        default="auto",
        choices=["auto", "psycopg", "psycopg2", "psql"],
        help="PostgreSQL client driver selection (default: auto -> psycopg, psycopg2, psql).",
    )
    cap_parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=15_000,
        help="PostgreSQL SET LOCAL statement_timeout in milliseconds (default: 15000).",
    )
    cap_parser.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=5_000,
        help="SQL Server SET LOCK_TIMEOUT in milliseconds (default: 5000).",
    )

    # validate subcommand
    val_parser = subparsers.add_parser(
        "validate",
        help="Validate immutable snapshot JSON schema integrity without running comparison.",
    )
    val_group = val_parser.add_mutually_exclusive_group(required=True)
    val_group.add_argument(
        "--snapshot-dir",
        default=None,
        help="Directory containing mssql/snapshot.json and pg/snapshot.json.",
    )
    val_group.add_argument(
        "--snapshot-file",
        default=None,
        help="Path to a single snapshot JSON file to validate.",
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint returning POSIX exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "capture":
        try:
            dsn = resolve_dsn(dsn=args.dsn, dsn_env=args.dsn_env)
            engine_arg = args.engine.lower()
            include_data_stats = not args.skip_data_stats
            t0 = time.perf_counter()

            if engine_arg in {"postgres", "postgresql"}:
                schema = args.schema or "public"
                snapshot = capture_postgres_snapshot(
                    dsn=dsn,
                    schema=schema,
                    server_name=args.server,
                    database_name=args.database,
                    include_data_stats=include_data_stats,
                    statement_timeout_ms=args.statement_timeout_ms,
                    driver=args.driver,
                )
            else:
                schema = args.schema or "dbo"
                snapshot = capture_mssql_snapshot(
                    conn_str=dsn,
                    schema=schema,
                    server_name=args.server,
                    database_name=args.database,
                    include_data_stats=include_data_stats,
                    lock_timeout_ms=args.lock_timeout_ms,
                )

            out_path = write_snapshot(snapshot, args.out)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
        except (MissingDriverError, SnapshotCaptureError, ValueError) as exc:
            print(f"[ERROR] Snapshot capture failed: {exc}", file=sys.stderr)
            return 1

        print(
            f"[OK] Captured {snapshot.engine} snapshot "
            f"({snapshot.server_name}/{snapshot.database_name}.{snapshot.schema_name}): "
            f"{len(snapshot.tables)} tables, {len(snapshot.routines)} routines, "
            f"{len(snapshot.security_grants)} grants, {len(snapshot.data_stats)} data_stats "
            f"in {elapsed_ms:.1f} ms -> {out_path}"
        )
        return 0

    if args.command == "validate":
        if args.snapshot_file:
            snap = load_snapshot(args.snapshot_file)
            print(
                f"[OK] Validated snapshot: "
                f"{snap.engine}({snap.server_name}/{snap.database_name}.{snap.schema_name}, "
                f"{len(snap.tables)} tables, {len(snap.routines)} routines, "
                f"{len(snap.security_grants)} grants, {len(snap.data_stats)} data_stats)"
            )
            return 0
        source, target = load_snapshot_pair(args.snapshot_dir)
        print(
            f"[OK] Validated snapshot pair: "
            f"{source.engine}({source.server_name}/{source.database_name}, {len(source.tables)} tables) "
            f"-> {target.engine}({target.server_name}/{target.database_name}, {len(target.tables)} tables)"
        )
        return 0

    if args.command == "compare":
        source, target = load_snapshot_pair(args.snapshot_dir)
        type_map = load_type_map(args.type_map)

        overrides_path: Optional[Path] = None
        if not args.no_overrides:
            if args.overrides:
                overrides_path = Path(args.overrides)
            elif DEFAULT_OVERRIDES.exists():
                overrides_path = DEFAULT_OVERRIDES

        overrides = load_name_overrides(overrides_path)
        report = run_comparison(
            source=source,
            target=target,
            type_map=type_map,
            overrides=overrides,
            run_id=args.run_id,
        )

        md_content = render_markdown_report(report)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(md_content)

        if args.sql_out:
            sql_content = render_audit_sql(report)
            sql_path = Path(args.sql_out)
            sql_path.parent.mkdir(parents=True, exist_ok=True)
            with sql_path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(sql_content)

        print(
            f"[{report.overall_status.value}] Parity comparison complete (run_id={report.run_id}): "
            f"PASS={report.pass_count}, WARN={report.warn_count}, FAIL={report.fail_count}, "
            f"SKIP={report.skip_count}. Report written to {out_path}"
        )

        if args.strict and report.overall_status == CheckStatus.FAIL:
            return 1
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
