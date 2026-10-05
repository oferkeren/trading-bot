"""Offline-first, read-only source feasibility overlay for an IBKR coverage report."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from microcap_history import AlpacaHistory, CoverageError, parse_utc
from microcap_massive_roster import MassiveRoster
from microcap_news_coverage import observed_news


_BASE_FIELDS = (
    "schema_version", "decision", "coverage_status", "reasons", "order_approval",
    "model_calibrated", "target_probabilities", "sample_manifest", "roster_status",
    "matrix", "missing_fractions", "counts", "bar_interval_coverage", "request_window",
)
_CREDENTIALS = ("MASSIVE_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")


def _window(value: object) -> tuple[datetime, datetime]:
    if not isinstance(value, Mapping):
        raise CoverageError("INPUT_INVALID")
    start, end = value.get("start_utc"), value.get("end_utc")
    if not isinstance(start, str) or not isinstance(end, str):
        raise CoverageError("INPUT_INVALID")
    try:
        parsed_start, parsed_end = parse_utc(start), parse_utc(end)
    except (CoverageError, ValueError, OverflowError):
        raise CoverageError("INPUT_INVALID") from None
    if not start.endswith("Z") or not end.endswith("Z") or parsed_start >= parsed_end:
        raise CoverageError("INPUT_INVALID")
    return parsed_start, parsed_end


def _base(base: object) -> tuple[dict, str, str, str]:
    if not isinstance(base, Mapping) or not all(key in base for key in _BASE_FIELDS):
        raise CoverageError("INPUT_INVALID")
    if (type(base["schema_version"]) is not int or base["schema_version"] != 1
            or base["decision"] != "NO_TRADE"
            or base["order_approval"] is not False
            or base["model_calibrated"] is not False
            or base["target_probabilities"] != "unavailable"):
        raise CoverageError("INPUT_INVALID")
    manifest = base["sample_manifest"]
    if not isinstance(manifest, Mapping) or manifest.get("status") != "predeclared":
        raise CoverageError("INPUT_INVALID")
    issuers, dates = manifest.get("issuer_ids"), manifest.get("dates")
    if (not isinstance(issuers, list) or len(issuers) != 1
            or not isinstance(issuers[0], str) or not issuers[0].strip()
            or not isinstance(dates, list) or len(dates) != 1
            or not isinstance(dates[0], str)):
        raise CoverageError("INPUT_INVALID")
    try:
        day = date.fromisoformat(dates[0])
    except ValueError:
        raise CoverageError("INPUT_INVALID") from None
    if day.isoformat() != dates[0]:
        raise CoverageError("INPUT_INVALID")
    start, end = _window(base["request_window"])
    day_start = datetime.combine(day, datetime.min.time(), timezone.utc)
    if not (day_start <= start < end <= day_start + timedelta(days=1)):
        raise CoverageError("INPUT_INVALID")
    if (not isinstance(base["reasons"], list)
            or not all(isinstance(reason, str) for reason in base["reasons"])
            or not isinstance(base["matrix"], list)
            or not isinstance(base["roster_status"], Mapping)
            or not isinstance(base["bar_interval_coverage"], Mapping)
            or any(interval not in base["bar_interval_coverage"]
                   for interval in ("1 min", "1 hour", "1 day"))
            or not isinstance(base["missing_fractions"], Mapping)
            or not isinstance(base["counts"], Mapping)
            or base["coverage_status"] not in
            ("PROBE_COVERAGE_OBSERVED", "PROBE_COVERAGE_INCOMPLETE")):
        raise CoverageError("INPUT_INVALID")
    for row in base["matrix"]:
        if (not isinstance(row, Mapping) or row.get("issuer_id") != issuers[0]
                or row.get("date") != dates[0]):
            raise CoverageError("INPUT_INVALID")
    symbol = manifest.get("symbol")
    if (not isinstance(symbol, str) or not symbol.isascii()
            or not symbol.isalnum() or symbol != symbol.upper()):
        raise CoverageError("INPUT_INVALID")
    return dict(base), issuers[0], dates[0], symbol


def _roster_summary(
    value: object, day: str, symbol: str, *, fetched_live: bool
) -> dict[str, object]:
    result: dict[str, object] = {
        "provider": "massive", "status": "MISSING", "active_count": None,
        "inactive_count": None, "snapshot_date": day,
        "completeness": "UNVERIFIED_BY_PROVIDER",
        "provider_completeness": "UNVERIFIED_BY_PROVIDER",
        "dated_coverage": "UNVERIFIED",
        "unresolved_identity_count": None,
        "pagination": {
            "active": {"page_count": None, "complete": None},
            "inactive": {"page_count": None, "complete": None},
        },
        "blocking_reasons": ["MASSIVE_SOURCE_MISSING", "ROSTER_COVERAGE_UNVERIFIED"],
        "entitlement": "BASIC_TWO_YEAR_HISTORY_LIMIT",
    }
    if value is None:
        return result
    result["status"] = "INVALID"
    result["blocking_reasons"] = ["MASSIVE_SOURCE_INVALID", "ROSTER_COVERAGE_UNVERIFIED"]
    if not isinstance(value, Mapping) or set(value) != {"active", "inactive"}:
        return result
    result["blocking_reasons"] = ["ROSTER_COVERAGE_UNVERIFIED"]
    counts = {}
    unresolved = 0
    identity_count_valid = True
    structural_valid = True
    pagination_valid = True
    for label, active in (("active", True), ("inactive", False)):
        snapshot = value[label]
        if not isinstance(snapshot, Mapping):
            structural_valid = False
            identity_count_valid = False
            pagination_valid = False
            continue
        records = snapshot.get("records")
        if not isinstance(records, list):
            structural_valid = False
            identity_count_valid = False
            pagination_valid = False
            continue
        for record in records:
            if not isinstance(record, Mapping) or (
                record.get("identity_status") != "verified"
                or not (record.get("cik") or record.get("composite_figi"))):
                unresolved += 1
        page_count = snapshot.get("page_count")
        complete = snapshot.get("pagination_complete")
        if (type(page_count) is not int or page_count < 1 or complete is not True
                or snapshot.get("next_url")):
            pagination_valid = False
        if (snapshot.get("source") != "massive"
                or snapshot.get("snapshot_date") != day
                or active and len(records) != 1):
            structural_valid = False
        for record in records:
            if (not isinstance(record, Mapping) or record.get("source") != "massive"
                    or record.get("snapshot_date") != day
                    or record.get("active") is not active
                    or not isinstance(record.get("ticker"), str)
                    or record["ticker"] != symbol):
                structural_valid = False
        counts[label] = len(records)
        result["pagination"][label] = {
            "page_count": page_count if fetched_live and type(page_count) is int
            and page_count >= 1 else None,
            "complete": complete if fetched_live and complete is True else None,
        }
    result["unresolved_identity_count"] = (
        unresolved if fetched_live and identity_count_valid else None
    )
    result["active_count"] = counts.get("active") if fetched_live else None
    result["inactive_count"] = counts.get("inactive") if fetched_live else None
    if not fetched_live:
        pagination_valid = False
    if unresolved or not identity_count_valid:
        result["blocking_reasons"].append("MASSIVE_IDENTITY_UNRESOLVED")
    if not pagination_valid:
        result["blocking_reasons"].append("MASSIVE_PAGINATION_UNVERIFIED")
    if counts.get("active") and counts.get("inactive"):
        structural_valid = False
    if not structural_valid:
        return result
    if not fetched_live:
        result.update({
            "status": "SAVED_EVIDENCE_UNVERIFIED",
            "blocking_reasons": sorted(set(result["blocking_reasons"])
                                       | {"MASSIVE_SAVED_EVIDENCE_UNVERIFIED"}),
        })
        return result
    if pagination_valid:
        result["dated_coverage"] = "DATED_FILTER_RESPONSE_OBSERVED"
    result.update({
        "status": "DATED_ROSTER_OBSERVED" if pagination_valid else "PAGINATION_UNVERIFIED",
        "blocking_reasons": sorted(set(result["blocking_reasons"])),
    })
    return result


def _news_summary(value: object, start: str, end: str, symbol: str) -> dict[str, object]:
    result: dict[str, object] = {
        "provider": "alpaca_news", "status": "MISSING", "article_count": None,
        "completeness": "UNVERIFIED_BY_PROVIDER",
        "pagination": {"page_count": None, "complete": None},
        "blocking_reasons": ["ALPACA_NEWS_MISSING", "NEWS_COVERAGE_UNVERIFIED"],
    }
    if not isinstance(value, Mapping):
        if value is not None:
            result["status"] = "INVALID"
            result["blocking_reasons"] = ["ALPACA_NEWS_INVALID", "NEWS_COVERAGE_UNVERIFIED"]
        return result
    articles = value.get("articles")
    count = value.get("article_count")
    if (value.get("request_start") != start or value.get("request_end") != end
            or value.get("symbol") != symbol
            or value.get("completeness") != "UNVERIFIED_BY_PROVIDER"
            or type(count) is not int or count < 0 or not isinstance(articles, list)
            or len(articles) != count):
        result["status"] = "INVALID"
        result["blocking_reasons"] = ["ALPACA_NEWS_INVALID", "NEWS_COVERAGE_UNVERIFIED"]
        return result
    seen = set()
    for article in articles:
        if not isinstance(article, Mapping) or article.get("data_source") != "alpaca_news":
            result["status"] = "INVALID"
            result["blocking_reasons"] = ["ALPACA_NEWS_INVALID", "NEWS_COVERAGE_UNVERIFIED"]
            return result
        identity = article.get("id")
        if (isinstance(identity, bool) or not isinstance(identity, (str, int))
                or not identity or identity in seen
                or article.get("request_start") != start
                or article.get("request_end") != end
                or not isinstance(article.get("symbols"), list)
                or symbol not in article["symbols"]):
            result["status"] = "INVALID"
            result["blocking_reasons"] = ["ALPACA_NEWS_INVALID", "NEWS_COVERAGE_UNVERIFIED"]
            return result
        seen.add(identity)
        try:
            published = parse_utc(article["created_at"])
            fetched = parse_utc(article["fetched_at"])
            lower, upper = parse_utc(start), parse_utc(end)
        except (KeyError, CoverageError, TypeError, ValueError, OverflowError):
            result["status"] = "INVALID"
            result["blocking_reasons"] = ["ALPACA_NEWS_INVALID", "NEWS_COVERAGE_UNVERIFIED"]
            return result
        if not lower <= published < upper or published > fetched:
            result["status"] = "INVALID"
            result["blocking_reasons"] = ["ALPACA_NEWS_INVALID", "NEWS_COVERAGE_UNVERIFIED"]
            return result
    result["article_count"] = count
    result["status"] = "ARTICLES_OBSERVED" if count else "EMPTY"
    result["blocking_reasons"] = ["NEWS_COVERAGE_UNVERIFIED"]
    return result


def extend_coverage(
    base_report: Mapping, *, roster: object, alpaca_news: object, cap_evidence: object,
    massive_fetched_live: bool = False,
) -> dict[str, object]:
    """Project bounded source observations onto a validated, fail-closed IBKR report."""
    base, _, day, symbol = _base(base_report)
    window = base["request_window"]
    massive = _roster_summary(roster, day, symbol, fetched_live=massive_fetched_live)
    news = _news_summary(alpaca_news, window["start_utc"], window["end_utc"], symbol)
    reasons = set(base["reasons"])
    reasons.add("MARKET_CAP_UNVERIFIED")
    reasons.add("ROSTER_COVERAGE_UNVERIFIED")
    reasons.update(massive["blocking_reasons"])
    reasons.update(news["blocking_reasons"])
    if news["status"] != "ARTICLES_OBSERVED":
        reasons.add("NEWS_COVERAGE_UNVERIFIED")
    # JSON-supplied cap evidence cannot carry an audited-source witness.
    market_cap = {"status": "MARKET_CAP_UNVERIFIED",
                  "reason": "NO_AUDITED_FILED_SHARE_SERIES",
                  "blocking_reasons": ["MARKET_CAP_UNVERIFIED"]}
    matrix = [{
        **row,
        "massive_roster": massive,
        "alpaca_news": news,
        "market_cap_gate": market_cap,
        "blocking_reasons": sorted(set(massive["blocking_reasons"])
                                   | set(news["blocking_reasons"])
                                   | set(market_cap["blocking_reasons"])),
    } for row in base["matrix"]]
    return {**{key: base[key] for key in _BASE_FIELDS},
            "matrix": matrix,
            "reasons": sorted(reasons), "massive_roster": massive,
            "alpaca_news": news,
            "market_cap": market_cap}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CoverageError("INPUT_INVALID")


def _page_cap(value: str) -> int:
    if value not in ("1", "2"):
        raise argparse.ArgumentTypeError("page cap must be 1 or 2")
    return int(value)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Read-only micro-cap source feasibility; always NO_TRADE")
    parser.add_argument("--base-report", required=True, help="absolute external IBKR report JSON")
    parser.add_argument("--sample-manifest", required=True, help="absolute predeclared manifest JSON")
    parser.add_argument("--start", required=True, help="inclusive UTC within one declared date")
    parser.add_argument("--end", required=True, help="exclusive UTC within that date")
    parser.add_argument("--roster-evidence", help="absolute external saved Massive snapshots JSON")
    parser.add_argument("--news-evidence", help="absolute external saved Alpaca news JSON")
    parser.add_argument("--cap-evidence", help="absolute external share evidence JSON; never audited")
    parser.add_argument("--fetch", action="store_true", help="opt in to read-only Massive and Alpaca calls")
    parser.add_argument("--max-pages", type=_page_cap, default=2,
                        help="maximum Massive pages per active filter and Alpaca news pages (1 or 2; default 2)")
    return parser


def _load(path: str) -> object:
    if not Path(path).is_absolute():
        raise CoverageError("INPUT_INVALID")
    try:
        repository = Path(__file__).resolve().parent
        if (Path(os.path.abspath(path)).is_relative_to(repository)
                or Path(path).resolve().is_relative_to(repository)):
            raise CoverageError("INPUT_INVALID")
        with open(path, encoding="utf-8") as source:
            return json.load(source)
    except (OSError, UnicodeError, ValueError, RecursionError, RuntimeError):
        raise CoverageError("INPUT_INVALID") from None


_ALPACA_DENIED = re.compile(r"(?:NEWS_COVERAGE_UNAVAILABLE: )?HTTP_ERROR: status 40[13] ")


def _secret_present(value: object) -> bool:
    payload = json.dumps(value, ensure_ascii=False)
    return any(secret and secret in payload
               for name in _CREDENTIALS if (secret := os.environ.get(name, "").strip()))


def _error(code: int, *, denied: str | None = None) -> int:
    messages = (
        ({"error": "INPUT_INVALID"}, {"failure": "INVALID_INPUT"}, {"fail": 2})
        if code == 2 else
        ({"error": denied or "PROVIDER_UNAVAILABLE"}, {"failure": "PROVIDER_ERROR"},
         {"fail": 3})
    )
    for message in messages:
        if not _secret_present(message):
            print(json.dumps(message, separators=(",", ":")), file=sys.stderr)
            break
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        manifest, base = _load(args.sample_manifest), _load(args.base_report)
        if not isinstance(manifest, Mapping):
            raise CoverageError("INPUT_INVALID")
        _, issuer, day, symbol = _base(base)
        if (manifest.get("status") != "predeclared"
                or manifest.get("issuer_ids") != [issuer]
                or manifest.get("dates") != [day]
                or manifest.get("symbol") != symbol
                or base["request_window"] != {"start_utc": args.start, "end_utc": args.end}):
            raise CoverageError("INPUT_INVALID")
        if _secret_present(manifest) or _secret_present(base):
            raise CoverageError("INPUT_INVALID")
        if args.fetch and any((args.roster_evidence, args.news_evidence, args.cap_evidence)):
            raise CoverageError("INPUT_INVALID")
        roster = _load(args.roster_evidence) if args.roster_evidence else None
        news = _load(args.news_evidence) if args.news_evidence else None
        cap = _load(args.cap_evidence) if args.cap_evidence else None
        if any(_secret_present(value) for value in (roster, news, cap)):
            raise CoverageError("INPUT_INVALID")
    except CoverageError:
        return _error(2)
    if args.fetch:
        try:
            client = MassiveRoster.from_environment()
            roster = {
                "active": client.fetch_snapshot(day, active=True, ticker=symbol,
                                                max_pages=args.max_pages),
                "inactive": client.fetch_snapshot(day, active=False, ticker=symbol,
                                                  max_pages=args.max_pages),
            }
        except CoverageError as error:
            return _error(3, denied="ROSTER_ACCESS_DENIED" if "HTTP 403" in str(error)
                          else None)
        except (OSError, ValueError):
            return _error(3)
        try:
            news = observed_news(AlpacaHistory.from_environment(), symbol, args.start, args.end,
                                 max_pages=args.max_pages)
        except CoverageError as error:
            if "PAGINATION_TRUNCATED" in str(error):
                return _error(3, denied="NEWS_PAGE_CAP_EXCEEDED")
            return _error(3, denied="NEWS_ACCESS_DENIED" if _ALPACA_DENIED.match(str(error))
                          else None)
        except (OSError, ValueError):
            return _error(3)
    result = extend_coverage(base, roster=roster, alpaca_news=news, cap_evidence=cap,
                             massive_fetched_live=args.fetch)
    if _secret_present(result):
        return _error(2)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
