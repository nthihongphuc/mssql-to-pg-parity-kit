"""Unit tests for canonical-token naming matching, collision detection, overrides, and type compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.comparators.naming import (
    canonicalize,
    check_type_compatibility,
    match_names,
    normalize_base_type,
)
from src.snapshot.loader import load_type_map
from src.snapshot.models import CheckStatus, ColumnSchema

TYPE_MAP = load_type_map(Path("config/type_map.yaml"))


@pytest.mark.parametrize(
    ("mssql_name", "pg_name"),
    [
        ("SKUCode", "sku_code"),
        ("VATAmount", "vat_amount"),
        ("OrderID", "order_id"),
        ("HTTPStatusCode", "http_status_code"),
        ("CreatedAtUTC", "created_at_utc"),
        ("Customer_TaxID_V2", "customer_tax_id_v2"),
        ("OrderItems", "order_items"),
    ],
)
def test_canonicalize_handles_acronyms_and_separators(mssql_name: str, pg_name: str) -> None:
    """Canonical-token normalization matches acronyms where naive PascalCase->snake_case fails."""
    assert canonicalize(mssql_name) == canonicalize(pg_name)


def test_match_names_resolves_acronyms_and_explicit_overrides() -> None:
    source = ["CustomerID", "SKUCode", "VATAmount", "PaymentRefCode", "PromoChannelCode"]
    target = ["customer_id", "sku_code", "vat_amount", "external_payment_ref", "promo_chnl_cd"]
    overrides = {"PaymentRefCode": "external_payment_ref"}

    result = match_names(source, target, overrides=overrides)
    mapping = result.source_to_target()

    assert mapping["CustomerID"] == "customer_id"
    assert mapping["SKUCode"] == "sku_code"
    assert mapping["VATAmount"] == "vat_amount"
    assert mapping["PaymentRefCode"] == "external_payment_ref"
    assert result.unmatched_source == ["PromoChannelCode"]
    assert result.unmatched_target == ["promo_chnl_cd"]
    assert result.collisions == []
    assert result.invalid_overrides == []


def test_match_names_detects_source_canonical_collision_without_guessing() -> None:
    """Two source identifiers collapsing to the same canonical token must trigger a collision."""
    source = ["OrderID", "Order_ID", "TotalAmount"]
    target = ["order_id", "total_amount"]

    result = match_names(source, target)

    assert len(result.collisions) == 1
    assert result.collisions[0].canonical_token == "orderid"
    assert result.collisions[0].source_names == ("OrderID", "Order_ID")
    assert result.collisions[0].target_names == ("order_id",)
    assert result.source_to_target() == {"TotalAmount": "total_amount"}


def test_match_names_detects_target_and_both_sides_canonical_collisions() -> None:
    """Duplicate canonical tokens on target side or on both sides must raise CanonicalCollision."""
    target_dup = match_names(["OrderID", "Status"], ["order_id", "orderid", "status"])
    assert len(target_dup.collisions) == 1
    assert target_dup.collisions[0].canonical_token == "orderid"
    assert target_dup.collisions[0].source_names == ("OrderID",)
    assert target_dup.collisions[0].target_names == ("order_id", "orderid")
    assert target_dup.source_to_target() == {"Status": "status"}

    both_dup = match_names(["OrderID", "Order_ID"], ["order_id", "orderid"])
    assert len(both_dup.collisions) == 1
    assert both_dup.collisions[0].source_names == ("OrderID", "Order_ID")
    assert both_dup.collisions[0].target_names == ("order_id", "orderid")
    assert both_dup.matched == []


def test_explicit_override_disambiguates_canonical_collision() -> None:
    """An explicit override consumes one colliding identifier so the remaining pair matches cleanly."""
    source = ["OrderID", "Order_ID"]
    target = ["order_id", "legacy_order_ref"]
    overrides = {"Order_ID": "legacy_order_ref"}

    result = match_names(source, target, overrides=overrides)

    assert result.collisions == []
    assert result.source_to_target() == {
        "Order_ID": "legacy_order_ref",
        "OrderID": "order_id",
    }


def test_invalid_override_source_and_dangling_target_do_not_silently_fallback() -> None:
    """Dangling target overrides must fail AND never silently fall back to canonical matching."""
    missing_src = match_names(["Orders"], ["orders"], overrides={"GhostTable": "orders"})
    assert len(missing_src.invalid_overrides) == 1
    assert "GhostTable" in missing_src.invalid_overrides[0].reason

    # Even though 'orders' canonically matches 'Orders', the broken explicit override must NOT fallback.
    dangling_tgt = match_names(["Orders"], ["orders"], overrides={"Orders": "nonexistent_orders"})
    assert len(dangling_tgt.invalid_overrides) == 1
    assert "nonexistent_orders" in dangling_tgt.invalid_overrides[0].reason
    assert dangling_tgt.matched == []
    assert dangling_tgt.unmatched_source == ["Orders"]
    assert dangling_tgt.unmatched_target == ["orders"]

    # Duplicate target overrides must flag the second override and hold its source unmatched.
    dup_tgt = match_names(
        ["TableA", "TableB"],
        ["shared_table", "table_b"],
        overrides={"TableA": "shared_table", "TableB": "shared_table"},
    )
    assert len(dup_tgt.invalid_overrides) == 1
    assert dup_tgt.source_to_target() == {"TableA": "shared_table"}
    assert "TableB" in dup_tgt.unmatched_source


def test_empty_canonical_names_never_auto_match_but_support_explicit_override() -> None:
    """Symbol-only identifiers producing empty canonical tokens must never match each other."""
    unmatched = match_names(["___", "OrderID"], ["---", "order_id"])
    assert unmatched.source_to_target() == {"OrderID": "order_id"}
    assert unmatched.unmatched_source == ["___"]
    assert unmatched.unmatched_target == ["---"]

    overridden = match_names(["___", "OrderID"], ["legacy_sep", "order_id"], overrides={"___": "legacy_sep"})
    assert overridden.source_to_target() == {"___": "legacy_sep", "OrderID": "order_id"}
    assert overridden.unmatched_source == []
    assert overridden.unmatched_target == []


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("NVARCHAR(255)", "nvarchar"),
        ("decimal(18, 2)", "decimal"),
        ("timestamp(6) with time zone", "timestamp with time zone"),
        ("CHARACTER VARYING(100)", "character varying"),
    ],
)
def test_normalize_base_type(raw_type: str, expected: str) -> None:
    assert normalize_base_type(raw_type) == expected


def test_type_compatibility_pass_warn_and_fail() -> None:
    # PASS: nvarchar(255) -> character varying(255)
    res_pass = check_type_compatibility(
        ColumnSchema("EmailAddress", "nvarchar", False, 1, max_length=255),
        ColumnSchema("email_address", "character varying", False, 1, max_length=255),
        TYPE_MAP,
    )
    assert res_pass.status == CheckStatus.PASS

    # PASS: nvarchar(max) (-1) -> text
    res_max = check_type_compatibility(
        ColumnSchema("EventPayload", "nvarchar", True, 1, max_length=-1),
        ColumnSchema("event_payload", "text", True, 1),
        TYPE_MAP,
    )
    assert res_max.status == CheckStatus.PASS

    # FAIL: nvarchar(max) (-1) -> bounded varchar(500)
    res_trunc = check_type_compatibility(
        ColumnSchema("EventPayload", "nvarchar", True, 1, max_length=-1),
        ColumnSchema("event_payload", "varchar", True, 1, max_length=500),
        TYPE_MAP,
    )
    assert res_trunc.status == CheckStatus.FAIL

    # FAIL: nvarchar(max) (-1) -> bounded char(10) or bare char (warn base type, still truncates!)
    res_max_char = check_type_compatibility(
        ColumnSchema("EventPayload", "nvarchar", True, 1, max_length=-1),
        ColumnSchema("event_payload", "char", True, 1, max_length=10),
        TYPE_MAP,
    )
    assert res_max_char.status == CheckStatus.FAIL

    res_max_bare_char = check_type_compatibility(
        ColumnSchema("EventPayload", "nvarchar(max)", True, 1),
        ColumnSchema("event_payload", "bpchar", True, 1),
        TYPE_MAP,
    )
    assert res_max_bare_char.status == CheckStatus.FAIL

    # FAIL: NVARCHAR(200) -> VARCHAR(100) length shrinkage (explicit & inline)
    res_len_shrink = check_type_compatibility(
        ColumnSchema("ProductName", "nvarchar", False, 1, max_length=200),
        ColumnSchema("product_name", "varchar", False, 1, max_length=100),
        TYPE_MAP,
    )
    assert res_len_shrink.status == CheckStatus.FAIL

    res_inline_len_shrink = check_type_compatibility(
        ColumnSchema("ProductName", "NVARCHAR(200)", False, 1),
        ColumnSchema("product_name", "VARCHAR(100)", False, 1),
        TYPE_MAP,
    )
    assert res_inline_len_shrink.status == CheckStatus.FAIL

    # FAIL: DECIMAL(18,4) -> NUMERIC(18,2) scale truncation
    res_scale = check_type_compatibility(
        ColumnSchema("TaxRate", "decimal", False, 1, numeric_precision=18, numeric_scale=4),
        ColumnSchema("tax_rate", "numeric", False, 1, numeric_precision=18, numeric_scale=2),
        TYPE_MAP,
    )
    assert res_scale.status == CheckStatus.FAIL
    assert "Scale truncation" in res_scale.message

    # FAIL: DECIMAL(18,2) -> NUMERIC(12,2) precision narrowing
    res_prec = check_type_compatibility(
        ColumnSchema("TotalAmount", "decimal", False, 1, numeric_precision=18, numeric_scale=2),
        ColumnSchema("total_amount", "numeric", False, 1, numeric_precision=12, numeric_scale=2),
        TYPE_MAP,
    )
    assert res_prec.status == CheckStatus.FAIL

    # FAIL: DECIMAL(18,2) -> NUMERIC(18,4) integer-digit overflow (16 -> 14 integer digits)
    res_int_overflow = check_type_compatibility(
        ColumnSchema("LedgerTotal", "decimal", False, 1, numeric_precision=18, numeric_scale=2),
        ColumnSchema("ledger_total", "numeric", False, 1, numeric_precision=18, numeric_scale=4),
        TYPE_MAP,
    )
    assert res_int_overflow.status == CheckStatus.FAIL
    assert "Integer-digit overflow" in res_int_overflow.message

    # PASS: DECIMAL(18,4) -> unconstrained PostgreSQL NUMERIC
    res_unbounded_num = check_type_compatibility(
        ColumnSchema("TotalAmount", "decimal", False, 1, numeric_precision=18, numeric_scale=4),
        ColumnSchema("total_amount", "numeric", False, 1),
        TYPE_MAP,
    )
    assert res_unbounded_num.status == CheckStatus.PASS

    # FAIL: MONEY (19,4) -> NUMERIC(10,2) truncation; PASS -> NUMERIC(19,4)
    assert (
        check_type_compatibility(
            ColumnSchema("Balance", "money", False, 1),
            ColumnSchema("balance", "numeric", False, 1, numeric_precision=10, numeric_scale=2),
            TYPE_MAP,
        ).status
        == CheckStatus.FAIL
    )
    assert (
        check_type_compatibility(
            ColumnSchema("Balance", "money", False, 1),
            ColumnSchema("balance", "numeric", False, 1, numeric_precision=19, numeric_scale=4),
            TYPE_MAP,
        ).status
        == CheckStatus.PASS
    )

    # WARN: uniqueidentifier -> varchar (suboptimal compared to native uuid)
    res_warn = check_type_compatibility(
        ColumnSchema("ExternalUUID", "uniqueidentifier", False, 1),
        ColumnSchema("external_uuid", "varchar", False, 1, max_length=36),
        TYPE_MAP,
    )
    assert res_warn.status == CheckStatus.WARN

    # FAIL: int -> boolean (incompatible)
    res_incompat = check_type_compatibility(
        ColumnSchema("StockQty", "int", False, 1),
        ColumnSchema("stock_qty", "boolean", False, 1),
        TYPE_MAP,
    )
    assert res_incompat.status == CheckStatus.FAIL
