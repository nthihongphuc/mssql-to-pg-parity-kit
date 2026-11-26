"""Canonical-token identifier matching, collision detection, overrides, and type compatibility."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from src.snapshot.models import CheckStatus, ColumnSchema, TypeMapRule

_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")
_PAREN_SPEC_RE = re.compile(r"\s*\([^)]*\)")
_PAREN_CAPTURE_RE = re.compile(r"\(\s*([^)]+?)\s*\)")
_MULTI_SPACE_RE = re.compile(r"\s+")

_CHAR_SOURCE_TYPES = frozenset({"varchar", "nvarchar", "char", "nchar", "sysname", "text", "ntext"})
_CHAR_TARGET_TYPES = frozenset({"character varying", "varchar", "character", "char", "bpchar", "text"})
_FIXED_CHAR_TARGET_TYPES = frozenset({"character", "char", "bpchar"})
_NUMERIC_SOURCE_TYPES = frozenset({"decimal", "numeric", "money", "smallmoney"})
_NUMERIC_TARGET_TYPES = frozenset({"numeric", "decimal"})


def canonicalize(name: str) -> str:
    """Normalize an identifier by stripping all non-alphanumeric chars and lowercasing.

    Examples:
        SKUCode -> skucode
        sku_code -> skucode
        VATAmount -> vatamount
        vat_amount -> vatamount
        OrderDateUTC -> orderdateutc
        order_date_utc -> orderdateutc
    """
    return _NON_ALNUM_RE.sub("", name).lower()


@dataclass(frozen=True)
class MatchedPair:
    """Resolved source-to-target object name pairing."""

    source: str
    target: str
    method: str  # "override" | "canonical"
    canonical_token: str


@dataclass(frozen=True)
class CanonicalCollision:
    """Ambiguity detected when multiple identifiers collapse to one canonical token."""

    canonical_token: str
    source_names: tuple[str, ...]
    target_names: tuple[str, ...]


@dataclass(frozen=True)
class InvalidOverride:
    """Configured override whose source or target name does not exist in the snapshots."""

    source: str
    target: str
    reason: str


@dataclass
class NameMatchSet:
    """Complete deterministic result of matching source and target identifier collections."""

    matched: list[MatchedPair] = field(default_factory=list)
    unmatched_source: list[str] = field(default_factory=list)
    unmatched_target: list[str] = field(default_factory=list)
    collisions: list[CanonicalCollision] = field(default_factory=list)
    invalid_overrides: list[InvalidOverride] = field(default_factory=list)

    def source_to_target(self) -> dict[str, str]:
        """Return a mapping of matched source identifier -> target identifier."""
        return {pair.source: pair.target for pair in self.matched}

    def target_to_source(self) -> dict[str, str]:
        """Return a mapping of matched target identifier -> source identifier."""
        return {pair.target: pair.source for pair in self.matched}


def match_names(
    source_names: Iterable[str],
    target_names: Iterable[str],
    overrides: Optional[dict[str, str]] = None,
) -> NameMatchSet:
    """Match source and target identifiers using overrides first, then canonical-token matching.

    Safety invariants:
        - Explicit overrides targeting a non-existent target object are recorded as
          ``InvalidOverride`` AND the source object is held as unmatched (never silently
          re-matched via canonical fallback).
        - Identifiers whose canonical token is empty (e.g., symbol-only names like ``"___"``
          or ``"---"``) never participate in canonical auto-matching and must be mapped via
          explicit overrides.
        - If two or more unconsumed identifiers on either side share the same non-empty
          canonical token (e.g., ``OrderID`` and ``Order_ID`` on source, or ``order_id`` and
          ``orderid`` on target), the token is flagged as an ambiguous ``CanonicalCollision``
          rather than guessing an arbitrary mapping.
    """
    src_list = list(source_names)
    tgt_list = list(target_names)
    src_set = set(src_list)
    tgt_set = set(tgt_list)

    result = NameMatchSet()
    consumed_src: set[str] = set()
    consumed_tgt: set[str] = set()

    # Step 1: Apply explicit overrides before canonical grouping so overrides can resolve collisions.
    for src_name, tgt_name in (overrides or {}).items():
        if src_name not in src_set:
            result.invalid_overrides.append(
                InvalidOverride(
                    source=src_name,
                    target=tgt_name,
                    reason=f"Override source '{src_name}' not found in source snapshot.",
                )
            )
            continue
        if tgt_name not in tgt_set:
            result.invalid_overrides.append(
                InvalidOverride(
                    source=src_name,
                    target=tgt_name,
                    reason=f"Override target '{tgt_name}' not found in target snapshot.",
                )
            )
            if src_name not in consumed_src:
                consumed_src.add(src_name)
                result.unmatched_source.append(src_name)
            continue
        if src_name in consumed_src or tgt_name in consumed_tgt:
            result.invalid_overrides.append(
                InvalidOverride(
                    source=src_name,
                    target=tgt_name,
                    reason=f"Duplicate override targeting already-mapped object '{src_name}' -> '{tgt_name}'.",
                )
            )
            if src_name not in consumed_src:
                consumed_src.add(src_name)
                result.unmatched_source.append(src_name)
            continue

        result.matched.append(
            MatchedPair(
                source=src_name,
                target=tgt_name,
                method="override",
                canonical_token=canonicalize(src_name),
            )
        )
        consumed_src.add(src_name)
        consumed_tgt.add(tgt_name)

    # Step 2: Group remaining identifiers by canonical token.
    src_groups: dict[str, list[str]] = defaultdict(list)
    tgt_groups: dict[str, list[str]] = defaultdict(list)

    for s in src_list:
        if s not in consumed_src:
            src_groups[canonicalize(s)].append(s)

    for t in tgt_list:
        if t not in consumed_tgt:
            tgt_groups[canonicalize(t)].append(t)

    all_tokens = sorted(set(src_groups.keys()) | set(tgt_groups.keys()))

    # Step 3: Reject empty canonical tokens, detect collisions on either side, and resolve 1-to-1 pairs.
    for token in all_tokens:
        s_names = src_groups.get(token, [])
        t_names = tgt_groups.get(token, [])

        if token == "":
            # Never auto-match identifiers with empty canonical tokens (e.g., "___" vs "---").
            result.unmatched_source.extend(sorted(s_names))
            result.unmatched_target.extend(sorted(t_names))
            continue

        if len(s_names) > 1 or len(t_names) > 1:
            result.collisions.append(
                CanonicalCollision(
                    canonical_token=token,
                    source_names=tuple(sorted(s_names)),
                    target_names=tuple(sorted(t_names)),
                )
            )
            continue

        if len(s_names) == 1 and len(t_names) == 1:
            result.matched.append(
                MatchedPair(
                    source=s_names[0],
                    target=t_names[0],
                    method="canonical",
                    canonical_token=token,
                )
            )
        elif len(s_names) == 1:
            result.unmatched_source.append(s_names[0])
        elif len(t_names) == 1:
            result.unmatched_target.append(t_names[0])

    result.unmatched_source.sort()
    result.unmatched_target.sort()
    return result


def normalize_base_type(data_type: str) -> str:
    """Extract normalized lowercase base SQL type without length/precision qualifiers.

    Examples:
        'nvarchar(100)' -> 'nvarchar'
        'DECIMAL(18, 2)' -> 'decimal'
        'timestamp(6) with time zone' -> 'timestamp with time zone'
    """
    stripped = _PAREN_SPEC_RE.sub("", data_type)
    return _MULTI_SPACE_RE.sub(" ", stripped).strip().lower()


def _extract_inline_type_specs(
    data_type: str,
    base_type: str,
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Parse optional inline (length) or (precision, scale) specifiers from a raw SQL type string."""
    match = _PAREN_CAPTURE_RE.search(data_type)
    if not match:
        return None, None, None

    spec = match.group(1).strip().lower()
    if base_type in _CHAR_SOURCE_TYPES | _CHAR_TARGET_TYPES:
        if spec == "max":
            return -1, None, None
        if spec.isdigit():
            return int(spec), None, None
        return None, None, None

    if base_type in _NUMERIC_SOURCE_TYPES | _NUMERIC_TARGET_TYPES:
        parts = [p.strip() for p in spec.split(",")]
        if len(parts) == 1 and parts[0].isdigit():
            return None, int(parts[0]), 0
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return None, int(parts[0]), int(parts[1])

    return None, None, None


@dataclass(frozen=True)
class TypeCompatibilityResult:
    """Result of evaluating SQL Server -> PostgreSQL column data type compatibility."""

    status: CheckStatus
    source_base_type: str
    target_base_type: str
    message: str
    remediation: Optional[str] = None


def check_type_compatibility(
    source_col: ColumnSchema,
    target_col: ColumnSchema,
    type_map: dict[str, TypeMapRule],
) -> TypeCompatibilityResult:
    """Validate SQL Server column type, length, and precision/scale against PostgreSQL target."""
    src_base = normalize_base_type(source_col.data_type)
    tgt_base = normalize_base_type(target_col.data_type)

    rule = type_map.get(src_base)
    if rule is None:
        return TypeCompatibilityResult(
            status=CheckStatus.WARN,
            source_base_type=src_base,
            target_base_type=tgt_base,
            message=f"Unmapped SQL Server source type '{src_base}' (target '{tgt_base}').",
            remediation=f"Add an explicit mapping rule for '{src_base}' in config/type_map.yaml.",
        )

    is_compatible_base = tgt_base in rule.compatible
    is_warn_base = tgt_base in rule.warn

    if is_compatible_base or is_warn_base:
        # Resolve effective length / precision / scale (explicit metadata overrides inline type strings).
        in_src_len, in_src_prec, in_src_scale = _extract_inline_type_specs(source_col.data_type, src_base)
        in_tgt_len, in_tgt_prec, in_tgt_scale = _extract_inline_type_specs(target_col.data_type, tgt_base)

        src_max_len = source_col.max_length if source_col.max_length is not None else in_src_len
        tgt_max_len = target_col.max_length if target_col.max_length is not None else in_tgt_len
        src_prec = source_col.numeric_precision if source_col.numeric_precision is not None else in_src_prec
        src_scale = source_col.numeric_scale if source_col.numeric_scale is not None else in_src_scale
        tgt_prec = target_col.numeric_precision if target_col.numeric_precision is not None else in_tgt_prec
        tgt_scale = target_col.numeric_scale if target_col.numeric_scale is not None else in_tgt_scale

        # Implicit SQL Server metadata bounds when not explicitly populated.
        if src_base == "sysname" and src_max_len is None:
            src_max_len = 128
        elif src_base == "money":
            if src_prec is None:
                src_prec = 19
            if src_scale is None:
                src_scale = 4
        elif src_base == "smallmoney":
            if src_prec is None:
                src_prec = 10
            if src_scale is None:
                src_scale = 4

        # 1. Character length & MAX truncation checks (applies to both compatible AND warn char types).
        if src_base in _CHAR_SOURCE_TYPES and tgt_base in _CHAR_TARGET_TYPES:
            src_is_unbounded = (src_base in {"text", "ntext"}) or (src_max_len == -1)
            # In PostgreSQL, bare char/character/bpchar without a length defaults to length 1.
            tgt_effective_len = (
                1 if (tgt_base in _FIXED_CHAR_TARGET_TYPES and tgt_max_len is None) else tgt_max_len
            )

            if src_is_unbounded and tgt_base != "text" and tgt_effective_len is not None:
                src_label = f"{src_base}(max)" if src_max_len == -1 else src_base
                return TypeCompatibilityResult(
                    status=CheckStatus.FAIL,
                    source_base_type=src_base,
                    target_base_type=tgt_base,
                    message=(
                        f"Length truncation risk: SQL Server {src_label} "
                        f"mapped to bounded PostgreSQL {tgt_base}({tgt_effective_len})."
                    ),
                    remediation="Change PostgreSQL column type to unbounded text.",
                )

            if (
                not src_is_unbounded
                and src_max_len is not None
                and src_max_len > 0
                and tgt_effective_len is not None
                and tgt_effective_len > 0
                and tgt_effective_len < src_max_len
            ):
                return TypeCompatibilityResult(
                    status=CheckStatus.FAIL,
                    source_base_type=src_base,
                    target_base_type=tgt_base,
                    message=(
                        f"Length narrowing risk: source max_length={src_max_len} "
                        f"> target max_length={tgt_effective_len}."
                    ),
                    remediation=f"Widen target column to at least {src_max_len} characters or text.",
                )

        # 2. Fixed-point numeric precision, scale, and integer-digit capacity checks.
        if src_base in _NUMERIC_SOURCE_TYPES and tgt_base in _NUMERIC_TARGET_TYPES:
            if src_prec is not None and tgt_prec is not None:
                eff_src_scale = src_scale if src_scale is not None else 0
                eff_tgt_scale = tgt_scale if tgt_scale is not None else 0

                if tgt_prec < src_prec:
                    return TypeCompatibilityResult(
                        status=CheckStatus.FAIL,
                        source_base_type=src_base,
                        target_base_type=tgt_base,
                        message=(
                            f"Precision narrowing risk: source numeric({src_prec},"
                            f"{eff_src_scale}) > target numeric({tgt_prec},"
                            f"{eff_tgt_scale})."
                        ),
                        remediation=f"Alter target column to numeric({src_prec},{eff_src_scale}).",
                    )

                if eff_tgt_scale < eff_src_scale:
                    return TypeCompatibilityResult(
                        status=CheckStatus.FAIL,
                        source_base_type=src_base,
                        target_base_type=tgt_base,
                        message=(
                            f"Scale truncation risk: source numeric({src_prec},{eff_src_scale}) "
                            f"scale={eff_src_scale} > target scale={eff_tgt_scale}."
                        ),
                        remediation=f"Alter target column to at least numeric({src_prec},{eff_src_scale}).",
                    )

                src_int_digits = src_prec - eff_src_scale
                tgt_int_digits = tgt_prec - eff_tgt_scale
                if tgt_int_digits < src_int_digits:
                    required_prec = src_int_digits + eff_tgt_scale
                    return TypeCompatibilityResult(
                        status=CheckStatus.FAIL,
                        source_base_type=src_base,
                        target_base_type=tgt_base,
                        message=(
                            f"Integer-digit overflow risk: source numeric({src_prec},{eff_src_scale}) "
                            f"allows {src_int_digits} integer digits, but target numeric({tgt_prec},"
                            f"{eff_tgt_scale}) only allows {tgt_int_digits}."
                        ),
                        remediation=f"Alter target column to at least numeric({required_prec},{eff_tgt_scale}).",
                    )

        if is_compatible_base:
            return TypeCompatibilityResult(
                status=CheckStatus.PASS,
                source_base_type=src_base,
                target_base_type=tgt_base,
                message=f"Compatible data type mapping ({source_col.data_type} -> {target_col.data_type}).",
            )

        return TypeCompatibilityResult(
            status=CheckStatus.WARN,
            source_base_type=src_base,
            target_base_type=tgt_base,
            message=(
                f"Suboptimal/lossy type mapping: '{source_col.data_type}' -> '{target_col.data_type}'. "
                f"{rule.note}"
            ).strip(),
            remediation=f"Prefer one of {list(rule.compatible)} for SQL Server '{src_base}'.",
        )

    return TypeCompatibilityResult(
        status=CheckStatus.FAIL,
        source_base_type=src_base,
        target_base_type=tgt_base,
        message=(
            f"Incompatible type mapping: '{source_col.data_type}' -> '{target_col.data_type}'. "
            f"{rule.note}"
        ).strip(),
        remediation=f"Change target type to one of {list(rule.compatible)}.",
    )
