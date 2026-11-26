"""Layer 5 Comparator: Security role/privilege grant parity and least-privilege hardening checks."""

from __future__ import annotations

from src.comparators.naming import NameMatchSet, canonicalize
from src.comparators.structure import pg_quote_ident
from src.snapshot.models import (
    CheckResult,
    CheckStatus,
    NameOverrides,
    ParityLayer,
    SecurityGrant,
    Severity,
    Snapshot,
)

ADMIN_CANONICAL_ROLES = frozenset(
    {
        "postgres",
        "sa",
        "rdsadmin",
        "dbowner",
        "dbaadmin",
        "cloudsqlsuperuser",
        "azurepgadmin",
    }
)


def _is_admin_principal(principal: str) -> bool:
    """Return True only for known exact canonical DBA/cloud-provider admin roles (never substring match)."""
    return canonicalize(principal) in ADMIN_CANONICAL_ROLES


def _pg_object_ref(object_type: str, object_name: str) -> str:
    quoted = pg_quote_ident(object_name)
    if object_type.upper() == "ROUTINE":
        return f"ROUTINE {quoted}"
    return quoted


def _normalize_grant_key(
    grant: SecurityGrant,
    role_map: dict[str, str],
    object_map: dict[str, str],
    is_source: bool,
) -> tuple[str, str, str, str]:
    principal = role_map.get(grant.principal, grant.principal) if is_source else grant.principal
    obj_name = object_map.get(grant.object_name, grant.object_name) if is_source else grant.object_name
    return (
        canonicalize(principal),
        grant.object_type.upper(),
        canonicalize(obj_name),
        grant.privilege.upper(),
    )


def compare_security(
    source: Snapshot,
    target: Snapshot,
    table_matches: NameMatchSet,
    overrides: NameOverrides,
) -> list[CheckResult]:
    """Compare object-level privilege grants and enforce least-privilege guardrails on target PostgreSQL."""
    results: list[CheckResult] = []
    object_src_to_tgt = {
        **table_matches.source_to_target(),
        **overrides.routine_overrides,
    }

    src_keys: dict[tuple[str, str, str, str], SecurityGrant] = {}
    for g in source.security_grants:
        key = _normalize_grant_key(g, overrides.role_overrides, object_src_to_tgt, is_source=True)
        src_keys[key] = g

    tgt_keys: dict[tuple[str, str, str, str], SecurityGrant] = {}
    for g in target.security_grants:
        key = _normalize_grant_key(g, {}, {}, is_source=False)
        tgt_keys[key] = g

    # 1. Parity checks: ensure source least-privilege grants are preserved on target
    for key, src_g in sorted(src_keys.items()):
        if key in tgt_keys:
            tgt_g = tgt_keys[key]
            results.append(
                CheckResult(
                    layer=ParityLayer.SECURITY,
                    check_code="GRANT_PARITY",
                    status=CheckStatus.PASS,
                    severity=Severity.INFO,
                    object_type="SECURITY_GRANT",
                    source_object=f"{src_g.principal} -> {src_g.object_name}",
                    target_object=f"{tgt_g.principal} -> {tgt_g.object_name}",
                    source_value=src_g.privilege,
                    target_value=tgt_g.privilege,
                    message=(
                        f"Privilege '{tgt_g.privilege}' on '{tgt_g.object_name}' "
                        f"matches for principal '{tgt_g.principal}'."
                    ),
                )
            )
        else:
            # Check if target satisfies it via overly broad ALL PRIVILEGES on the same object
            broad_key = (key[0], key[1], key[2], "ALL PRIVILEGES")
            if broad_key in tgt_keys:
                tgt_broad = tgt_keys[broad_key]
                results.append(
                    CheckResult(
                        layer=ParityLayer.SECURITY,
                        check_code="GRANT_OVERBROAD_SUBSTITUTION",
                        status=CheckStatus.WARN,
                        severity=Severity.MEDIUM,
                        object_type="SECURITY_GRANT",
                        source_object=f"{src_g.principal} -> {src_g.object_name}",
                        target_object=f"{tgt_broad.principal} -> {tgt_broad.object_name}",
                        source_value=src_g.privilege,
                        target_value=tgt_broad.privilege,
                        message=(
                            f"Target role '{tgt_broad.principal}' holds ALL PRIVILEGES on "
                            f"'{tgt_broad.object_name}' instead of granular '{src_g.privilege}'."
                        ),
                        remediation=(
                            f"REVOKE ALL ON {_pg_object_ref(tgt_broad.object_type, tgt_broad.object_name)} "
                            f"FROM {pg_quote_ident(tgt_broad.principal)}; "
                            f"and grant only required DML privileges."
                        ),
                    )
                )
            else:
                mapped_obj = object_src_to_tgt.get(src_g.object_name, src_g.object_name)
                mapped_role = overrides.role_overrides.get(src_g.principal, src_g.principal)
                results.append(
                    CheckResult(
                        layer=ParityLayer.SECURITY,
                        check_code="GRANT_MISSING_TARGET",
                        status=CheckStatus.FAIL,
                        severity=Severity.HIGH,
                        object_type="SECURITY_GRANT",
                        source_object=f"{src_g.principal} -> {src_g.object_name}",
                        target_object=None,
                        source_value=src_g.privilege,
                        target_value="MISSING",
                        message=(
                            f"Missing target privilege: principal '{src_g.principal}' had "
                            f"'{src_g.privilege}' on '{src_g.object_name}' in SQL Server."
                        ),
                        remediation=(
                            f"GRANT {src_g.privilege} ON {_pg_object_ref(src_g.object_type, mapped_obj)} "
                            f"TO {pg_quote_ident(mapped_role)};"
                        ),
                    )
                )

    # 2. Target least-privilege hardening checks
    reported_superusers: set[str] = set()
    for tgt_g in sorted(
        target.security_grants,
        key=lambda g: (g.principal, g.object_name, g.privilege),
    ):
        principal_lower = tgt_g.principal.strip().lower()
        obj_ref = _pg_object_ref(tgt_g.object_type, tgt_g.object_name)

        # Check A: Direct grants to PUBLIC pseudo-role
        if principal_lower == "public":
            results.append(
                CheckResult(
                    layer=ParityLayer.SECURITY,
                    check_code="SECURITY_PUBLIC_GRANT",
                    status=CheckStatus.WARN,
                    severity=Severity.HIGH,
                    object_type="SECURITY_GRANT",
                    source_object=None,
                    target_object=f"PUBLIC -> {tgt_g.object_name}",
                    source_value="NONE",
                    target_value=tgt_g.privilege,
                    message=(
                        f"Least-privilege violation: PUBLIC role granted '{tgt_g.privilege}' "
                        f"on '{tgt_g.object_name}'."
                    ),
                    remediation=f"REVOKE {tgt_g.privilege} ON {obj_ref} FROM PUBLIC;",
                )
            )

        # Check B: Non-admin principal with SUPERUSER flag (deduplicated per role)
        if tgt_g.is_superuser and not _is_admin_principal(tgt_g.principal):
            norm_role = canonicalize(tgt_g.principal)
            if norm_role not in reported_superusers:
                reported_superusers.add(norm_role)
                results.append(
                    CheckResult(
                        layer=ParityLayer.SECURITY,
                        check_code="SECURITY_NON_ADMIN_SUPERUSER",
                        status=CheckStatus.FAIL,
                        severity=Severity.CRITICAL,
                        object_type="ROLE",
                        source_object=None,
                        target_object=tgt_g.principal,
                        source_value="NOSUPERUSER",
                        target_value="SUPERUSER",
                        message=(
                            f"Application/non-admin role '{tgt_g.principal}' has PostgreSQL SUPERUSER privilege."
                        ),
                        remediation=f"ALTER ROLE {pg_quote_ident(tgt_g.principal)} NOSUPERUSER;",
                    )
                )

        # Check C: ALL PRIVILEGES granted to non-admin application roles
        if tgt_g.privilege == "ALL PRIVILEGES" and not _is_admin_principal(tgt_g.principal):
            results.append(
                CheckResult(
                    layer=ParityLayer.SECURITY,
                    check_code="SECURITY_EXCESSIVE_ALL_PRIVILEGES",
                    status=CheckStatus.WARN,
                    severity=Severity.HIGH,
                    object_type="SECURITY_GRANT",
                    source_object=None,
                    target_object=f"{tgt_g.principal} -> {tgt_g.object_name}",
                    source_value="LEAST_PRIVILEGE",
                    target_value="ALL PRIVILEGES",
                    message=(
                        f"Least-privilege warning: role '{tgt_g.principal}' holds ALL PRIVILEGES "
                        f"(including TRUNCATE/REFERENCES/TRIGGER) on '{tgt_g.object_name}'."
                    ),
                    remediation=(
                        f"REVOKE ALL ON {obj_ref} FROM {pg_quote_ident(tgt_g.principal)}; "
                        f"and grant only explicit SELECT/INSERT/UPDATE/DELETE privileges."
                    ),
                )
            )

        # Check D: WITH GRANT OPTION on non-admin roles
        if tgt_g.is_grantable and not _is_admin_principal(tgt_g.principal):
            results.append(
                CheckResult(
                    layer=ParityLayer.SECURITY,
                    check_code="SECURITY_GRANT_OPTION_ENABLED",
                    status=CheckStatus.WARN,
                    severity=Severity.MEDIUM,
                    object_type="SECURITY_GRANT",
                    source_object=None,
                    target_object=f"{tgt_g.principal} -> {tgt_g.object_name}",
                    source_value="is_grantable=False",
                    target_value="is_grantable=True",
                    message=(
                        f"Privilege escalation risk: role '{tgt_g.principal}' holds '{tgt_g.privilege}' "
                        f"WITH GRANT OPTION on '{tgt_g.object_name}'."
                    ),
                    remediation=(
                        f"REVOKE GRANT OPTION FOR {tgt_g.privilege} ON {obj_ref} "
                        f"FROM {pg_quote_ident(tgt_g.principal)};"
                    ),
                )
            )

    return results
