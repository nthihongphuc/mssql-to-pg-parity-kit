"""Layer 3 Comparator: Data parity (row counts, exact numeric SUM/MIN/MAX, aggregate checksums)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from src.comparators.naming import NameMatchSet, canonicalize
from src.snapshot.models import (
    CheckResult,
    CheckStatus,
    NameOverrides,
    ParityLayer,
    Severity,
    Snapshot,
)


def _compare_numeric_or_str(left: Optional[str], right: Optional[str]) -> bool:
    """Compare aggregate values using exact Decimal equality when numeric, else normalized string.

    Distinguishes SQL NULL (None, e.g. SUM on an empty table) from numeric zero ('0' / '0.00'),
    treats trailing zeros as numerically equal ('100.00' == '100'), and safely rejects NaN/sNaN.
    """
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    left_s = left.strip()
    right_s = right.strip()
    try:
        d_left = Decimal(left_s)
        d_right = Decimal(right_s)
        if d_left.is_nan() or d_right.is_nan():
            return False
        return d_left == d_right
    except InvalidOperation:
        return left_s == right_s


def compare_data(
    source: Snapshot,
    target: Snapshot,
    table_matches: NameMatchSet,
    column_matches_by_table: dict[str, NameMatchSet],
    overrides: Optional[NameOverrides] = None,
) -> list[CheckResult]:
    """Compare row counts, numeric aggregates (SUM/MIN/MAX), and table aggregate checksums."""
    results: list[CheckResult] = []
    src_stats_map = source.data_stats_map()
    tgt_stats_map = target.data_stats_map()
    _ = overrides  # Reserved for future per-table tolerance rules

    for pair in sorted(table_matches.matched, key=lambda p: p.source):
        src_table_name = pair.source
        tgt_table_name = pair.target
        src_stats = src_stats_map.get(src_table_name)
        tgt_stats = tgt_stats_map.get(tgt_table_name)

        if src_stats is None and tgt_stats is None:
            results.append(
                CheckResult(
                    layer=ParityLayer.DATA,
                    check_code="DATA_STATS_MISSING",
                    status=CheckStatus.SKIP,
                    severity=Severity.LOW,
                    object_type="TABLE",
                    source_object=src_table_name,
                    target_object=tgt_table_name,
                    source_value="NONE",
                    target_value="NONE",
                    message=f"No data statistics captured for table pair '{src_table_name}' -> '{tgt_table_name}'.",
                )
            )
            continue

        if src_stats is None or tgt_stats is None:
            results.append(
                CheckResult(
                    layer=ParityLayer.DATA,
                    check_code="DATA_STATS_INCOMPLETE",
                    status=CheckStatus.WARN,
                    severity=Severity.MEDIUM,
                    object_type="TABLE",
                    source_object=src_table_name,
                    target_object=tgt_table_name,
                    source_value="CAPTURED" if src_stats else "MISSING",
                    target_value="CAPTURED" if tgt_stats else "MISSING",
                    message=(
                        f"Data statistics missing on one side for '{src_table_name}' -> '{tgt_table_name}'."
                    ),
                    remediation="Re-run snapshot capture ensuring both source and target data_stats are populated.",
                )
            )
            continue

        # 1. Row count parity
        row_diff = tgt_stats.row_count - src_stats.row_count
        if row_diff == 0:
            results.append(
                CheckResult(
                    layer=ParityLayer.DATA,
                    check_code="ROW_COUNT_PARITY",
                    status=CheckStatus.PASS,
                    severity=Severity.INFO,
                    object_type="TABLE",
                    source_object=src_table_name,
                    target_object=tgt_table_name,
                    source_value=str(src_stats.row_count),
                    target_value=str(tgt_stats.row_count),
                    message=f"Row count matches exactly ({src_stats.row_count} rows).",
                )
            )
        else:
            results.append(
                CheckResult(
                    layer=ParityLayer.DATA,
                    check_code="ROW_COUNT_DRIFT",
                    status=CheckStatus.FAIL,
                    severity=Severity.CRITICAL,
                    object_type="TABLE",
                    source_object=src_table_name,
                    target_object=tgt_table_name,
                    source_value=str(src_stats.row_count),
                    target_value=str(tgt_stats.row_count),
                    message=(
                        f"Row count drift on '{tgt_table_name}': source={src_stats.row_count}, "
                        f"target={tgt_stats.row_count} (delta={row_diff:+d})."
                    ),
                    remediation=(
                        f"Inspect replication/CDC lag or failed batch loads for '{tgt_table_name}' "
                        f"and reconcile missing/extra rows."
                    ),
                )
            )

        # 2. Aggregate table checksum parity (if present on both sides)
        if src_stats.aggregate_checksum is not None and tgt_stats.aggregate_checksum is not None:
            if src_stats.aggregate_checksum.lower() == tgt_stats.aggregate_checksum.lower():
                results.append(
                    CheckResult(
                        layer=ParityLayer.DATA,
                        check_code="TABLE_CHECKSUM_PARITY",
                        status=CheckStatus.PASS,
                        severity=Severity.INFO,
                        object_type="TABLE",
                        source_object=src_table_name,
                        target_object=tgt_table_name,
                        source_value=src_stats.aggregate_checksum,
                        target_value=tgt_stats.aggregate_checksum,
                        message=f"Aggregate table checksum matches on '{tgt_table_name}'.",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        layer=ParityLayer.DATA,
                        check_code="TABLE_CHECKSUM_DRIFT",
                        status=CheckStatus.FAIL,
                        severity=Severity.CRITICAL,
                        object_type="TABLE",
                        source_object=src_table_name,
                        target_object=tgt_table_name,
                        source_value=src_stats.aggregate_checksum,
                        target_value=tgt_stats.aggregate_checksum,
                        message=(
                            f"Aggregate checksum mismatch on '{tgt_table_name}': "
                            f"row contents or ordering diverged."
                        ),
                        remediation="Run primary-key range bisection diff to isolate mutated or missing rows.",
                    )
                )
        elif src_stats.aggregate_checksum is not None or tgt_stats.aggregate_checksum is not None:
            results.append(
                CheckResult(
                    layer=ParityLayer.DATA,
                    check_code="TABLE_CHECKSUM_INCOMPLETE",
                    status=CheckStatus.WARN,
                    severity=Severity.MEDIUM,
                    object_type="TABLE",
                    source_object=src_table_name,
                    target_object=tgt_table_name,
                    source_value=src_stats.aggregate_checksum or "MISSING",
                    target_value=tgt_stats.aggregate_checksum or "MISSING",
                    message=(
                        f"Aggregate checksum captured on only one side for '{src_table_name}' -> '{tgt_table_name}'."
                    ),
                    remediation="Ensure snapshot collector captures aggregate_checksum on both source and target.",
                )
            )

        # 3. Column numeric aggregates (SUM / MIN / MAX)
        col_matches = column_matches_by_table.get(src_table_name)
        col_src_to_tgt = col_matches.source_to_target() if col_matches else {}
        tgt_agg_map = tgt_stats.aggregate_map()
        tgt_agg_by_canon = {canonicalize(k): v for k, v in tgt_agg_map.items()}

        for src_agg in src_stats.column_aggregates:
            mapped_col = col_src_to_tgt.get(src_agg.column_name)
            if col_matches is not None:
                tgt_agg = tgt_agg_map.get(mapped_col) if mapped_col is not None else None
            else:
                canon_col = canonicalize(src_agg.column_name)
                tgt_agg = tgt_agg_by_canon.get(canon_col) if canon_col else None

            if tgt_agg is None:
                results.append(
                    CheckResult(
                        layer=ParityLayer.DATA,
                        check_code="COLUMN_AGGREGATE_MISSING",
                        status=CheckStatus.WARN,
                        severity=Severity.MEDIUM,
                        object_type="COLUMN",
                        source_object=f"{src_table_name}.{src_agg.column_name}",
                        target_object=f"{tgt_table_name}.{mapped_col or '?'}",
                        source_value=f"sum={src_agg.sum_value}, min={src_agg.min_value}, max={src_agg.max_value}",
                        target_value="MISSING",
                        message=(
                            f"No target aggregate statistics found for column "
                            f"'{src_table_name}.{src_agg.column_name}'."
                        ),
                        remediation="Ensure snapshot collector captures numeric aggregates for all mapped columns.",
                    )
                )
                continue

            mismatches: list[str] = []
            if not _compare_numeric_or_str(src_agg.sum_value, tgt_agg.sum_value):
                mismatches.append(f"SUM ({src_agg.sum_value} vs {tgt_agg.sum_value})")
            if not _compare_numeric_or_str(src_agg.min_value, tgt_agg.min_value):
                mismatches.append(f"MIN ({src_agg.min_value} vs {tgt_agg.min_value})")
            if not _compare_numeric_or_str(src_agg.max_value, tgt_agg.max_value):
                mismatches.append(f"MAX ({src_agg.max_value} vs {tgt_agg.max_value})")
            if src_agg.null_count != tgt_agg.null_count:
                mismatches.append(f"NULL_COUNT ({src_agg.null_count} vs {tgt_agg.null_count})")

            src_summary = (
                f"sum={src_agg.sum_value}, min={src_agg.min_value}, "
                f"max={src_agg.max_value}, nulls={src_agg.null_count}"
            )
            tgt_summary = (
                f"sum={tgt_agg.sum_value}, min={tgt_agg.min_value}, "
                f"max={tgt_agg.max_value}, nulls={tgt_agg.null_count}"
            )

            if not mismatches:
                results.append(
                    CheckResult(
                        layer=ParityLayer.DATA,
                        check_code="COLUMN_AGGREGATE_PARITY",
                        status=CheckStatus.PASS,
                        severity=Severity.INFO,
                        object_type="COLUMN",
                        source_object=f"{src_table_name}.{src_agg.column_name}",
                        target_object=f"{tgt_table_name}.{tgt_agg.column_name}",
                        source_value=src_summary,
                        target_value=tgt_summary,
                        message=(
                            f"Numeric aggregates (SUM/MIN/MAX/NULLS) match on "
                            f"'{tgt_table_name}.{tgt_agg.column_name}'."
                        ),
                    )
                )
            else:
                results.append(
                    CheckResult(
                        layer=ParityLayer.DATA,
                        check_code="COLUMN_AGGREGATE_DRIFT",
                        status=CheckStatus.FAIL,
                        severity=Severity.CRITICAL,
                        object_type="COLUMN",
                        source_object=f"{src_table_name}.{src_agg.column_name}",
                        target_object=f"{tgt_table_name}.{tgt_agg.column_name}",
                        source_value=src_summary,
                        target_value=tgt_summary,
                        message=(
                            f"Aggregate drift on '{tgt_table_name}.{tgt_agg.column_name}': "
                            f"{'; '.join(mismatches)}."
                        ),
                        remediation=(
                            "Reconcile row-level differences or decimal/rounding transformations "
                            "for this column before cutover."
                        ),
                    )
                )

    return results
