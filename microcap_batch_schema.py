"""Strict schemas for micro-cap batch manifests, batch reports and schema 2 snapshots."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from microcap_history import CoverageError
from microcap_readiness import (
    _CREDENTIAL_ENV, _REPORT_REASONS, _ROSTER_STATUSES, _SEC_BLOCKERS, _is_count,
    _utc_timestamp,
)


BIAS = ("RUNNER_SCREEN_SELECTED", "SELECTION_USES_SAME_DAY_OUTCOME", "UNVERIFIED_PILOT")
SYMBOL_PATTERN = "[A-Z]{1,6}"
DEFAULT_RULE = {
    "days": 10, "per_day": 2, "min_open": 1.0, "max_open": 20.0,
    "min_volume": 1_000_000, "min_move": 0.30, "max_candidates_per_day": 10,
    "symbol_pattern": SYMBOL_PATTERN,
}
OUTCOMES = frozenset({
    "SELECTED", "NOT_COMMON_STOCK", "NO_SEC_CIK", "DUPLICATE_ISSUER", "PROVIDER_ERROR",
})
STAGES = ("ibkr", "source", "sec")
STAGE_STATUSES = ("OK", "PROVIDER_ERROR", "INVALID", "TIMEOUT", "SKIPPED")
STAGE_ERRORS = frozenset(
    f"{stage}:{status}" for stage in STAGES for status in STAGE_STATUSES if status != "OK"
)
COVERAGE_KEYS = ("ibkr_minute", "ibkr_quotes", "roster_dated", "news_found", "sec_shares")
REQUIRED_BLOCKERS = frozenset({
    "MARKET_CAP_UNVERIFIED", "ROSTER_COVERAGE_UNVERIFIED", "RESEARCH_ONLY_NOT_CALIBRATED",
})
MAX_SAMPLES = 20
MAX_CALENDAR_DAYS = 21

_NEW_YORK = ZoneInfo("America/New_York")
_CIK = re.compile(r"[0-9]{10}\Z")
_SYMBOL = re.compile(SYMBOL_PATTERN + r"\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_FIXED = {"decision": "NO_TRADE", "order_approval": False, "model_calibrated": False,
          "target_probabilities": "unavailable"}
_MANIFEST_KEYS = {"schema_version", "kind", "created_at", "as_of", "rule", "trading_days",
                  "candidates", "samples", "bias", "sha256"}
_SAMPLE_KEYS = {"issuer_id", "symbol", "company", "date", "move_pct", "start_utc", "end_utc"}
_ROW_KEYS = {"date", "symbol", "issuer_id", "move_pct", "ibkr_minute", "ibkr_quotes",
             "roster", "news_count", "sec_observations", "stage_errors"}
_BODY_KEYS = {"generated_at", "decision", "order_approval", "model_calibrated",
              "target_probabilities", "batch", "coverage", "samples", "blockers"}
_BATCH_KEYS = {"as_of", "manifest_sha256", "sample_count", "rule", "bias"}


def _invalid() -> CoverageError:
    return CoverageError("INPUT_INVALID")


def _require(condition: bool) -> None:
    if not condition:
        raise _invalid()


def _keys(value: object, expected: set[str]) -> bool:
    return type(value) is dict and value.keys() == expected


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _iso_date(value: object) -> date:
    _require(isinstance(value, str))
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _invalid() from None
    _require(parsed.isoformat() == value)
    return parsed


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_window(day: date) -> tuple[str, str]:
    """Regular US session 09:30-16:00 America/New_York as UTC strings."""
    start = datetime.combine(day, time(9, 30), _NEW_YORK)
    end = datetime.combine(day, time(16, 0), _NEW_YORK)
    return _utc_text(start), _utc_text(end)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_hash(manifest: Mapping) -> str:
    body = {key: value for key, value in manifest.items() if key != "sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def validate_rule(rule: object) -> None:
    _require(_keys(rule, set(DEFAULT_RULE)))
    _require(_is_count(rule["days"], minimum=1) and rule["days"] <= MAX_SAMPLES)
    _require(_is_count(rule["per_day"], minimum=1) and rule["per_day"] <= 5)
    _require(_is_count(rule["min_volume"], minimum=0))
    _require(_is_count(rule["max_candidates_per_day"], minimum=1)
             and rule["max_candidates_per_day"] <= 50)
    for key in ("min_open", "max_open", "min_move"):
        _require(_number(rule[key]) and rule[key] >= 0)
    _require(rule["min_open"] <= rule["max_open"])
    _require(rule["symbol_pattern"] == SYMBOL_PATTERN)


def _validate_symbol(value: object) -> None:
    _require(isinstance(value, str) and bool(_SYMBOL.fullmatch(value)))


def _validate_cik(value: object) -> None:
    _require(isinstance(value, str) and bool(_CIK.fullmatch(value)))


def _validate_move(value: object) -> None:
    _require(_number(value) and 0 <= value <= 100_000)


def validate_manifest(manifest: object) -> None:
    _require(_keys(manifest, _MANIFEST_KEYS))
    _require(manifest["schema_version"] == 1 and type(manifest["schema_version"]) is int)
    _require(manifest["kind"] == "microcap_batch_manifest")
    _utc_timestamp(manifest["created_at"])
    _iso_date(manifest["as_of"])
    validate_rule(manifest["rule"])
    _require(manifest["bias"] == list(BIAS))
    _require(isinstance(manifest["sha256"], str) and bool(_SHA256.fullmatch(manifest["sha256"])))
    _require(manifest["sha256"] == manifest_hash(manifest))
    days = manifest["trading_days"]
    _require(isinstance(days, list) and len(days) <= manifest["rule"]["days"])
    for entry in days:
        _require(_keys(entry, {"date", "request_id"}))
        _iso_date(entry["date"])
        _require(isinstance(entry["request_id"], str)
                 and bool(_REQUEST_ID.fullmatch(entry["request_id"])))
    candidates = manifest["candidates"]
    _require(isinstance(candidates, list)
             and len(candidates) <= MAX_CALENDAR_DAYS * manifest["rule"]["max_candidates_per_day"])
    for entry in candidates:
        _require(_keys(entry, {"date", "ticker", "move_pct", "outcome"}))
        _iso_date(entry["date"])
        _validate_symbol(entry["ticker"])
        _validate_move(entry["move_pct"])
        _require(entry["outcome"] in OUTCOMES)
    samples = manifest["samples"]
    _require(isinstance(samples, list)
             and 1 <= len(samples) <= min(MAX_SAMPLES,
                                          manifest["rule"]["days"] * manifest["rule"]["per_day"]))
    issuers = set()
    for entry in samples:
        _require(_keys(entry, _SAMPLE_KEYS))
        _validate_cik(entry["issuer_id"])
        _require(entry["issuer_id"] not in issuers)
        issuers.add(entry["issuer_id"])
        _validate_symbol(entry["symbol"])
        _require(isinstance(entry["company"], str) and 0 < len(entry["company"]) <= 120
                 and entry["company"].isprintable())
        day = _iso_date(entry["date"])
        _validate_move(entry["move_pct"])
        _require((entry["start_utc"], entry["end_utc"]) == session_window(day))


def validate_row(row: object) -> None:
    _require(_keys(row, _ROW_KEYS))
    _iso_date(row["date"])
    _validate_symbol(row["symbol"])
    _validate_cik(row["issuer_id"])
    _validate_move(row["move_pct"])
    _require(type(row["ibkr_minute"]) is bool and type(row["ibkr_quotes"]) is bool)
    _require(isinstance(row["roster"], str) and row["roster"] in _ROSTER_STATUSES)
    _require(row["news_count"] is None or _is_count(row["news_count"]))
    _require(_is_count(row["sec_observations"]) and row["sec_observations"] <= 2)
    errors = row["stage_errors"]
    _require(isinstance(errors, list)
             and all(isinstance(item, str) and item in STAGE_ERRORS for item in errors)
             and errors == sorted(set(errors)))


def coverage_from_rows(rows: list[Mapping]) -> dict[str, dict[str, int]]:
    total = len(rows)
    observed = {
        "ibkr_minute": sum(1 for row in rows if row["ibkr_minute"]),
        "ibkr_quotes": sum(1 for row in rows if row["ibkr_quotes"]),
        "roster_dated": sum(1 for row in rows if row["roster"] == "DATED_ROSTER_OBSERVED"),
        "news_found": sum(1 for row in rows
                          if row["news_count"] is not None and row["news_count"] >= 1),
        "sec_shares": sum(1 for row in rows if row["sec_observations"] >= 1),
    }
    return {key: {"observed": observed[key], "total": total} for key in COVERAGE_KEYS}


def validate_blockers(value: object) -> list[str]:
    allowed = _REPORT_REASONS | _SEC_BLOCKERS
    _require(isinstance(value, list)
             and all(isinstance(item, str) and item in allowed for item in value)
             and value == sorted(set(value))
             and REQUIRED_BLOCKERS <= set(value))
    return value


def _validate_body(value: Mapping) -> datetime:
    for key, expected in _FIXED.items():
        _require(value[key] == expected and type(value[key]) is type(expected))
    generated = _utc_timestamp(value["generated_at"])
    batch = value["batch"]
    _require(_keys(batch, _BATCH_KEYS))
    _iso_date(batch["as_of"])
    _require(isinstance(batch["manifest_sha256"], str)
             and bool(_SHA256.fullmatch(batch["manifest_sha256"])))
    validate_rule(batch["rule"])
    _require(batch["bias"] == list(BIAS))
    rows = value["samples"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= MAX_SAMPLES)
    keys = set()
    for row in rows:
        validate_row(row)
        key = (row["date"], row["issuer_id"])
        _require(key not in keys)
        keys.add(key)
    _require(_is_count(batch["sample_count"]) and batch["sample_count"] == len(rows))
    _require(value["coverage"] == coverage_from_rows(rows))
    validate_blockers(value["blockers"])
    serialized = canonical_json(value)
    _require(not any(secret and secret in serialized
                     for name in _CREDENTIAL_ENV
                     if (secret := os.environ.get(name, "").strip())))
    return generated


def validate_batch_report(value: object) -> datetime:
    _require(_keys(value, _BODY_KEYS | {"kind", "schema_version"}))
    _require(value["kind"] == "microcap_batch_report")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    return _validate_body(value)


def validate_snapshot_v2(value: object) -> datetime:
    _require(_keys(value, _BODY_KEYS | {"schema_version"}))
    _require(type(value["schema_version"]) is int and value["schema_version"] == 2)
    return _validate_body(value)


def project_batch_snapshot(report: Mapping, *, now: datetime) -> dict[str, object]:
    _require(isinstance(now, datetime) and now.tzinfo is not None
             and now.utcoffset() is not None)
    validate_batch_report(report)
    snapshot = {key: copy.deepcopy(report[key]) for key in _BODY_KEYS}
    snapshot["schema_version"] = 2
    snapshot["generated_at"] = _utc_text(now)
    validate_snapshot_v2(snapshot)
    return snapshot
