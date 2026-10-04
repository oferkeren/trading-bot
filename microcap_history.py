"""Fail-closed access to Alpaca historical bars and news."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.message import Message
from numbers import Real
from typing import Mapping, Sequence, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


_BARS_ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
_NEWS_ENDPOINT = "https://data.alpaca.markets/v1beta1/news"
_BAR_FIELDS = ("o", "h", "l", "c", "v", "t")
_NEWS_FIELDS = ("id", "created_at", "headline", "source", "symbols", "summary")
_MARKET_TZ = ZoneInfo("America/New_York")


class CoverageError(RuntimeError):
    """Raised when historical data cannot be safely validated or fetched."""


class BarRecord(TypedDict):
    symbol: str
    t: str
    o: Real
    h: Real
    l: Real
    c: Real
    v: Real
    request_feed: str
    request_adjustment: str
    fetched_at: str


class NewsRecord(TypedDict):
    id: str | int
    created_at: str
    headline: str
    source: str
    symbols: list[str]
    summary: str
    fetched_at: str
    request_start: str
    request_end: str


@dataclass(frozen=True)
class MissingMinutes:
    """Unobserved timestamp slots strictly between observed bars on one NY day."""

    symbol: str
    day: date
    timeframe: str
    timestamps: tuple[datetime, ...]


@dataclass(frozen=True)
class UnobservedSession:
    """Weekday with no observed bars; holidays and zero-trade IEX days are possible."""

    symbol: str
    day: date
    timeframe: str


@dataclass(frozen=True)
class CoverageReport:
    """Observations and candidate gaps, not proof of contiguous exchange coverage."""

    bar_timestamps: tuple[datetime, ...]
    news_article_count: int
    missing_minutes: tuple[MissingMinutes, ...] = ()
    unobserved_sessions: tuple[UnobservedSession, ...] = ()


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp with an explicit timezone and normalize to UTC."""
    if not isinstance(value, str):
        raise CoverageError("TIMESTAMP_INVALID: timestamp must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise CoverageError("TIMESTAMP_INVALID: malformed ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CoverageError("TIMESTAMP_INVALID: timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _period(start: str, end: str) -> tuple[datetime, datetime]:
    start_utc = parse_utc(start)
    end_utc = parse_utc(end)
    if start_utc >= end_utc:
        raise CoverageError("RANGE_INVALID: start must be earlier than end")
    return start_utc, end_utc


def _symbols_query(symbols: Sequence[str]) -> str:
    if not symbols or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
        raise CoverageError("SYMBOLS_INVALID: at least one non-empty symbol is required")
    return ",".join(symbols)


class AlpacaHistory:
    """Fetch split-adjusted IEX bars and historical Alpaca news."""

    def __init__(self, key_id: str, secret_key: str) -> None:
        if not key_id or not secret_key:
            raise CoverageError("CREDENTIALS_MISSING: Alpaca key ID and secret are both required")
        self._key_id = key_id
        self._secret_key = secret_key

    @classmethod
    def from_environment(cls) -> AlpacaHistory:
        key_id = os.environ.get("APCA_API_KEY_ID")
        secret_key = os.environ.get("APCA_API_SECRET_KEY")
        if not key_id or not secret_key:
            raise CoverageError(
                "CREDENTIALS_MISSING: APCA_API_KEY_ID and APCA_API_SECRET_KEY are required"
            )
        return cls(key_id, secret_key)

    def fetch_bars(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        *,
        timeframe: str = "1Min",
        feed: str = "iex",
    ) -> list[BarRecord]:
        """Fetch validated bars with explicit feed, adjustment, and fetch-time provenance."""
        if feed != "iex":
            raise CoverageError(f"FEED_UNSUPPORTED: only the iex feed is supported, not {feed!r}")
        if not timeframe:
            raise CoverageError("TIMEFRAME_INVALID: timeframe must not be empty")
        start_utc, end_utc = _period(start, end)
        symbol_query = _symbols_query(symbols)
        params: dict[str, str | int] = {
            "symbols": symbol_query,
            "start": _format_utc(start_utc),
            "end": _format_utc(end_utc),
            "timeframe": timeframe,
            "adjustment": "split",
            "feed": feed,
            "limit": 10000,
        }
        raw_records = self._pages(_BARS_ENDPOINT, params, "bars", symbols=symbols)
        fetched_at = _format_utc(datetime.now(timezone.utc))
        records: list[BarRecord] = []
        for raw in raw_records:
            records.append(
                self._validate_bar(raw, start_utc, end_utc, feed, fetched_at)
            )
        return records

    def fetch_news(
        self, symbols: Sequence[str], start: str, end: str
    ) -> list[NewsRecord]:
        """Fetch validated news articles, deduplicated by article ID."""
        start_utc, end_utc = _period(start, end)
        symbol_query = _symbols_query(symbols)
        params: dict[str, str | int] = {
            "symbols": symbol_query,
            "start": _format_utc(start_utc),
            "end": _format_utc(end_utc),
            "limit": 50,
        }
        raw_records = self._pages(_NEWS_ENDPOINT, params, "news")
        fetched_at = _format_utc(datetime.now(timezone.utc))
        records: list[NewsRecord] = []
        seen_ids: set[str | int] = set()
        for raw in raw_records:
            record = self._validate_news(raw, start_utc, end_utc, fetched_at)
            if record["id"] in seen_ids:
                continue
            seen_ids.add(record["id"])
            records.append(record)
        return records

    @staticmethod
    def require_coverage(
        bars: Sequence[Mapping[str, object]],
        news: Sequence[Mapping[str, object]],
        start: str,
        end: str,
        *,
        symbols: Sequence[str] | None = None,
        timeframe: str = "1Min",
    ) -> CoverageReport:
        """Report candidate gaps, never equating absent IEX trades with missing data.

        Minute slots are counted only between observed bars on the same NY day.
        Unobserved sessions are weekdays whose regular hours intersect the range,
        not confirmed exchange sessions (holidays are not excluded).
        """
        start_utc, end_utc = _period(start, end)
        if not timeframe:
            raise CoverageError("TIMEFRAME_INVALID: timeframe must not be empty")
        requested = set(symbols) if symbols is not None else None
        if symbols is not None:
            _symbols_query(symbols)
        if not bars:
            raise CoverageError("BAR_COVERAGE_UNVERIFIED: no historical bars were returned")
        if not news:
            raise CoverageError(
                "NEWS_COVERAGE_UNVERIFIED: an empty news response cannot establish "
                "historical coverage or the absence of a catalyst"
            )
        bar_timestamps: list[datetime] = []
        by_symbol_day: dict[tuple[str, date], set[datetime]] = {}
        for bar_record in bars:
            timestamp_value = bar_record.get("t")
            if not isinstance(timestamp_value, str):
                raise CoverageError("BAR_INVALID: required timestamp field 't' is missing")
            timestamp = parse_utc(timestamp_value)
            if not start_utc <= timestamp < end_utc:
                raise CoverageError("BAR_OUT_OF_RANGE: returned bar is outside the requested range")
            symbol = bar_record.get("symbol")
            if not isinstance(symbol, str) or not symbol or (requested is not None and symbol not in requested):
                raise CoverageError("BAR_INVALID: symbol must be one of the requested symbols")
            by_symbol_day.setdefault((symbol, timestamp.astimezone(_MARKET_TZ).date()), set()).add(timestamp)
            bar_timestamps.append(timestamp)
        for article_record in news:
            timestamp_value = article_record.get("created_at")
            if "id" not in article_record or not isinstance(timestamp_value, str):
                raise CoverageError("NEWS_INVALID: required article ID or created_at is missing")
            timestamp = parse_utc(timestamp_value)
            if not start_utc <= timestamp < end_utc:
                raise CoverageError("NEWS_OUT_OF_RANGE: returned article is outside the requested range")
        step_match = re.fullmatch(r"([1-9]\d*)Min", timeframe)
        missing_minutes: list[MissingMinutes] = []
        if step_match:
            step = timedelta(minutes=int(step_match.group(1)))
            for (symbol, day), stamps in sorted(by_symbol_day.items()):
                gaps: list[datetime] = []
                ordered = sorted(stamps)
                for previous, following in zip(ordered, ordered[1:]):
                    slot = previous + step
                    while slot < following:
                        gaps.append(slot)
                        slot += step
                if gaps:
                    missing_minutes.append(MissingMinutes(symbol, day, timeframe, tuple(gaps)))

        unobserved_sessions: list[UnobservedSession] = []
        last_day = end_utc.astimezone(_MARKET_TZ).date()
        for symbol in sorted(requested if requested is not None else {key[0] for key in by_symbol_day}):
            day = start_utc.astimezone(_MARKET_TZ).date()
            while day <= last_day:
                session_open = datetime.combine(day, time(9, 30), _MARKET_TZ).astimezone(timezone.utc)
                session_close = datetime.combine(day, time(16), _MARKET_TZ).astimezone(timezone.utc)
                if (day.weekday() < 5 and start_utc < session_close and end_utc > session_open
                        and (symbol, day) not in by_symbol_day):
                    unobserved_sessions.append(UnobservedSession(symbol, day, timeframe))
                day += timedelta(days=1)
        return CoverageReport(tuple(bar_timestamps), len(news), tuple(missing_minutes),
                              tuple(unobserved_sessions))

    def _pages(
        self, endpoint: str, params: Mapping[str, str | int], collection: str,
        *, symbols: Sequence[str] | None = None,
    ) -> list[Mapping[str, object]]:
        records: list[Mapping[str, object]] = []
        next_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            query = dict(params)
            if next_token is not None:
                query["page_token"] = next_token
            request = Request(
                f"{endpoint}?{urlencode(query)}",
                headers={
                    "APCA-API-KEY-ID": self._key_id,
                    "APCA-API-SECRET-KEY": self._secret_key,
                    "Accept": "application/json",
                },
            )
            payload = self._open_json(request, endpoint)
            if not isinstance(payload, dict):
                raise CoverageError(f"RESPONSE_INVALID: {urlsplit(endpoint).path} returned a non-object")
            page = payload.get(collection)
            if collection == "bars":
                if not isinstance(page, dict):
                    raise CoverageError("RESPONSE_INVALID: bars must be a symbol-to-list object")
                page_records: list[object] = []
                for symbol, group in page.items():
                    if (not isinstance(symbol, str) or not symbol or symbols is None
                            or symbol not in symbols or not isinstance(group, list)):
                        raise CoverageError("RESPONSE_INVALID: invalid bar symbol or group")
                    for record in group:
                        if not isinstance(record, dict) or ("symbol" in record and record["symbol"] != symbol):
                            raise CoverageError("RESPONSE_INVALID: invalid bar record or symbol")
                        page_records.append({**record, "symbol": symbol})
            elif isinstance(page, list):
                page_records = page
            else:
                raise CoverageError(
                    f"RESPONSE_INVALID: {urlsplit(endpoint).path} omitted the {collection!r} list"
                )
            for record in page_records:
                if not isinstance(record, dict):
                    raise CoverageError(
                        f"RESPONSE_INVALID: {urlsplit(endpoint).path} returned a non-object record"
                    )
                records.append(record)
            token = payload.get("next_page_token")
            if token is None or token == "":
                return records
            if not isinstance(token, str):
                raise CoverageError(f"PAGINATION_INVALID: {urlsplit(endpoint).path} token is not a string")
            if token in seen_tokens:
                raise CoverageError(f"PAGINATION_REPEAT: repeated page token from {urlsplit(endpoint).path}")
            seen_tokens.add(token)
            next_token = token

    @staticmethod
    def _open_json(request: Request, endpoint: str) -> object:
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
        except HTTPError as error:
            retry_after = error.headers.get("Retry-After") if error.headers else None
            retry_detail = f", Retry-After: {retry_after}" if error.code == 429 and retry_after else ""
            raise CoverageError(
                f"HTTP_ERROR: status {error.code} from {urlsplit(endpoint).path} "
                f"(HTTPError{retry_detail})"
            ) from None
        except URLError as error:
            raise CoverageError(
                f"NETWORK_ERROR: {urlsplit(endpoint).path} ({type(error).__name__})"
            ) from None
        try:
            return json.loads(body.decode("utf-8"))
        except UnicodeDecodeError:
            raise CoverageError(f"RESPONSE_INVALID: {urlsplit(endpoint).path} is not UTF-8") from None
        except json.JSONDecodeError:
            raise CoverageError(f"RESPONSE_INVALID: {urlsplit(endpoint).path} is not valid JSON") from None

    @staticmethod
    def _validate_bar(
        raw: Mapping[str, object],
        start: datetime,
        end: datetime,
        feed: str,
        fetched_at: str,
    ) -> BarRecord:
        missing = [field for field in _BAR_FIELDS if field not in raw]
        if missing:
            raise CoverageError(f"BAR_INVALID: missing required fields {', '.join(missing)}")
        timestamp_value = raw["t"]
        if not isinstance(timestamp_value, str):
            raise CoverageError("BAR_INVALID: timestamp field 't' must be a string")
        timestamp = parse_utc(timestamp_value)
        if not start <= timestamp < end:
            raise CoverageError("BAR_OUT_OF_RANGE: returned bar is outside the requested range")
        values: dict[str, Real] = {}
        for field in ("o", "h", "l", "c", "v"):
            value = raw[field]
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
                raise CoverageError(f"BAR_INVALID: field {field!r} must be a finite number")
            values[field] = value
        return {
            "symbol": raw["symbol"],
            "t": _format_utc(timestamp),
            "o": values["o"],
            "h": values["h"],
            "l": values["l"],
            "c": values["c"],
            "v": values["v"],
            "request_feed": feed,
            "request_adjustment": "split",
            "fetched_at": fetched_at,
        }

    @staticmethod
    def _validate_news(
        raw: Mapping[str, object], start: datetime, end: datetime, fetched_at: str
    ) -> NewsRecord:
        missing = [field for field in _NEWS_FIELDS if field not in raw]
        if missing:
            raise CoverageError(f"NEWS_INVALID: missing required fields {', '.join(missing)}")
        article_id = raw["id"]
        if isinstance(article_id, bool) or not isinstance(article_id, (str, int)) or article_id == "":
            raise CoverageError("NEWS_INVALID: article id must be a non-empty string or integer")
        timestamp_value = raw["created_at"]
        if not isinstance(timestamp_value, str):
            raise CoverageError("NEWS_INVALID: field 'created_at' must be a timestamp string")
        timestamp = parse_utc(timestamp_value)
        if not start <= timestamp < end:
            raise CoverageError("NEWS_OUT_OF_RANGE: returned article is outside the requested range")
        headline = raw["headline"]
        source = raw["source"]
        summary = raw["summary"]
        if not isinstance(headline, str):
            raise CoverageError("NEWS_INVALID: field 'headline' must be a string")
        if not isinstance(source, str):
            raise CoverageError("NEWS_INVALID: field 'source' must be a string")
        if not isinstance(summary, str):
            raise CoverageError("NEWS_INVALID: field 'summary' must be a string")
        symbols = raw["symbols"]
        if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
            raise CoverageError("NEWS_INVALID: field 'symbols' must be a list of strings")
        return {
            "id": article_id,
            "created_at": _format_utc(timestamp),
            "headline": headline,
            "source": source,
            "symbols": list(symbols),
            "summary": summary,
            "fetched_at": fetched_at,
            "request_start": _format_utc(start),
            "request_end": _format_utc(end),
        }
