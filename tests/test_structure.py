"""Unit tests for Layer 1 & 2 structural parity (tables, columns, types, nullability, constraints, indexes)."""

from __future__ import annotations

from pathlib import Path

import sqlglot
from sqlglot import exp

from src.comparators.structure import compare_structure
from src.snapshot.loader import load_type_map
from src.snapshot.models import (
    CheckStatus,
    ColumnSchema,
    ConstraintSchema,
    IndexSchema,
    NameOverrides,
    Snapshot,
    TableSchema,
)

TYPE_MAP = load_type_map(Path("config/type_map.yaml"))


def _make_snapshot(engine: str, tables: tuple[TableSchema, ...]) -> Snapshot:
    return Snapshot(
        engine=engine,
        server_name="sql-src-01" if engine == "mssql" else "pg-target-01",
        database_name="ecommerce_oltp",
        schema_name="dbo" if engine == "mssql" else "public",
        captured_at="2026-02-15T00:00:00Z",
        tables=tables,
    )


def test_structure_clean_parity_passes_all_checks() -> None:
    src_table = TableSchema(
        name="Orders",
        schema_name="dbo",
        columns=(
            ColumnSchema("OrderID", "bigint", False, 1, is_identity=True),
            ColumnSchema("VATAmount", "decimal", False, 2, numeric_precision=18, numeric_scale=2),
            ColumnSchema("SKUCode", "varchar", False, 3, max_length=64),
        ),
        constraints=(
            ConstraintSchema("PK_Orders", "PRIMARY KEY", ("OrderID",)),
            ConstraintSchema("UQ_Orders_SKU", "UNIQUE", ("SKUCode",)),
        ),
        indexes=(
            IndexSchema("PK_Orders", ("OrderID",), is_unique=True, is_primary=True),
            IndexSchema("IX_Orders_SKUCode", ("SKUCode",), is_unique=True, is_primary=False),
        ),
    )
    tgt_table = TableSchema(
        name="orders",
        schema_name="public",
        columns=(
            ColumnSchema("order_id", "bigint", False, 1, is_identity=True),
            ColumnSchema("vat_amount", "numeric", False, 2, numeric_precision=18, numeric_scale=2),
            ColumnSchema("sku_code", "character varying", False, 3, max_length=64),
        ),
        constraints=(
            ConstraintSchema("orders_pkey", "PRIMARY KEY", ("order_id",)),
            ConstraintSchema("orders_sku_code_key", "UNIQUE", ("sku_code",)),
        ),
        indexes=(
            IndexSchema("orders_pkey", ("order_id",), is_unique=True, is_primary=True),
            IndexSchema("idx_orders_sku_code", ("sku_code",), is_unique=True, is_primary=False),
        ),
    )

    out = compare_structure(
        _make_snapshot("mssql", (src_table,)),
        _make_snapshot("postgresql", (tgt_table,)),
        TYPE_MAP,
        NameOverrides(),
    )

    assert all(r.status == CheckStatus.PASS for r in out.results)


def test_structure_detects_nullability_constraint_and_index_drifts() -> None:
    src_table = TableSchema(
        name="Customers",
        schema_name="dbo",
        columns=(
            ColumnSchema("CustomerID", "bigint", False, 1),
            ColumnSchema("EmailAddress", "nvarchar", False, 2, max_length=255),
            ColumnSchema("Notes", "nvarchar", True, 3, max_length=500),
        ),
        constraints=(
            ConstraintSchema("PK_Customers", "PRIMARY KEY", ("CustomerID",)),
            ConstraintSchema("UQ_Customers_Email", "UNIQUE", ("EmailAddress",)),
        ),
        indexes=(
            IndexSchema("PK_Customers", ("CustomerID",), is_unique=True, is_primary=True),
            IndexSchema("IX_Customers_Email", ("EmailAddress",), is_unique=False, is_primary=False),
        ),
    )
    tgt_table = TableSchema(
        name="customers",
        schema_name="public",
        columns=(
            ColumnSchema("customer_id", "bigint", False, 1),
            # Loosened NOT NULL -> NULL (FAIL)
            ColumnSchema("email_address", "varchar", True, 2, max_length=255),
            # Tightened NULL -> NOT NULL (WARN)
            ColumnSchema("notes", "varchar", False, 3, max_length=500),
        ),
        constraints=(
            ConstraintSchema("customers_pkey", "PRIMARY KEY", ("customer_id",)),
            # Missing UNIQUE constraint on email_address (FAIL)
        ),
        indexes=(
            IndexSchema("customers_pkey", ("customer_id",), is_unique=True, is_primary=True),
            # Missing secondary index on email_address (FAIL)
        ),
    )

    out = compare_structure(
        _make_snapshot("mssql", (src_table,)),
        _make_snapshot("postgresql", (tgt_table,)),
        TYPE_MAP,
        NameOverrides(),
    )

    by_code = {(r.check_code, r.status) for r in out.results}
    assert ("COLUMN_NULLABILITY_MISMATCH", CheckStatus.FAIL) in by_code
    assert ("COLUMN_NULLABILITY_MISMATCH", CheckStatus.WARN) in by_code
    assert ("CONSTRAINT_MISSING", CheckStatus.FAIL) in by_code
    assert ("INDEX_MISSING", CheckStatus.FAIL) in by_code


def test_structure_detects_index_uniqueness_mismatch() -> None:
    src_table = TableSchema(
        name="Products",
        schema_name="dbo",
        columns=(
            ColumnSchema("ProductID", "bigint", False, 1),
            ColumnSchema("SKUCode", "varchar", False, 2, max_length=64),
        ),
        indexes=(IndexSchema("UX_Products_SKU", ("SKUCode",), is_unique=True, is_primary=False),),
    )
    tgt_table = TableSchema(
        name="products",
        schema_name="public",
        columns=(
            ColumnSchema("product_id", "bigint", False, 1),
            ColumnSchema("sku_code", "varchar", False, 2, max_length=64),
        ),
        indexes=(IndexSchema("idx_products_sku", ("sku_code",), is_unique=False, is_primary=False),),
    )

    out = compare_structure(
        _make_snapshot("mssql", (src_table,)),
        _make_snapshot("postgresql", (tgt_table,)),
        TYPE_MAP,
        NameOverrides(),
    )

    uniq_fails = [r for r in out.results if r.check_code == "INDEX_UNIQUENESS_MISMATCH"]
    assert len(uniq_fails) == 1
    assert uniq_fails[0].status == CheckStatus.FAIL
    assert 'CREATE UNIQUE INDEX CONCURRENTLY ON "public"."products" ("sku_code");' in (
        uniq_fails[0].remediation or ""
    )


def test_structure_enforces_composite_pk_unique_fk_and_index_column_order() -> None:
    """Composite PK, UNIQUE, FK, and secondary indexes must fail when column order is swapped."""
    src_parent = TableSchema(
        name="TenantCustomers",
        schema_name="dbo",
        columns=(
            ColumnSchema("TenantID", "bigint", False, 1),
            ColumnSchema("CustomerID", "bigint", False, 2),
        ),
        constraints=(
            ConstraintSchema("PK_TenantCustomers", "PRIMARY KEY", ("TenantID", "CustomerID")),
        ),
    )
    tgt_parent = TableSchema(
        name="tenant_customers",
        schema_name="public",
        columns=(
            ColumnSchema("tenant_id", "bigint", False, 1),
            ColumnSchema("customer_id", "bigint", False, 2),
        ),
        constraints=(
            # Reversed composite PK order -> MUST FAIL
            ConstraintSchema("tenant_customers_pkey", "PRIMARY KEY", ("customer_id", "tenant_id")),
        ),
    )

    src_orders = TableSchema(
        name="Orders",
        schema_name="dbo",
        columns=(
            ColumnSchema("TenantID", "bigint", False, 1),
            ColumnSchema("CustomerID", "bigint", False, 2),
            ColumnSchema("OrderDateUTC", "datetime2", False, 3),
        ),
        constraints=(
            ConstraintSchema("UQ_Orders_Cust_Date", "UNIQUE", ("CustomerID", "OrderDateUTC")),
            ConstraintSchema(
                "FK_Orders_TenantCustomers",
                "FOREIGN KEY",
                ("TenantID", "CustomerID"),
                referenced_table="TenantCustomers",
                referenced_columns=("TenantID", "CustomerID"),
            ),
        ),
        indexes=(
            IndexSchema(
                "IX_Orders_Customer_OrderDate",
                ("CustomerID", "OrderDateUTC"),
                is_unique=False,
                is_primary=False,
            ),
        ),
    )
    tgt_orders = TableSchema(
        name="orders",
        schema_name="public",
        columns=(
            ColumnSchema("tenant_id", "bigint", False, 1),
            ColumnSchema("customer_id", "bigint", False, 2),
            ColumnSchema("order_date_utc", "timestamptz", False, 3),
        ),
        constraints=(
            # Reversed composite UNIQUE order -> MUST FAIL
            ConstraintSchema("orders_date_cust_key", "UNIQUE", ("order_date_utc", "customer_id")),
            # Reversed composite FK order -> MUST FAIL
            ConstraintSchema(
                "orders_tenant_cust_fkey",
                "FOREIGN KEY",
                ("customer_id", "tenant_id"),
                referenced_table="tenant_customers",
                referenced_columns=("customer_id", "tenant_id"),
            ),
        ),
        indexes=(
            # Reversed composite INDEX order -> MUST FAIL
            IndexSchema(
                "idx_orders_date_customer",
                ("order_date_utc", "customer_id"),
                is_unique=False,
                is_primary=False,
            ),
        ),
    )

    out = compare_structure(
        _make_snapshot("mssql", (src_parent, src_orders)),
        _make_snapshot("postgresql", (tgt_parent, tgt_orders)),
        TYPE_MAP,
        NameOverrides(),
    )

    failed_constraints = [r for r in out.results if r.check_code == "CONSTRAINT_MISSING" and r.status == CheckStatus.FAIL]
    assert len(failed_constraints) == 3
    for finding in failed_constraints:
        assert "Column order mismatch" in finding.message

    failed_indexes = [r for r in out.results if r.check_code == "INDEX_MISSING" and r.status == CheckStatus.FAIL]
    assert len(failed_indexes) == 1
    assert "different order" in failed_indexes[0].message


def test_structure_resolves_fk_referenced_table_and_columns_after_name_overrides() -> None:
    """FK parity must resolve both table_overrides and referenced column_overrides across tables."""
    src_payments = TableSchema(
        name="LegacyPayments",
        schema_name="dbo",
        columns=(
            ColumnSchema("PaymentRefCode", "varchar", False, 1, max_length=64),
        ),
        constraints=(
            ConstraintSchema("PK_LegacyPayments", "PRIMARY KEY", ("PaymentRefCode",)),
        ),
    )
    src_refunds = TableSchema(
        name="Refunds",
        schema_name="dbo",
        columns=(
            ColumnSchema("RefundID", "bigint", False, 1),
            ColumnSchema("PaymentRefCode", "varchar", False, 2, max_length=64),
        ),
        constraints=(
            ConstraintSchema("PK_Refunds", "PRIMARY KEY", ("RefundID",)),
            ConstraintSchema(
                "FK_Refunds_LegacyPayments",
                "FOREIGN KEY",
                ("PaymentRefCode",),
                referenced_table="LegacyPayments",
                referenced_columns=("PaymentRefCode",),
            ),
        ),
    )

    tgt_payments = TableSchema(
        name="settled_payments",
        schema_name="public",
        columns=(
            ColumnSchema("external_payment_ref", "character varying", False, 1, max_length=64),
        ),
        constraints=(
            ConstraintSchema("settled_payments_pkey", "PRIMARY KEY", ("external_payment_ref",)),
        ),
    )
    tgt_refunds = TableSchema(
        name="refunds",
        schema_name="public",
        columns=(
            ColumnSchema("refund_id", "bigint", False, 1),
            ColumnSchema("payment_ref_code", "character varying", False, 2, max_length=64),
        ),
        constraints=(
            ConstraintSchema("refunds_pkey", "PRIMARY KEY", ("refund_id",)),
            ConstraintSchema(
                "refunds_payment_ref_fkey",
                "FOREIGN KEY",
                ("payment_ref_code",),
                referenced_table="settled_payments",
                referenced_columns=("external_payment_ref",),
            ),
        ),
    )

    overrides = NameOverrides(
        table_overrides={"LegacyPayments": "settled_payments"},
        column_overrides={"LegacyPayments": {"PaymentRefCode": "external_payment_ref"}},
    )

    out = compare_structure(
        _make_snapshot("mssql", (src_payments, src_refunds)),
        _make_snapshot("postgresql", (tgt_payments, tgt_refunds)),
        TYPE_MAP,
        overrides,
    )
    assert all(r.status == CheckStatus.PASS for r in out.results)


def test_structure_dangling_table_and_column_overrides_surface_fail_findings() -> None:
    """Dangling overrides for missing target tables, missing target columns, or missing override tables must FAIL."""
    src_table = TableSchema(
        name="Orders",
        schema_name="dbo",
        columns=(
            ColumnSchema("OrderID", "bigint", False, 1),
            ColumnSchema("RefCode", "varchar", False, 2, max_length=32),
        ),
    )
    tgt_table = TableSchema(
        name="orders",
        schema_name="public",
        columns=(
            ColumnSchema("order_id", "bigint", False, 1),
            ColumnSchema("ref_code", "varchar", False, 2, max_length=32),
        ),
    )

    # Case 1: Dangling table_override to nonexistent target table
    out_tbl = compare_structure(
        _make_snapshot("mssql", (src_table,)),
        _make_snapshot("postgresql", (tgt_table,)),
        TYPE_MAP,
        NameOverrides(table_overrides={"Orders": "nonexistent_target_table"}),
    )
    tbl_fails = {(r.check_code, r.status) for r in out_tbl.results}
    assert ("TABLE_OVERRIDE_INVALID", CheckStatus.FAIL) in tbl_fails
    assert ("TABLE_MISSING_TARGET", CheckStatus.FAIL) in tbl_fails
    assert ("TABLE_MAPPED", CheckStatus.PASS) not in tbl_fails

    # Case 2: Dangling column_override on matched table + stale table key in column_overrides
    out_col = compare_structure(
        _make_snapshot("mssql", (src_table,)),
        _make_snapshot("postgresql", (tgt_table,)),
        TYPE_MAP,
        NameOverrides(
            column_overrides={
                "Orders": {"RefCode": "nonexistent_col"},
                "GhostTable": {"OldCol": "new_col"},
            }
        ),
    )
    col_invalid = [r for r in out_col.results if r.check_code == "COLUMN_OVERRIDE_INVALID"]
    assert len(col_invalid) == 2
    assert all(r.status == CheckStatus.FAIL for r in col_invalid)
    assert any(r.check_code == "COLUMN_MISSING_TARGET" and r.source_object == "Orders.RefCode" for r in out_col.results)


def test_structure_create_index_concurrently_remediation_safely_quotes_identifiers() -> None:
    """CREATE INDEX CONCURRENTLY remediation must safely double-quote reserved words and embedded quotes."""
    src_table = TableSchema(
        name="Order",
        schema_name="dbo",
        columns=(
            ColumnSchema("User", "bigint", False, 1),
            ColumnSchema('Ref"Code', "varchar", False, 2, max_length=32),
        ),
        indexes=(
            IndexSchema("IX_Order_User_Ref", ("User", 'Ref"Code'), is_unique=True, is_primary=False),
        ),
    )
    tgt_table = TableSchema(
        name="order",
        schema_name="public",
        columns=(
            ColumnSchema("user", "bigint", False, 1),
            ColumnSchema('ref"code', "varchar", False, 2, max_length=32),
        ),
        indexes=(),
    )

    out = compare_structure(
        _make_snapshot("mssql", (src_table,)),
        _make_snapshot("postgresql", (tgt_table,)),
        TYPE_MAP,
        NameOverrides(column_overrides={"Order": {'Ref"Code': 'ref"code'}}),
    )

    idx_missing = [r for r in out.results if r.check_code == "INDEX_MISSING"]
    assert len(idx_missing) == 1
    remediation = idx_missing[0].remediation or ""
    assert remediation == 'CREATE UNIQUE INDEX CONCURRENTLY ON "public"."order" ("user", "ref""code");'

    # Verify PostgreSQL parser accepts the generated remediation DDL cleanly!
    parsed = sqlglot.parse_one(remediation, read="postgres", error_level=sqlglot.ErrorLevel.RAISE)
    assert isinstance(parsed, exp.Create)
