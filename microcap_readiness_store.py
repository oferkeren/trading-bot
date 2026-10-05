"""Bounded, fail-closed reader for the offline micro-cap readiness snapshot."""

from __future__ import annotations

import json
import os
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from microcap_batch_schema import validate_snapshot_v2
from microcap_history import CoverageError
from microcap_readiness import (
    _MAX_JSON_BYTES,
    _ISSUER, _NEWS_STATUSES, _REPORT_REASONS, _ROSTER_STATUSES, _SEC_BLOCKERS,
    _SYMBOL, _external, _is_count, _reject_constant, _unique_object,
    _utc_timestamp,
)


_UNAVAILABLE = {
    "status": "UNAVAILABLE", "decision": "NO_TRADE",
    "order_approval": False, "model_calibrated": False,
    "target_probabilities": "unavailable",
    "blockers": ["READINESS_UNAVAILABLE"],
}
_STALE = {**_UNAVAILABLE, "status": "STALE", "blockers": ["READINESS_STALE"]}


def _keys(value: object, expected: set[str]) -> bool:
    return type(value) is dict and value.keys() == expected


def _count_or_none(value: object) -> bool:
    return value is None or _is_count(value)


def _fallback(status: str) -> dict[str, object]:
    return dict(_STALE if status == "STALE" else _UNAVAILABLE,
                blockers=["READINESS_STALE" if status == "STALE"
                          else "READINESS_UNAVAILABLE"])


def _validate(value: object) -> datetime:
    if not _keys(value, {
        "schema_version", "generated_at", "decision", "order_approval",
        "model_calibrated", "target_probabilities", "sample", "sample_window",
        "sources", "blockers",
    }):
        raise ValueError()
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["decision"] != "NO_TRADE"
            or value["order_approval"] is not False
            or value["model_calibrated"] is not False
            or value["target_probabilities"] != "unavailable"):
        raise ValueError()
    generated = _utc_timestamp(value["generated_at"])
    sample = value["sample"]
    if (not _keys(sample, {"issuer_id", "symbol", "date"})
            or type(sample["issuer_id"]) is not str
            or not _ISSUER.fullmatch(sample["issuer_id"])
            or type(sample["symbol"]) is not str
            or not _SYMBOL.fullmatch(sample["symbol"])
            or type(sample["date"]) is not str):
        raise ValueError()
    try:
        day = date.fromisoformat(sample["date"])
        if day.isoformat() != sample["date"]:
            raise ValueError()
    except ValueError:
        raise ValueError() from None
    window = value["sample_window"]
    if not _keys(window, {"start_utc", "end_utc"}):
        raise ValueError()
    start, end = _utc_timestamp(window["start_utc"]), _utc_timestamp(window["end_utc"])
    if not (start.date() == day and start < end
            and end <= datetime.combine(day + timedelta(days=1),
                                        datetime.min.time(), timezone.utc)):
        raise ValueError()

    sources = value["sources"]
    if not _keys(sources, {"roster", "news", "ibkr", "shares", "sec"}):
        raise ValueError()
    roster, news, ibkr = (sources[key] for key in ("roster", "news", "ibkr"))
    shares, sec = sources["shares"], sources["sec"]
    if (not _keys(roster, {"status", "evidence_status", "active_count", "inactive_count"})
            or roster["status"] != "UNVERIFIED"
            or type(roster["evidence_status"]) is not str
            or roster["evidence_status"] not in _ROSTER_STATUSES
            or not all(_count_or_none(roster[key])
                       for key in ("active_count", "inactive_count"))
            or (roster["evidence_status"] in ("MISSING", "SAVED_EVIDENCE_UNVERIFIED")
                and (roster["active_count"] is not None
                     or roster["inactive_count"] is not None))
            or (roster["evidence_status"] in ("DATED_ROSTER_OBSERVED",
                                             "PAGINATION_UNVERIFIED")
                and (roster["active_count"] is None
                     or roster["inactive_count"] is None))):
        raise ValueError()
    if (not _keys(news, {"status", "article_count"})
            or type(news["status"]) is not str
            or news["status"] not in _NEWS_STATUSES
            or not _count_or_none(news["article_count"])
            or (news["status"] in ("MISSING", "INVALID")
                and news["article_count"] is not None)
            or (news["status"] == "ARTICLES_OBSERVED"
                and (news["article_count"] is None or news["article_count"] == 0))
            or (news["status"] == "EMPTY" and news["article_count"] != 0)):
        raise ValueError()
    if (not _keys(ibkr, {"minute_cells", "quote_cells"})
            or not _is_count(ibkr["minute_cells"])
            or not _is_count(ibkr["quote_cells"])
            or ibkr["minute_cells"] > 1 or ibkr["quote_cells"] > 1
            or not _keys(shares, {"status", "sec_observation_count"})
            or shares["status"] != "MARKET_CAP_UNVERIFIED"
            or not _is_count(shares["sec_observation_count"])
            or not _keys(sec, {"status", "observation_count", "verification"})
            or sec["status"] not in ("MISSING", "UNAVAILABLE", "OBSERVED")
            or sec["verification"] != "UNVERIFIED"
            or not _is_count(sec["observation_count"])
            or sec["observation_count"] != shares["sec_observation_count"]
            or (sec["status"] == "OBSERVED") != (sec["observation_count"] > 0)):
        raise ValueError()
    blockers = value["blockers"]
    if (type(blockers) is not list
            or any(type(blocker) is not str
                   or blocker not in (_REPORT_REASONS | _SEC_BLOCKERS)
                   for blocker in blockers)
            or blockers != sorted(set(blockers))
            or not {"MARKET_CAP_UNVERIFIED", "ROSTER_COVERAGE_UNVERIFIED"}
            <= set(blockers)):
        raise ValueError()
    return generated


def _validate_any(value: object) -> datetime:
    if type(value) is dict and type(value.get("schema_version")) is int \
            and value["schema_version"] == 2:
        return validate_snapshot_v2(value)
    return _validate(value)


def _load_validated(resolved: Path) -> dict[str, object]:
    expected = os.stat(resolved, follow_symlinks=False)
    if not stat.S_ISREG(expected.st_mode):
        raise OSError()
    fd: int | None = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(resolved, flags)
        actual = os.fstat(fd)
        if (not stat.S_ISREG(actual.st_mode)
                or actual.st_dev != expected.st_dev
                or actual.st_ino != expected.st_ino):
            raise OSError()
        chunks = []
        remaining = _MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_JSON_BYTES:
            raise ValueError()
        value = json.loads(payload, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise CoverageError("INPUT_INVALID") from None
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(value, dict):
        raise CoverageError("INPUT_INVALID")
    return value


def read_readiness(path: str | None, *, now: datetime) -> dict[str, object]:
    """Read only a configured absolute external file; never expose invalid/stale claims."""
    if not path or not isinstance(path, str):
        return _fallback("UNAVAILABLE")
    try:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError()
        instant = now.astimezone(timezone.utc)
        generated_file = _load_validated(_external(Path(path), existing=True))
        generated_at = _validate_any(generated_file)
        age = instant - generated_at
        if age < timedelta(0):
            return _fallback("UNAVAILABLE")
        if age > timedelta(days=7):
            return _fallback("STALE")
    except (CoverageError, OSError, ValueError, TypeError, OverflowError):
        return _fallback("UNAVAILABLE")
    return {**generated_file, "status": "CURRENT", "age_seconds": int(age.total_seconds())}
