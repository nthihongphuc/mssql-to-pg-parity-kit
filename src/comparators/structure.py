"""Layer 1 & 2 Comparator: Naming mapping, tables, columns, types, nullability, constraints, and indexes."""

from __future__ import annotations

from dataclasses import dataclass

from src.comparators.naming import (
    NameMatchSet,
    canonicalize,
    check_type_compatibility,
    match_names,
)
from src.snapshot.models import (
    CheckResult,
    CheckStatus,
    ConstraintSchema,
    IndexSchema,
    NameOverrides,
    ParityLayer,
    Severity,
    Snapshot,
    TableSchema,
    TypeMapRule,
)


@dataclass
class StructureComparisonOutput:
    """Combined output of naming and structural checks plus resolved mappings for downstream layers."""

    results: list[CheckResult]
    table_matches: NameMatchSet
    column_matches_by_table: dict[str, NameMatchSet]


def pg_quote_ident(ident: str) -> str:
    """Safely double-quote a PostgreSQL identifier, escaping embedded double quotes."""
    escaped = ident.replace('"', '""')
    return f'"{escaped}"'


def pg_qualified_table(table: TableSchema) -> str:
    """Return a safely double-quoted PostgreSQL schema-qualified table reference."""
    if table.schema_name:
        return f"{pg_quote_ident(table.schema_name)}.{pg_quote_ident(table.name)}"
    return pg_quote_ident(table.name)


def _strip_schema_prefix(table_ref: str) -> str:
    """Strip an optional leading schema prefix (e.g., 'dbo.Customers' -> 'Customers')."""
    if "." in table_ref:
        return table_ref.split(".")[-1]
    return table_ref


def _format_col_type(col_type: str, max_len: int | None, prec: int | None, scale: int | None) -> str:
    if prec is not None:
        return f"{col_type}({prec},{scale if scale is not None else 0})"
    if max_len is not None:
        return f"{col_type}({'max' if max_len == -1 else max_len})"
    return col_type


def _canonical_col_tuple(
    columns: tuple[str, ...],
    col_override_map: dict[str, str],
) -> tuple[str, ...]:
    """Resolve a sequence of column names into canonical tokens, honoring explicit mappings."""
    resolved: list[str] = []
    for col in columns:
        mapped = col_override_map.get(col, col)
        resolved.append(canonicalize(mapped))
    return tuple(resolved)


def _compare_constraints(
    src_table: TableSchema,
    tgt_table: TableSchema,
    table_src_to_tgt: dict[str, str],
    col_src_to_tgt: dict[str, str],
    col_src_to_tgt_by_table: dict[str, dict[str, str]],
) -> list[CheckResult]:
    results: list[CheckResult] = []
    # Allow lookups whether referenced_table is schema-qualified ("dbo.Customers") or bare ("Customers").
    table_src_to_tgt_bare = {_strip_schema_prefix(k): v for k, v in table_src_to_tgt.items()}
    col_map_by_bare_table = {_strip_schema_prefix(k): v for k, v in col_src_to_tgt_by_table.items()}

    def _constraint_signature(
        constraint: ConstraintSchema,
        is_source: bool,
    ) -> tuple[str, tuple[str, ...], str | None, tuple[str, ...]]:
        # Composite PK, UNIQUE, and FK constraints ALL preserve exact column order:
        # B-Tree backing indexes for PK/UNIQUE depend on leading column order for seek semantics,
        # and composite FK columns map positionally to referenced_columns.
        if is_source:
            cols = _canonical_col_tuple(constraint.columns, col_src_to_tgt)
            if constraint.referenced_table:
                bare_ref = _strip_schema_prefix(constraint.referenced_table)
                mapped_ref_tbl = table_src_to_tgt.get(
                    constraint.referenced_table,
                    table_src_to_tgt_bare.get(bare_ref, bare_ref),
                )
                ref_tbl = canonicalize(mapped_ref_tbl)
                ref_col_map = col_src_to_tgt_by_table.get(
                    constraint.referenced_table,
                    col_map_by_bare_table.get(bare_ref, {}),
                )
                ref_cols = _canonical_col_tuple(constraint.referenced_columns, ref_col_map)
            else:
                ref_tbl = None
                ref_cols = ()
        else:
            cols = tuple(canonicalize(c) for c in constraint.columns)
            ref_tbl = (
                canonicalize(_strip_schema_prefix(constraint.referenced_table))
                if constraint.referenced_table
                else None
            )
            ref_cols = tuple(canonicalize(c) for c in constraint.referenced_columns)

        return (constraint.constraint_type, cols, ref_tbl, ref_cols)

    src_sigs = {
        _constraint_signature(c, is_source=True): c
        for c in src_table.constraints
        if c.constraint_type in {"PRIMARY KEY", "FOREIGN KEY", "UNIQUE"}
    }
    tgt_sigs = {
        _constraint_signature(c, is_source=False): c
        for c in tgt_table.constraints
        if c.constraint_type in {"PRIMARY KEY", "FOREIGN KEY", "UNIQUE"}
    }

    # Pre-index target signatures by (ctype, sorted_cols) to give clear diagnostics on order mismatches.
    tgt_by_unordered: dict[tuple[str, tuple[str, ...]], list[tuple[tuple[str, tuple[str, ...], str | None, tuple[str, ...]], ConstraintSchema]]] = {}
    for sig, tgt_c in tgt_sigs.items():
        tgt_by_unordered.setdefault((sig[0], tuple(sorted(sig[1]))), []).append((sig, tgt_c))

    for sig, src_c in sorted(src_sigs.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "", kv[0][3])):
        ctype = src_c.constraint_type
        if sig in tgt_sigs:
            tgt_c = tgt_sigs[sig]
            src_ref_suffix = (
                f" REFERENCES {src_c.referenced_table}({', '.join(src_c.referenced_columns)})"
                if src_c.referenced_table
                else ""
            )
            tgt_ref_suffix = (
                f" REFERENCES {tgt_c.referenced_table}({', '.join(tgt_c.referenced_columns)})"
                if tgt_c.referenced_table
                else ""
            )
            results.append(
                CheckResult(
                    layer=ParityLayer.STRUCTURE,
                    check_code="CONSTRAINT_PARITY",
                    status=CheckStatus.PASS,
                    severity=Severity.INFO,
                    object_type="CONSTRAINT",
                    source_object=f"{src_table.name}.{src_c.name}",
                    target_object=f"{tgt_table.name}.{tgt_c.name}",
                    source_value=f"{ctype}({', '.join(src_c.columns)}){src_ref_suffix}",
                    target_value=f"{ctype}({', '.join(tgt_c.columns)}){tgt_ref_suffix}",
                    message=f"{ctype} constraint matches on {src_table.name} -> {tgt_table.name}.",
                )
            )
        else:
            sev = Severity.CRITICAL if ctype == "PRIMARY KEY" else Severity.HIGH
            ref_hint = (
                f" REFERENCES {src_c.referenced_table}({', '.join(src_c.referenced_columns)})"
                if src_c.referenced_table
                else ""
            )
            order_hint = ""
            candidates = tgt_by_unordered.get((ctype, tuple(sorted(sig[1]))), [])
            if candidates:
                cand_sig, cand_c = candidates[0]
                if cand_sig[1] != sig[1]:
                    order_hint = (
                        f" Column order mismatch: target constraint '{cand_c.name}' uses "
                        f"({', '.join(cand_c.columns)})."
                    )
                elif ctype == "FOREIGN KEY" and (cand_sig[2] != sig[2] or cand_sig[3] != sig[3]):
                    order_hint = (
                        f" Foreign key target mismatch: target constraint '{cand_c.name}' references "
                        f"{cand_c.referenced_table}({', '.join(cand_c.referenced_columns)})."
                    )

            results.append(
                CheckResult(
                    layer=ParityLayer.STRUCTURE,
                    check_code="CONSTRAINT_MISSING",
                    status=CheckStatus.FAIL,
                    severity=sev,
                    object_type="CONSTRAINT",
                    source_object=f"{src_table.name}.{src_c.name}",
                    target_object=tgt_table.name,
                    source_value=f"{ctype}({', '.join(src_c.columns)}){ref_hint}",
                    target_value="MISSING",
                    message=(
                        f"Missing {ctype} constraint on target table '{tgt_table.name}' "
                        f"for columns ({', '.join(src_c.columns)}).{order_hint}"
                    ),
                    remediation=(
                        f"Add equivalent {ctype} constraint to PostgreSQL table "
                        f"{pg_qualified_table(tgt_table)} preserving exact column order before cutover."
                    ),
                )
            )

    for sig, tgt_c in sorted(tgt_sigs.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "", kv[0][3])):
        if sig not in src_sigs:
            tgt_ref_suffix = (
                f" REFERENCES {tgt_c.referenced_table}({', '.join(tgt_c.referenced_columns)})"
                if tgt_c.referenced_table
                else ""
            )
            results.append(
                CheckResult(
                    layer=ParityLayer.STRUCTURE,
                    check_code="CONSTRAINT_EXTRA_TARGET",
                    status=CheckStatus.WARN,
                    severity=Severity.LOW,
                    object_type="CONSTRAINT",
                    source_object=src_table.name,
                    target_object=f"{tgt_table.name}.{tgt_c.name}",
                    source_value="NONE",
                    target_value=f"{tgt_c.constraint_type}({', '.join(tgt_c.columns)}){tgt_ref_suffix}",
                    message=(
                        f"Extra {tgt_c.constraint_type} constraint '{tgt_c.name}' exists on target "
                        f"table '{tgt_table.name}' but not on source."
                    ),
                    remediation="Confirm extra target constraint is intentional and does not block ETL writes.",
                )
            )

    return results


def _compare_indexes(
    src_table: TableSchema,
    tgt_table: TableSchema,
    col_src_to_tgt: dict[str, str],
) -> list[CheckResult]:
    results: list[CheckResult] = []
    qualified_tgt = pg_qualified_table(tgt_table)

    def _index_key(idx: IndexSchema, is_source: bool) -> tuple[str, ...]:
        # Ordered tuple of canonical column tokens: (customer_id, order_date) != (order_date, customer_id).
        if is_source:
            return _canonical_col_tuple(idx.columns, col_src_to_tgt)
        return tuple(canonicalize(c) for c in idx.columns)

    # Primary key indexes are already verified by PRIMARY KEY constraint parity.
    # Deterministic precedence if duplicate column tuples exist: UNIQUE first, then name.
    src_indexes: dict[tuple[str, ...], IndexSchema] = {}
    for idx in sorted(
        (i for i in src_table.indexes if not i.is_primary),
        key=lambda i: (not i.is_unique, i.name),
    ):
        src_indexes.setdefault(_index_key(idx, is_source=True), idx)

    tgt_indexes: dict[tuple[str, ...], IndexSchema] = {}
    for idx in sorted(
        (i for i in tgt_table.indexes if not i.is_primary),
        key=lambda i: (not i.is_unique, i.name),
    ):
        tgt_indexes.setdefault(_index_key(idx, is_source=False), idx)

    tgt_by_unordered: dict[tuple[str, ...], list[IndexSchema]] = {}
    for key_cols, idx in tgt_indexes.items():
        tgt_by_unordered.setdefault(tuple(sorted(key_cols)), []).append(idx)

    for key_cols, src_idx in sorted(src_indexes.items()):
        quoted_cols = ", ".join(
            pg_quote_ident(col_src_to_tgt.get(c, c)) for c in src_idx.columns
        )
        if key_cols not in tgt_indexes:
            order_note = ""
            reordered = tgt_by_unordered.get(tuple(sorted(key_cols)), [])
            if reordered:
                order_note = (
                    f" Target has index '{reordered[0].name}' on the same columns in a different order "
                    f"({', '.join(reordered[0].columns)}), which does not provide equivalent leading-column seek semantics."
                )
            results.append(
                CheckResult(
                    layer=ParityLayer.STRUCTURE,
                    check_code="INDEX_MISSING",
                    status=CheckStatus.FAIL,
                    severity=Severity.HIGH,
                    object_type="INDEX",
                    source_object=f"{src_table.name}.{src_idx.name}",
                    target_object=tgt_table.name,
                    source_value=f"{'UNIQUE ' if src_idx.is_unique else ''}INDEX({', '.join(src_idx.columns)})",
                    target_value="MISSING",
                    message=(
                        f"Missing secondary index on target table '{tgt_table.name}' covering "
                        f"({', '.join(src_idx.columns)}). Query regression risk.{order_note}"
                    ),
                    remediation=(
                        f"CREATE {'UNIQUE ' if src_idx.is_unique else ''}INDEX CONCURRENTLY "
                        f"ON {qualified_tgt} ({quoted_cols});"
                    ),
                )
            )
            continue

        tgt_idx = tgt_indexes[key_cols]
        if src_idx.is_unique != tgt_idx.is_unique:
            quoted_tgt_idx = (
                f"{pg_quote_ident(tgt_table.schema_name)}.{pg_quote_ident(tgt_idx.name)}"
                if tgt_table.schema_name
                else pg_quote_ident(tgt_idx.name)
            )
            results.append(
                CheckResult(
                    layer=ParityLayer.STRUCTURE,
                    check_code="INDEX_UNIQUENESS_MISMATCH",
                    status=CheckStatus.FAIL,
                    severity=Severity.HIGH,
                    object_type="INDEX",
                    source_object=f"{src_table.name}.{src_idx.name}",
                    target_object=f"{tgt_table.name}.{tgt_idx.name}",
                    source_value=f"is_unique={src_idx.is_unique}",
                    target_value=f"is_unique={tgt_idx.is_unique}",
                    message=(
                        f"Index uniqueness mismatch on '{tgt_table.name}' for columns "
                        f"({', '.join(tgt_idx.columns)})."
                    ),
                    remediation=(
                        f"DROP INDEX CONCURRENTLY IF EXISTS {quoted_tgt_idx}; "
                        f"CREATE {'UNIQUE ' if src_idx.is_unique else ''}INDEX CONCURRENTLY "
                        f"ON {qualified_tgt} ({quoted_cols});"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    layer=ParityLayer.STRUCTURE,
                    check_code="INDEX_PARITY",
                    status=CheckStatus.PASS,
                    severity=Severity.INFO,
                    object_type="INDEX",
                    source_object=f"{src_table.name}.{src_idx.name}",
                    target_object=f"{tgt_table.name}.{tgt_idx.name}",
                    source_value=f"{'UNIQUE ' if src_idx.is_unique else ''}({', '.join(src_idx.columns)})",
                    target_value=f"{'UNIQUE ' if tgt_idx.is_unique else ''}({', '.join(tgt_idx.columns)})",
                    message=f"Index parity confirmed on '{tgt_table.name}' ({', '.join(tgt_idx.columns)}).",
                )
            )

    for key_cols, tgt_idx in sorted(tgt_indexes.items()):
        if key_cols not in src_indexes:
            results.append(
                CheckResult(
                    layer=ParityLayer.STRUCTURE,
                    check_code="INDEX_EXTRA_TARGET",
                    status=CheckStatus.WARN,
                    severity=Severity.LOW,
                    object_type="INDEX",
                    source_object=src_table.name,
                    target_object=f"{tgt_table.name}.{tgt_idx.name}",
                    source_value="NONE",
                    target_value=f"{'UNIQUE ' if tgt_idx.is_unique else ''}({', '.join(tgt_idx.columns)})",
                    message=(
                        f"Additional index '{tgt_idx.name}' present on PostgreSQL table '{tgt_table.name}'."
                    ),
                    remediation="Verify extra index is intentional for PostgreSQL access patterns (e.g., FK indexing).",
                )
            )

    return results


def compare_structure(
    source: Snapshot,
    target: Snapshot,
    type_map: dict[str, TypeMapRule],
    overrides: NameOverrides,
) -> StructureComparisonOutput:
    """Run Layer 1 (Naming & Mapping) and Layer 2 (Structural Parity) across source and target snapshots."""
    results: list[CheckResult] = []
    src_tables = source.table_map()
    tgt_tables = target.table_map()

    table_matches = match_names(
        source_names=src_tables.keys(),
        target_names=tgt_tables.keys(),
        overrides=overrides.table_overrides,
    )

    # Record Layer 1 table mapping results
    for inv in table_matches.invalid_overrides:
        results.append(
            CheckResult(
                layer=ParityLayer.NAMING,
                check_code="TABLE_OVERRIDE_INVALID",
                status=CheckStatus.FAIL,
                severity=Severity.HIGH,
                object_type="TABLE",
                source_object=inv.source,
                target_object=inv.target,
                source_value=inv.source,
                target_value=inv.target,
                message=inv.reason,
                remediation="Fix or remove stale entry in config/name_overrides.yaml (table_overrides).",
            )
        )

    # Catch column_overrides configured for a nonexistent source table rather than ignoring them silently.
    for override_tbl, col_map in sorted(overrides.column_overrides.items()):
        if override_tbl not in src_tables:
            for src_col_name, tgt_col_name in sorted(col_map.items()):
                results.append(
                    CheckResult(
                        layer=ParityLayer.NAMING,
                        check_code="COLUMN_OVERRIDE_INVALID",
                        status=CheckStatus.FAIL,
                        severity=Severity.HIGH,
                        object_type="COLUMN",
                        source_object=f"{override_tbl}.{src_col_name}",
                        target_object=tgt_col_name,
                        source_value=f"{override_tbl}.{src_col_name}",
                        target_value=tgt_col_name,
                        message=(
                            f"Column override table '{override_tbl}' not found in source snapshot "
                            f"(override '{src_col_name}' -> '{tgt_col_name}')."
                        ),
                        remediation="Fix or remove stale table key in config/name_overrides.yaml (column_overrides).",
                    )
                )

    for collision in table_matches.collisions:
        results.append(
            CheckResult(
                layer=ParityLayer.NAMING,
                check_code="TABLE_CANONICAL_COLLISION",
                status=CheckStatus.FAIL,
                severity=Severity.CRITICAL,
                object_type="TABLE",
                source_object=", ".join(collision.source_names) or None,
                target_object=", ".join(collision.target_names) or None,
                source_value=collision.canonical_token,
                target_value=collision.canonical_token,
                message=(
                    f"Ambiguous table canonical token '{collision.canonical_token}': "
                    f"source={list(collision.source_names)} vs target={list(collision.target_names)}."
                ),
                remediation="Disambiguate explicitly under table_overrides in config/name_overrides.yaml.",
            )
        )

    for src_name in table_matches.unmatched_source:
        canon = canonicalize(src_name)
        empty_note = (
            " Identifier produces an empty canonical token and requires an explicit table_overrides entry."
            if canon == ""
            else ""
        )
        results.append(
            CheckResult(
                layer=ParityLayer.NAMING,
                check_code="TABLE_MISSING_TARGET",
                status=CheckStatus.FAIL,
                severity=Severity.CRITICAL,
                object_type="TABLE",
                source_object=src_name,
                target_object=None,
                source_value=src_name,
                target_value="MISSING",
                message=(
                    f"Source table '{src_name}' has no matching table in target PostgreSQL snapshot."
                    f"{empty_note}"
                ),
                remediation="Migrate missing table or add explicit mapping in config/name_overrides.yaml.",
            )
        )

    for tgt_name in table_matches.unmatched_target:
        results.append(
            CheckResult(
                layer=ParityLayer.NAMING,
                check_code="TABLE_EXTRA_TARGET",
                status=CheckStatus.WARN,
                severity=Severity.MEDIUM,
                object_type="TABLE",
                source_object=None,
                target_object=tgt_name,
                source_value="NONE",
                target_value=tgt_name,
                message=f"Target table '{tgt_name}' has no counterpart in source SQL Server snapshot.",
                remediation="Confirm whether target table is newly introduced or requires a table_overrides entry.",
            )
        )

    table_src_to_tgt = table_matches.source_to_target()
    matched_pairs = sorted(table_matches.matched, key=lambda p: p.source)
    column_matches_by_table: dict[str, NameMatchSet] = {}
    col_src_to_tgt_by_table: dict[str, dict[str, str]] = {}

    # Pass 1: Resolve Layer 1 column mappings for ALL matched tables before checking cross-table FKs.
    for pair in matched_pairs:
        src_table = src_tables[pair.source]
        tgt_table = tgt_tables[pair.target]

        results.append(
            CheckResult(
                layer=ParityLayer.NAMING,
                check_code="TABLE_MAPPED",
                status=CheckStatus.PASS,
                severity=Severity.INFO,
                object_type="TABLE",
                source_object=src_table.name,
                target_object=tgt_table.name,
                source_value=src_table.name,
                target_value=tgt_table.name,
                message=f"Table '{src_table.name}' mapped to '{tgt_table.name}' via {pair.method}.",
            )
        )

        src_cols = src_table.column_map()
        tgt_cols = tgt_table.column_map()
        col_overrides = overrides.column_overrides.get(src_table.name, {})
        col_matches = match_names(
            source_names=src_cols.keys(),
            target_names=tgt_cols.keys(),
            overrides=col_overrides,
        )
        column_matches_by_table[src_table.name] = col_matches
        col_src_to_tgt_by_table[src_table.name] = col_matches.source_to_target()

        for inv in col_matches.invalid_overrides:
            results.append(
                CheckResult(
                    layer=ParityLayer.NAMING,
                    check_code="COLUMN_OVERRIDE_INVALID",
                    status=CheckStatus.FAIL,
                    severity=Severity.HIGH,
                    object_type="COLUMN",
                    source_object=f"{src_table.name}.{inv.source}",
                    target_object=f"{tgt_table.name}.{inv.target}",
                    source_value=inv.source,
                    target_value=inv.target,
                    message=inv.reason,
                    remediation="Update column_overrides in config/name_overrides.yaml.",
                )
            )

        for collision in col_matches.collisions:
            src_col_obj = (
                f"{src_table.name}.({', '.join(collision.source_names)})"
                if collision.source_names
                else src_table.name
            )
            tgt_col_obj = (
                f"{tgt_table.name}.({', '.join(collision.target_names)})"
                if collision.target_names
                else tgt_table.name
            )
            results.append(
                CheckResult(
                    layer=ParityLayer.NAMING,
                    check_code="COLUMN_CANONICAL_COLLISION",
                    status=CheckStatus.FAIL,
                    severity=Severity.CRITICAL,
                    object_type="COLUMN",
                    source_object=src_col_obj,
                    target_object=tgt_col_obj,
                    source_value=collision.canonical_token,
                    target_value=collision.canonical_token,
                    message=(
                        f"Ambiguous column canonical token '{collision.canonical_token}' in table "
                        f"'{src_table.name}': source={list(collision.source_names)} vs target={list(collision.target_names)}."
                    ),
                    remediation="Add explicit column_overrides entries to disambiguate colliding columns.",
                )
            )

        for missing_col in col_matches.unmatched_source:
            canon = canonicalize(missing_col)
            canon_desc = (
                "empty canonical token; explicit column_overrides required"
                if canon == ""
                else f"canonical token: '{canon}'"
            )
            results.append(
                CheckResult(
                    layer=ParityLayer.NAMING,
                    check_code="COLUMN_MISSING_TARGET",
                    status=CheckStatus.FAIL,
                    severity=Severity.CRITICAL,
                    object_type="COLUMN",
                    source_object=f"{src_table.name}.{missing_col}",
                    target_object=tgt_table.name,
                    source_value=missing_col,
                    target_value="MISSING",
                    message=(
                        f"Source column '{src_table.name}.{missing_col}' has no matching column on "
                        f"target table '{tgt_table.name}' ({canon_desc})."
                    ),
                    remediation=(
                        "Add the missing column to target PostgreSQL DDL or map an intentional rename "
                        "in config/name_overrides.yaml."
                    ),
                )
            )

        for extra_col in col_matches.unmatched_target:
            canon = canonicalize(extra_col)
            canon_desc = (
                "empty canonical token; explicit column_overrides required"
                if canon == ""
                else f"canonical token: '{canon}'"
            )
            results.append(
                CheckResult(
                    layer=ParityLayer.NAMING,
                    check_code="COLUMN_EXTRA_TARGET",
                    status=CheckStatus.WARN,
                    severity=Severity.MEDIUM,
                    object_type="COLUMN",
                    source_object=src_table.name,
                    target_object=f"{tgt_table.name}.{extra_col}",
                    source_value="NONE",
                    target_value=extra_col,
                    message=(
                        f"Target column '{tgt_table.name}.{extra_col}' has no counterpart in source "
                        f"table '{src_table.name}' ({canon_desc})."
                    ),
                    remediation=(
                        "Verify if this is an unmapped rename/abbreviation requiring column_overrides "
                        "or an intentional target-only column."
                    ),
                )
            )

    # Pass 2: Layer 2 structural parity (data types, nullability, constraints with full FK mapping, indexes).
    for pair in matched_pairs:
        src_table = src_tables[pair.source]
        tgt_table = tgt_tables[pair.target]
        src_cols = src_table.column_map()
        tgt_cols = tgt_table.column_map()
        col_matches = column_matches_by_table[src_table.name]
        col_src_to_tgt = col_src_to_tgt_by_table[src_table.name]
        qualified_tgt = pg_qualified_table(tgt_table)

        for col_pair in sorted(col_matches.matched, key=lambda cp: src_cols[cp.source].ordinal_position):
            s_col = src_cols[col_pair.source]
            t_col = tgt_cols[col_pair.target]

            type_check = check_type_compatibility(s_col, t_col, type_map)
            type_sev = (
                Severity.INFO
                if type_check.status == CheckStatus.PASS
                else (Severity.MEDIUM if type_check.status == CheckStatus.WARN else Severity.HIGH)
            )
            results.append(
                CheckResult(
                    layer=ParityLayer.STRUCTURE,
                    check_code="COLUMN_TYPE_COMPATIBILITY",
                    status=type_check.status,
                    severity=type_sev,
                    object_type="COLUMN",
                    source_object=f"{src_table.name}.{s_col.name}",
                    target_object=f"{tgt_table.name}.{t_col.name}",
                    source_value=_format_col_type(
                        s_col.data_type, s_col.max_length, s_col.numeric_precision, s_col.numeric_scale
                    ),
                    target_value=_format_col_type(
                        t_col.data_type, t_col.max_length, t_col.numeric_precision, t_col.numeric_scale
                    ),
                    message=type_check.message,
                    remediation=type_check.remediation,
                )
            )

            # Nullability check
            if s_col.is_nullable == t_col.is_nullable:
                results.append(
                    CheckResult(
                        layer=ParityLayer.STRUCTURE,
                        check_code="COLUMN_NULLABILITY",
                        status=CheckStatus.PASS,
                        severity=Severity.INFO,
                        object_type="COLUMN",
                        source_object=f"{src_table.name}.{s_col.name}",
                        target_object=f"{tgt_table.name}.{t_col.name}",
                        source_value="NULL" if s_col.is_nullable else "NOT NULL",
                        target_value="NULL" if t_col.is_nullable else "NOT NULL",
                        message=f"Nullability matches on '{tgt_table.name}.{t_col.name}'.",
                    )
                )
            elif not s_col.is_nullable and t_col.is_nullable:
                # Loosening NOT NULL -> NULL compromises data integrity
                results.append(
                    CheckResult(
                        layer=ParityLayer.STRUCTURE,
                        check_code="COLUMN_NULLABILITY_MISMATCH",
                        status=CheckStatus.FAIL,
                        severity=Severity.HIGH,
                        object_type="COLUMN",
                        source_object=f"{src_table.name}.{s_col.name}",
                        target_object=f"{tgt_table.name}.{t_col.name}",
                        source_value="NOT NULL",
                        target_value="NULL",
                        message=(
                            f"Nullability regression: '{src_table.name}.{s_col.name}' is NOT NULL "
                            f"in SQL Server but NULLABLE in PostgreSQL '{tgt_table.name}.{t_col.name}'."
                        ),
                        remediation=(
                            f"ALTER TABLE {qualified_tgt} "
                            f"ALTER COLUMN {pg_quote_ident(t_col.name)} SET NOT NULL;"
                        ),
                    )
                )
            else:
                # Tightening NULL -> NOT NULL may fail ETL loads if historical NULLs exist
                results.append(
                    CheckResult(
                        layer=ParityLayer.STRUCTURE,
                        check_code="COLUMN_NULLABILITY_MISMATCH",
                        status=CheckStatus.WARN,
                        severity=Severity.MEDIUM,
                        object_type="COLUMN",
                        source_object=f"{src_table.name}.{s_col.name}",
                        target_object=f"{tgt_table.name}.{t_col.name}",
                        source_value="NULL",
                        target_value="NOT NULL",
                        message=(
                            f"Nullability tightened: '{src_table.name}.{s_col.name}' allows NULL "
                            f"in SQL Server but '{tgt_table.name}.{t_col.name}' is NOT NULL in PostgreSQL."
                        ),
                        remediation=(
                            "Verify zero NULL rows exist in source or relax target NOT NULL constraint."
                        ),
                    )
                )

        results.extend(
            _compare_constraints(
                src_table,
                tgt_table,
                table_src_to_tgt,
                col_src_to_tgt,
                col_src_to_tgt_by_table,
            )
        )
        results.extend(_compare_indexes(src_table, tgt_table, col_src_to_tgt))

    return StructureComparisonOutput(
        results=results,
        table_matches=table_matches,
        column_matches_by_table=column_matches_by_table,
    )
