"""Snapshot JSON loader, structural validator, and YAML configuration loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from src.snapshot.models import (
    ColumnAggregate,
    ColumnSchema,
    ConstraintSchema,
    IndexSchema,
    NameOverrides,
    RoutineSchema,
    SecurityGrant,
    Snapshot,
    TableDataStats,
    TableSchema,
    TypeMapRule,
)

VALID_ENGINES = {"mssql", "postgresql"}
VALID_CONSTRAINT_TYPES = {"PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK"}
VALID_ROUTINE_TYPES = {"PROCEDURE", "FUNCTION"}


class SnapshotValidationError(ValueError):
    """Raised when a frozen snapshot JSON file violates required schema invariants."""


def _require_keys(payload: dict[str, Any], required: tuple[str, ...], context: str) -> None:
    missing = [key for key in required if key not in payload or payload[key] is None]
    if missing:
        raise SnapshotValidationError(
            f"Missing required field(s) {missing} in {context}."
        )


def _parse_column(raw: dict[str, Any], table_name: str) -> ColumnSchema:
    _require_keys(raw, ("name", "data_type", "is_nullable", "ordinal_position"), f"table '{table_name}' column")
    return ColumnSchema(
        name=str(raw["name"]),
        data_type=str(raw["data_type"]),
        is_nullable=bool(raw["is_nullable"]),
        ordinal_position=int(raw["ordinal_position"]),
        max_length=int(raw["max_length"]) if raw.get("max_length") is not None else None,
        numeric_precision=(
            int(raw["numeric_precision"]) if raw.get("numeric_precision") is not None else None
        ),
        numeric_scale=int(raw["numeric_scale"]) if raw.get("numeric_scale") is not None else None,
        default_value=str(raw["default_value"]) if raw.get("default_value") is not None else None,
        is_identity=bool(raw.get("is_identity", False)),
    )


def _parse_constraint(raw: dict[str, Any], table_name: str) -> ConstraintSchema:
    _require_keys(raw, ("name", "constraint_type", "columns"), f"table '{table_name}' constraint")
    ctype = str(raw["constraint_type"]).upper().strip()
    if ctype not in VALID_CONSTRAINT_TYPES:
        raise SnapshotValidationError(
            f"Unsupported constraint_type '{ctype}' on table '{table_name}'. "
            f"Expected one of {sorted(VALID_CONSTRAINT_TYPES)}."
        )
    return ConstraintSchema(
        name=str(raw["name"]),
        constraint_type=ctype,
        columns=tuple(str(c) for c in raw["columns"]),
        referenced_table=str(raw["referenced_table"]) if raw.get("referenced_table") else None,
        referenced_columns=tuple(str(c) for c in raw.get("referenced_columns", [])),
        definition=str(raw["definition"]) if raw.get("definition") is not None else None,
    )


def _parse_index(raw: dict[str, Any], table_name: str) -> IndexSchema:
    _require_keys(raw, ("name", "columns"), f"table '{table_name}' index")
    return IndexSchema(
        name=str(raw["name"]),
        columns=tuple(str(c) for c in raw["columns"]),
        is_unique=bool(raw.get("is_unique", False)),
        is_primary=bool(raw.get("is_primary", False)),
        index_type=str(raw.get("index_type", "BTREE")).upper(),
        include_columns=tuple(str(c) for c in raw.get("include_columns", [])),
        filter_predicate=str(raw["filter_predicate"]) if raw.get("filter_predicate") else None,
    )


def _parse_table(raw: dict[str, Any], default_schema: str) -> TableSchema:
    _require_keys(raw, ("name", "columns"), "table entry")
    table_name = str(raw["name"])
    columns_raw = raw.get("columns", [])
    if not isinstance(columns_raw, list) or not columns_raw:
        raise SnapshotValidationError(f"Table '{table_name}' must contain at least one column.")

    columns = tuple(_parse_column(col, table_name) for col in columns_raw)
    seen_cols: set[str] = set()
    for col in columns:
        if col.name in seen_cols:
            raise SnapshotValidationError(
                f"Duplicate exact column name '{col.name}' in table '{table_name}'."
            )
        seen_cols.add(col.name)

    constraints = tuple(_parse_constraint(c, table_name) for c in raw.get("constraints", []))
    indexes = tuple(_parse_index(i, table_name) for i in raw.get("indexes", []))

    return TableSchema(
        name=table_name,
        schema_name=str(raw.get("schema_name", default_schema)),
        columns=columns,
        constraints=constraints,
        indexes=indexes,
    )


def _parse_routine(raw: dict[str, Any], default_schema: str) -> RoutineSchema:
    _require_keys(raw, ("name", "routine_type"), "routine entry")
    rtype = str(raw["routine_type"]).upper().strip()
    if rtype not in VALID_ROUTINE_TYPES:
        raise SnapshotValidationError(
            f"Unsupported routine_type '{rtype}' for routine '{raw['name']}'."
        )
    return RoutineSchema(
        name=str(raw["name"]),
        schema_name=str(raw.get("schema_name", default_schema)),
        routine_type=rtype,
        parameter_count=int(raw.get("parameter_count", 0)),
        return_type=str(raw["return_type"]) if raw.get("return_type") is not None else None,
        definition_hash=str(raw["definition_hash"]) if raw.get("definition_hash") else None,
    )


def _parse_security_grant(raw: dict[str, Any]) -> SecurityGrant:
    _require_keys(raw, ("principal", "object_type", "object_name", "privilege"), "security_grant entry")
    return SecurityGrant(
        principal=str(raw["principal"]),
        object_type=str(raw["object_type"]).upper().strip(),
        object_name=str(raw["object_name"]),
        privilege=str(raw["privilege"]).upper().strip(),
        is_grantable=bool(raw.get("is_grantable", False)),
        is_superuser=bool(raw.get("is_superuser", False)),
    )


def _parse_data_stats(raw: dict[str, Any]) -> TableDataStats:
    _require_keys(raw, ("table_name", "row_count"), "data_stats entry")
    row_count = int(raw["row_count"])
    if row_count < 0:
        raise SnapshotValidationError(
            f"Negative row_count ({row_count}) for table '{raw['table_name']}'."
        )
    aggregates = tuple(
        ColumnAggregate(
            column_name=str(agg["column_name"]),
            sum_value=str(agg["sum_value"]) if agg.get("sum_value") is not None else None,
            min_value=str(agg["min_value"]) if agg.get("min_value") is not None else None,
            max_value=str(agg["max_value"]) if agg.get("max_value") is not None else None,
            null_count=int(agg.get("null_count", 0)),
        )
        for agg in raw.get("column_aggregates", [])
    )
    return TableDataStats(
        table_name=str(raw["table_name"]),
        row_count=row_count,
        aggregate_checksum=(
            str(raw["aggregate_checksum"]) if raw.get("aggregate_checksum") is not None else None
        ),
        column_aggregates=aggregates,
    )


def load_snapshot(path: str | Path) -> Snapshot:
    """Load and validate an immutable JSON database snapshot from disk."""
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot file not found: {snapshot_path}")

    with snapshot_path.open("r", encoding="utf-8") as fh:
        try:
            payload = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SnapshotValidationError(f"Invalid JSON in {snapshot_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SnapshotValidationError(f"Snapshot root must be a JSON object: {snapshot_path}")

    _require_keys(
        payload,
        ("engine", "server_name", "database_name", "schema_name", "captured_at"),
        f"snapshot '{snapshot_path.name}'",
    )

    engine = str(payload["engine"]).lower().strip()
    if engine not in VALID_ENGINES:
        raise SnapshotValidationError(
            f"Unsupported engine '{engine}' in {snapshot_path}. Expected one of {sorted(VALID_ENGINES)}."
        )

    default_schema = str(payload["schema_name"])
    tables = tuple(_parse_table(t, default_schema) for t in payload.get("tables", []))

    seen_tables: set[str] = set()
    for table in tables:
        if table.name in seen_tables:
            raise SnapshotValidationError(f"Duplicate exact table name '{table.name}' in snapshot.")
        seen_tables.add(table.name)

    routines = tuple(_parse_routine(r, default_schema) for r in payload.get("routines", []))
    security_grants = tuple(_parse_security_grant(g) for g in payload.get("security_grants", []))
    data_stats = tuple(_parse_data_stats(d) for d in payload.get("data_stats", []))

    return Snapshot(
        engine=engine,
        server_name=str(payload["server_name"]),
        database_name=str(payload["database_name"]),
        schema_name=default_schema,
        captured_at=str(payload["captured_at"]),
        capture_version=str(payload.get("capture_version", "1.0")),
        tables=tables,
        routines=routines,
        security_grants=security_grants,
        data_stats=data_stats,
    )


def load_snapshot_pair(snapshot_dir: str | Path) -> tuple[Snapshot, Snapshot]:
    """Load source (mssql) and target (pg) snapshots from a run directory."""
    root = Path(snapshot_dir)
    mssql_path = root / "mssql" / "snapshot.json"
    pg_path = root / "pg" / "snapshot.json"

    source = load_snapshot(mssql_path)
    target = load_snapshot(pg_path)

    if source.engine != "mssql":
        raise SnapshotValidationError(
            f"Expected source snapshot engine 'mssql', got '{source.engine}'."
        )
    if target.engine != "postgresql":
        raise SnapshotValidationError(
            f"Expected target snapshot engine 'postgresql', got '{target.engine}'."
        )
    return source, target


def load_type_map(path: str | Path) -> dict[str, TypeMapRule]:
    """Load the SQL Server -> PostgreSQL data type compatibility matrix from YAML."""
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"Type map configuration not found: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    types_section = raw.get("types", {})
    if not isinstance(types_section, dict) or not types_section:
        raise ValueError(f"Type map '{yaml_path}' contains no 'types' entries.")

    rules: dict[str, TypeMapRule] = {}
    for src_type, spec in types_section.items():
        norm_src = str(src_type).strip().lower()
        rules[norm_src] = TypeMapRule(
            source_type=norm_src,
            compatible=tuple(str(t).strip().lower() for t in spec.get("compatible", [])),
            warn=tuple(str(t).strip().lower() for t in spec.get("warn", [])),
            note=str(spec.get("note", "")),
        )
    return rules


def load_name_overrides(path: str | Path | None) -> NameOverrides:
    """Load optional explicit object rename and retirement overrides from YAML."""
    if path is None:
        return NameOverrides()
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"Name overrides file not found: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    return NameOverrides(
        table_overrides={str(k): str(v) for k, v in (raw.get("table_overrides") or {}).items()},
        column_overrides={
            str(tbl): {str(ck): str(cv) for ck, cv in (cols or {}).items()}
            for tbl, cols in (raw.get("column_overrides") or {}).items()
        },
        routine_overrides={str(k): str(v) for k, v in (raw.get("routine_overrides") or {}).items()},
        routine_retirements={
            str(k): {str(mk): str(mv) for mk, mv in (meta or {}).items()}
            for k, meta in (raw.get("routine_retirements") or {}).items()
        },
        role_overrides={str(k): str(v) for k, v in (raw.get("role_overrides") or {}).items()},
    )
