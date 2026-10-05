"""Sanitized, fail-closed projection of micro-cap research coverage snapshots."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from microcap_history import CoverageError, parse_utc


_REPOSITORY = Path(__file__).resolve().parent
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_MATRIX_ROWS = 1000
_CREDENTIAL_ENV = (
    "MASSIVE_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY",
    "MARKETAUX_API_TOKEN", "SEC_USER_AGENT",
)
_SYMBOL = re.compile(r"[A-Z0-9]{1,32}\Z")
_ISSUER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_IDENTITY = re.compile(r"[0-9]{1,10}\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z")
_REPORT_REASONS = frozenset({
    "ALPACA_NEWS_INVALID", "ALPACA_NEWS_MISSING",
    "BAR_COVERAGE_UNAVAILABLE", "BAR_INTERVAL_UNVERIFIED",
    "CONTRACT_UNRESOLVED", "CREDENTIAL_CONFLICT",
    "DAILY_BAR_COVERAGE_UNAVAILABLE", "DAILY_BAR_DECISION_TIME_UNVERIFIED",
    "IBKR_CONNECTION_FAILED", "IBKR_COVERAGE_UNAVAILABLE", "IBKR_DISCONNECT_FAILED",
    "IBKR_DISCONNECTED", "IBKR_ERROR", "IBKR_NETWORK_LOOP_FAILED",
    "IBKR_PACING_VIOLATION", "IBKR_PERMISSION_DENIED", "IBKR_PROBE_FAILED",
    "IBKR_REQUEST_FAILED", "IBKR_TIMESTAMP_AMBIGUOUS", "MARKET_CAP_UNVERIFIED",
    "MINUTE_BARS_MISSING_HOURLY_ONLY", "MINUTE_BAR_COVERAGE_UNAVAILABLE",
    "MASSIVE_IDENTITY_UNRESOLVED", "MASSIVE_PAGINATION_UNVERIFIED",
    "MASSIVE_SAVED_EVIDENCE_UNVERIFIED", "MASSIVE_SOURCE_INVALID",
    "MASSIVE_SOURCE_MISSING", "NEWS_COVERAGE_UNVERIFIED", "NEWS_PROVIDERS_UNAVAILABLE",
    "NEWS_PROVIDER_UNAVAILABLE", "NEWS_RESULTS_TRUNCATED",
    "NEWS_TIMESTAMP_AMBIGUOUS", "NEWS_WINDOW_COVERAGE_UNVERIFIED",
    "ALPACA_NEWS_MISSING", "ALPACA_NEWS_INVALID",
    "PROVIDER_ERROR", "QUOTE_COVERAGE_UNAVAILABLE", "QUOTE_RESULTS_TRUNCATED",
    "RESEARCH_ONLY_NOT_CALIBRATED", "REQUEST_TIMEOUT", "ROSTER_COVERAGE_UNVERIFIED",
    "ROSTER_UNVERIFIED", "SAMPLE_INCOMPLETE",
})
_SEC_BLOCKERS = frozenset({
    "ACCEPTANCE_TIME_INVALID", "ACCESSION_CIK_MISMATCH", "ACCESSION_LIMIT_INVALID",
    "AMBIGUOUS_CLASS_COVERAGE", "AMENDMENT_PRESENT", "CIK_MISMATCH",
    "CLASS_COVERAGE_UNVERIFIED", "CONTRADICTORY_FACTS", "DECISION_TIME_INVALID",
    "FACT_ROWS_INVALID", "FILED_DATE_INVALID", "FILED_DATE_MISMATCH",
    "FILING_FORM_UNSUPPORTED", "FETCH_TIME_INVALID", "REPORT_DATE_INVALID",
    "REPORT_DATE_MISMATCH", "SHARES_COUNT_INVALID", "SHARES_FACT_ABSENT",
    "SUBMISSIONS_ARRAYS_INVALID", "SUBMISSIONS_ROW_INVALID", "FUTURE_ACCEPTANCE",
    "ISSUER_MISMATCH", "SEC_COVERAGE_TRUNCATED",
})
_ROSTER_STATUSES = frozenset({
    "MISSING", "INVALID", "SAVED_EVIDENCE_UNVERIFIED", "PAGINATION_UNVERIFIED",
    "DATED_ROSTER_OBSERVED",
})
_NEWS_STATUSES = frozenset({"MISSING", "INVALID", "ARTICLES_OBSERVED", "EMPTY"})
_CHANNEL_STATUSES = frozenset({
    "observed", "partial", "unavailable", "not_applicable", "unverified",
    "NEWS_ARTICLES_OBSERVED", "NEWS_COVERAGE_UNVERIFIED",
})
_PROVIDERS = frozenset({"ibkr", "marketaux", "unspecified"})
_SESSIONS = frozenset({
    "unspecified", "regular", "morning", "afternoon", "premarket", "postmarket",
})


def _invalid() -> CoverageError:
    return CoverageError("INPUT_INVALID")


def _is_count(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _reason_list(value: object, *, sec: bool = False) -> list[str]:
    allowed = _SEC_BLOCKERS if sec else _REPORT_REASONS
    if (not isinstance(value, list)
            or any(not isinstance(reason, str) or reason not in allowed for reason in value)
            or value != sorted(set(value))):
        raise _invalid()
    return value


def _utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise _invalid()
    try:
        parsed = parse_utc(value)
    except (CoverageError, ValueError, OverflowError):
        raise _invalid() from None
    return parsed.astimezone(timezone.utc)


def _count_or_none(value: object) -> int | None:
    if value is None:
        return None
    if not _is_count(value):
        raise _invalid()
    return value


def _validate_window(report: Mapping, day: str) -> None:
    window = report.get("request_window")
    if not isinstance(window, Mapping):
        raise _invalid()
    start_text, end_text = window.get("start_utc"), window.get("end_utc")
    start, end = _utc_timestamp(start_text), _utc_timestamp(end_text)
    parsed_day = date.fromisoformat(day)
    lower = datetime.combine(parsed_day, time.min, timezone.utc)
    upper = lower + timedelta(days=1)
    if not lower <= start < end <= upper:
        raise _invalid()


def _validate_count_map(value: object, keys: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise _invalid()
    result = {}
    for key in keys:
        entry = value.get(key)
        if not isinstance(entry, Mapping) or not _is_count(entry.get("count")):
            raise _invalid()
        result[key] = entry["count"]
    return result


def _validate_channel(
    value: object, *, channel: str
) -> tuple[str, list[str], list[str]]:
    if not isinstance(value, Mapping):
        raise _invalid()
    status = value.get("status")
    allowed_statuses = _CHANNEL_STATUSES if channel == "news" else {
        "observed", "partial", "unavailable", "not_applicable",
    }
    if not isinstance(status, str) or status not in allowed_statuses:
        raise _invalid()
    _count_or_none(value.get("count"))
    reasons = _reason_list(value.get("reasons", []))
    errors = value.get("errors", [])
    if not isinstance(errors, list):
        raise _invalid()
    error_reasons = []
    for error in errors:
        if not isinstance(error, Mapping):
            raise _invalid()
        error_reason = error.get("reason")
        if not isinstance(error_reason, str) or error_reason not in _REPORT_REASONS:
            raise _invalid()
        error_reasons.append(error_reason)
    if channel == "bars":
        if status == "not_applicable":
            if "intervals" in value and value["intervals"] is not None:
                _validate_count_map(value["intervals"], ("1 min", "1 hour"))
        elif value.get("intervals") is not None:
            _validate_count_map(value["intervals"], ("1 min", "1 hour"))
    return status, reasons, error_reasons


def _validate_roster(value: object, day: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid()
    status = value.get("status")
    if not isinstance(status, str) or status not in _ROSTER_STATUSES:
        raise _invalid()
    if (value.get("provider") != "massive" or value.get("snapshot_date") != day
            or value.get("completeness") != "UNVERIFIED_BY_PROVIDER"
            or value.get("provider_completeness") != "UNVERIFIED_BY_PROVIDER"
            or value.get("dated_coverage") not in
            ("UNVERIFIED", "DATED_FILTER_RESPONSE_OBSERVED")
            or value.get("entitlement") != "BASIC_TWO_YEAR_HISTORY_LIMIT"):
        raise _invalid()
    for key in ("active_count", "inactive_count", "unresolved_identity_count"):
        _count_or_none(value.get(key))
    if (status in ("MISSING", "SAVED_EVIDENCE_UNVERIFIED")
            and (value.get("active_count") is not None
                 or value.get("inactive_count") is not None)
            or status in ("PAGINATION_UNVERIFIED", "DATED_ROSTER_OBSERVED")
            and (value.get("active_count") is None
                 or value.get("inactive_count") is None)):
        raise _invalid()
    pagination = value.get("pagination")
    if not isinstance(pagination, Mapping):
        raise _invalid()
    for label in ("active", "inactive"):
        page = pagination.get(label)
        if not isinstance(page, Mapping):
            raise _invalid()
        _count_or_none(page.get("page_count"))
        if page.get("complete") not in (None, True):
            raise _invalid()
    return {"status": status, "active_count": value.get("active_count"),
            "inactive_count": value.get("inactive_count")}


def _validate_news(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid()
    status = value.get("status")
    if (not isinstance(status, str) or status not in _NEWS_STATUSES
            or value.get("provider") != "alpaca_news"
            or value.get("completeness") != "UNVERIFIED_BY_PROVIDER"):
        raise _invalid()
    count = _count_or_none(value.get("article_count"))
    if (status in ("MISSING", "INVALID") and count is not None
            or status == "ARTICLES_OBSERVED" and (count is None or count < 1)
            or status == "EMPTY" and count != 0):
        raise _invalid()
    pagination = value.get("pagination")
    if (not isinstance(pagination, Mapping)
            or pagination.get("page_count") is not None
            or pagination.get("complete") is not None):
        raise _invalid()
    return {"status": status, "article_count": count}


def _validate_market_cap(value: object) -> None:
    if not isinstance(value, Mapping):
        raise _invalid()
    if (value.get("status") != "MARKET_CAP_UNVERIFIED"
            or value.get("reason") != "NO_AUDITED_FILED_SHARE_SERIES"):
        raise _invalid()
    if _reason_list(value.get("blocking_reasons", [])) != ["MARKET_CAP_UNVERIFIED"]:
        raise _invalid()


def _validate_source(report: Mapping) -> tuple[str, str, str, str, int, int, list[str]]:
    required = (
        "schema_version", "decision", "coverage_status", "reasons", "order_approval",
        "model_calibrated", "target_probabilities", "sample_manifest", "roster_status",
        "matrix", "missing_fractions", "counts", "bar_interval_coverage",
        "request_window", "massive_roster", "alpaca_news", "market_cap",
    )
    if (not isinstance(report, Mapping) or any(key not in report for key in required)
            or type(report["schema_version"]) is not int or report["schema_version"] != 1
            or report["decision"] != "NO_TRADE"
            or report["order_approval"] is not False
            or report["model_calibrated"] is not False
            or report["target_probabilities"] != "unavailable"
            or report["coverage_status"] not in
            ("PROBE_COVERAGE_OBSERVED", "PROBE_COVERAGE_INCOMPLETE")):
        raise _invalid()

    manifest = report["sample_manifest"]
    if not isinstance(manifest, Mapping) or manifest.get("status") != "predeclared":
        raise _invalid()
    issuers, dates = manifest.get("issuer_ids"), manifest.get("dates")
    symbol = manifest.get("symbol")
    if (not isinstance(issuers, list) or len(issuers) != 1
            or not isinstance(issuers[0], str) or not _ISSUER.fullmatch(issuers[0])
            or not isinstance(dates, list) or len(dates) != 1
            or not isinstance(dates[0], str)
            or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)):
        raise _invalid()
    try:
        parsed_day = date.fromisoformat(dates[0])
    except ValueError:
        raise _invalid() from None
    if parsed_day.isoformat() != dates[0]:
        raise _invalid()
    _validate_window(report, dates[0])

    reasons = _reason_list(report["reasons"])
    roster = _validate_roster(report["massive_roster"], dates[0])
    news = _validate_news(report["alpaca_news"])
    _validate_market_cap(report["market_cap"])

    roster_status = report["roster_status"]
    if (not isinstance(roster_status, Mapping)
            or roster_status.get("status") not in ("verified", "unverified")):
        raise _invalid()
    for key in ("reason", "sampling_bias"):
        value = roster_status.get(key)
        if value is not None and (not isinstance(value, str) or value not in _REPORT_REASONS):
            raise _invalid()

    matrix = report["matrix"]
    if not isinstance(matrix, list) or len(matrix) > _MAX_MATRIX_ROWS:
        raise _invalid()
    sessions, providers = set(), set()
    minute_cells: set[tuple[str, str]] = set()
    quote_cells: set[tuple[str, str]] = set()
    interval_cells = {"1 min": set(), "1 hour": set(), "1 day": set()}
    blockers = set(reasons) | {"ROSTER_COVERAGE_UNVERIFIED", "MARKET_CAP_UNVERIFIED"}
    blockers.update(_reason_list(report["massive_roster"].get("blocking_reasons", [])))
    blockers.update(_reason_list(report["alpaca_news"].get("blocking_reasons", [])))
    for row in matrix:
        if (not isinstance(row, Mapping) or row.get("issuer_id") != issuers[0]
                or row.get("date") != dates[0]):
            raise _invalid()
        session, provider = row.get("session"), row.get("provider")
        if (not isinstance(session, str) or session not in _SESSIONS
                or not isinstance(provider, str) or provider not in _PROVIDERS):
            raise _invalid()
        sessions.add(session)
        providers.add(provider)
        if "symbol" in row and row["symbol"] != symbol:
            raise _invalid()
        row_roster = _validate_roster(row.get("massive_roster"), dates[0])
        row_news = _validate_news(row.get("alpaca_news"))
        _validate_market_cap(row.get("market_cap_gate"))
        _reason_list(row["massive_roster"].get("blocking_reasons", []))
        _reason_list(row["alpaca_news"].get("blocking_reasons", []))
        if row_roster != roster or row_news != news:
            raise _invalid()
        row_blockers = _reason_list(row.get("blocking_reasons"))
        blockers.update(row_blockers)

        bars_status, bars_reasons, bars_errors = _validate_channel(
            row.get("bars"), channel="bars")
        quotes_status, quotes_reasons, quotes_errors = _validate_channel(
            row.get("quotes"), channel="quotes")
        _, news_reasons, news_errors = _validate_channel(
            row.get("news"), channel="news")
        blockers.update(
            bars_reasons + bars_errors + quotes_reasons + quotes_errors
            + news_reasons + news_errors
        )
        if bars_status != "not_applicable":
            intervals = _validate_count_map(row["bars"].get("intervals"),
                                            ("1 min", "1 hour")) \
                if row["bars"].get("intervals") is not None else None
            if provider == "ibkr":
                cell = (issuers[0], dates[0])
                if intervals is not None:
                    for interval in ("1 min", "1 hour"):
                        if intervals[interval] > 0 and not bars_errors:
                            interval_cells[interval].add(cell)
                    if intervals["1 min"] > 0 and not bars_errors:
                        minute_cells.add(cell)
        if (provider == "ibkr" and quotes_status == "observed"
                and not quotes_reasons and not quotes_errors):
            quote_cells.add((issuers[0], dates[0]))

        if "daily_bars" in row:
            daily = row["daily_bars"]
            if (not isinstance(daily, Mapping)
                    or daily.get("status") not in ("observed", "partial", "unavailable")
                    or daily.get("timestamp_basis") != "SESSION_DATE_ONLY"
                    or daily.get("decision_time_verified") is not False
                    or daily.get("intraday_gate_eligible") is not False):
                raise _invalid()
            _count_or_none(daily.get("count"))
            _reason_list(daily.get("reasons", []))
            if (provider == "ibkr" and daily["status"] == "observed"
                    and daily.get("count", 0)):
                interval_cells["1 day"].add((issuers[0], dates[0]))

    counts = report["counts"]
    if not isinstance(counts, Mapping):
        raise _invalid()
    expected_counts = {
        "observations": len(matrix), "issuers": len({row["issuer_id"] for row in matrix}),
        "dates": len({row["date"] for row in matrix}), "sessions": len(sessions),
        "providers": len(providers), "declared_issuers": 1, "declared_dates": 1,
    }
    if any(not _is_count(counts.get(key)) or counts[key] != value
           for key, value in expected_counts.items()):
        raise _invalid()
    missing_fractions = report["missing_fractions"]
    if not isinstance(missing_fractions, Mapping):
        raise _invalid()
    for channel in ("bars", "quotes", "news"):
        value = missing_fractions.get(channel)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 1):
            raise _invalid()
    interval_summary = report["bar_interval_coverage"]
    if not isinstance(interval_summary, Mapping):
        raise _invalid()
    for interval in ("1 min", "1 hour", "1 day"):
        entry = interval_summary.get(interval)
        if not isinstance(entry, Mapping):
            raise _invalid()
        observed = len(interval_cells[interval])
        declared = 1
        missing = 1 - observed / declared
        fraction = entry.get("missing_fraction")
        if (not _is_count(entry.get("observed_cells"))
                or entry["observed_cells"] != observed
                or not _is_count(entry.get("declared_cells"))
                or entry["declared_cells"] != declared
                or isinstance(fraction, bool) or not isinstance(fraction, (int, float))
                or not math.isfinite(fraction) or not math.isclose(fraction, missing)):
            raise _invalid()
    daily_summary = interval_summary["1 day"]
    if (daily_summary.get("timestamp_basis") != "SESSION_DATE_ONLY"
            or daily_summary.get("decision_time_verified") is not False
            or daily_summary.get("intraday_gate_eligible") is not False):
        raise _invalid()
    return (issuers[0], symbol, dates[0], roster["status"],
            len(minute_cells), len(quote_cells), sorted(blockers))


def _validate_sec(sec: Mapping) -> tuple[int, list[str]]:
    if (sec.get("decision") != "NO_TRADE" or sec.get("order_approval") is not False
            or sec.get("model_calibrated") is not False
            or sec.get("target_probabilities") != "unavailable"):
        raise _invalid()
    gate = sec.get("market_cap_gate")
    if not isinstance(gate, Mapping):
        raise _invalid()
    if (gate.get("status") != "MARKET_CAP_UNVERIFIED"
            or gate.get("source_verified") is not False
            or gate.get("coverage") != "UNVERIFIED"
            or type(gate.get("coverage_truncated")) is not bool):
        raise _invalid()
    cik = gate.get("cik")
    if cik is not None and (not isinstance(cik, str) or not _IDENTITY.fullmatch(cik)):
        raise _invalid()
    decision_at = gate.get("decision_at")
    if decision_at is not None:
        _utc_timestamp(decision_at)
    observations = gate.get("observations")
    if not isinstance(observations, list) or len(observations) > 2:
        raise _invalid()
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise _invalid()
        accession = observation.get("accession")
        if (not isinstance(accession, str)
                or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)):
            raise _invalid()
        _utc_timestamp(observation.get("accepted_at"))
        _utc_timestamp(observation.get("fetched_at"))
        report_date = observation.get("report_date")
        if not isinstance(report_date, str):
            raise _invalid()
        try:
            if date.fromisoformat(report_date).isoformat() != report_date:
                raise _invalid()
        except ValueError:
            raise _invalid() from None
        if observation.get("filing_class") not in ("annual", "quarterly", "current",
                                                   "other", "amendment"):
            raise _invalid()
        shares_count = observation.get("shares_count")
        if shares_count is not None and not _is_count(shares_count, minimum=1):
            raise _invalid()
    return len(observations), _reason_list(gate.get("blockers"), sec=True)


def project_readiness(
    report: Mapping, sec: Mapping | None = None, *, now: datetime
) -> dict[str, object]:
    """Return a compact, always-NO_TRADE snapshot from validated research evidence."""
    if (not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None):
        raise _invalid()
    instant = now.astimezone(timezone.utc)
    issuer, symbol, day, roster_status, minute_cells, quote_cells, blockers = \
        _validate_source(report)
    sec_count = 0
    if sec is not None:
        if not isinstance(sec, Mapping):
            raise _invalid()
        sec_count, sec_blockers = _validate_sec(sec)
        blockers = sorted(set(blockers) | set(sec_blockers))
        if sec["market_cap_gate"]["coverage_truncated"]:
            blockers = sorted(set(blockers) | {"SEC_COVERAGE_TRUNCATED"})
    result: dict[str, object] = {
        "schema_version": 1,
        "generated_at": instant.isoformat().replace("+00:00", "Z"),
        "decision": "NO_TRADE",
        "order_approval": False,
        "model_calibrated": False,
        "target_probabilities": "unavailable",
        "sample": {"issuer_id": issuer, "symbol": symbol, "date": day},
        "sources": {
            "roster": {
                "status": roster_status,
                "active_count": report["massive_roster"]["active_count"],
                "inactive_count": report["massive_roster"]["inactive_count"],
            },
            "news": {
                "status": report["alpaca_news"]["status"],
                "article_count": report["alpaca_news"]["article_count"],
            },
            "ibkr": {"minute_cells": minute_cells, "quote_cells": quote_cells},
            "shares": {
                "status": "MARKET_CAP_UNVERIFIED",
                "sec_observation_count": sec_count,
            },
        },
        "blockers": blockers,
    }
    serialized = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if any(secret and secret in serialized
           for name in _CREDENTIAL_ENV if (secret := os.environ.get(name, "").strip())):
        raise _invalid()
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _external(value: Path, *, existing: bool) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise _invalid()
    try:
        resolved = path.resolve(strict=existing)
        if resolved.is_relative_to(_REPOSITORY):
            raise _invalid()
        if existing and not resolved.is_file():
            raise _invalid()
        if not existing and (
            not resolved.parent.is_dir() or path.is_symlink() or resolved.is_dir()
        ):
            raise _invalid()
    except (OSError, RuntimeError):
        raise _invalid() from None
    return resolved


def _load(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as source:
            payload = source.read(_MAX_JSON_BYTES + 1)
        if len(payload) > _MAX_JSON_BYTES:
            raise _invalid()
        value = json.loads(payload, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise _invalid() from None
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _save(path: Path, value: Mapping) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".readiness-",
            suffix=".json", delete=False,
        ) as destination:
            temporary = destination.name
            json.dump(value, destination, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
            destination.write("\n")
        os.replace(temporary, path)
    except (OSError, ValueError):
        raise _invalid() from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                raise _invalid() from None


def _error_output() -> str | None:
    candidates = (
        '{"error":"INPUT_INVALID"}',
        '{"failure":"INVALID_INPUT"}',
        '{"fail":2}',
        "{}",
    )
    secrets = [os.environ.get(name, "").strip() for name in _CREDENTIAL_ENV]
    for candidate in candidates:
        if not any(secret and secret in candidate for secret in secrets):
            return candidate
    return None


def publish_readiness(
    report_path: Path, status_path: Path, sec_path: Path | None = None
) -> dict[str, object]:
    """Load external bounded reports, project safely, and atomically publish the snapshot."""
    source = _external(report_path, existing=True)
    destination = _external(status_path, existing=False)
    sec_source = _external(sec_path, existing=True) if sec_path is not None else None
    if destination in (source, sec_source):
        raise _invalid()
    report = _load(source)
    sec = _load(sec_source) if sec_source is not None else None
    result = project_readiness(report, sec, now=datetime.now(timezone.utc))
    _save(destination, result)
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _invalid()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Publish a sanitized, always-NO_TRADE micro-cap readiness snapshot")
    parser.add_argument("--source-report", required=True, help="absolute external source report JSON")
    parser.add_argument("--output", required=True, help="absolute external readiness snapshot JSON")
    parser.add_argument("--sec-report", help="optional absolute external SEC pilot JSON")
    try:
        args = parser.parse_args(argv)
        result = publish_readiness(Path(args.source_report), Path(args.output),
                                   Path(args.sec_report) if args.sec_report else None)
    except CoverageError:
        diagnostic = _error_output()
        if diagnostic is not None:
            print(diagnostic, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
