"""Markdown drift report renderer and PostgreSQL migration_audit SQL generator."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Optional

from src.snapshot.models import (
    CheckResult,
    CheckStatus,
    ParityLayer,
    ParityReport,
    Severity,
)

_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}

_STATUS_ORDER: dict[CheckStatus, int] = {
    CheckStatus.FAIL: 0,
    CheckStatus.WARN: 1,
    CheckStatus.SKIP: 2,
    CheckStatus.PASS: 3,
}

_LAYER_TITLES: dict[ParityLayer, str] = {
    ParityLayer.NAMING: "Layer 1 — Naming & Object Mapping",
    ParityLayer.STRUCTURE: "Layer 2 — Structural Parity (Types, Nullability, Constraints, Indexes)",
    ParityLayer.DATA: "Layer 3 — Data Parity (Row Counts, Numeric Aggregates, Checksums)",
    ParityLayer.ROUTINES: "Layer 4 — Routine Inventory Parity (Procedures & Functions)",
    ParityLayer.SECURITY: "Layer 5 — Security & Least-Privilege Parity",
}


def _md_escape(value: Optional[str]) -> str:
    if value is None or value == "":
        return "-"
    return value.replace("|", "\\|").replace("\n", " ")


def _sql_lit(value: Optional[str]) -> str:
    """Render a safely quoted standard-SQL/PostgreSQL string literal (escapes single quotes and strips NUL)."""
    if value is None:
        return "NULL"
    cleaned = value.replace("\x00", "")
    escaped = cleaned.replace("'", "''")
    return f"'{escaped}'"


def _layer_status(results: list[CheckResult]) -> CheckStatus:
    if any(r.status == CheckStatus.FAIL for r in results):
        return CheckStatus.FAIL
    if any(r.status == CheckStatus.WARN for r in results):
        return CheckStatus.WARN
    return CheckStatus.PASS


def render_markdown_report(report: ParityReport) -> str:
    """Render a complete, actionable Markdown parity & drift report."""
    src = report.source_snapshot
    tgt = report.target_snapshot
    lines: list[str] = []

    lines.append("# Heterogeneous Database Migration Parity Report")
    lines.append("")
    lines.append(f"- **Run ID:** `{report.run_id}`")
    lines.append(f"- **Generated At (UTC):** `{report.generated_at}`")
    lines.append(
        f"- **Source (SQL Server):** `{src.server_name}` / `{src.database_name}.{src.schema_name}` "
        f"(snapshot: `{src.captured_at}`)"
    )
    lines.append(
        f"- **Target (PostgreSQL):** `{tgt.server_name}` / `{tgt.database_name}.{tgt.schema_name}` "
        f"(snapshot: `{tgt.captured_at}`)"
    )
    lines.append(f"- **Overall Cutover Gate:** **{report.overall_status.value}**")
    lines.append("")

    # Summary Table
    lines.append("## Executive Parity Summary")
    lines.append("")
    lines.append("| Layer | Status | PASS | WARN | FAIL | SKIP | Total |")
    lines.append("| :--- | :---: | ---: | ---: | ---: | ---: | ---: |")

    for layer in ParityLayer:
        layer_res = report.results_by_layer(layer)
        p_cnt = sum(1 for r in layer_res if r.status == CheckStatus.PASS)
        w_cnt = sum(1 for r in layer_res if r.status == CheckStatus.WARN)
        f_cnt = sum(1 for r in layer_res if r.status == CheckStatus.FAIL)
        s_cnt = sum(1 for r in layer_res if r.status == CheckStatus.SKIP)
        st = _layer_status(layer_res)
        lines.append(
            f"| `{layer.value}` | **{st.value}** | {p_cnt} | {w_cnt} | {f_cnt} | {s_cnt} | {len(layer_res)} |"
        )

    lines.append(
        f"| **TOTAL** | **{report.overall_status.value}** | **{report.pass_count}** | "
        f"**{report.warn_count}** | **{report.fail_count}** | **{report.skip_count}** | "
        f"**{len(report.results)}** |"
    )
    lines.append("")

    # Actionable Drift Details grouped by Layer
    lines.append("## Actionable Drift Findings (FAIL & WARN)")
    lines.append("")

    actionable_total = report.fail_count + report.warn_count
    if actionable_total == 0:
        lines.append("All parity checks passed across all five layers. Zero drift detected.")
        lines.append("")
    else:
        for layer in ParityLayer:
            drifts = [
                r
                for r in report.results_by_layer(layer)
                if r.status in {CheckStatus.FAIL, CheckStatus.WARN}
            ]
            if not drifts:
                continue

            drifts.sort(
                key=lambda r: (
                    _STATUS_ORDER[r.status],
                    _SEVERITY_ORDER[r.severity],
                    r.check_code,
                    r.source_object or r.target_object or "",
                )
            )

            lines.append(f"### {_LAYER_TITLES[layer]}")
            lines.append("")
            lines.append(
                "| Status | Severity | Check Code | Source Object | Target Object | "
                "Source Value | Target Value | Remediation |"
            )
            lines.append("| :---: | :---: | :--- | :--- | :--- | :--- | :--- | :--- |")
            for item in drifts:
                lines.append(
                    f"| **{item.status.value}** | `{item.severity.value}` | `{item.check_code}` | "
                    f"`{_md_escape(item.source_object)}` | `{_md_escape(item.target_object)}` | "
                    f"{_md_escape(item.source_value)} | {_md_escape(item.target_value)} | "
                    f"{_md_escape(item.remediation or item.message)} |"
                )
            lines.append("")

    # Passed checks summary table for audit completeness
    passed = [r for r in report.results if r.status == CheckStatus.PASS]
    if passed:
        lines.append("## Verified Parity Summary (PASS Checks)")
        lines.append("")
        counts = Counter((r.layer.value, r.check_code) for r in passed)
        lines.append("| Layer | Check Code | Passed Count |")
        lines.append("| :--- | :--- | ---: |")
        for (layer_name, code), count in sorted(counts.items()):
            lines.append(f"| `{layer_name}` | `{code}` | {count} |")
        lines.append("")

    return "\n".join(lines)


def render_audit_sql(report: ParityReport) -> str:
    """Generate transactional PostgreSQL INSERT statements for migration_audit tables."""
    # Validate UUID up front so generated ::uuid casts and comment headers cannot fail or inject SQL.
    normalized_run_id = str(uuid.UUID(report.run_id))

    src = report.source_snapshot
    tgt = report.target_snapshot

    metadata_json = json.dumps(
        {
            "capture_version": src.capture_version,
            "source_schema": src.schema_name,
            "target_schema": tgt.schema_name,
            "source_captured_at": src.captured_at,
            "target_captured_at": tgt.captured_at,
        },
        sort_keys=True,
    )

    lines: list[str] = [
        "-- Auto-generated PostgreSQL audit trail inserts for mssql-to-pg-parity-kit",
        f"-- Run ID: {normalized_run_id}",
        "BEGIN;",
        "",
        "INSERT INTO migration_audit.migration_run (",
        "    run_id, source_engine, source_host, source_db,",
        "    target_engine, target_host, target_db,",
        "    snapshot_captured_at, started_at, completed_at,",
        "    overall_status, total_checks, pass_count, warn_count, fail_count, skip_count, metadata",
        ") VALUES (",
        f"    {_sql_lit(normalized_run_id)}::uuid, {_sql_lit(src.engine)}, {_sql_lit(src.server_name)}, {_sql_lit(src.database_name)},",
        f"    {_sql_lit(tgt.engine)}, {_sql_lit(tgt.server_name)}, {_sql_lit(tgt.database_name)},",
        f"    {_sql_lit(src.captured_at)}::timestamptz, {_sql_lit(report.generated_at)}::timestamptz, {_sql_lit(report.generated_at)}::timestamptz,",
        f"    {_sql_lit(report.overall_status.value)}, {len(report.results)}, {report.pass_count}, {report.warn_count}, {report.fail_count}, {report.skip_count}, {_sql_lit(metadata_json)}::jsonb",
        ");",
        "",
    ]

    for item in report.results:
        lines.append(
            "INSERT INTO migration_audit.check_result ("
            "run_id, layer, check_code, severity, status, object_type, "
            "source_object, target_object, source_value, target_value, message, remediation"
            ") VALUES ("
            f"{_sql_lit(normalized_run_id)}::uuid, "
            f"{_sql_lit(item.layer.value)}, "
            f"{_sql_lit(item.check_code)}, "
            f"{_sql_lit(item.severity.value)}, "
            f"{_sql_lit(item.status.value)}, "
            f"{_sql_lit(item.object_type)}, "
            f"{_sql_lit(item.source_object)}, "
            f"{_sql_lit(item.target_object)}, "
            f"{_sql_lit(item.source_value)}, "
            f"{_sql_lit(item.target_value)}, "
            f"{_sql_lit(item.message)}, "
            f"{_sql_lit(item.remediation)}"
            ");"
        )

    lines.append("")
    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)
