"""Pure, fail-closed point-in-time issuer market-cap classification."""

from __future__ import annotations

import math
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, Overflow, Underflow, localcontext
from numbers import Real
from typing import cast

from microcap_history import CoverageError, parse_utc


_CAP_CEILING = Decimal("300000000")
_MAX_PRICE_AGE = timedelta(minutes=5)
_SAFE_PRICE_SOURCE_IDENTIFIERS = frozenset({"ibkr-bars"})
_SAFE_SHARES_SOURCE_IDENTIFIERS = frozenset({"audited-filing", "sec-companyfacts"})


def _unverified(reason: str) -> dict[str, object]:
    return {"status": "MARKET_CAP_UNVERIFIED", "reason": reason}


def _timestamp(record: Mapping[str, object], field: str) -> datetime | None:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_utc(value)
    except (CoverageError, OverflowError, ValueError):
        return None


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, Real)):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _reported_decimal(value: Decimal) -> int | float | str:
    max_digits = min(sys.get_int_max_str_digits() or 4300, 4300)
    if value == value.to_integral_value():
        if value.adjusted() >= max_digits:
            return str(value)
        return int(value)
    try:
        float_value = float(value)
    except OverflowError:
        return str(value)
    if math.isfinite(float_value) and Decimal(str(float_value)) == value:
        return float_value
    return str(value)


def _source_identifier(
    record: Mapping[str, object],
    allowed_identifiers: frozenset[str],
) -> str | None:
    value = record.get("source")
    if isinstance(value, str) and value in allowed_identifiers:
        return value
    return None


def _class_id(record: Mapping[str, object]) -> str | None:
    value = record.get("class_id")
    return value if isinstance(value, str) and value and value == value.strip() else None


def _issuer_matches(record: Mapping[str, object], issuer_id: str) -> bool:
    value = record.get("issuer_id")
    return isinstance(value, str) and value.strip() == issuer_id


def _disallowed_massive_shares(record: Mapping[str, object]) -> bool:
    source = record.get("source")
    if not isinstance(source, str):
        return False
    normalized = source.casefold().replace("-", "_").replace(" ", "_")
    return "massive" in normalized and (
        "ticker" in normalized
        or "detail" in normalized
        or ("filing" not in normalized and "edgar" not in normalized)
    )


def classify_microcap(
    *,
    issuer_id: str,
    decision_at: str,
    price: Mapping[str, object] | None,
    shares: Sequence[Mapping[str, object]] | None,
    classes_complete: bool,
    corporate_actions_verified: bool,
    source_verified: bool = False,
) -> dict[str, object]:
    """Classify an issuer only from complete, verified, then-known raw evidence.

    ``source_verified`` is an external audited-source witness. It is deliberately
    independent of source claims carried inside the price or shares mappings.
    """
    if not isinstance(issuer_id, str) or not issuer_id.strip():
        return _unverified("issuer identity is missing")
    issuer_id = issuer_id.strip()

    if not isinstance(decision_at, str):
        return _unverified("decision timestamp is missing or invalid")
    try:
        decision = parse_utc(decision_at)
    except (CoverageError, OverflowError, ValueError):
        return _unverified("decision timestamp is missing or invalid")

    if classes_complete is not True:
        return _unverified("common share-class coverage is incomplete")
    if corporate_actions_verified is not True:
        return _unverified("corporate actions are not verified")
    if source_verified is not True:
        return _unverified("share source has no external audited verification")
    if not isinstance(price, Mapping):
        return _unverified("raw price evidence is missing")
    if (
        not isinstance(shares, Sequence)
        or isinstance(shares, (str, bytes, bytearray))
        or len(shares) != 1
    ):
        return _unverified("exactly one complete common share class is required")
    share_record = shares[0]
    if not isinstance(share_record, Mapping):
        return _unverified("share-class evidence is invalid")

    if not _issuer_matches(price, issuer_id) or not _issuer_matches(share_record, issuer_id):
        return _unverified("price and share evidence do not verify the requested issuer")
    price_class = _class_id(price)
    shares_class = _class_id(share_record)
    if price_class is None or shares_class is None or price_class != shares_class:
        return _unverified("price and share evidence do not match one share class")
    for record in (price, share_record):
        class_type = record.get("class_type")
        if class_type not in ("common", "common_stock"):
            return _unverified("share evidence is not for a common share class")

    if price.get("basis") != "raw" or share_record.get("basis") != "raw":
        return _unverified("price and share counts must use the matching raw basis")
    if _disallowed_massive_shares(share_record):
        return _unverified("Massive ticker-details shares are not valid as-of evidence")

    known_at = _timestamp(price, "known_at")
    filed_at = _timestamp(share_record, "filed_at")
    available_at = _timestamp(share_record, "available_at")
    if known_at is None:
        return _unverified("raw price known_at timestamp is missing or invalid")
    if decision < known_at or decision - known_at > _MAX_PRICE_AGE:
        return _unverified("raw price is future-dated or more than five minutes stale")
    if filed_at is None or available_at is None:
        return _unverified("share filing and availability timestamps are required")
    if not filed_at <= available_at <= decision:
        return _unverified("share filing availability is not known by the decision time")

    raw_price = _positive_decimal(price.get("raw_close"))
    share_count = _positive_decimal(share_record.get("count"))
    if raw_price is None:
        return _unverified("raw price must be finite and strictly positive")
    if share_count is None:
        return _unverified("outstanding share count must be finite and strictly positive")

    try:
        with localcontext() as context:
            context.prec = max(
                context.prec,
                len(raw_price.as_tuple().digits) + len(share_count.as_tuple().digits),
            )
            context.traps[Underflow] = True
            raw_market_cap = raw_price * share_count
    except (InvalidOperation, Overflow, Underflow):
        return _unverified("raw market cap exceeds supported numeric limits")

    max_digits = min(sys.get_int_max_str_digits() or 4300, 4300)
    if raw_market_cap.adjusted() >= max_digits:
        return _unverified("raw market cap exceeds supported numeric limits")

    reported_cap: int | float | str
    if raw_market_cap == raw_market_cap.to_integral_value():
        reported_cap = int(raw_market_cap)
    else:
        float_cap = float(raw_market_cap)
        if not math.isfinite(float_cap):
            return _unverified("raw market cap exceeds supported numeric limits")
        reported_cap = (
            float_cap if Decimal(str(float_cap)) == raw_market_cap else str(raw_market_cap)
        )

    result = {
        "status": "MICROCAP" if raw_market_cap <= _CAP_CEILING else "NOT_MICROCAP",
        "issuer_id": issuer_id,
        "class_id": price_class,
        "raw_market_cap": reported_cap,
        "raw_close": _reported_decimal(raw_price),
        "shares_count": _reported_decimal(share_count),
        "cap_ceiling": int(_CAP_CEILING),
        "decision_at": _timestamp_text(decision),
        "price_known_at": _timestamp_text(known_at),
        "shares_filed_at": _timestamp_text(filed_at),
        "shares_available_at": _timestamp_text(available_at),
        "price_basis": cast(str, price["basis"]),
        "shares_basis": cast(str, share_record["basis"]),
    }
    price_source = _source_identifier(price, _SAFE_PRICE_SOURCE_IDENTIFIERS)
    shares_source = _source_identifier(share_record, _SAFE_SHARES_SOURCE_IDENTIFIERS)
    if price_source is not None:
        result["price_source"] = price_source
    if shares_source is not None:
        result["shares_source"] = shares_source
    return result
