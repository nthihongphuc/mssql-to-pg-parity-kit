"""Typed immutable dataclasses representing frozen database snapshots and parity audit results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CheckStatus(str, Enum):
    """Outcome of an individual parity verification check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


class Severity(str, Enum):
    """Operational severity of a parity finding."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ParityLayer(str, Enum):
    """Five-layer heterogeneous migration parity model."""

    NAMING = "naming"
    STRUCTURE = "structure"
    DATA = "data"
    ROUTINES = "routines"
    SECURITY = "security"


@dataclass(frozen=True)
class ColumnSchema:
    """Immutable metadata for a single table column."""

    name: str
    data_type: str
    is_nullable: bool
    ordinal_position: int
    max_length: Optional[int] = None
    numeric_precision: Optional[int] = None
    numeric_scale: Optional[int] = None
    default_value: Optional[str] = None
    is_identity: bool = False


@dataclass(frozen=True)
class ConstraintSchema:
    """Immutable metadata for a table constraint (PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK)."""

    name: str
    constraint_type: str  # PRIMARY KEY | FOREIGN KEY | UNIQUE | CHECK
    columns: tuple[str, ...]
    referenced_table: Optional[str] = None
    referenced_columns: tuple[str, ...] = ()
    definition: Optional[str] = None


@dataclass(frozen=True)
class IndexSchema:
    """Immutable metadata for a table index."""

    name: str
    columns: tuple[str, ...]
    is_unique: bool = False
    is_primary: bool = False
    index_type: str = "BTREE"
    include_columns: tuple[str, ...] = ()
    filter_predicate: Optional[str] = None


@dataclass(frozen=True)
class TableSchema:
    """Immutable metadata for a relational table."""

    name: str
    schema_name: str
    columns: tuple[ColumnSchema, ...]
    constraints: tuple[ConstraintSchema, ...] = ()
    indexes: tuple[IndexSchema, ...] = ()

    def column_map(self) -> dict[str, ColumnSchema]:
        """Return a mapping of exact column name to ColumnSchema."""
        return {col.name: col for col in self.columns}


@dataclass(frozen=True)
class RoutineSchema:
    """Immutable metadata for a stored procedure or database function."""

    name: str
    schema_name: str
    routine_type: str  # PROCEDURE | FUNCTION
    parameter_count: int = 0
    return_type: Optional[str] = None
    definition_hash: Optional[str] = None


@dataclass(frozen=True)
class SecurityGrant:
    """Immutable metadata for a role/principal privilege grant."""

    principal: str
    object_type: str  # TABLE | ROUTINE | SCHEMA | DATABASE
    object_name: str
    privilege: str  # SELECT | INSERT | UPDATE | DELETE | EXECUTE | ALL PRIVILEGES
    is_grantable: bool = False
    is_superuser: bool = False


@dataclass(frozen=True)
class ColumnAggregate:
    """Immutable numeric/range aggregate statistics for a single column."""

    column_name: str
    sum_value: Optional[str] = None  # Stored as exact decimal string to avoid float drift
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    null_count: int = 0


@dataclass(frozen=True)
class TableDataStats:
    """Immutable row-level statistics and aggregate checksums captured for a table."""

    table_name: str
    row_count: int
    aggregate_checksum: Optional[str] = None
    column_aggregates: tuple[ColumnAggregate, ...] = ()

    def aggregate_map(self) -> dict[str, ColumnAggregate]:
        """Return a mapping of exact column name to ColumnAggregate."""
        return {agg.column_name: agg for agg in self.column_aggregates}


@dataclass(frozen=True)
class Snapshot:
    """Frozen point-in-time read-only capture of a single database instance."""

    engine: str  # mssql | postgresql
    server_name: str
    database_name: str
    schema_name: str
    captured_at: str
    capture_version: str = "1.0"
    tables: tuple[TableSchema, ...] = ()
    routines: tuple[RoutineSchema, ...] = ()
    security_grants: tuple[SecurityGrant, ...] = ()
    data_stats: tuple[TableDataStats, ...] = ()

    def table_map(self) -> dict[str, TableSchema]:
        """Return a mapping of exact table name to TableSchema."""
        return {table.name: table for table in self.tables}

    def routine_map(self) -> dict[str, RoutineSchema]:
        """Return a mapping of exact routine name to RoutineSchema."""
        return {routine.name: routine for routine in self.routines}

    def data_stats_map(self) -> dict[str, TableDataStats]:
        """Return a mapping of exact table name to TableDataStats."""
        return {stats.table_name: stats for stats in self.data_stats}


@dataclass(frozen=True)
class TypeMapRule:
    """Type compatibility rule from SQL Server base type to PostgreSQL target types."""

    source_type: str
    compatible: tuple[str, ...]
    warn: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class NameOverrides:
    """Explicit override configuration for intentional migration renames and retirements."""

    table_overrides: dict[str, str] = field(default_factory=dict)
    column_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    routine_overrides: dict[str, str] = field(default_factory=dict)
    routine_retirements: dict[str, dict[str, str]] = field(default_factory=dict)
    role_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResult:
    """Single atomic parity verification finding recorded in reports and audit tables."""

    layer: ParityLayer
    check_code: str
    status: CheckStatus
    severity: Severity
    object_type: str
    source_object: Optional[str]
    target_object: Optional[str]
    source_value: Optional[str]
    target_value: Optional[str]
    message: str
    remediation: Optional[str] = None


@dataclass
class ParityReport:
    """Complete multi-layer parity verification report for a migration snapshot pair."""

    run_id: str
    source_snapshot: Snapshot
    target_snapshot: Snapshot
    generated_at: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.PASS)

    @property
    def warn_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.WARN)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.FAIL)

    @property
    def skip_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.SKIP)

    @property
    def overall_status(self) -> CheckStatus:
        if self.fail_count > 0:
            return CheckStatus.FAIL
        if self.warn_count > 0:
            return CheckStatus.WARN
        return CheckStatus.PASS

    def results_by_layer(self, layer: ParityLayer) -> list[CheckResult]:
        return [r for r in self.results if r.layer == layer]
