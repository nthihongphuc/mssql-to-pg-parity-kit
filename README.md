# mssql-to-pg-parity-kit

[![CI](https://github.com/nthihongphuc/mssql-to-pg-parity-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/nthihongphuc/mssql-to-pg-parity-kit/actions/workflows/ci.yml)
![SQL Server 2022](https://img.shields.io/badge/SQL%20Server-2022-CC2927?style=flat-square&logo=microsoftsqlserver&logoColor=white)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![sqlglot AST](https://img.shields.io/badge/sqlglot-AST%20Guard-0D9488?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-2EA44F?style=flat-square)](./LICENSE)

[Architecture](#architecture) • [Five-Layer Parity Model](#five-layer-parity-model) • [Quickstart](#quickstart) • [Verification](#sample-drift-report-output) • [License](#license)

**Heterogeneous Database Migration Parity & Audit Framework (SQL Server to PostgreSQL)** built on an **Immutable Snapshot** architecture.

Captures a single read-only point-in-time snapshot from source SQL Server and target PostgreSQL instances, executes deterministic multi-layer verification offline (zero recurring load on production OLTP), and produces both an actionable Markdown drift report and a transactional PostgreSQL audit trail (`migration_audit`).

---

## Architecture

```mermaid
flowchart LR
    subgraph LiveDBs["Single Read-Only Capture Window"]
        MSSQL["SQL Server 2022 (sql-src-01)"]
        PG["PostgreSQL 16 (pg-target-01)"]
    end

    subgraph Frozen["Immutable Snapshot Artifacts (JSON)"]
        MSNAP["mssql/snapshot.json"]
        PGSNAP["pg/snapshot.json"]
    end

    subgraph Config["Declarative Governance Config"]
        TMAP["config/type_map.yaml"]
        OVR["config/name_overrides.yaml"]
    end

    subgraph Engine["Offline 5-Layer Parity Engine (src/)"]
        L1["L1: Naming & Canonical Token"]
        L2["L2: Structure, Types & Indexes"]
        L3["L3: Data Counts, Sums & Checksums"]
        L4["L4: Routine Inventory"]
        L5["L5: Security & Least-Privilege"]
        L1 --> L2 --> L3
        L1 --> L4
        L1 --> L5
    end

    subgraph Outputs["Audit & Cutover Gate Outputs"]
        MD["Markdown Drift Report (out/report.md)"]
        SQL["Audit Trail Inserts (out/audit.sql)"]
        AUDITDB["PostgreSQL migration_audit Schema"]
        GATE["CI/CD Strict Gate (Exit 0 or 1)"]
    end

    MSSQL -->|"Read-only metadata + aggregates"| MSNAP
    PG -->|"Read-only metadata + aggregates"| PGSNAP

    MSNAP --> L1
    PGSNAP --> L1
    TMAP --> L2
    OVR --> L1
    OVR --> L4
    OVR --> L5

    L3 --> MD
    L4 --> MD
    L5 --> MD
    L3 --> SQL
    L4 --> SQL
    L5 --> SQL
    SQL --> AUDITDB
    MD --> GATE
```

---

## Problem Statement

Migrating mission-critical OLTP workloads from SQL Server to PostgreSQL routinely fails at the verification stage for two structural reasons:

### 1. Why Naive `PascalCase -> snake_case` Regex Conversion Fails in Real Schemas

Many ad-hoc migration scripts try to map SQL Server identifiers to PostgreSQL identifiers using naive regex case conversion (e.g., inserting `_` before every capital letter). That approach breaks immediately on real-world enterprise acronyms and mixed conventions:

| SQL Server Identifier | Naive Regex Output | Actual PostgreSQL Column | Naive Match? | `canon(name)` Token |
| :--- | :--- | :--- | :---: | :--- |
| `SKUCode` | `s_k_u_code` | `sku_code` | FAIL | `skucode` (MATCH) |
| `VATAmount` | `v_a_t_amount` | `vat_amount` | FAIL | `vatamount` (MATCH) |
| `OrderDateUTC` | `order_date_u_t_c` | `order_date_utc` | FAIL | `orderdateutc` (MATCH) |
| `HTTPStatusCode` | `h_t_t_p_status_code` | `http_status_code` | FAIL | `httpstatuscode` (MATCH) |
| `CustomerID` | `customer_i_d` | `customer_id` | FAIL | `customerid` (MATCH) |

False negatives flood DBA triage reports with noise, causing genuine structural drift to be overlooked.

### 2. Why Repeated Live-DB Parity Checks Hurt Production OLTP

Running iterative comparison scripts directly against live SQL Server and PostgreSQL databases creates three operational hazards:

1. **OLTP Contention & Buffer Pool Eviction:** Repeated `COUNT(*)`, `SUM()`, and catalog inspection queries scan large hot tables, evicting transactional pages and competing for I/O and CPU during cutover rehearsals.
2. **Moving-Target Non-Reproducibility:** Under active CDC or dual-write replication, live comparisons drift between check #1 (row counts) and check #2 (aggregates), making it impossible to distinguish real data corruption from replication lag.
3. **Weak Compliance Auditability:** Re-running ad-hoc queries leaves no frozen evidence of *exact* schema and data state at cutover sign-off.

**Immutable Snapshot Solution:** Capture metadata, aggregates, routines, and grants **once** into frozen JSON artifacts (`mssql/snapshot.json` and `pg/snapshot.json`). All five parity layers then execute deterministically offline—enabling unlimited rule tuning, CI/CD gating, peer review, and reproducible compliance audits with zero additional production queries.

---

## Canonical-Token Matching & Collision Safety

Layer 1 (`src/comparators/naming.py`) resolves heterogeneous object names through a strict three-stage pipeline:

1. **Explicit Overrides First (`config/name_overrides.yaml`):** Intentional renames (e.g., `LegacyAuditLog -> archive_audit_events`, `Payments.PaymentRefCode -> external_payment_ref`) are bound first and validated for existence on both sides. Dangling overrides targeting a nonexistent table or column emit an explicit `FAIL` (`*_OVERRIDE_INVALID` + `*_MISSING_TARGET`) and **never** silently fall back to canonical matching.
2. **Canonical-Token Normalization:** Remaining identifiers are reduced via:
   ```text
   canon(name) = lower(strip_non_alnum(name))
   ```
   This matches `SKUCode` <-> `sku_code` (`skucode`) and `VATAmount` <-> `vat_amount` (`vatamount`) deterministically without fragile acronym dictionaries. Symbol-only identifiers that collapse to an empty canonical token (e.g., `"___"` vs `"---"`) are never auto-matched and require explicit overrides.
3. **Zero-Guess Collision Detection (Both Sides):** If two or more unconsumed identifiers on **either** side collapse to the same canonical token (for example, source `OrderID`/`Order_ID` or target `order_id`/`orderid` normalizing to `orderid`), the engine **refuses to guess** and emits a `CRITICAL` `*_CANONICAL_COLLISION` failure until disambiguated via `config/name_overrides.yaml`.

---

## Read-Only Live Snapshot Capture Engine (`src/snapshot/capturer.py` & `queries.py`)

To back the immutable snapshot JSON format with real production metadata extraction, [`src/snapshot/queries.py`](src/snapshot/queries.py) ships 13 parameterized, strictly read-only catalog queries executed by [`src/snapshot/capturer.py`](src/snapshot/capturer.py) under a multi-layer read-only safety architecture:

| Engine | System Catalogs & DMVs | Read-Only Session & DBA Hardening Guarantees |
| :--- | :--- | :--- |
| **SQL Server (`tsql`)** | `sys.tables`, `sys.columns`, `sys.key_constraints`, `sys.foreign_keys`, `sys.indexes`, `sys.index_columns`, `sys.objects`, `sys.database_permissions`, `sys.dm_db_partition_stats` | Enforces `ApplicationIntent=ReadOnly` in connection strings, ODBC `readonly=True`, `SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED`, `SET LOCK_TIMEOUT 5000`, unconditional `ROLLBACK` on close, `NVARCHAR`/`NCHAR` UTF-16 byte-to-char length normalization (`c.max_length / 2`, `-1` for `MAX`), and safe `[identifier]` bracket quoting on numeric aggregates. |
| **PostgreSQL (`postgres`)** | `pg_class`, `pg_attribute`, `pg_constraint`, `pg_index`, `pg_proc`, `information_schema.role_table_grants`, `information_schema.role_routine_grants`, `pg_roles`, `pg_stat_user_tables` | Runs inside an explicit `BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = '15s'; ... ROLLBACK;` block; auto-detects `psycopg` (v3), `psycopg2`, or zero-C-extension `psql` CLI subprocess; filters out invalid `CONCURRENTLY` indexes (`indisvalid AND indisready`); distinguishes key vs `INCLUDE` columns (`indnkeyatts`); double-quotes identifiers via `pg_quote_ident`. |

Every catalog query and dynamically generated table aggregate query (`COUNT(*)`, `SUM`, `MIN`, `MAX`, `NULL` counts) is statically verified by `validate_read_only_sql(...)` (`sqlglot` AST traversal) **immediately before execution**, rejecting stacked queries, mutating DML/DDL/DCL statements, PostgreSQL writable CTEs (`WITH ... DELETE/UPDATE`), `SELECT INTO`, and row-locking clauses (`FOR UPDATE` / `FOR SHARE`).

### Live Snapshot Capture CLI (`cli capture`)

Prefer `--dsn-env` over `--dsn` so database credentials never appear in shell history, process lists, or CLI output:

```bash
# Capture PostgreSQL snapshot (full metadata + exact row counts & numeric aggregates)
export PG_CAPTURE_DSN="postgresql://readonly_audit:CHANGE_ME@127.0.0.1:5432/retail_demo"
python -m src.cli capture \
  --engine postgres \
  --dsn-env PG_CAPTURE_DSN \
  --schema public \
  --server pg-target-01 \
  --database retail_demo \
  --out snapshots/pg/snapshot.json

# Metadata-only capture (zero table scans)
python -m src.cli capture \
  --engine postgres \
  --dsn-env PG_CAPTURE_DSN \
  --schema public \
  --server pg-target-01 \
  --database retail_demo \
  --skip-data-stats \
  --out snapshots/pg/snapshot_meta.json

# Capture SQL Server snapshot (ApplicationIntent=ReadOnly enforced automatically)
export MSSQL_CAPTURE_DSN="Driver={ODBC Driver 18 for SQL Server};Server=127.0.0.1,1433;Database=retail_demo;UID=readonly_audit;PWD=CHANGE_ME;Encrypt=yes;"
python -m src.cli capture \
  --engine mssql \
  --dsn-env MSSQL_CAPTURE_DSN \
  --schema dbo \
  --server sql-src-01 \
  --database retail_demo \
  --out snapshots/mssql/snapshot.json
```

### Live PostgreSQL 16 Verification Benchmark (`demo/pg_init.sql`)

Measured end-to-end against a live Docker PostgreSQL 16 instance seeded with [`demo/pg_init.sql`](demo/pg_init.sql) (`ANALYZE;` completed) via `python -m src.cli capture --engine postgres --dsn-env PG_CAPTURE_DSN`:

| Metric | Live PG16 Measured Result |
| :--- | :--- |
| **Full Capture Latency** (metadata + exact `COUNT(*)` / `SUM` / `MIN` / `MAX` / `NULL`s) | **210.7 ms** |
| **Metadata-Only Capture Latency** (`--skip-data-stats`, zero table scans) | **180.5 ms** |
| **Captured Schema & Security Objects** | `6` tables, `34` columns, `13` constraints, `10` indexes, `2` functions, `63` catalog grant rows |
| **Captured Data Statistics** | `6` table stats covering `17` numeric column aggregates |
| **Server-Verified Read-Only Guard** | `SHOW transaction_read_only = on`, `SHOW statement_timeout = 15s`, DDL blocked (`ReadOnlySqlTransaction`) |
| **End-to-End Pipeline (`capture -> validate -> compare`)** | Validated cleanly (`0` schema errors); 5-layer comparison against `fixtures/retail_demo_run/mssql/snapshot.json` evaluated `132` checks (`113 PASS`, `13 WARN`, `6 FAIL`), detecting every intentional drift across Layers 1–5 |

---

## Five-Layer Parity Model

| Layer | Module | What It Verifies | Examples of Caught Drift |
| :--- | :--- | :--- | :--- |
| **L1: Naming** | [`src/comparators/naming.py`](src/comparators/naming.py) | Canonical-token matching, explicit override validation, ambiguity/collision detection | Unmapped abbreviation (`PromoChannelCode` vs `promo_chnl_cd`), canonical collisions |
| **L2: Structure** | [`src/comparators/structure.py`](src/comparators/structure.py) | Tables, columns, type compatibility via `config/type_map.yaml`, length/precision/scale truncation, nullability, PK/FK/UNIQUE constraints, secondary indexes | `NOT NULL -> NULL` regression, missing composite index, `nvarchar(max) -> varchar(500)`, `decimal(18,2) -> numeric(12,2)` |
| **L3: Data** | [`src/comparators/data.py`](src/comparators/data.py) | Exact row counts, exact `Decimal` numeric `SUM`/`MIN`/`MAX`/NULL counts, table aggregate checksums | CDC lag row count drift (`4` vs `3`), financial sum mismatch (`875.05` vs `743.05`), checksum divergence |
| **L4: Routines** | [`src/comparators/routines.py`](src/comparators/routines.py) | Stored procedure & function inventory parity, documented retirements (`replaced_by_app`), parameter count checks | Unmigrated stored procedure (`usp_ArchiveOldOrders`), signature parameter mismatch |
| **L5: Security** | [`src/comparators/security.py`](src/comparators/security.py) | Principal-to-object DML/EXECUTE privilege parity + PostgreSQL least-privilege hardening | `PUBLIC` granted `SELECT`, `ALL PRIVILEGES` on application role, non-admin `SUPERUSER`, `WITH GRANT OPTION` |

---

## Repository Structure

```text
mssql-to-pg-parity-kit/
├── config/
│   ├── type_map.yaml              # SQL Server -> PostgreSQL data type compatibility matrix
│   └── name_overrides.yaml        # Explicit rename & routine retirement overrides
├── demo/
│   ├── docker-compose.yml         # SQL Server 2022 (sql-src-01) + PostgreSQL 16 (pg-target-01)
│   ├── mssql_init.sql             # Synthetic retail_demo source schema, routines, grants, data
│   └── pg_init.sql                # Synthetic retail_demo target schema with intentional drift
├── fixtures/
│   └── retail_demo_run/
│       ├── mssql/snapshot.json    # Frozen source snapshot for offline CLI & CI testing
│       └── pg/snapshot.json       # Frozen target snapshot for offline CLI & CI testing
├── sql/
│   └── migration_audit_schema.sql # PostgreSQL migration_audit DDL, indexes & summary views
├── src/
│   ├── snapshot/
│   │   ├── models.py              # Frozen typed dataclasses for snapshots & check results
│   │   ├── loader.py              # JSON snapshot validation & YAML config loaders
│   │   ├── queries.py             # Read-only MSSQL & PostgreSQL catalog queries + AST guard
│   │   └── capturer.py            # Live read-only PG/MSSQL snapshot capturer + JSON writer
│   ├── comparators/
│   │   ├── naming.py              # Canonical-token matcher, collision detector & type checker
│   │   ├── structure.py           # Table, column, type, nullability, constraint & index parity
│   │   ├── data.py                # Row count, exact Decimal SUM/MIN/MAX & checksum parity
│   │   ├── routines.py            # Procedure/function migration & retirement inventory
│   │   └── security.py            # Grant parity & PostgreSQL least-privilege guardrails
│   ├── report.py                  # Markdown drift report + PostgreSQL audit SQL renderer
│   └── cli.py                     # CLI entrypoint (capture, validate & compare subcommands)
├── tests/
│   ├── test_capturer.py           # Read-only AST guard, PG/MSSQL mock capture & CLI capture tests
│   ├── test_naming.py
│   ├── test_structure.py
│   ├── test_data_routines_security.py
│   ├── test_sql_and_queries.py    # sqlglot AST, read-only guard & audit schema alignment tests
│   └── test_cli_e2e.py
├── .env.example
├── pyproject.toml
└── requirements.txt
```

---

## Quickstart

### Option A: Offline Fixture Mode (Zero Docker Required)

1. Install dependencies (Python 3.10+):
   ```bash
   pip install -r requirements.txt
   ```

2. Validate the immutable snapshot pair:
   ```bash
   python -m src.cli validate --snapshot-dir fixtures/retail_demo_run
   ```

3. Run the 5-layer parity comparison and generate both the Markdown drift report and PostgreSQL audit trail SQL:
   ```bash
   python -m src.cli compare \
     --snapshot-dir fixtures/retail_demo_run \
     --out out/report.md \
     --sql-out out/audit.sql
   ```
   Add `--strict` in CI/CD cutover pipelines to exit with code `1` whenever any `FAIL` finding exists.

4. Run the automated test suite:
   ```bash
   pytest
   ```

### Option B: Live Docker Compose Demo (SQL Server 2022 + PostgreSQL 16)

1. Configure environment placeholders:
   ```bash
   cp .env.example .env
   # Edit .env and replace all CHANGE_ME passwords
   ```

2. Start `sql-src-01` (MSSQL 2022) and `pg-target-01` (PostgreSQL 16 with `retail_demo` + `migration_audit` auto-initialized):
   ```bash
   docker compose --env-file .env -f demo/docker-compose.yml up -d
   ```

3. Initialize the synthetic `retail_demo` database on SQL Server:
   ```bash
   docker exec -i sql-src-01 /opt/mssql-tools18/bin/sqlcmd \
     -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -i /init/mssql_init.sql
   ```

4. Persist an audit run into PostgreSQL `migration_audit` and query the summary view:
   ```bash
   docker exec -i pg-target-01 psql -U retail_admin -d retail_demo < out/audit.sql
   docker exec -i pg-target-01 psql -U retail_admin -d retail_demo \
     -c "SELECT * FROM migration_audit.v_run_layer_summary;"
   ```

---

## Sample Drift Report Output

Running `python -m src.cli compare --snapshot-dir fixtures/retail_demo_run --out out/report.md` produces:

```markdown
# Heterogeneous Database Migration Parity Report

- **Run ID:** `2a7c9e10-4b8d-4f3a-9c12-7e6d5a4b3c21`
- **Source (SQL Server):** `sql-src-01` / `retail_demo.dbo` (snapshot: `2026-02-15T02:00:00Z`)
- **Target (PostgreSQL):** `pg-target-01` / `retail_demo.public` (snapshot: `2026-02-15T02:00:04Z`)
- **Overall Cutover Gate:** **FAIL**

## Executive Parity Summary

| Layer | Status | PASS | WARN | FAIL | SKIP | Total |
| :--- | :---: | ---: | ---: | ---: | ---: | ---: |
| `naming` | **FAIL** | 6 | 1 | 1 | 0 | 8 |
| `structure` | **FAIL** | 79 | 0 | 2 | 0 | 81 |
| `data` | **FAIL** | 18 | 0 | 3 | 0 | 21 |
| `routines` | **FAIL** | 3 | 0 | 1 | 0 | 4 |
| `security` | **WARN** | 9 | 5 | 0 | 0 | 14 |
| **TOTAL** | **FAIL** | **115** | **6** | **7** | **0** | **128** |
```

Key drift findings surfaced automatically with copy-pasteable remediation SQL:

- **Layer 1 (`FAIL` / `CRITICAL`):** `Orders.PromoChannelCode` missing on target (`orders.promo_chnl_cd` flagged as unmapped target column).
- **Layer 2 (`FAIL` / `HIGH`):** `Customers.EmailAddress` loosened from `NOT NULL` to `NULL` -> `ALTER TABLE "public"."customers" ALTER COLUMN "email_address" SET NOT NULL;`
- **Layer 2 (`FAIL` / `HIGH`):** Missing composite index `Orders.IX_Orders_CustomerID_OrderDateUTC` -> `CREATE INDEX CONCURRENTLY ON "public"."orders" ("customer_id", "order_date_utc");`
- **Layer 3 (`FAIL` / `CRITICAL`):** `Payments` row count (`4` vs `3`), `PaidAmount` sum (`875.05` vs `743.05`), and aggregate checksum mismatch.
- **Layer 4 (`FAIL` / `HIGH`):** `usp_ArchiveOldOrders` not migrated and not listed under `routine_retirements`.
- **Layer 5 (`WARN` / `HIGH`):** `PUBLIC` granted `SELECT` on `customers` (`REVOKE SELECT ON "customers" FROM PUBLIC;`) and `app_retail_rw` granted overly broad `ALL PRIVILEGES` on `payments`.

---

## Key DBA & Engineering Design Decisions

- **Exact `Decimal` Arithmetic & NULL Fidelity for Financial Parity:** Column `SUM`, `MIN`, and `MAX` values are stored as exact strings in snapshots and compared via Python `decimal.Decimal`, eliminating IEEE-754 floating-point drift (`100.00 == 100`) while strictly distinguishing SQL `NULL` (`None` on empty tables) from numeric `"0"` and safely rejecting `NaN`.
- **Order-Sensitive Composite Key & Index Parity:** Composite `PRIMARY KEY`, `UNIQUE`, `FOREIGN KEY`, and secondary indexes all enforce **exact column order** (`(customer_id, order_date)` != `(order_date, customer_id)`) so B-Tree leading-column seek semantics never silently regress, and foreign keys resolve both target table and target referenced column overrides across tables.
- **Strict Length, Precision, Scale & Integer-Digit Guards:** Catches `NVARCHAR(MAX)` (`max_length = -1`) truncation against bounded `VARCHAR`/`CHAR`, length narrowing (`NVARCHAR(200) -> VARCHAR(100)`), precision/scale shrinkage (`DECIMAL(18,4) -> NUMERIC(18,2)`), and integer-digit overflow (`DECIMAL(18,2) -> NUMERIC(18,4)`).
- **Asymmetric Nullability Severity:** Loosening `NOT NULL -> NULL` on target is a `FAIL` (data integrity regression), whereas tightening `NULL -> NOT NULL` is a `WARN` (ETL load hazard if historical NULLs exist).
- **Safe Quoted DDL & Verified Audit Trail Alignment:** Remediation SQL (`CREATE INDEX CONCURRENTLY`, `ALTER TABLE`, `GRANT`/`REVOKE`) double-quotes PostgreSQL identifiers safely, and `render_audit_sql` validates UUIDs, escapes single quotes (`'` -> `''`) across text and JSONB literals, and is AST-tested with `sqlglot` for 100% column and `NOT NULL`/FK alignment against `sql/migration_audit_schema.sql`.
- **Exact-Match Admin Allowlist & Partial Index on Active Drift:** Security checks use an exact canonical admin allowlist (preventing substring bypasses such as `app_postgres_worker`), and `migration_audit.check_result` defines a partial index (`WHERE status IN ('FAIL', 'WARN')`) so cutover dashboards remain sub-millisecond across historical rehearsal runs.

---

## License

Licensed under the [MIT License](./LICENSE).

