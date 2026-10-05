"""Read-only, bounded coverage inventory; never a trading or calibration gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from microcap_history import CoverageError, parse_utc
from microcap_ibkr_probe import probe_ibkr
from microcap_marketaux_probe import MarketauxProbe
from microcap_roster import eligible_members, member_for_issuer


VERSION = 1
_CHANNELS = ("bars", "quotes", "news")
_GOOD = {"observed", "NEWS_ARTICLES_OBSERVED"}
_MISSING = {
    "bars": "BAR_COVERAGE_UNAVAILABLE",
    "quotes": "QUOTE_COVERAGE_UNAVAILABLE",
    "news": "NEWS_COVERAGE_UNVERIFIED",
}
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,79}$")
_UTC_TEXT = re.compile(r"[0-9T:.Z+\-]+")
_SESSION_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INTRADAY = ("1 min", "1 hour")
_DAILY_STATUSES = {"observed", "partial", "unavailable"}


_CONFLICT = "CREDENTIAL_CONFLICT"
# Fallback diagnostics, ordered by preference. The last two share only JSON punctuation,
# so any token other than a substring of '{"', '":' or the braces has a safe choice.
_INPUT_FALLBACKS = ({"error": _CONFLICT}, {"failure": "INVALID_INPUT"}, {"fail": 2})
_PROVIDER_FALLBACKS = ({"error": "PROVIDER_UNAVAILABLE"}, {"failure": "PROVIDER_ERROR"},
                       {"fail": 3})


def _secret() -> str:
    return os.environ.get("MARKETAUX_API_TOKEN", "").strip()


def _reason(value: object, fallback: str) -> str:
    secret = _secret()
    if isinstance(value, str) and _CODE.fullmatch(value) and not (secret and secret in value):
        return value
    return fallback


def _scrub(value: object) -> object:
    """Redact the configured Marketaux token from a user-supplied string value."""
    secret = _secret()
    return value.replace(secret, "[REDACTED]") if secret and isinstance(value, str) else value


def _contains_secret(item: object, secret: str) -> bool:
    if isinstance(item, str):
        return secret in item
    if isinstance(item, Mapping):
        return any(_contains_secret(key, secret) or _contains_secret(child, secret)
                   for key, child in item.items())
    if isinstance(item, (list, tuple)):
        return any(_contains_secret(child, secret) for child in item)
    return False


def _diagnostic(*candidates: Mapping) -> str | None:
    """First candidate whose JSON text cannot reveal the token; None only if all would."""
    secret = _secret()
    for candidate in candidates:
        line = json.dumps(candidate, separators=(",", ":"))
        if not (secret and secret in line):
            return line
    return None


def _ensure_no_secret(output: object) -> None:
    """Fail closed if the token still appears, i.e. it collides with fixed schema labels."""
    secret = _secret()
    if secret and _contains_secret(output, secret):
        raise CoverageError(_CONFLICT)


def _safe_source_records(records: object) -> list[dict]:
    if not isinstance(records, list):
        raise CoverageError("OBSERVATIONS_INVALID")
    return [
        {key: value for key, value in record.items()
         if key in ("id", "article_id", "t", "published_at", "fetched_at", "time_basis", "source")
         and isinstance(value, str) and len(value) <= 100
         and re.fullmatch(r"[A-Za-z0-9.:_+\-TZ]+", value)
         and not (_secret() and _secret() in value)}
        for record in records if isinstance(record, Mapping)
    ]


def _utc_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _UTC_TEXT.fullmatch(value) else None


def _safe_intervals(value: object) -> dict | None:
    """Per-interval intraday counts and UTC range only; a daily entry is never intraday."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CoverageError("OBSERVATIONS_INVALID")
    result = {}
    for interval in _INTRADAY:
        item = value.get(interval, {"count": 0})
        if not isinstance(item, Mapping) or type(item.get("count")) is not int or item["count"] < 0:
            raise CoverageError("OBSERVATIONS_INVALID")
        result[interval] = {"count": item["count"],
                            "first_utc": _utc_or_none(item.get("first_utc")),
                            "last_utc": _utc_or_none(item.get("last_utc"))}
    return result


def _safe_daily(value: object) -> dict:
    """Session-date-only diagnostic; never a UTC instant or a decision-time observation."""
    if not isinstance(value, Mapping):
        raise CoverageError("OBSERVATIONS_INVALID")
    reasons = value.get("reasons", [])
    if not isinstance(reasons, list):
        raise CoverageError("OBSERVATIONS_INVALID")
    status = value.get("status") if value.get("status") in _DAILY_STATUSES else "unavailable"
    count = value.get("count") if type(value.get("count")) is int and value["count"] >= 0 else None
    safe_reasons = {_reason(reason, "DAILY_BAR_COVERAGE_UNAVAILABLE") for reason in reasons}
    safe_reasons.add("DAILY_BAR_DECISION_TIME_UNVERIFIED")
    if status != "observed" or not count:
        safe_reasons.add("DAILY_BAR_COVERAGE_UNAVAILABLE")
    dates = {
        key: value.get(key) if isinstance(value.get(key), str)
        and _SESSION_DATE.fullmatch(value[key]) else None
        for key in ("first_session_date", "last_session_date")
    }
    return {"status": status, "count": count, **dates,
            "timestamp_basis": "SESSION_DATE_ONLY", "decision_time_verified": False,
            "intraday_gate_eligible": False, "reasons": sorted(safe_reasons)}


def coverage_report(roster_status: Mapping, observations: list, *, sample_manifest: Mapping) -> dict:
    """Inventory declared coverage without deriving catalysts, odds, or order permissions."""
    if not isinstance(roster_status, Mapping) or not isinstance(sample_manifest, Mapping):
        raise CoverageError("INPUT_INVALID")
    issuers, dates = sample_manifest.get("issuer_ids"), sample_manifest.get("dates")
    if (sample_manifest.get("status") != "predeclared"
            or not isinstance(issuers, list) or not issuers
            or any(not isinstance(x, str) or not x for x in issuers)
            or len(set(issuers)) != len(issuers)
            or dates is not None and (
                not isinstance(dates, list) or not dates
                or any(not isinstance(x, str) or not x for x in dates)
                or len(set(dates)) != len(dates)
            )):
        raise CoverageError("SAMPLE_MANIFEST_INVALID")
    if not isinstance(observations, list):
        raise CoverageError("OBSERVATIONS_INVALID")
    reasons = set()
    if roster_status.get("status") != "verified":
        reasons.add("ROSTER_UNVERIFIED")
    matrix = []
    missing = {name: 0 for name in _CHANNELS}
    denominators = {name: 0 for name in _CHANNELS}
    seen = set()
    complete_ibkr_cells = set()
    ibkr_channel_cells = {channel: set() for channel in ("bars", "quotes")}
    ibkr_observed_channels = {channel: set() for channel in ("bars", "quotes")}
    interval_cells = {interval: set() for interval in (*_INTRADAY, "1 day")}
    bar_rows = set()
    for raw in observations:
        if not isinstance(raw, Mapping):
            raise CoverageError("OBSERVATIONS_INVALID")
        issuer, day = raw.get("issuer_id"), raw.get("date")
        if issuer not in issuers or not isinstance(day, str) or dates is not None and day not in dates:
            raise CoverageError("OBSERVATIONS_OUTSIDE_SAMPLE")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise CoverageError("OBSERVATIONS_INVALID") from None
        session, provider = raw.get("session", "unspecified"), raw.get("provider", "unspecified")
        if not isinstance(session, str) or not session or not isinstance(provider, str) or not provider:
            raise CoverageError("OBSERVATIONS_INVALID")
        key = (issuer, day, session, provider)
        if key in seen:
            raise CoverageError("OBSERVATIONS_DUPLICATE")
        seen.add(key)
        ibkr = provider.casefold() == "ibkr"
        cell = (issuer, day)
        observed_in_row = set()
        row = {"issuer_id": issuer, "date": day, "session": session, "provider": provider}
        for channel in _CHANNELS:
            item = raw.get(channel, {"status": "unavailable"})
            if not isinstance(item, Mapping) or not isinstance(item.get("status"), str):
                raise CoverageError("OBSERVATIONS_INVALID")
            status = item["status"]
            if status == "not_applicable":
                row[channel] = {"status": status}
                continue
            if ibkr and channel in ibkr_channel_cells:
                ibkr_channel_cells[channel].add(cell)
            denominators[channel] += 1
            records = _safe_source_records(item.get("source_records", []))
            channel_reasons = {
                _reason(reason, _MISSING[channel])
                for reason in item.get("reasons", [])
            } if isinstance(item.get("reasons", []), list) else {_MISSING[channel]}
            errors = item.get("errors", [])
            if not isinstance(errors, list):
                raise CoverageError("OBSERVATIONS_INVALID")
            safe_errors = [
                {"reason": _reason(error.get("reason"), "PROVIDER_ERROR")}
                if isinstance(error, Mapping) else {"reason": "PROVIDER_ERROR"}
                for error in errors
            ]
            channel_reasons.update(error["reason"] for error in safe_errors)
            intervals = None
            if channel == "bars":
                intervals = _safe_intervals(item.get("intervals"))
                if ibkr:
                    bar_rows.add(cell)
                if intervals is None:
                    if status in _GOOD:
                        channel_reasons.add("BAR_INTERVAL_UNVERIFIED")
                    channel_reasons.add("MINUTE_BAR_COVERAGE_UNAVAILABLE")
                else:
                    if ibkr and not safe_errors:
                        for interval in _INTRADAY:
                            if intervals[interval]["count"]:
                                interval_cells[interval].add(cell)
                    if not intervals["1 min"]["count"]:
                        # A positive combined/hourly count never satisfies the minute gate.
                        channel_reasons.add("MINUTE_BAR_COVERAGE_UNAVAILABLE")
                        if intervals["1 hour"]["count"]:
                            channel_reasons.add("MINUTE_BARS_MISSING_HOURLY_ONLY")
            if channel == "news" and records and any(
                record.get("time_basis") == "AMBIGUOUS"
                or not (record.get("t") or record.get("published_at"))
                for record in records
            ):
                channel_reasons.add("NEWS_TIMESTAMP_AMBIGUOUS")
            if status not in _GOOD or channel_reasons or safe_errors:
                if status not in _GOOD:
                    channel_reasons.add(_MISSING[channel])
                missing[channel] += 1
            elif ibkr and channel in ibkr_observed_channels:
                ibkr_observed_channels[channel].add(cell)
                observed_in_row.add(channel)
            reasons.update(channel_reasons)
            row[channel] = {
                "status": status, "reasons": sorted(channel_reasons), "errors": safe_errors,
                "count": item.get("count") if type(item.get("count")) is int and item["count"] >= 0 else None,
                "first_utc": _utc_or_none(item.get("first_utc")),
                "last_utc": _utc_or_none(item.get("last_utc")),
                "source_records": records,
            }
            if channel == "bars":
                row[channel]["intervals"] = intervals
        if "daily_bars" in raw:
            row["daily_bars"] = _safe_daily(raw["daily_bars"])
            if ibkr and row["daily_bars"]["status"] == "observed" and row["daily_bars"]["count"]:
                interval_cells["1 day"].add(cell)
        if ibkr and {"bars", "quotes"} <= observed_in_row:
            complete_ibkr_cells.add(cell)
        if raw.get("listing_status") == "delisted" and (
            not isinstance(raw.get("contract"), Mapping)
            or raw["contract"].get("status") != "resolved"
        ):
            reasons.add("CONTRACT_UNRESOLVED")
        matrix.append(row)
    if dates is not None:
        for issuer in issuers:
            for day in dates:
                cell = (issuer, day)
                if cell not in complete_ibkr_cells:
                    reasons.add("IBKR_COVERAGE_UNAVAILABLE")
                if cell not in ibkr_observed_channels["bars"]:
                    reasons.add("MINUTE_BAR_COVERAGE_UNAVAILABLE")
                for channel in ("bars", "quotes"):
                    if cell not in ibkr_observed_channels[channel]:
                        reasons.add(_MISSING[channel])
                        if cell not in ibkr_channel_cells[channel]:
                            denominators[channel] += 1
                            missing[channel] += 1
    if dates is not None and any(
        not any(row["issuer_id"] == issuer and row["date"] == day and row["provider"] == "ibkr"
                for row in matrix)
        for issuer in issuers for day in dates
    ) and matrix:
        # Offline observations may have no provider label; still require declared cells.
        if any(not any(row["issuer_id"] == issuer and row["date"] == day for row in matrix)
               for issuer in issuers for day in dates):
            reasons.add("SAMPLE_INCOMPLETE")
    if not matrix:
        reasons.add("SAMPLE_INCOMPLETE")
    coverage_status = "PROBE_COVERAGE_OBSERVED" if not reasons else "PROBE_COVERAGE_INCOMPLETE"
    cells = len(issuers) * len(dates) if dates is not None else len(bar_rows)
    bar_interval_coverage = {
        interval: {"observed_cells": len(interval_cells[interval]), "declared_cells": cells,
                   "missing_fraction": 1 - len(interval_cells[interval]) / cells if cells else 1.0}
        for interval in (*_INTRADAY, "1 day")
    }
    bar_interval_coverage["1 day"].update({
        "timestamp_basis": "SESSION_DATE_ONLY", "decision_time_verified": False,
        "intraday_gate_eligible": False,
    })
    reasons.add("RESEARCH_ONLY_NOT_CALIBRATED")
    report = {
        "schema_version": VERSION, "decision": "NO_TRADE", "coverage_status": coverage_status,
        "reasons": sorted(reasons),
        "order_approval": False, "model_calibrated": False,
        "target_probabilities": "unavailable",
        "sample_manifest": {"status": "predeclared", "issuer_ids": [_scrub(x) for x in issuers],
                            "dates": [_scrub(x) for x in dates] if dates is not None else None},
        "roster_status": {"status": _scrub(roster_status.get("status")),
                          "reason": _reason(roster_status.get("reason"), "ROSTER_UNVERIFIED")
                          if roster_status.get("status") != "verified" else None,
                          "sampling_bias": _reason(roster_status.get("sampling_bias"),
                                                   "ROSTER_UNVERIFIED")
                          if roster_status.get("status") != "verified" else None},
        "matrix": [_scrub_row(row) for row in matrix],
        "missing_fractions": {
            channel: missing[channel] / denominators[channel] if denominators[channel] else 1.0
            for channel in _CHANNELS
        },
        "counts": {
            "observations": len(matrix),
            "issuers": len({row["issuer_id"] for row in matrix}),
            "dates": len({row["date"] for row in matrix}),
            "sessions": len({row["session"] for row in matrix}),
            "providers": len({row["provider"] for row in matrix}),
            "declared_issuers": len(issuers),
            "declared_dates": len(dates) if dates is not None else None,
        },
        "bar_interval_coverage": bar_interval_coverage,
    }
    _ensure_no_secret(report)
    return report


def _scrub_row(row: Mapping) -> dict:
    out = {key: _scrub(row[key]) for key in ("issuer_id", "date", "session", "provider")}
    for channel in _CHANNELS:
        out[channel] = {
            key: _scrub(value) if key in ("status", "first_utc", "last_utc") else value
            for key, value in row[channel].items()
        }
    if "daily_bars" in row:
        out["daily_bars"] = dict(row["daily_bars"])
    return out


def _load_json(path: str) -> object:
    if not Path(path).is_absolute():
        raise CoverageError("PATH_NOT_ABSOLUTE")
    try:
        with open(path, encoding="utf-8") as source:
            return json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CoverageError("INPUT_FILE_INVALID") from None


def _utc(value: str) -> datetime:
    try:
        parsed = parse_utc(value).astimezone(timezone.utc)
    except (CoverageError, OverflowError, ValueError):
        raise CoverageError("TIMESTAMP_INVALID") from None
    if not value.endswith("Z") or parsed.microsecond:
        raise CoverageError("TIMESTAMP_INVALID")
    return parsed


def _positive(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected positive integer") from None
    if result < 1:
        raise argparse.ArgumentTypeError("expected positive integer")
    return result


def _validate_manifest(manifest: Mapping, start: datetime, end: datetime) -> None:
    if manifest.get("status") != "predeclared":
        raise CoverageError("SAMPLE_MANIFEST_INVALID")
    issuers, dates = manifest.get("issuer_ids"), manifest.get("dates")
    if (not isinstance(issuers, list) or not issuers
            or any(not isinstance(value, str) or not value.strip() for value in issuers)
            or len(set(issuers)) != len(issuers)
            or not isinstance(dates, list) or not dates
            or any(not isinstance(value, str) for value in dates)
            or len(set(dates)) != len(dates)):
        raise CoverageError("SAMPLE_MANIFEST_INVALID")
    for day in dates:
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError:
            raise CoverageError("SAMPLE_MANIFEST_INVALID") from None
        if parsed_day.isoformat() != day:
            raise CoverageError("SAMPLE_MANIFEST_INVALID")
        day_start = datetime.combine(parsed_day, datetime.min.time(), timezone.utc)
        if not day_start < end or day_start + timedelta(days=1) <= start:
            raise CoverageError("SAMPLE_MANIFEST_INVALID")


def _resolve_manifest_members(roster: object, manifest: Mapping, *, pilot: bool = False) -> dict:
    resolved = {}
    if not isinstance(roster, Mapping):
        return resolved
    pilot_roster = roster.get("coverage_claim") == "pilot-unverified"
    if pilot_roster and not pilot:
        raise CoverageError("ROSTER_UNVERIFIED")
    for issuer in manifest["issuer_ids"]:
        for day in manifest["dates"]:
            try:
                member = member_for_issuer(roster, issuer, f"{day}T12:00:00Z", allow_pilot=pilot)
            except CoverageError as error:
                if pilot_roster or str(error).startswith("CONTRACT_UNRESOLVED"):
                    raise
                continue
            if member is not None and isinstance(member.get("symbol"), str):
                resolved[(issuer, day)] = member
            elif pilot_roster:
                raise CoverageError("ROSTER_UNVERIFIED: no member for predeclared issuer and date")
    return resolved


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CoverageError("ARGUMENTS_INVALID")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeParser(description="Read-only micro-cap coverage inventory; never approves trades")
    parser.add_argument("--roster", required=True, help="absolute external historical roster JSON path")
    parser.add_argument("--sample-manifest", required=True, help="absolute predeclared sample JSON path")
    parser.add_argument("--observations", help="absolute external observations JSON path (offline replay)")
    parser.add_argument("--start", required=True, help="UTC inclusive start, e.g. 2024-05-15T00:00:00Z")
    parser.add_argument("--end", required=True, help="UTC exclusive end, e.g. 2024-05-16T00:00:00Z")
    parser.add_argument("--fetch", action="store_true", help="enable read-only IBKR requests")
    parser.add_argument("--marketaux", action="store_true",
                        help="also request optional Marketaux news (requires MARKETAUX_API_TOKEN)")
    parser.add_argument("--pilot", action="store_true", help="biased, unverified roster pilot (NO_TRADE)")
    parser.add_argument("--max-symbols", type=_positive, help="required fetch cap (1..10)")
    parser.add_argument("--max-days", type=_positive, help="required fetch cap (1..7)")
    return parser


def _roster_status(roster: object, start: datetime, *, pilot: bool) -> tuple[dict, dict]:
    if not isinstance(roster, Mapping):
        raise CoverageError("ROSTER_INVALID")
    try:
        members = eligible_members(roster, start.isoformat().replace("+00:00", "Z"),
                                   allow_pilot=pilot)
    except CoverageError:
        members = []
    # A source's coverage_claim and self-supplied provenance are not independent
    # verification of historical current AND delisted membership.
    return {"status": "unverified", "reason": "ROSTER_UNVERIFIED",
            "sampling_bias": "UNVERIFIED_PILOT" if pilot else "ROSTER_UNVERIFIED"}, {
                str(member["issuer_id"]): member for member in members
            }


def _fetch(manifest: Mapping, start: datetime, end: datetime, resolved_members: Mapping,
           *, marketaux_enabled: bool) -> tuple[list, bool]:
    client_id = os.environ.get("IB_PROBE_CLIENT_ID", "")
    if not client_id.isdecimal() or int(client_id) < 1:
        raise CoverageError("IB_PROBE_CLIENT_ID_REQUIRED")
    if int(client_id) in _active_client_ids():
        raise CoverageError("IB_PROBE_CLIENT_ID_IN_USE")
    host = os.environ.get("IB_PROBE_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("IB_PROBE_PORT", "7497"))
    except ValueError:
        raise CoverageError("IB_PROBE_PORT_INVALID") from None
    observations = []
    provider_error = False
    news_probe = None
    if marketaux_enabled:
        try:
            news_probe = MarketauxProbe.from_environment()
        except CoverageError:
            provider_error = True
    for issuer in manifest["issuer_ids"]:
        for day in manifest["dates"]:
            member = resolved_members.get((issuer, day))
            if not member or not isinstance(member.get("symbol"), str):
                observations.append({
                    "issuer_id": issuer, "date": day, "session": "unspecified", "provider": "ibkr",
                    "bars": {"status": "unavailable", "reasons": ["ROSTER_UNVERIFIED"]},
                    "quotes": {"status": "unavailable"}, "news": {"status": "unverified"},
                })
                continue
            symbol = member["symbol"]
            day_start = datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc)
            begin = max(start, day_start)
            finish = min(end, day_start + timedelta(days=1))
            try:
                ib = probe_ibkr(symbol, begin.isoformat().replace("+00:00", "Z"),
                                finish.isoformat().replace("+00:00", "Z"),
                                host=host, port=port, client_id=int(client_id))
            except CoverageError:
                ib = {channel: {"status": "unavailable", "reasons": ["IBKR_PROBE_FAILED"]}
                      for channel in _CHANNELS}
                ib["contract"] = {"status": "unresolved"}
                ib["daily_bars"] = {"status": "unavailable", "reasons": ["IBKR_PROBE_FAILED"]}
                provider_error = True
            entry = {"issuer_id": issuer, "date": day, "session": "unspecified",
                     "provider": "ibkr", "listing_status": member.get("listing_status"),
                     "contract": ib.get("contract"),
                     "daily_bars": ib.get("daily_bars") if isinstance(ib.get("daily_bars"), Mapping)
                     else {"status": "unavailable", "reasons": ["DAILY_BAR_COVERAGE_UNAVAILABLE"]}}
            for channel in _CHANNELS:
                data = ib.get(channel, {})
                entry[channel] = {
                    "status": data.get("status", "unavailable"),
                    "reasons": data.get("reasons", []),
                    "errors": [e for e in ib.get("errors", [])
                               if e.get("channel") in (channel, "all", "global")
                               and e.get("severity") != "notice"],
                    "count": data.get("count"), "first_utc": data.get("first_utc"),
                    "last_utc": data.get("last_utc"),
                    "source_records": [] if channel == "bars" else [
                        {"t": record.get("t"), "article_id": record.get("article_id"),
                         "time_basis": record.get("time_basis")}
                        for record in data.get("observations", [])
                    ],
                }
            entry["bars"]["intervals"] = _interval_summary(ib.get("bars", {}).get("observations", []))
            if any(e.get("severity") != "notice" for e in ib.get("errors", [])):
                provider_error = True
            observations.append(entry)
            if marketaux_enabled:
                news = {"status": "NEWS_COVERAGE_UNVERIFIED"}
                if news_probe is not None:
                    try:
                        response = news_probe.fetch_articles(symbol, begin, finish)
                        news = {
                            "status": response["status"], "count": response["collected"],
                            "source_records": [
                                {"id": item["id"], "published_at": item["published_at"],
                                 "source": item["source"], "fetched_at": item["fetched_at"]}
                                for item in response["articles"]
                            ],
                        }
                    except CoverageError:
                        news = {"status": "unavailable",
                                "errors": [{"reason": "NEWS_PROVIDER_UNAVAILABLE"}]}
                        provider_error = True
                else:
                    news["errors"] = [{"reason": "NEWS_PROVIDER_UNAVAILABLE"}]
                observations.append({
                    "issuer_id": issuer, "date": day, "session": "unspecified",
                    "provider": "marketaux", "bars": {"status": "not_applicable"},
                    "quotes": {"status": "not_applicable"}, "news": news,
                })
    return observations, provider_error


def _interval_summary(records: object) -> dict:
    """Counts and UTC range per intraday interval; bar prices/volumes are never kept."""
    result = {}
    for interval in _INTRADAY:
        times = sorted(
            record["t"] for record in (records if isinstance(records, list) else [])
            if isinstance(record, Mapping) and record.get("interval") == interval
            and isinstance(record.get("t"), str)
        )
        result[interval] = {"count": len(times), "first_utc": times[0] if times else None,
                            "last_utc": times[-1] if times else None}
    return result


def _active_client_ids() -> set[int]:
    # Reserve low IDs and all configured TradingMax services; never connect as one of them.
    ids = set(range(0, 100))
    for name in (
        "IB_CLIENT_ID", "IB_MONITOR_CLIENT_ID", "IB_STATUS_CLIENT_ID",
        "IB_MARKET_CONTEXT_CLIENT_ID", "IB_NEWS_CLIENT_ID",
        "MICROSTRUCTURE_IB_CLIENT_ID", "IB_PRELIVE_CLIENT_ID",
        "IB_POSITION_CHECK_CLIENT_ID",
    ):
        value = os.environ.get(name, "")
        if value.isdecimal():
            ids.add(int(value))
    return ids


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        start, end = _utc(args.start), _utc(args.end)
        if not start < end or end > datetime.now(timezone.utc):
            raise CoverageError("RANGE_INVALID")
        if args.fetch and args.observations:
            raise CoverageError("FETCH_AND_REPLAY_EXCLUSIVE")
        if args.marketaux and not args.fetch:
            raise CoverageError("MARKETAUX_REQUIRES_FETCH")
        if args.fetch and (args.max_symbols is None or args.max_days is None
                           or args.max_symbols > 10 or args.max_days > 7
                           or end - start > timedelta(days=args.max_days)):
            raise CoverageError("FETCH_BOUNDS_INVALID")
        if args.fetch and (not os.environ.get("IB_PROBE_CLIENT_ID", "").isdecimal()
                           or int(os.environ["IB_PROBE_CLIENT_ID"]) in _active_client_ids()
                           or int(os.environ["IB_PROBE_CLIENT_ID"]) < 1):
            raise CoverageError("IB_PROBE_CLIENT_ID_REQUIRED")
        manifest = _load_json(args.sample_manifest)
        roster = _load_json(args.roster)
        if not isinstance(manifest, Mapping) or not isinstance(roster, Mapping):
            raise CoverageError("INPUT_INVALID")
        _validate_manifest(manifest, start, end)
        issuers, dates = manifest["issuer_ids"], manifest["dates"]
        if args.fetch and len(dates) > args.max_days:
            raise CoverageError("FETCH_BOUNDS_INVALID")
        resolved_members = _resolve_manifest_members(roster, manifest, pilot=args.pilot)
        if args.fetch and len({
            member["symbol"].casefold() for member in resolved_members.values()
        }) > args.max_symbols:
            raise CoverageError("FETCH_BOUNDS_INVALID")
        status, _ = _roster_status(roster, start, pilot=args.pilot)
        if args.fetch and not args.pilot:
            raise CoverageError("ROSTER_UNVERIFIED")
        provider_error = False
        if args.fetch:
            observations, provider_error = _fetch(
                manifest, start, end, resolved_members, marketaux_enabled=args.marketaux)
        elif args.observations:
            observations = _load_json(args.observations)
            if isinstance(observations, Mapping):
                observations = observations.get("observations")
        else:
            observations = []
        report = coverage_report(status, observations, sample_manifest=manifest)
        report["request_window"] = {
            "start_utc": start.isoformat().replace("+00:00", "Z"),
            "end_utc": end.isoformat().replace("+00:00", "Z"),
        }
        text = json.dumps(report, default=str, sort_keys=True)
        if _secret() and _secret() in text:
            raise CoverageError(_CONFLICT)
        print(text)
        if provider_error:
            notice = _diagnostic({"error": "PROVIDER_UNAVAILABLE", "reason": "PROVIDER_ERROR"},
                                 *_PROVIDER_FALLBACKS)
            if notice is not None:
                print(notice, file=sys.stderr)
        return 3 if provider_error else 0
    except (CoverageError, TypeError, ValueError, OverflowError):
        reason = _reason(str(sys.exc_info()[1]).split(":")[0], "INPUT_INVALID")
        primary = {"error": "INPUT_OR_COVERAGE_INVALID", "reason": reason}
        # A token that collides with output labels or punctuation is invalid credential input.
        error = _diagnostic(primary, *_INPUT_FALLBACKS)
        if error is not None:
            print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
