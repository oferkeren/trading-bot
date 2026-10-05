from collections.abc import Mapping
from datetime import datetime, timezone
from typing import cast
from urllib.parse import urlsplit

from microcap_history import CoverageError, parse_utc


_ROSTER_CLAIM = "historical-listed-and-delisted"
_PILOT_CLAIM = "pilot-unverified"
_MEMBER_FIELDS = (
    "issuer_id",
    "symbol",
    "company",
    "valid_from",
    "valid_to",
    "listing_status",
    "source_record_id",
)


def _roster_unverified(detail: str) -> CoverageError:
    return CoverageError(f"ROSTER_UNVERIFIED: {detail}")


def _roster_invalid(detail: str) -> CoverageError:
    return CoverageError(f"ROSTER_INVALID: {detail}")


def _parse_roster_timestamp(value: str) -> datetime:
    try:
        return parse_utc(value)
    except OverflowError:
        raise CoverageError("TIMESTAMP_INVALID: timestamp is outside the supported range") from None


def eligible_members(roster: Mapping[str, object], at: str, *, allow_pilot: bool = False) -> list[dict[str, object]]:
    """Return roster records whose half-open listing windows include ``at``."""
    if not isinstance(roster, Mapping):
        raise _roster_unverified("roster must be an object")

    source_url = roster.get("source_url")
    retrieved_at = roster.get("retrieved_at")
    coverage_claim = roster.get("coverage_claim")
    members = roster.get("members")

    if (not isinstance(source_url, str) or not source_url.strip()
            or not isinstance(retrieved_at, str) or not retrieved_at.strip()
            or coverage_claim not in ((_ROSTER_CLAIM, _PILOT_CLAIM)
                                      if allow_pilot else (_ROSTER_CLAIM,))
            or not isinstance(members, list) or not members):
        raise _roster_unverified("source URL, retrieval time, coverage claim, and members are required")

    try:
        parsed_url = urlsplit(source_url.strip())
        hostname = parsed_url.hostname
    except ValueError:
        raise _roster_unverified("source URL is malformed") from None
    if (parsed_url.scheme not in ("http", "https") or not hostname
            or parsed_url.username is not None or parsed_url.password is not None
            or "?" in source_url or "#" in source_url):
        raise _roster_unverified("source URL must be an HTTP(S) URL")
    try:
        _parse_roster_timestamp(retrieved_at)
    except CoverageError as error:
        raise _roster_unverified(f"invalid retrieval timestamp: {error}") from None

    at_utc = _parse_roster_timestamp(at)
    source_records: set[str] = set()
    parsed_members: list[tuple[dict[str, object], datetime, datetime | None]] = []

    for index, raw_member in enumerate(members):
        if not isinstance(raw_member, Mapping):
            raise _roster_invalid(f"member {index} must be an object")
        missing = [field for field in _MEMBER_FIELDS if field not in raw_member]
        if missing:
            raise _roster_invalid(f"member {index} is missing required fields {', '.join(missing)}")

        member_record = cast(dict[str, object], dict(raw_member))
        for field in (
            "issuer_id", "symbol", "company", "valid_from", "listing_status", "source_record_id"
        ):
            value = member_record[field]
            if not isinstance(value, str) or not value.strip():
                raise _roster_invalid(f"member {index} field {field!r} must be a non-empty string")

        issuer_id = cast(str, member_record["issuer_id"])
        symbol = cast(str, member_record["symbol"])
        source_record_id = cast(str, member_record["source_record_id"])
        if symbol != symbol.strip():
            raise _roster_invalid(f"member {index} field 'symbol' must not have surrounding whitespace")
        listing_status = member_record["listing_status"]
        valid_to_value = member_record["valid_to"]
        if listing_status not in ("listed", "delisted"):
            raise _roster_invalid(f"member {index} has an unsupported listing status")
        if (listing_status == "listed") != (valid_to_value is None):
            raise _roster_invalid(f"member {index} listing status conflicts with its validity end")
        if source_record_id in source_records:
            raise _roster_invalid(f"duplicate source_record_id {source_record_id!r}")
        source_records.add(source_record_id)

        try:
            valid_from = _parse_roster_timestamp(cast(str, member_record["valid_from"]))
            valid_to = (
                _parse_roster_timestamp(cast(str, valid_to_value))
                if valid_to_value is not None else None
            )
        except CoverageError as error:
            raise _roster_invalid(f"member {index} has an invalid validity timestamp: {error}") from None
        if valid_to is not None and valid_from >= valid_to:
            raise _roster_invalid(f"member {index} validity window must be non-empty")
        parsed_members.append((member_record, valid_from, valid_to))

    windows_by_symbol: dict[str, list[tuple[datetime, datetime | None]]] = {}
    for member_record, valid_from, valid_to in parsed_members:
        symbol = cast(str, member_record["symbol"])
        windows_by_symbol.setdefault(symbol.casefold(), []).append((valid_from, valid_to))

    for windows in windows_by_symbol.values():
        windows.sort(key=lambda window: window[0])
        furthest_end = windows[0][1]
        for start, end in windows[1:]:
            if furthest_end is None or start < furthest_end:
                raise _roster_invalid("overlapping validity windows for a symbol")
            if end is None or (furthest_end is not None and end > furthest_end):
                furthest_end = end

    at_text = at_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    result: list[dict[str, object]] = []
    for member_record, valid_from, valid_to in parsed_members:
        if valid_from <= at_utc and (valid_to is None or at_utc < valid_to):
            result.append({
                **member_record,
                "at": at_text,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "coverage_claim": coverage_claim,
            })
    if not result:
        raise CoverageError("ROSTER_NO_MEMBERS_AT_DATE: no roster member is valid at the requested time")
    return result


def member_for_issuer(roster: Mapping[str, object], issuer_id: str, at: str, *,
                      allow_pilot: bool = False) -> dict[str, object] | None:
    """Return the single roster record active for ``issuer_id`` at ``at``, if any.

    Multiple concurrently active symbols for one issuer are point-in-time ambiguous and
    are rejected rather than resolved by input order.
    """
    matches = [item for item in eligible_members(roster, at, allow_pilot=allow_pilot)
               if item.get("issuer_id") == issuer_id]
    if len(matches) > 1:
        raise CoverageError("CONTRACT_UNRESOLVED: multiple symbols are active for one issuer")
    return matches[0] if matches else None
