"""Production read-only metadata & statistics SQL query catalog for immutable snapshot capture.

All queries in this catalog are strictly read-only catalog inspections designed for
single-window, low-impact capture against SQL Server (T-SQL) and PostgreSQL instances:
  - Parameterized schema filtering (@schema_name for T-SQL, $1 for PostgreSQL).
  - Zero table scans for row-count estimation (sys.dm_db_partition_stats / pg_stat_user_tables).
  - Proper SQL Server NVARCHAR/NCHAR byte-to-character length normalization preserving -1 for MAX.
  - PostgreSQL indisvalid/indisready filtering to ignore broken CONCURRENTLY indexes.
  - PostgreSQL indnkeyatts distinction between key columns and INCLUDE covering columns.
"""

from __future__ import annotations

from dataclasses import dataclass

# Dialect mapping for sqlglot AST verification
ENGINE_TO_SQLGLOT_DIALECT: dict[str, str] = {
    "mssql": "tsql",
    "postgresql": "postgres",
}


@dataclass(frozen=True)
class SnapshotQuery:
    """Single immutable read-only catalog query used during snapshot capture."""

    name: str
    engine: str  # "mssql" | "postgresql"
    layer: str  # "structure" | "data" | "routines" | "security"
    description: str
    sql: str

    @property
    def sqlglot_dialect(self) -> str:
        return ENGINE_TO_SQLGLOT_DIALECT[self.engine]


# =============================================================================
# SQL Server (T-SQL) Read-Only Catalog Queries
# =============================================================================

MSSQL_TABLES_AND_COLUMNS = SnapshotQuery(
    name="mssql_tables_and_columns",
    engine="mssql",
    layer="structure",
    description=(
        "Capture user tables and column metadata from sys.tables/sys.columns, "
        "normalizing UTF-16 byte lengths for NVARCHAR/NCHAR while preserving -1 for MAX."
    ),
    sql="""
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    c.name AS column_name,
    tp.name AS data_type,
    CASE
        WHEN tp.name IN (N'nvarchar', N'nchar') AND c.max_length > 0 THEN c.max_length / 2
        ELSE c.max_length
    END AS max_length,
    CASE
        WHEN tp.name IN (N'decimal', N'numeric') THEN CAST(c.precision AS INT)
        ELSE NULL
    END AS numeric_precision,
    CASE
        WHEN tp.name IN (N'decimal', N'numeric') THEN CAST(c.scale AS INT)
        ELSE NULL
    END AS numeric_scale,
    c.is_nullable AS is_nullable,
    c.column_id AS ordinal_position,
    c.is_identity AS is_identity,
    dc.definition AS default_value
FROM sys.tables AS t
INNER JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
INNER JOIN sys.columns AS c
    ON c.object_id = t.object_id
INNER JOIN sys.types AS tp
    ON tp.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints AS dc
    ON dc.parent_object_id = c.object_id
   AND dc.parent_column_id = c.column_id
WHERE t.is_ms_shipped = 0
  AND s.name = @schema_name
ORDER BY t.name, c.column_id
""".strip(),
)

MSSQL_KEY_CONSTRAINTS = SnapshotQuery(
    name="mssql_key_constraints",
    engine="mssql",
    layer="structure",
    description="Capture PRIMARY KEY and UNIQUE constraints with exact composite key_ordinal ordering.",
    sql="""
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    kc.name AS constraint_name,
    CASE kc.type
        WHEN 'PK' THEN 'PRIMARY KEY'
        WHEN 'UQ' THEN 'UNIQUE'
    END AS constraint_type,
    ic.key_ordinal AS key_ordinal,
    c.name AS column_name
FROM sys.key_constraints AS kc
INNER JOIN sys.tables AS t
    ON t.object_id = kc.parent_object_id
INNER JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
INNER JOIN sys.indexes AS i
    ON i.object_id = kc.parent_object_id
   AND i.index_id = kc.unique_index_id
INNER JOIN sys.index_columns AS ic
    ON ic.object_id = i.object_id
   AND ic.index_id = i.index_id
   AND ic.is_included_column = 0
INNER JOIN sys.columns AS c
    ON c.object_id = ic.object_id
   AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0
  AND s.name = @schema_name
ORDER BY t.name, kc.name, ic.key_ordinal
""".strip(),
)

MSSQL_FOREIGN_KEYS = SnapshotQuery(
    name="mssql_foreign_keys",
    engine="mssql",
    layer="structure",
    description="Capture FOREIGN KEY constraints with exact positional parent and referenced columns.",
    sql="""
SELECT
    ps.name AS schema_name,
    pt.name AS table_name,
    fk.name AS constraint_name,
    fkc.constraint_column_id AS key_ordinal,
    pc.name AS column_name,
    rs.name AS referenced_schema,
    rt.name AS referenced_table,
    rc.name AS referenced_column
FROM sys.foreign_keys AS fk
INNER JOIN sys.foreign_key_columns AS fkc
    ON fkc.constraint_object_id = fk.object_id
INNER JOIN sys.tables AS pt
    ON pt.object_id = fk.parent_object_id
INNER JOIN sys.schemas AS ps
    ON ps.schema_id = pt.schema_id
INNER JOIN sys.columns AS pc
    ON pc.object_id = fkc.parent_object_id
   AND pc.column_id = fkc.parent_column_id
INNER JOIN sys.tables AS rt
    ON rt.object_id = fk.referenced_object_id
INNER JOIN sys.schemas AS rs
    ON rs.schema_id = rt.schema_id
INNER JOIN sys.columns AS rc
    ON rc.object_id = fkc.referenced_object_id
   AND rc.column_id = fkc.referenced_column_id
WHERE pt.is_ms_shipped = 0
  AND ps.name = @schema_name
ORDER BY pt.name, fk.name, fkc.constraint_column_id
""".strip(),
)

MSSQL_INDEXES = SnapshotQuery(
    name="mssql_indexes",
    engine="mssql",
    layer="structure",
    description="Capture clustered and nonclustered indexes with key order, INCLUDE columns, and filter predicates.",
    sql="""
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    i.name AS index_name,
    i.type_desc AS index_type,
    i.is_unique AS is_unique,
    i.is_primary_key AS is_primary,
    i.filter_definition AS filter_predicate,
    ic.key_ordinal AS key_ordinal,
    ic.is_included_column AS is_included_column,
    c.name AS column_name
FROM sys.indexes AS i
INNER JOIN sys.tables AS t
    ON t.object_id = i.object_id
INNER JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
INNER JOIN sys.index_columns AS ic
    ON ic.object_id = i.object_id
   AND ic.index_id = i.index_id
INNER JOIN sys.columns AS c
    ON c.object_id = ic.object_id
   AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0
  AND i.type > 0
  AND i.is_disabled = 0
  AND s.name = @schema_name
ORDER BY t.name, i.name, ic.is_included_column, ic.key_ordinal, ic.index_column_id
""".strip(),
)

MSSQL_ROUTINES = SnapshotQuery(
    name="mssql_routines",
    engine="mssql",
    layer="routines",
    description="Capture stored procedure and function inventory, parameter counts, and SHA-256 definition hashes.",
    sql="""
SELECT
    s.name AS schema_name,
    o.name AS routine_name,
    CASE o.type
        WHEN 'P' THEN 'PROCEDURE'
        ELSE 'FUNCTION'
    END AS routine_type,
    COUNT(p.parameter_id) AS parameter_count,
    CONVERT(VARCHAR(64), HASHBYTES('SHA2_256', COALESCE(MAX(m.definition), N'')), 2) AS definition_hash
FROM sys.objects AS o
INNER JOIN sys.schemas AS s
    ON s.schema_id = o.schema_id
LEFT JOIN sys.parameters AS p
    ON p.object_id = o.object_id
   AND p.parameter_id > 0
LEFT JOIN sys.sql_modules AS m
    ON m.object_id = o.object_id
WHERE o.is_ms_shipped = 0
  AND o.type IN ('P', 'FN', 'IF', 'TF')
  AND s.name = @schema_name
GROUP BY s.name, o.name, o.type
ORDER BY o.name
""".strip(),
)

MSSQL_SECURITY_GRANTS = SnapshotQuery(
    name="mssql_security_grants",
    engine="mssql",
    layer="security",
    description="Capture explicit object-level GRANT permissions and db_owner membership from SQL Server catalogs.",
    sql="""
SELECT
    dp.name AS principal,
    CASE
        WHEN o.type = 'U' THEN 'TABLE'
        WHEN o.type IN ('P', 'FN', 'IF', 'TF') THEN 'ROUTINE'
        ELSE o.type_desc
    END AS object_type,
    o.name AS object_name,
    perm.permission_name AS privilege,
    CASE
        WHEN perm.state = 'W' THEN 1
        ELSE 0
    END AS is_grantable,
    COALESCE(IS_ROLEMEMBER(N'db_owner', dp.name), 0) AS is_superuser
FROM sys.database_permissions AS perm
INNER JOIN sys.database_principals AS dp
    ON dp.principal_id = perm.grantee_principal_id
INNER JOIN sys.objects AS o
    ON o.object_id = perm.major_id
INNER JOIN sys.schemas AS s
    ON s.schema_id = o.schema_id
WHERE perm.class = 1
  AND perm.minor_id = 0
  AND perm.state IN ('G', 'W')
  AND o.is_ms_shipped = 0
  AND o.type IN ('U', 'P', 'FN', 'IF', 'TF')
  AND s.name = @schema_name
ORDER BY dp.name, o.name, perm.permission_name
""".strip(),
)

MSSQL_ROW_COUNTS = SnapshotQuery(
    name="mssql_row_counts",
    engine="mssql",
    layer="data",
    description="Capture zero-scan partition-level table row counts from sys.dm_db_partition_stats.",
    sql="""
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    SUM(ps.row_count) AS row_count
FROM sys.tables AS t
INNER JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
INNER JOIN sys.dm_db_partition_stats AS ps
    ON ps.object_id = t.object_id
   AND ps.index_id IN (0, 1)
WHERE t.is_ms_shipped = 0
  AND s.name = @schema_name
GROUP BY s.name, t.name
ORDER BY t.name
""".strip(),
)


# =============================================================================
# PostgreSQL Read-Only Catalog Queries
# =============================================================================

PG_TABLES_AND_COLUMNS = SnapshotQuery(
    name="pg_tables_and_columns",
    engine="postgresql",
    layer="structure",
    description=(
        "Capture ordinary and partitioned tables with column types, decoded character lengths, "
        "numeric precision/scale, nullability, identity flags, and defaults from pg_catalog."
    ),
    sql="""
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    a.attname AS column_name,
    pg_catalog.format_type(a.atttypid, NULL) AS data_type,
    CASE
        WHEN t.typname IN ('varchar', 'bpchar') AND a.atttypmod > 0 THEN a.atttypmod - 4
        ELSE NULL
    END AS max_length,
    CASE
        WHEN t.typname = 'numeric' AND a.atttypmod > 0 THEN ((a.atttypmod - 4) >> 16) & 65535
        ELSE NULL
    END AS numeric_precision,
    CASE
        WHEN t.typname = 'numeric' AND a.atttypmod > 0 THEN (a.atttypmod - 4) & 65535
        ELSE NULL
    END AS numeric_scale,
    NOT a.attnotnull AS is_nullable,
    a.attnum AS ordinal_position,
    a.attidentity IN ('a', 'd') AS is_identity,
    pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS default_value
FROM pg_catalog.pg_class AS c
INNER JOIN pg_catalog.pg_namespace AS n
    ON n.oid = c.relnamespace
INNER JOIN pg_catalog.pg_attribute AS a
    ON a.attrelid = c.oid
   AND a.attnum > 0
   AND NOT a.attisdropped
INNER JOIN pg_catalog.pg_type AS t
    ON t.oid = a.atttypid
LEFT JOIN pg_catalog.pg_attrdef AS ad
    ON ad.adrelid = a.attrelid
   AND ad.adnum = a.attnum
WHERE c.relkind IN ('r', 'p')
  AND n.nspname = $1
ORDER BY c.relname, a.attnum
""".strip(),
)

PG_CONSTRAINTS = SnapshotQuery(
    name="pg_constraints",
    engine="postgresql",
    layer="structure",
    description=(
        "Capture PRIMARY KEY, UNIQUE, and FOREIGN KEY constraints from pg_constraint "
        "preserving exact composite column order and positional FK referenced columns."
    ),
    sql="""
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    con.conname AS constraint_name,
    CASE con.contype
        WHEN 'p' THEN 'PRIMARY KEY'
        WHEN 'u' THEN 'UNIQUE'
        WHEN 'f' THEN 'FOREIGN KEY'
    END AS constraint_type,
    key_cols.ord AS key_ordinal,
    a.attname AS column_name,
    ref_n.nspname AS referenced_schema,
    ref_c.relname AS referenced_table,
    ref_a.attname AS referenced_column
FROM pg_catalog.pg_constraint AS con
INNER JOIN pg_catalog.pg_class AS c
    ON c.oid = con.conrelid
INNER JOIN pg_catalog.pg_namespace AS n
    ON n.oid = c.relnamespace
INNER JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key_cols(attnum, ord)
    ON TRUE
INNER JOIN pg_catalog.pg_attribute AS a
    ON a.attrelid = con.conrelid
   AND a.attnum = key_cols.attnum
LEFT JOIN pg_catalog.pg_class AS ref_c
    ON ref_c.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace AS ref_n
    ON ref_n.oid = ref_c.relnamespace
LEFT JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS ref_cols(attnum, ord)
    ON con.contype = 'f'
   AND ref_cols.ord = key_cols.ord
LEFT JOIN pg_catalog.pg_attribute AS ref_a
    ON ref_a.attrelid = con.confrelid
   AND ref_a.attnum = ref_cols.attnum
WHERE c.relkind IN ('r', 'p')
  AND con.contype IN ('p', 'u', 'f')
  AND n.nspname = $1
ORDER BY c.relname, con.conname, key_cols.ord
""".strip(),
)

PG_INDEXES = SnapshotQuery(
    name="pg_indexes",
    engine="postgresql",
    layer="structure",
    description=(
        "Capture valid ready indexes from pg_index preserving key order, INCLUDE columns "
        "(ord > indnkeyatts), uniqueness, access method, and partial index predicates."
    ),
    sql="""
SELECT
    n.nspname AS schema_name,
    t.relname AS table_name,
    i.relname AS index_name,
    UPPER(am.amname) AS index_type,
    ix.indisunique AS is_unique,
    ix.indisprimary AS is_primary,
    pg_catalog.pg_get_expr(ix.indpred, ix.indrelid) AS filter_predicate,
    idx_cols.ord AS key_ordinal,
    idx_cols.ord > ix.indnkeyatts AS is_included_column,
    COALESCE(a.attname, pg_catalog.pg_get_indexdef(ix.indexrelid, idx_cols.ord::INT, TRUE)) AS column_name
FROM pg_catalog.pg_index AS ix
INNER JOIN pg_catalog.pg_class AS t
    ON t.oid = ix.indrelid
INNER JOIN pg_catalog.pg_namespace AS n
    ON n.oid = t.relnamespace
INNER JOIN pg_catalog.pg_class AS i
    ON i.oid = ix.indexrelid
INNER JOIN pg_catalog.pg_am AS am
    ON am.oid = i.relam
INNER JOIN LATERAL unnest(ix.indkey::SMALLINT[]) WITH ORDINALITY AS idx_cols(attnum, ord)
    ON TRUE
LEFT JOIN pg_catalog.pg_attribute AS a
    ON a.attrelid = t.oid
   AND a.attnum = idx_cols.attnum
WHERE t.relkind IN ('r', 'p')
  AND ix.indisvalid
  AND ix.indisready
  AND n.nspname = $1
ORDER BY t.relname, i.relname, idx_cols.ord
""".strip(),
)

PG_ROUTINES = SnapshotQuery(
    name="pg_routines",
    engine="postgresql",
    layer="routines",
    description="Capture user functions and procedures from pg_proc with argument count, return type, and source hash.",
    sql="""
SELECT
    n.nspname AS schema_name,
    p.proname AS routine_name,
    CASE p.prokind
        WHEN 'p' THEN 'PROCEDURE'
        ELSE 'FUNCTION'
    END AS routine_type,
    p.pronargs AS parameter_count,
    pg_catalog.format_type(p.prorettype, NULL) AS return_type,
    md5(COALESCE(p.prosrc, '')) AS definition_hash
FROM pg_catalog.pg_proc AS p
INNER JOIN pg_catalog.pg_namespace AS n
    ON n.oid = p.pronamespace
WHERE p.prokind IN ('f', 'p')
  AND n.nspname = $1
ORDER BY p.proname, p.pronargs
""".strip(),
)

PG_SECURITY_GRANTS = SnapshotQuery(
    name="pg_security_grants",
    engine="postgresql",
    layer="security",
    description=(
        "Capture table and routine grants from information_schema.role_table_grants and "
        "role_routine_grants enriched with pg_roles.rolsuper superuser indicators."
    ),
    sql="""
SELECT
    g.grantee AS principal,
    'TABLE' AS object_type,
    g.table_name AS object_name,
    g.privilege_type AS privilege,
    g.is_grantable = 'YES' AS is_grantable,
    COALESCE(r.rolsuper, FALSE) AS is_superuser
FROM information_schema.role_table_grants AS g
LEFT JOIN pg_catalog.pg_roles AS r
    ON r.rolname = g.grantee
WHERE g.table_schema = $1

UNION ALL

SELECT
    rg.grantee AS principal,
    'ROUTINE' AS object_type,
    rg.routine_name AS object_name,
    rg.privilege_type AS privilege,
    rg.is_grantable = 'YES' AS is_grantable,
    COALESCE(r.rolsuper, FALSE) AS is_superuser
FROM information_schema.role_routine_grants AS rg
LEFT JOIN pg_catalog.pg_roles AS r
    ON r.rolname = rg.grantee
WHERE rg.routine_schema = $1
ORDER BY principal, object_type, object_name, privilege
""".strip(),
)

PG_ROW_COUNTS = SnapshotQuery(
    name="pg_row_counts",
    engine="postgresql",
    layer="data",
    description="Capture zero-scan estimated live tuple counts and analyze freshness from pg_stat_user_tables.",
    sql="""
SELECT
    s.schemaname AS schema_name,
    s.relname AS table_name,
    s.n_live_tup AS estimated_row_count,
    s.n_dead_tup AS dead_tuple_count,
    s.last_analyze AS last_analyze,
    s.last_autoanalyze AS last_autoanalyze
FROM pg_catalog.pg_stat_user_tables AS s
WHERE s.schemaname = $1
ORDER BY s.relname
""".strip(),
)


# =============================================================================
# Catalog Registries & Static Read-Only AST Guard
# =============================================================================

MSSQL_QUERIES: dict[str, SnapshotQuery] = {
    q.name: q
    for q in (
        MSSQL_TABLES_AND_COLUMNS,
        MSSQL_KEY_CONSTRAINTS,
        MSSQL_FOREIGN_KEYS,
        MSSQL_INDEXES,
        MSSQL_ROUTINES,
        MSSQL_SECURITY_GRANTS,
        MSSQL_ROW_COUNTS,
    )
}

POSTGRES_QUERIES: dict[str, SnapshotQuery] = {
    q.name: q
    for q in (
        PG_TABLES_AND_COLUMNS,
        PG_CONSTRAINTS,
        PG_INDEXES,
        PG_ROUTINES,
        PG_SECURITY_GRANTS,
        PG_ROW_COUNTS,
    )
}

ALL_SNAPSHOT_QUERIES: tuple[SnapshotQuery, ...] = (
    *MSSQL_QUERIES.values(),
    *POSTGRES_QUERIES.values(),
)


class UnsafeSnapshotQueryError(ValueError):
    """Raised when a snapshot catalog query fails read-only AST or syntax validation."""


def validate_read_only_sql(sql: str, dialect: str) -> None:
    """Parse SQL with sqlglot and assert it is a single, strictly read-only, lock-free query.

    Rejects:
      - Multiple stacked statements (e.g., ``SELECT 1; DROP TABLE t``).
      - Non-SELECT/UNION root statements or fallback ``Command`` AST nodes.
      - Any mutating/DDL/DCL AST nodes anywhere in the tree (including PostgreSQL writable CTEs).
      - ``SELECT ... INTO`` table creation clauses.
      - Row-locking clauses (``FOR UPDATE``, ``FOR SHARE``, etc.).
    """
    import sqlglot
    from sqlglot import exp

    try:
        statements = [
            stmt
            for stmt in sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
            if stmt is not None
        ]
    except Exception as exc:
        raise UnsafeSnapshotQueryError(f"SQL failed {dialect} syntax validation: {exc}") from exc

    if len(statements) != 1:
        raise UnsafeSnapshotQueryError(
            f"Expected exactly 1 read-only statement, parsed {len(statements)}."
        )

    root = statements[0]
    if not isinstance(root, (exp.Select, exp.Union)):
        raise UnsafeSnapshotQueryError(
            f"Snapshot query root must be SELECT or UNION, got {type(root).__name__}."
        )

    forbidden_nodes = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Alter,
        exp.Create,
        exp.Merge,
        exp.Grant,
        exp.Revoke,
        exp.Transaction,
        exp.Commit,
        exp.Rollback,
        exp.Copy,
        exp.Set,
        exp.Use,
        exp.Command,
        exp.TruncateTable,
        exp.Into,
        exp.Lock,
    )

    for node in root.walk():
        if isinstance(node, forbidden_nodes):
            raise UnsafeSnapshotQueryError(
                f"Forbidden non-read-only SQL AST node detected: {type(node).__name__}."
            )
        if isinstance(node, exp.Select):
            if node.args.get("into") is not None:
                raise UnsafeSnapshotQueryError("SELECT INTO is forbidden in read-only snapshot queries.")
            if node.args.get("locks"):
                raise UnsafeSnapshotQueryError("Row-locking clauses (FOR UPDATE/SHARE) are forbidden.")


def validate_snapshot_query(query: SnapshotQuery) -> None:
    """Validate a registered SnapshotQuery against its target sqlglot dialect."""
    validate_read_only_sql(query.sql, dialect=query.sqlglot_dialect)
