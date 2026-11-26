"""Layer 4 Comparator: Stored procedure and function inventory parity & migration status tracking."""

from __future__ import annotations

from src.comparators.naming import match_names
from src.snapshot.models import (
    CheckResult,
    CheckStatus,
    NameOverrides,
    ParityLayer,
    Severity,
    Snapshot,
)


def compare_routines(
    source: Snapshot,
    target: Snapshot,
    overrides: NameOverrides,
) -> list[CheckResult]:
    """Verify stored procedure/function inventory parity, retirement tracking, and signature alignment."""
    results: list[CheckResult] = []
    src_routines = source.routine_map()
    tgt_routines = target.routine_map()

    # Separate intentionally retired / app-replaced routines before matching.
    active_src_names: list[str] = []
    for name, routine in sorted(src_routines.items()):
        if name in overrides.routine_retirements:
            meta = overrides.routine_retirements[name]
            status_label = meta.get("status", "deprecated")
            reason = meta.get("reason", "Documented retirement in config/name_overrides.yaml.")
            results.append(
                CheckResult(
                    layer=ParityLayer.ROUTINES,
                    check_code="ROUTINE_RETIRED",
                    status=CheckStatus.PASS,
                    severity=Severity.INFO,
                    object_type=routine.routine_type,
                    source_object=name,
                    target_object=None,
                    source_value=f"{routine.routine_type}({routine.parameter_count} params)",
                    target_value=status_label.upper(),
                    message=f"Routine '{name}' tracked as '{status_label}': {reason}",
                )
            )
        else:
            active_src_names.append(name)

    # Flag stale routine_retirements referencing routines no longer present in source snapshot.
    for retired_name in sorted(overrides.routine_retirements):
        if retired_name not in src_routines:
            results.append(
                CheckResult(
                    layer=ParityLayer.ROUTINES,
                    check_code="ROUTINE_RETIREMENT_UNKNOWN_SOURCE",
                    status=CheckStatus.WARN,
                    severity=Severity.LOW,
                    object_type="ROUTINE",
                    source_object=retired_name,
                    target_object=None,
                    source_value="MISSING",
                    target_value=overrides.routine_retirements[retired_name].get("status", "deprecated").upper(),
                    message=(
                        f"Configured routine retirement '{retired_name}' was not found in source snapshot."
                    ),
                    remediation="Verify or clean up stale entry in config/name_overrides.yaml (routine_retirements).",
                )
            )

    routine_matches = match_names(
        source_names=active_src_names,
        target_names=tgt_routines.keys(),
        overrides=overrides.routine_overrides,
    )

    for inv in routine_matches.invalid_overrides:
        results.append(
            CheckResult(
                layer=ParityLayer.ROUTINES,
                check_code="ROUTINE_OVERRIDE_INVALID",
                status=CheckStatus.FAIL,
                severity=Severity.HIGH,
                object_type="ROUTINE",
                source_object=inv.source,
                target_object=inv.target,
                source_value=inv.source,
                target_value=inv.target,
                message=inv.reason,
                remediation="Correct routine_overrides in config/name_overrides.yaml.",
            )
        )

    for collision in routine_matches.collisions:
        results.append(
            CheckResult(
                layer=ParityLayer.ROUTINES,
                check_code="ROUTINE_CANONICAL_COLLISION",
                status=CheckStatus.FAIL,
                severity=Severity.HIGH,
                object_type="ROUTINE",
                source_object=", ".join(collision.source_names) or None,
                target_object=", ".join(collision.target_names) or None,
                source_value=collision.canonical_token,
                target_value=collision.canonical_token,
                message=(
                    f"Ambiguous routine canonical token '{collision.canonical_token}': "
                    f"source={list(collision.source_names)} vs target={list(collision.target_names)}."
                ),
                remediation="Add explicit routine_overrides entries to disambiguate.",
            )
        )

    for pair in sorted(routine_matches.matched, key=lambda p: p.source):
        src_r = src_routines[pair.source]
        tgt_r = tgt_routines[pair.target]

        if src_r.parameter_count != tgt_r.parameter_count:
            results.append(
                CheckResult(
                    layer=ParityLayer.ROUTINES,
                    check_code="ROUTINE_SIGNATURE_MISMATCH",
                    status=CheckStatus.WARN,
                    severity=Severity.MEDIUM,
                    object_type=src_r.routine_type,
                    source_object=src_r.name,
                    target_object=tgt_r.name,
                    source_value=f"{src_r.routine_type}({src_r.parameter_count} params)",
                    target_value=f"{tgt_r.routine_type}({tgt_r.parameter_count} params)",
                    message=(
                        f"Migrated routine '{src_r.name}' -> '{tgt_r.name}' has different parameter "
                        f"count ({src_r.parameter_count} vs {tgt_r.parameter_count})."
                    ),
                    remediation="Verify application callers match the new PostgreSQL function/procedure signature.",
                )
            )
        else:
            results.append(
                CheckResult(
                    layer=ParityLayer.ROUTINES,
                    check_code="ROUTINE_MIGRATED",
                    status=CheckStatus.PASS,
                    severity=Severity.INFO,
                    object_type=src_r.routine_type,
                    source_object=src_r.name,
                    target_object=tgt_r.name,
                    source_value=f"{src_r.routine_type}({src_r.parameter_count} params)",
                    target_value=f"{tgt_r.routine_type}({tgt_r.parameter_count} params)",
                    message=(
                        f"Routine '{src_r.name}' migrated to '{tgt_r.name}' ({pair.method}) "
                        f"with matching parameter count ({src_r.parameter_count})."
                    ),
                )
            )

    for missing in routine_matches.unmatched_source:
        src_r = src_routines[missing]
        results.append(
            CheckResult(
                layer=ParityLayer.ROUTINES,
                check_code="ROUTINE_MISSING_TARGET",
                status=CheckStatus.FAIL,
                severity=Severity.HIGH,
                object_type=src_r.routine_type,
                source_object=src_r.name,
                target_object=None,
                source_value=f"{src_r.routine_type}({src_r.parameter_count} params)",
                target_value="MISSING",
                message=(
                    f"Source {src_r.routine_type.lower()} '{src_r.name}' is not migrated to target "
                    f"PostgreSQL and is not marked retired."
                ),
                remediation=(
                    "Port the routine to PL/pgSQL, add a routine_overrides mapping, or document "
                    "its retirement under routine_retirements."
                ),
            )
        )

    for extra in routine_matches.unmatched_target:
        tgt_r = tgt_routines[extra]
        results.append(
            CheckResult(
                layer=ParityLayer.ROUTINES,
                check_code="ROUTINE_EXTRA_TARGET",
                status=CheckStatus.WARN,
                severity=Severity.LOW,
                object_type=tgt_r.routine_type,
                source_object=None,
                target_object=tgt_r.name,
                source_value="NONE",
                target_value=f"{tgt_r.routine_type}({tgt_r.parameter_count} params)",
                message=f"Additional routine '{tgt_r.name}' exists on target PostgreSQL only.",
                remediation="Verify whether this is a newly created helper function or an unmapped rename.",
            )
        )

    return results
