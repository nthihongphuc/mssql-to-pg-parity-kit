"""Strictly read-only live database snapshot capture engine for PostgreSQL and SQL Server.

Design invariants (ADR-003):
  1. Every catalog query from ``src/snapshot/queries.py`` and every dynamically generated
     aggregate query MUST pass ``validate_read_only_sql(sql, dialect=...)`` BEFORE execution.
  2. PostgreSQL captures run inside an explicit read-only transaction
     (``BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = ...; ... ROLLBACK;``)
     via optional ``psycopg`` (v3), ``psycopg2``, or built-in ``psql`` subprocess adapter.
  3. SQL Server captures enforce ``ApplicationIntent=ReadOnly`` in the connection string,
     ``readonly=True`` at the ODBC driver layer, ``SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED``,
     ``SET LOCK_TIMEOUT 5000``, and unconditional ``ROLLBACK`` on close via optional ``pyodbc``.
  4. Injectable ``QueryRunner`` protocol allows deterministic offline unit testing without live
     drivers while keeping live execution strictly read-only.
  5. Serialized JSON snapshots are deterministic (sorted keys, UTC ISO-8601 ``captured_at``,
     UTF-8 LF) and 100% round-trip compatible with ``src/snapshot/loader.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from src.comparators.naming import normalize_base_type
from src.comparators.structure import pg_quote_ident
from src.snapshot.models import (
    ColumnAggregate,
    ColumnSchema,
    ConstraintSchema,
    IndexSchema,
    RoutineSchema,
    SecurityGrant,
    Snapshot,
    TableDataStats,
    TableSchema,
)
from src.snapshot.queries import (
    MSSQL_QUERIES,
    POSTGRES_QUERIES,
    SnapshotQuery,
    UnsafeSnapshotQueryError,
    validate_read_only_sql,
)

_PG_NUMERIC_TYPES = frozenset(
    {
        "smallint",
        "int2",
        "integer",
        "int",
        "int4",
        "bigint",
        "int8",
        "decimal",
        "numeric",
        "real",
        "float4",
        "double precision",
        "float8",
    }
)

_MSSQL_INTEGER_TYPES = frozenset({"tinyint", "smallint", "int", "bigint"})
_MSSQL_NUMERIC_TYPES = _MSSQL_INTEGER_TYPES | frozenset(
    {"decimal", "numeric", "money", "smallmoney", "float", "real"}
)
_MSSQL_LENGTH_BEARING_TYPES = frozenset(
    {"varchar", "nvarchar", "char", "nchar", "sysname", "varbinary", "binary"}
)

_PG_PARAM_RE = re.compile(r"\$1\b")
_MSSQL_PARAM_RE = re.compile(r"@schema_name\b", re.IGNORECASE)


class MissingDriverError(RuntimeError):
    """Raised when no compatible database client driver or CLI binary is installed."""


class SnapshotCaptureError(RuntimeError):
    """Raised when live snapshot capture encounters a configuration or runtime error."""


@runtime_checkable
class QueryRunner(Protocol):
    """Minimal read-only query execution protocol for live adapters and unit-test mocks."""

    def fetch_all(
        self,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        *,
        query_name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Execute a validated read-only SQL query and return rows as column-name dicts."""
        ...


# =============================================================================
# Identifier Quoting, Parameter Binding & Connection String Hardening
# =============================================================================


def _reject_nul_identifier(ident: str) -> str:
    if "\x00" in ident:
        raise UnsafeSnapshotQueryError("Identifier contains forbidden NUL byte.")
    return ident


def _safe_pg_quote_ident(ident: str) -> str:
    return pg_quote_ident(_reject_nul_identifier(ident))


def mssql_quote_ident(ident: str) -> str:
    """Safely bracket-quote a SQL Server identifier, escaping embedded closing brackets."""
    clean = _reject_nul_identifier(ident)
    escaped = clean.replace("]", "]]")
    return f"[{escaped}]"


def _validate_positive_timeout_ms(timeout_ms: int, label: str) -> int:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise ValueError(f"{label} must be a positive integer in milliseconds, got {timeout_ms!r}.")
    return timeout_ms


def _format_pg_timeout(timeout_ms: int) -> str:
    validated = _validate_positive_timeout_ms(timeout_ms, "statement_timeout_ms")
    if validated % 1000 == 0:
        return f"{validated // 1000}s"
    return f"{validated}ms"


def enforce_mssql_readonly_conn_str(conn_str: str) -> str:
    """Enforce ``ApplicationIntent=ReadOnly`` on a SQL Server ODBC connection string.

    Preserves all other key/value attributes while replacing any existing non-ReadOnly
    ``ApplicationIntent`` (e.g., ``ReadWrite``) or appending ``ApplicationIntent=ReadOnly``
    when absent.
    """
    if not conn_str or not conn_str.strip():
        raise ValueError("SQL Server connection string must be non-empty.")

    parts: list[str] = []
    for segment in conn_str.split(";"):
        stripped = segment.strip()
        if not stripped:
            continue
        if "=" in stripped:
            key, _ = stripped.split("=", 1)
            if key.strip().lower() == "applicationintent":
                continue
        parts.append(stripped)

    parts.append("ApplicationIntent=ReadOnly")
    return ";".join(parts) + ";"


def resolve_dsn(dsn: Optional[str] = None, dsn_env: Optional[str] = None) -> str:
    """Resolve a database connection string from either direct value or an environment variable."""
    if dsn and dsn_env:
        raise ValueError("Specify either dsn or dsn_env, not both.")
    if dsn_env:
        env_val = os.environ.get(dsn_env, "").strip()
        if not env_val:
            raise SnapshotCaptureError(
                f"Environment variable '{dsn_env}' is not set or empty."
            )
        return env_val
    if dsn and dsn.strip():
        return dsn.strip()
    raise ValueError("A connection DSN must be provided via --dsn or --dsn-env.")


# =============================================================================
# Read-Only Driver Adapters (PostgreSQL & SQL Server)
# =============================================================================


def _rows_from_cursor(cursor: Any) -> list[dict[str, Any]]:
    if not cursor.description:
        return []
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _bind_pg_named_params(sql: str, params: Optional[dict[str, Any]]) -> tuple[str, Optional[dict[str, Any]]]:
    if not params:
        return sql, None
    if "schema_name" in params and _PG_PARAM_RE.search(sql):
        return _PG_PARAM_RE.sub("%(schema_name)s", sql), {"schema_name": params["schema_name"]}
    return sql, params


def _bind_mssql_positional_params(sql: str, params: Optional[dict[str, Any]]) -> tuple[str, list[Any]]:
    if not params or "schema_name" not in params:
        return sql, []
    matches = _MSSQL_PARAM_RE.findall(sql)
    if not matches:
        return sql, []
    return _MSSQL_PARAM_RE.sub("?", sql), [params["schema_name"]] * len(matches)


class _PsycopgBaseQueryRunner:
    """Shared transactional read-only PostgreSQL runner for psycopg (v3) and psycopg2."""

    def __init__(self, conn: Any, statement_timeout_ms: int = 15_000) -> None:
        self._conn = conn
        self._timeout_literal = _format_pg_timeout(statement_timeout_ms)
        self._cur = self._conn.cursor()
        try:
            self._cur.execute("BEGIN TRANSACTION READ ONLY;")
            self._cur.execute(f"SET LOCAL statement_timeout = '{self._timeout_literal}';")
        except Exception:
            self.close()
            raise

    def fetch_all(
        self,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        *,
        query_name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        _ = query_name
        bound_sql, bound_params = _bind_pg_named_params(sql, params)
        if bound_params:
            self._cur.execute(bound_sql, bound_params)
        else:
            self._cur.execute(bound_sql)
        return _rows_from_cursor(self._cur)

    def close(self) -> None:
        try:
            if self._cur is not None and not getattr(self._cur, "closed", False):
                try:
                    self._cur.execute("ROLLBACK;")
                except Exception:
                    pass
                self._cur.close()
        finally:
            if self._conn is not None and not getattr(self._conn, "closed", False):
                self._conn.close()

    def __enter__(self) -> _PsycopgBaseQueryRunner:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class Psycopg3QueryRunner(_PsycopgBaseQueryRunner):
    """PostgreSQL read-only query runner backed by ``psycopg`` (v3)."""

    def __init__(self, dsn: str, statement_timeout_ms: int = 15_000) -> None:
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingDriverError(
                "PostgreSQL driver 'psycopg' (v3) is not installed. "
                "Install it with: pip install 'psycopg[binary]'"
            ) from exc
        conn = psycopg.connect(dsn, autocommit=True)
        super().__init__(conn, statement_timeout_ms=statement_timeout_ms)


class Psycopg2QueryRunner(_PsycopgBaseQueryRunner):
    """PostgreSQL read-only query runner backed by ``psycopg2``."""

    def __init__(self, dsn: str, statement_timeout_ms: int = 15_000) -> None:
        try:
            import psycopg2  # type: ignore[import-untyped]
        except ImportError as exc:
            raise MissingDriverError(
                "PostgreSQL driver 'psycopg2' is not installed. "
                "Install it with: pip install psycopg2-binary"
            ) from exc
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        super().__init__(conn, statement_timeout_ms=statement_timeout_ms)


class PsqlSubprocessQueryRunner:
    """Zero-C-extension PostgreSQL read-only query runner backed by the ``psql`` CLI."""

    def __init__(
        self,
        dsn: str,
        statement_timeout_ms: int = 15_000,
        psql_binary: Optional[str] = None,
    ) -> None:
        resolved_bin = psql_binary or shutil.which("psql")
        if not resolved_bin:
            raise MissingDriverError(
                "No PostgreSQL client available: neither 'psycopg', 'psycopg2', nor 'psql' "
                "on PATH was found. Install 'psycopg[binary]' or 'psycopg2-binary', or add 'psql' to PATH."
            )
        self._dsn = dsn
        self._psql_binary = resolved_bin
        self._timeout_literal = _format_pg_timeout(statement_timeout_ms)
        self._subprocess_timeout_s = max(30.0, (statement_timeout_ms / 1000.0) + 15.0)

    def _build_psql_script(self, sql: str, has_schema_param: bool) -> str:
        inner_sql = sql.strip().rstrip(";")
        if has_schema_param:
            inner_sql = _PG_PARAM_RE.sub(":'schema_name'", inner_sql)
        return (
            "BEGIN TRANSACTION READ ONLY;\n"
            f"SET LOCAL statement_timeout = '{self._timeout_literal}';\n"
            "SELECT COALESCE(json_agg(row_to_json(_snap_row)), '[]'::json)\n"
            "FROM (\n"
            f"{inner_sql}\n"
            ") AS _snap_row;\n"
            "ROLLBACK;\n"
        )

    def fetch_all(
        self,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        *,
        query_name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        has_schema_param = bool(params and "schema_name" in params and _PG_PARAM_RE.search(sql))
        script = self._build_psql_script(sql, has_schema_param=has_schema_param)
        cmd = [
            self._psql_binary,
            "-X",
            "-q",
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
        ]
        if has_schema_param:
            cmd.extend(["-v", f"schema_name={params['schema_name']}"])  # type: ignore[index]
        cmd.append(self._dsn)

        proc = subprocess.run(
            cmd,
            input=script,
            text=True,
            capture_output=True,
            timeout=self._subprocess_timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or f"exit code {proc.returncode}"
            raise SnapshotCaptureError(
                f"psql read-only capture failed on query '{query_name or 'unnamed'}': {err_msg}"
            )

        raw_out = proc.stdout.strip()
        if not raw_out:
            return []
        try:
            rows = json.loads(raw_out)
        except json.JSONDecodeError as exc:
            raise SnapshotCaptureError(
                f"Failed to decode JSON payload from psql for query '{query_name or 'unnamed'}': {exc}"
            ) from exc
        if not isinstance(rows, list):
            raise SnapshotCaptureError(
                f"Expected JSON array from psql for query '{query_name or 'unnamed'}', got {type(rows).__name__}."
            )
        return rows

    def close(self) -> None:
        return None

    def __enter__(self) -> PsqlSubprocessQueryRunner:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class PyodbcQueryRunner:
    """SQL Server read-only query runner backed by ``pyodbc``."""

    def __init__(self, conn_str: str, lock_timeout_ms: int = 5_000) -> None:
        self._lock_timeout_ms = _validate_positive_timeout_ms(lock_timeout_ms, "lock_timeout_ms")
        self._readonly_conn_str = enforce_mssql_readonly_conn_str(conn_str)
        try:
            import pyodbc  # type: ignore[import-untyped]
        except ImportError as exc:
            raise MissingDriverError(
                "SQL Server driver 'pyodbc' is not installed. "
                "Install it with: pip install pyodbc"
            ) from exc

        self._conn = pyodbc.connect(self._readonly_conn_str, readonly=True, autocommit=False)
        self._cur = self._conn.cursor()
        try:
            self._cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;")
            self._cur.execute(f"SET LOCK_TIMEOUT {self._lock_timeout_ms};")
        except Exception:
            self.close()
            raise

    @property
    def readonly_conn_str(self) -> str:
        return self._readonly_conn_str

    def fetch_all(
        self,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        *,
        query_name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        _ = query_name
        bound_sql, bound_params = _bind_mssql_positional_params(sql, params)
        if bound_params:
            self._cur.execute(bound_sql, bound_params)
        else:
            self._cur.execute(bound_sql)
        return _rows_from_cursor(self._cur)

    def close(self) -> None:
        try:
            try:
                self._conn.rollback()
            except Exception:
                pass
            if self._cur is not None:
                try:
                    self._cur.close()
                except Exception:
                    pass
        finally:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass

    def __enter__(self) -> PyodbcQueryRunner:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def create_postgres_runner(
    dsn: str,
    *,
    driver: str = "auto",
    statement_timeout_ms: int = 15_000,
) -> Psycopg3QueryRunner | Psycopg2QueryRunner | PsqlSubprocessQueryRunner:
    """Create a strictly read-only PostgreSQL query runner using the requested or auto-detected driver."""
    if not dsn or not dsn.strip():
        raise ValueError("PostgreSQL DSN must be non-empty.")

    mode = driver.strip().lower()
    if mode == "psycopg":
        return Psycopg3QueryRunner(dsn, statement_timeout_ms=statement_timeout_ms)
    if mode == "psycopg2":
        return Psycopg2QueryRunner(dsn, statement_timeout_ms=statement_timeout_ms)
    if mode == "psql":
        return PsqlSubprocessQueryRunner(dsn, statement_timeout_ms=statement_timeout_ms)
    if mode != "auto":
        raise ValueError(f"Unsupported PostgreSQL driver '{driver}'. Expected auto, psycopg, psycopg2, or psql.")

    for candidate in (Psycopg3QueryRunner, Psycopg2QueryRunner, PsqlSubprocessQueryRunner):
        try:
            return candidate(dsn, statement_timeout_ms=statement_timeout_ms)
        except MissingDriverError:
            continue

    raise MissingDriverError(
        "No PostgreSQL client driver available. Install 'psycopg[binary]' or 'psycopg2-binary', "
        "or ensure the 'psql' CLI binary is available on PATH."
    )


# =============================================================================
# Read-Only Query Guard & Dynamic Aggregate Query Builders
# =============================================================================


def _execute_readonly_query(
    runner: QueryRunner,
    sql: str,
    *,
    dialect: str,
    params: Optional[dict[str, Any]] = None,
    query_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Enforce sqlglot AST read-only verification immediately BEFORE executing any query."""
    validate_read_only_sql(sql, dialect=dialect)
    return runner.fetch_all(sql, params=params, query_name=query_name)


def _execute_catalog_query(
    runner: QueryRunner,
    query: SnapshotQuery,
    schema: str,
) -> list[dict[str, Any]]:
    return _execute_readonly_query(
        runner,
        query.sql,
        dialect=query.sqlglot_dialect,
        params={"schema_name": schema},
        query_name=query.name,
    )


def build_postgres_table_aggregate_sql(table: TableSchema) -> tuple[str, tuple[str, ...]]:
    """Build a single-pass read-only COUNT(*) + numeric SUM/MIN/MAX/NULL query for PostgreSQL."""
    qualified = (
        f"{_safe_pg_quote_ident(table.schema_name)}.{_safe_pg_quote_ident(table.name)}"
        if table.schema_name
        else _safe_pg_quote_ident(table.name)
    )
    select_exprs = ["COUNT(*) AS row_count"]
    numeric_cols: list[str] = []

    for col in table.columns:
        base_type = normalize_base_type(col.data_type)
        if base_type not in _PG_NUMERIC_TYPES:
            continue
        idx = len(numeric_cols)
        numeric_cols.append(col.name)
        qcol = _safe_pg_quote_ident(col.name)
        select_exprs.extend(
            [
                f"CAST(SUM({qcol}) AS TEXT) AS col_{idx}_sum",
                f"CAST(MIN({qcol}) AS TEXT) AS col_{idx}_min",
                f"CAST(MAX({qcol}) AS TEXT) AS col_{idx}_max",
                f"(COUNT(*) - COUNT({qcol})) AS col_{idx}_nulls",
            ]
        )

    sql = f"SELECT {', '.join(select_exprs)} FROM {qualified}"
    validate_read_only_sql(sql, dialect="postgres")
    return sql, tuple(numeric_cols)


def build_mssql_table_aggregate_sql(table: TableSchema) -> tuple[str, tuple[str, ...]]:
    """Build a single-pass read-only COUNT_BIG(*) + numeric SUM/MIN/MAX/NULL query for SQL Server."""
    qualified = (
        f"{mssql_quote_ident(table.schema_name)}.{mssql_quote_ident(table.name)}"
        if table.schema_name
        else mssql_quote_ident(table.name)
    )
    select_exprs = ["COUNT_BIG(*) AS row_count"]
    numeric_cols: list[str] = []

    for col in table.columns:
        base_type = normalize_base_type(col.data_type)
        if base_type not in _MSSQL_NUMERIC_TYPES:
            continue
        idx = len(numeric_cols)
        numeric_cols.append(col.name)
        qcol = mssql_quote_ident(col.name)
        sum_expr = f"SUM(CAST({qcol} AS DECIMAL(38, 0)))" if base_type in _MSSQL_INTEGER_TYPES else f"SUM({qcol})"
        select_exprs.extend(
            [
                f"CAST({sum_expr} AS NVARCHAR(MAX)) AS col_{idx}_sum",
                f"CAST(MIN({qcol}) AS NVARCHAR(MAX)) AS col_{idx}_min",
                f"CAST(MAX({qcol}) AS NVARCHAR(MAX)) AS col_{idx}_max",
                f"(COUNT_BIG(*) - COUNT_BIG({qcol})) AS col_{idx}_nulls",
            ]
        )

    sql = f"SELECT {', '.join(select_exprs)} FROM {qualified}"
    validate_read_only_sql(sql, dialect="tsql")
    return sql, tuple(numeric_cols)


# =============================================================================
# Row Normalization & Snapshot Assembly
# =============================================================================


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n", ""}:
        return False
    return bool(value)


def _to_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _normalize_agg_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    text = str(value).strip()
    return text if text != "" else None


def _assemble_constraints(
    constraint_rows: list[dict[str, Any]],
    default_schema: str,
) -> dict[str, tuple[ConstraintSchema, ...]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in constraint_rows:
        grouped[(str(row["table_name"]), str(row["constraint_name"]))].append(row)

    by_table: dict[str, list[ConstraintSchema]] = defaultdict(list)
    for (table_name, con_name), rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda r: int(r.get("key_ordinal", 0)))
        first = ordered[0]
        ctype = str(first["constraint_type"]).upper().strip()
        cols = tuple(str(r["column_name"]) for r in ordered)

        ref_table: Optional[str] = None
        ref_cols: tuple[str, ...] = ()
        if ctype == "FOREIGN KEY" and first.get("referenced_table"):
            raw_ref_table = str(first["referenced_table"])
            raw_ref_schema = str(first["referenced_schema"]) if first.get("referenced_schema") else default_schema
            ref_table = (
                raw_ref_table
                if raw_ref_schema == default_schema
                else f"{raw_ref_schema}.{raw_ref_table}"
            )
            ref_cols = tuple(
                str(r["referenced_column"]) for r in ordered if r.get("referenced_column") is not None
            )

        by_table[table_name].append(
            ConstraintSchema(
                name=con_name,
                constraint_type=ctype,
                columns=cols,
                referenced_table=ref_table,
                referenced_columns=ref_cols,
                definition=str(first["definition"]) if first.get("definition") is not None else None,
            )
        )

    return {
        tbl: tuple(sorted(items, key=lambda c: (c.constraint_type, c.name)))
        for tbl, items in by_table.items()
    }


def _assemble_indexes(index_rows: list[dict[str, Any]]) -> dict[str, tuple[IndexSchema, ...]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in index_rows:
        grouped[(str(row["table_name"]), str(row["index_name"]))].append(row)

    by_table: dict[str, list[IndexSchema]] = defaultdict(list)
    for (table_name, idx_name), rows in sorted(grouped.items()):
        key_rows = sorted(
            (r for r in rows if not _to_bool(r.get("is_included_column", False))),
            key=lambda r: int(r.get("key_ordinal", 0)),
        )
        inc_rows = sorted(
            (r for r in rows if _to_bool(r.get("is_included_column", False))),
            key=lambda r: int(r.get("key_ordinal", 0)),
        )
        first = rows[0]
        raw_filter = first.get("filter_predicate")
        by_table[table_name].append(
            IndexSchema(
                name=idx_name,
                columns=tuple(str(r["column_name"]) for r in key_rows),
                is_unique=_to_bool(first.get("is_unique", False)),
                is_primary=_to_bool(first.get("is_primary", False)),
                index_type=str(first.get("index_type", "BTREE")).upper().strip(),
                include_columns=tuple(str(r["column_name"]) for r in inc_rows),
                filter_predicate=str(raw_filter) if raw_filter else None,
            )
        )

    return {
        tbl: tuple(sorted(items, key=lambda i: (not i.is_primary, i.name)))
        for tbl, items in by_table.items()
    }


def _assemble_tables(
    col_rows: list[dict[str, Any]],
    constraints_by_table: dict[str, tuple[ConstraintSchema, ...]],
    indexes_by_table: dict[str, tuple[IndexSchema, ...]],
    default_schema: str,
    engine: str,
) -> tuple[TableSchema, ...]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    table_schemas: dict[str, str] = {}

    for row in col_rows:
        tname = str(row["table_name"])
        grouped[tname].append(row)
        table_schemas[tname] = str(row.get("schema_name") or default_schema)

    tables: list[TableSchema] = []
    for table_name in sorted(grouped.keys()):
        ordered_rows = sorted(grouped[table_name], key=lambda r: int(r["ordinal_position"]))
        columns: list[ColumnSchema] = []
        for row in ordered_rows:
            data_type = str(row["data_type"])
            raw_max_len = _to_optional_int(row.get("max_length"))
            if engine == "mssql" and normalize_base_type(data_type) not in _MSSQL_LENGTH_BEARING_TYPES:
                raw_max_len = None

            raw_default = row.get("default_value")
            columns.append(
                ColumnSchema(
                    name=str(row["column_name"]),
                    data_type=data_type,
                    is_nullable=_to_bool(row["is_nullable"]),
                    ordinal_position=int(row["ordinal_position"]),
                    max_length=raw_max_len,
                    numeric_precision=_to_optional_int(row.get("numeric_precision")),
                    numeric_scale=_to_optional_int(row.get("numeric_scale")),
                    default_value=str(raw_default) if raw_default is not None else None,
                    is_identity=_to_bool(row.get("is_identity", False)),
                )
            )

        tables.append(
            TableSchema(
                name=table_name,
                schema_name=table_schemas[table_name],
                columns=tuple(columns),
                constraints=constraints_by_table.get(table_name, ()),
                indexes=indexes_by_table.get(table_name, ()),
            )
        )

    return tuple(tables)


def _assemble_routines(routine_rows: list[dict[str, Any]], default_schema: str) -> tuple[RoutineSchema, ...]:
    routines = [
        RoutineSchema(
            name=str(row["routine_name"]),
            schema_name=str(row.get("schema_name") or default_schema),
            routine_type=str(row["routine_type"]).upper().strip(),
            parameter_count=int(row.get("parameter_count", 0)),
            return_type=str(row["return_type"]) if row.get("return_type") is not None else None,
            definition_hash=str(row["definition_hash"]) if row.get("definition_hash") else None,
        )
        for row in routine_rows
    ]
    routines.sort(key=lambda r: (r.name, r.parameter_count, r.routine_type))
    return tuple(routines)


def _assemble_security_grants(grant_rows: list[dict[str, Any]]) -> tuple[SecurityGrant, ...]:
    merged: dict[tuple[str, str, str, str], tuple[bool, bool]] = {}
    for row in grant_rows:
        key = (
            str(row["principal"]),
            str(row["object_type"]).upper().strip(),
            str(row["object_name"]),
            str(row["privilege"]).upper().strip(),
        )
        is_grantable = _to_bool(row.get("is_grantable", False))
        is_superuser = _to_bool(row.get("is_superuser", False))
        if key in merged:
            prev_g, prev_s = merged[key]
            merged[key] = (prev_g or is_grantable, prev_s or is_superuser)
        else:
            merged[key] = (is_grantable, is_superuser)

    return tuple(
        SecurityGrant(
            principal=principal,
            object_type=obj_type,
            object_name=obj_name,
            privilege=privilege,
            is_grantable=is_grantable,
            is_superuser=is_superuser,
        )
        for (principal, obj_type, obj_name, privilege), (is_grantable, is_superuser) in sorted(merged.items())
    )


def _capture_table_data_stats(
    runner: QueryRunner,
    tables: tuple[TableSchema, ...],
    catalog_row_counts: dict[str, int],
    *,
    engine: str,
) -> tuple[TableDataStats, ...]:
    dialect = "postgres" if engine == "postgresql" else "tsql"
    builder = build_postgres_table_aggregate_sql if engine == "postgresql" else build_mssql_table_aggregate_sql

    stats_list: list[TableDataStats] = []
    for table in tables:
        agg_sql, numeric_cols = builder(table)
        agg_rows = _execute_readonly_query(
            runner,
            agg_sql,
            dialect=dialect,
            params=None,
            query_name=f"{engine}_table_aggregates:{table.name}",
        )

        if not agg_rows:
            stats_list.append(
                TableDataStats(
                    table_name=table.name,
                    row_count=max(0, catalog_row_counts.get(table.name, 0)),
                    aggregate_checksum=None,
                    column_aggregates=(),
                )
            )
            continue

        agg_row = agg_rows[0]
        row_count = int(agg_row.get("row_count", catalog_row_counts.get(table.name, 0)))
        col_aggregates: list[ColumnAggregate] = []
        for idx, col_name in enumerate(numeric_cols):
            col_aggregates.append(
                ColumnAggregate(
                    column_name=col_name,
                    sum_value=_normalize_agg_value(agg_row.get(f"col_{idx}_sum")),
                    min_value=_normalize_agg_value(agg_row.get(f"col_{idx}_min")),
                    max_value=_normalize_agg_value(agg_row.get(f"col_{idx}_max")),
                    null_count=int(agg_row.get(f"col_{idx}_nulls") or 0),
                )
            )

        stats_list.append(
            TableDataStats(
                table_name=table.name,
                row_count=row_count,
                aggregate_checksum=None,
                column_aggregates=tuple(col_aggregates),
            )
        )

    return tuple(stats_list)


# =============================================================================
# Public Capture API (PostgreSQL & SQL Server)
# =============================================================================


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def capture_postgres_snapshot(
    dsn: Optional[str],
    schema: str,
    server_name: str,
    database_name: str,
    include_data_stats: bool = True,
    *,
    statement_timeout_ms: int = 15_000,
    driver: str = "auto",
    runner: Optional[QueryRunner] = None,
    queries: Optional[Mapping[str, SnapshotQuery]] = None,
    captured_at: Optional[str] = None,
) -> Snapshot:
    """Capture an immutable read-only snapshot from a live PostgreSQL instance."""
    query_catalog = dict(POSTGRES_QUERIES)
    if queries is not None:
        query_catalog.update(queries)

    owned_runner: Optional[Any] = None
    active_runner: QueryRunner
    if runner is not None:
        active_runner = runner
    else:
        if not dsn or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required when no QueryRunner is injected.")
        owned_runner = create_postgres_runner(
            dsn,
            driver=driver,
            statement_timeout_ms=statement_timeout_ms,
        )
        active_runner = owned_runner

    try:
        col_rows = _execute_catalog_query(active_runner, query_catalog["pg_tables_and_columns"], schema)
        con_rows = _execute_catalog_query(active_runner, query_catalog["pg_constraints"], schema)
        idx_rows = _execute_catalog_query(active_runner, query_catalog["pg_indexes"], schema)
        rtn_rows = _execute_catalog_query(active_runner, query_catalog["pg_routines"], schema)
        sec_rows = _execute_catalog_query(active_runner, query_catalog["pg_security_grants"], schema)

        constraints_by_table = _assemble_constraints(con_rows, default_schema=schema)
        indexes_by_table = _assemble_indexes(idx_rows)
        tables = _assemble_tables(
            col_rows,
            constraints_by_table=constraints_by_table,
            indexes_by_table=indexes_by_table,
            default_schema=schema,
            engine="postgresql",
        )
        routines = _assemble_routines(rtn_rows, default_schema=schema)
        security_grants = _assemble_security_grants(sec_rows)

        data_stats: tuple[TableDataStats, ...] = ()
        if include_data_stats:
            rc_rows = _execute_catalog_query(active_runner, query_catalog["pg_row_counts"], schema)
            catalog_counts = {
                str(r["table_name"]): max(0, int(r.get("estimated_row_count") or 0))
                for r in rc_rows
            }
            data_stats = _capture_table_data_stats(
                active_runner,
                tables,
                catalog_counts,
                engine="postgresql",
            )
    finally:
        if owned_runner is not None:
            owned_runner.close()

    return Snapshot(
        engine="postgresql",
        server_name=server_name,
        database_name=database_name,
        schema_name=schema,
        captured_at=captured_at or _utc_now_iso(),
        capture_version="1.0",
        tables=tables,
        routines=routines,
        security_grants=security_grants,
        data_stats=data_stats,
    )


def capture_mssql_snapshot(
    conn_str: Optional[str],
    schema: str,
    server_name: str,
    database_name: str,
    include_data_stats: bool = True,
    *,
    lock_timeout_ms: int = 5_000,
    runner: Optional[QueryRunner] = None,
    queries: Optional[Mapping[str, SnapshotQuery]] = None,
    captured_at: Optional[str] = None,
) -> Snapshot:
    """Capture an immutable read-only snapshot from a live SQL Server instance."""
    query_catalog = dict(MSSQL_QUERIES)
    if queries is not None:
        query_catalog.update(queries)

    if conn_str is not None:
        enforce_mssql_readonly_conn_str(conn_str)

    owned_runner: Optional[PyodbcQueryRunner] = None
    active_runner: QueryRunner
    if runner is not None:
        active_runner = runner
    else:
        if not conn_str or not conn_str.strip():
            raise ValueError("SQL Server connection string is required when no QueryRunner is injected.")
        owned_runner = PyodbcQueryRunner(conn_str, lock_timeout_ms=lock_timeout_ms)
        active_runner = owned_runner

    try:
        col_rows = _execute_catalog_query(active_runner, query_catalog["mssql_tables_and_columns"], schema)
        key_rows = _execute_catalog_query(active_runner, query_catalog["mssql_key_constraints"], schema)
        fk_rows = _execute_catalog_query(active_runner, query_catalog["mssql_foreign_keys"], schema)
        idx_rows = _execute_catalog_query(active_runner, query_catalog["mssql_indexes"], schema)
        rtn_rows = _execute_catalog_query(active_runner, query_catalog["mssql_routines"], schema)
        sec_rows = _execute_catalog_query(active_runner, query_catalog["mssql_security_grants"], schema)

        combined_constraints = [
            *key_rows,
            *({**row, "constraint_type": "FOREIGN KEY"} for row in fk_rows),
        ]
        constraints_by_table = _assemble_constraints(combined_constraints, default_schema=schema)
        indexes_by_table = _assemble_indexes(idx_rows)
        tables = _assemble_tables(
            col_rows,
            constraints_by_table=constraints_by_table,
            indexes_by_table=indexes_by_table,
            default_schema=schema,
            engine="mssql",
        )
        routines = _assemble_routines(rtn_rows, default_schema=schema)
        security_grants = _assemble_security_grants(sec_rows)

        data_stats: tuple[TableDataStats, ...] = ()
        if include_data_stats:
            rc_rows = _execute_catalog_query(active_runner, query_catalog["mssql_row_counts"], schema)
            catalog_counts = {
                str(r["table_name"]): max(0, int(r.get("row_count") or 0))
                for r in rc_rows
            }
            data_stats = _capture_table_data_stats(
                active_runner,
                tables,
                catalog_counts,
                engine="mssql",
            )
    finally:
        if owned_runner is not None:
            owned_runner.close()

    return Snapshot(
        engine="mssql",
        server_name=server_name,
        database_name=database_name,
        schema_name=schema,
        captured_at=captured_at or _utc_now_iso(),
        capture_version="1.0",
        tables=tables,
        routines=routines,
        security_grants=security_grants,
        data_stats=data_stats,
    )


# =============================================================================
# Deterministic Snapshot JSON Serialization
# =============================================================================


def _column_to_dict(col: ColumnSchema) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "data_type": col.data_type,
        "is_identity": col.is_identity,
        "is_nullable": col.is_nullable,
        "name": col.name,
        "ordinal_position": col.ordinal_position,
    }
    if col.max_length is not None:
        payload["max_length"] = col.max_length
    if col.numeric_precision is not None:
        payload["numeric_precision"] = col.numeric_precision
    if col.numeric_scale is not None:
        payload["numeric_scale"] = col.numeric_scale
    if col.default_value is not None:
        payload["default_value"] = col.default_value
    return payload


def _constraint_to_dict(con: ConstraintSchema) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "columns": list(con.columns),
        "constraint_type": con.constraint_type,
        "name": con.name,
    }
    if con.referenced_table is not None:
        payload["referenced_table"] = con.referenced_table
    if con.referenced_columns:
        payload["referenced_columns"] = list(con.referenced_columns)
    if con.definition is not None:
        payload["definition"] = con.definition
    return payload


def _index_to_dict(idx: IndexSchema) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "columns": list(idx.columns),
        "index_type": idx.index_type,
        "is_primary": idx.is_primary,
        "is_unique": idx.is_unique,
        "name": idx.name,
    }
    if idx.include_columns:
        payload["include_columns"] = list(idx.include_columns)
    if idx.filter_predicate is not None:
        payload["filter_predicate"] = idx.filter_predicate
    return payload


def _routine_to_dict(routine: RoutineSchema) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": routine.name,
        "parameter_count": routine.parameter_count,
        "routine_type": routine.routine_type,
        "schema_name": routine.schema_name,
    }
    if routine.return_type is not None:
        payload["return_type"] = routine.return_type
    if routine.definition_hash is not None:
        payload["definition_hash"] = routine.definition_hash
    return payload


def _grant_to_dict(grant: SecurityGrant) -> dict[str, Any]:
    return {
        "is_grantable": grant.is_grantable,
        "is_superuser": grant.is_superuser,
        "object_name": grant.object_name,
        "object_type": grant.object_type,
        "principal": grant.principal,
        "privilege": grant.privilege,
    }


def _data_stats_to_dict(stats: TableDataStats) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "column_aggregates": [
            {
                "column_name": agg.column_name,
                "max_value": agg.max_value,
                "min_value": agg.min_value,
                "null_count": agg.null_count,
                "sum_value": agg.sum_value,
            }
            for agg in stats.column_aggregates
        ],
        "row_count": stats.row_count,
        "table_name": stats.table_name,
    }
    if stats.aggregate_checksum is not None:
        payload["aggregate_checksum"] = stats.aggregate_checksum
    return payload


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Serialize a frozen ``Snapshot`` into a JSON-compatible dictionary loaded by ``load_snapshot``."""
    return {
        "capture_version": snapshot.capture_version,
        "captured_at": snapshot.captured_at,
        "data_stats": [_data_stats_to_dict(s) for s in snapshot.data_stats],
        "database_name": snapshot.database_name,
        "engine": snapshot.engine,
        "routines": [_routine_to_dict(r) for r in snapshot.routines],
        "schema_name": snapshot.schema_name,
        "security_grants": [_grant_to_dict(g) for g in snapshot.security_grants],
        "server_name": snapshot.server_name,
        "tables": [
            {
                "columns": [_column_to_dict(c) for c in tbl.columns],
                "constraints": [_constraint_to_dict(c) for c in tbl.constraints],
                "indexes": [_index_to_dict(i) for i in tbl.indexes],
                "name": tbl.name,
                "schema_name": tbl.schema_name,
            }
            for tbl in snapshot.tables
        ],
    }


def write_snapshot(snapshot: Snapshot, output_path: str | Path) -> Path:
    """Write a frozen ``Snapshot`` to disk as deterministic UTF-8 LF JSON, creating parent dirs."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(snapshot_to_dict(snapshot), indent=2, sort_keys=True) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(serialized)
    return path
