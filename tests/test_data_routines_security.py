"""Unit tests for Layer 3 (Data), Layer 4 (Routines), and Layer 5 (Security) comparators."""

from __future__ import annotations

from src.comparators.data import compare_data
from src.comparators.naming import MatchedPair, NameMatchSet
from src.comparators.routines import compare_routines
from src.comparators.security import compare_security
from src.snapshot.models import (
    CheckStatus,
    ColumnAggregate,
    NameOverrides,
    RoutineSchema,
    SecurityGrant,
    Snapshot,
    TableDataStats,
)


def _snapshot(
    engine: str,
    *,
    data_stats: tuple[TableDataStats, ...] = (),
    routines: tuple[RoutineSchema, ...] = (),
    security_grants: tuple[SecurityGrant, ...] = (),
) -> Snapshot:
    return Snapshot(
        engine=engine,
        server_name="sql-src-01" if engine == "mssql" else "pg-target-01",
        database_name="hotel_booking",
        schema_name="dbo" if engine == "mssql" else "public",
        captured_at="2026-02-15T00:00:00Z",
        data_stats=data_stats,
        routines=routines,
        security_grants=security_grants,
    )


def test_data_comparator_uses_exact_decimal_equality_and_catches_drift() -> None:
    table_matches = NameMatchSet(
        matched=[
            MatchedPair("Bookings", "bookings", "canonical", "bookings"),
            MatchedPair("Invoices", "invoices", "canonical", "invoices"),
        ]
    )
    col_matches = {
        "Bookings": NameMatchSet(
            matched=[MatchedPair("NightlyRate", "nightly_rate", "canonical", "nightlyrate")]
        ),
        "Invoices": NameMatchSet(
            matched=[MatchedPair("TotalAmount", "total_amount", "canonical", "totalamount")]
        ),
    }

    src = _snapshot(
        "mssql",
        data_stats=(
            TableDataStats(
                table_name="Bookings",
                row_count=100,
                aggregate_checksum="abc123",
                column_aggregates=(
                    ColumnAggregate("NightlyRate", sum_value="15000.00", min_value="89.50", max_value="450.00"),
                ),
            ),
            TableDataStats(
                table_name="Invoices",
                row_count=100,
                aggregate_checksum="def456",
                column_aggregates=(
                    ColumnAggregate("TotalAmount", sum_value="25000.55", min_value="100.00", max_value="1200.00"),
                ),
            ),
        ),
    )
    tgt = _snapshot(
        "postgresql",
        data_stats=(
            TableDataStats(
                table_name="bookings",
                row_count=100,
                aggregate_checksum="ABC123",
                column_aggregates=(
                    # Exact numeric equivalence despite trailing zero formatting difference
                    ColumnAggregate("nightly_rate", sum_value="15000.0", min_value="89.5", max_value="450"),
                ),
            ),
            TableDataStats(
                table_name="invoices",
                row_count=98,
                aggregate_checksum="deadbeef",
                column_aggregates=(
                    ColumnAggregate("total_amount", sum_value="24500.55", min_value="100.00", max_value="1200.00"),
                ),
            ),
        ),
    )

    results = compare_data(src, tgt, table_matches, col_matches)
    by_pair = {(r.source_object, r.check_code): r.status for r in results}

    assert by_pair[("Bookings", "ROW_COUNT_PARITY")] == CheckStatus.PASS
    assert by_pair[("Bookings", "TABLE_CHECKSUM_PARITY")] == CheckStatus.PASS
    assert by_pair[("Bookings.NightlyRate", "COLUMN_AGGREGATE_PARITY")] == CheckStatus.PASS

    assert by_pair[("Invoices", "ROW_COUNT_DRIFT")] == CheckStatus.FAIL
    assert by_pair[("Invoices", "TABLE_CHECKSUM_DRIFT")] == CheckStatus.FAIL
    assert by_pair[("Invoices.TotalAmount", "COLUMN_AGGREGATE_DRIFT")] == CheckStatus.FAIL


def test_data_comparator_decimal_edge_cases_empty_table_none_vs_zero_trailing_zeros_and_nan() -> None:
    """Verify None == None on empty tables, None != '0', '100.00' == '100', and NaN safety."""
    table_matches = NameMatchSet(
        matched=[
            MatchedPair("EmptyClean", "empty_clean", "canonical", "emptyclean"),
            MatchedPair("EmptyCoalesced", "empty_coalesced", "canonical", "emptycoalesced"),
            MatchedPair("PrecisionCheck", "precision_check", "canonical", "precisioncheck"),
        ]
    )
    col_matches = {
        "EmptyClean": NameMatchSet(matched=[MatchedPair("Amt", "amt", "canonical", "amt")]),
        "EmptyCoalesced": NameMatchSet(matched=[MatchedPair("Amt", "amt", "canonical", "amt")]),
        "PrecisionCheck": NameMatchSet(
            matched=[
                MatchedPair("TrailingZero", "trailing_zero", "canonical", "trailingzero"),
                MatchedPair("BadNan", "bad_nan", "canonical", "badnan"),
            ]
        ),
    }

    src = _snapshot(
        "mssql",
        data_stats=(
            TableDataStats(
                table_name="EmptyClean",
                row_count=0,
                aggregate_checksum=None,
                column_aggregates=(
                    ColumnAggregate("Amt", sum_value=None, min_value=None, max_value=None, null_count=0),
                ),
            ),
            TableDataStats(
                table_name="EmptyCoalesced",
                row_count=0,
                aggregate_checksum="0000",
                column_aggregates=(
                    # SQL SUM() on empty table is NULL (None)
                    ColumnAggregate("Amt", sum_value=None, min_value=None, max_value=None, null_count=0),
                ),
            ),
            TableDataStats(
                table_name="PrecisionCheck",
                row_count=2,
                aggregate_checksum="ff01",
                column_aggregates=(
                    ColumnAggregate("TrailingZero", sum_value="100.00", min_value="-0.00", max_value="100.000"),
                    ColumnAggregate("BadNan", sum_value="10.00", min_value="5.00", max_value="5.00"),
                ),
            ),
        ),
    )
    tgt = _snapshot(
        "postgresql",
        data_stats=(
            TableDataStats(
                table_name="empty_clean",
                row_count=0,
                aggregate_checksum=None,
                column_aggregates=(
                    ColumnAggregate("amt", sum_value=None, min_value=None, max_value=None, null_count=0),
                ),
            ),
            TableDataStats(
                table_name="empty_coalesced",
                row_count=0,
                aggregate_checksum=None,  # Missing checksum on one side -> WARN
                column_aggregates=(
                    # Erroneously coalesced to '0' instead of NULL -> MUST FAIL
                    ColumnAggregate("amt", sum_value="0", min_value=None, max_value=None, null_count=0),
                ),
            ),
            TableDataStats(
                table_name="precision_check",
                row_count=2,
                aggregate_checksum="FF01",
                column_aggregates=(
                    ColumnAggregate("trailing_zero", sum_value="100", min_value="0", max_value="100"),
                    ColumnAggregate("bad_nan", sum_value="NaN", min_value="5", max_value="5"),
                ),
            ),
        ),
    )

    results = compare_data(src, tgt, table_matches, col_matches)
    by_pair = {(r.source_object, r.check_code): r.status for r in results}

    assert by_pair[("EmptyClean", "ROW_COUNT_PARITY")] == CheckStatus.PASS
    assert by_pair[("EmptyClean.Amt", "COLUMN_AGGREGATE_PARITY")] == CheckStatus.PASS

    assert by_pair[("EmptyCoalesced", "TABLE_CHECKSUM_INCOMPLETE")] == CheckStatus.WARN
    assert by_pair[("EmptyCoalesced.Amt", "COLUMN_AGGREGATE_DRIFT")] == CheckStatus.FAIL

    assert by_pair[("PrecisionCheck.TrailingZero", "COLUMN_AGGREGATE_PARITY")] == CheckStatus.PASS
    assert by_pair[("PrecisionCheck.BadNan", "COLUMN_AGGREGATE_DRIFT")] == CheckStatus.FAIL


def test_routines_comparator_tracks_migrated_retired_signature_missing_and_dangling() -> None:
    src = _snapshot(
        "mssql",
        routines=(
            RoutineSchema("usp_GetBookings", "dbo", "PROCEDURE", parameter_count=1),
            RoutineSchema("usp_CalcTax", "dbo", "PROCEDURE", parameter_count=2),
            RoutineSchema("usp_LegacySync", "dbo", "PROCEDURE", parameter_count=0),
            RoutineSchema("usp_MissingProc", "dbo", "PROCEDURE", parameter_count=1),
            RoutineSchema("usp_DanglingTarget", "dbo", "PROCEDURE", parameter_count=1),
        ),
    )
    tgt = _snapshot(
        "postgresql",
        routines=(
            RoutineSchema("fn_get_bookings", "public", "FUNCTION", parameter_count=1),
            RoutineSchema("fn_calc_tax", "public", "FUNCTION", parameter_count=3),
        ),
    )
    overrides = NameOverrides(
        routine_overrides={
            "usp_GetBookings": "fn_get_bookings",
            "usp_CalcTax": "fn_calc_tax",
            "usp_DanglingTarget": "fn_does_not_exist",
        },
        routine_retirements={
            "usp_LegacySync": {"status": "replaced_by_app", "reason": "Moved to Go microservice."},
            "usp_StaleRetired": {"status": "deprecated", "reason": "Removed earlier."},
        },
    )

    results = compare_routines(src, tgt, overrides)
    by_src_code = {(r.source_object, r.check_code): r.status for r in results}

    assert by_src_code[("usp_GetBookings", "ROUTINE_MIGRATED")] == CheckStatus.PASS
    assert by_src_code[("usp_CalcTax", "ROUTINE_SIGNATURE_MISMATCH")] == CheckStatus.WARN
    assert by_src_code[("usp_LegacySync", "ROUTINE_RETIRED")] == CheckStatus.PASS
    assert by_src_code[("usp_MissingProc", "ROUTINE_MISSING_TARGET")] == CheckStatus.FAIL
    assert by_src_code[("usp_DanglingTarget", "ROUTINE_OVERRIDE_INVALID")] == CheckStatus.FAIL
    assert by_src_code[("usp_DanglingTarget", "ROUTINE_MISSING_TARGET")] == CheckStatus.FAIL
    assert by_src_code[("usp_StaleRetired", "ROUTINE_RETIREMENT_UNKNOWN_SOURCE")] == CheckStatus.WARN


def test_security_comparator_enforces_parity_least_privilege_and_blocks_admin_substring_bypass() -> None:
    table_matches = NameMatchSet(
        matched=[
            MatchedPair("Bookings", "bookings", "canonical", "bookings"),
            MatchedPair("Payments", "payments", "canonical", "payments"),
        ]
    )
    src = _snapshot(
        "mssql",
        security_grants=(
            SecurityGrant("app_rw", "TABLE", "Bookings", "SELECT"),
            SecurityGrant("app_rw", "TABLE", "Bookings", "INSERT"),
            SecurityGrant("app_rw", "TABLE", "Payments", "SELECT"),
        ),
    )
    tgt = _snapshot(
        "postgresql",
        security_grants=(
            SecurityGrant("app_rw", "TABLE", "bookings", "SELECT"),
            # Missing INSERT on bookings -> FAIL
            # ALL PRIVILEGES on payments -> WARN (overbroad substitution + excessive ALL PRIVILEGES)
            SecurityGrant("app_rw", "TABLE", "payments", "ALL PRIVILEGES"),
            # Direct PUBLIC grant -> WARN
            SecurityGrant("PUBLIC", "TABLE", "bookings", "SELECT"),
            # Non-admin role containing 'postgres' substring must NOT bypass superuser/ALL checks!
            SecurityGrant("app_postgres_worker", "TABLE", "bookings", "SELECT", is_grantable=True, is_superuser=True),
            SecurityGrant("app_postgres_worker", "TABLE", "payments", "ALL PRIVILEGES", is_superuser=True),
            # Real exact 'postgres' admin role is permitted superuser
            SecurityGrant("postgres", "TABLE", "bookings", "ALL PRIVILEGES", is_superuser=True),
        ),
    )

    results = compare_security(src, tgt, table_matches, NameOverrides())
    codes = {(r.check_code, r.status) for r in results}

    assert ("GRANT_PARITY", CheckStatus.PASS) in codes
    assert ("GRANT_MISSING_TARGET", CheckStatus.FAIL) in codes
    assert ("GRANT_OVERBROAD_SUBSTITUTION", CheckStatus.WARN) in codes
    assert ("SECURITY_PUBLIC_GRANT", CheckStatus.WARN) in codes
    assert ("SECURITY_EXCESSIVE_ALL_PRIVILEGES", CheckStatus.WARN) in codes
    assert ("SECURITY_NON_ADMIN_SUPERUSER", CheckStatus.FAIL) in codes
    assert ("SECURITY_GRANT_OPTION_ENABLED", CheckStatus.WARN) in codes

    # Deduplicated superuser finding for app_postgres_worker, zero findings for real postgres admin
    su_findings = [r for r in results if r.check_code == "SECURITY_NON_ADMIN_SUPERUSER"]
    assert len(su_findings) == 1
    assert su_findings[0].target_object == "app_postgres_worker"
    assert su_findings[0].remediation == 'ALTER ROLE "app_postgres_worker" NOSUPERUSER;'

    # PUBLIC remediation keeps PUBLIC unquoted while quoting table identifier
    pub_findings = [r for r in results if r.check_code == "SECURITY_PUBLIC_GRANT"]
    assert pub_findings[0].remediation == 'REVOKE SELECT ON "bookings" FROM PUBLIC;'
