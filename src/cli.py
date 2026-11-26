"""Command-line interface for offline multi-layer MSSQL -> PostgreSQL parity verification."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from src.comparators.data import compare_data
from src.comparators.routines import compare_routines
from src.comparators.security import compare_security
from src.comparators.structure import compare_structure
from src.report import render_audit_sql, render_markdown_report
from src.snapshot.loader import (
    load_name_overrides,
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

    # validate subcommand
    val_parser = subparsers.add_parser(
        "validate",
        help="Validate immutable snapshot JSON schema integrity without running comparison.",
    )
    val_parser.add_argument(
        "--snapshot-dir",
        required=True,
        help="Directory containing mssql/snapshot.json and pg/snapshot.json.",
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint returning POSIX exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
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
        out_path.write_text(md_content, encoding="utf-8")

        if args.sql_out:
            sql_content = render_audit_sql(report)
            sql_path = Path(args.sql_out)
            sql_path.parent.mkdir(parents=True, exist_ok=True)
            sql_path.write_text(sql_content, encoding="utf-8")

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
