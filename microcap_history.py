"""Fail-closed access to Alpaca historical bars and news."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from numbers import Real
from typing import Mapping, Sequence, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


_BARS_ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
_NEWS_ENDPOINT = "https://data.alpaca.markets/v1beta1/news"
_BAR_FIELDS = ("o", "h", "l", "c", "v", "t")
_NEWS_FIELDS = ("id", "created_at", "headline", "source", "symbols", "summary")


class CoverageError(RuntimeError):
    """Raised when historical data cannot be safely validated or fetched."""


class BarRecord(TypedDict):
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
class CoverageReport:
    """Observed records only; this does not claim contiguous session coverage."""

    bar_timestamps: tuple[datetime, ...]
    news_article_count: int


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
        raw_records = self._pages(_BARS_ENDPOINT, params, "bars")
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
    ) -> CoverageReport:
        """Require observations in-range; never infer full session coverage from sparse bars."""
        start_utc, end_utc = _period(start, end)
        if not bars:
            raise CoverageError("BAR_COVERAGE_UNVERIFIED: no historical bars were returned")
        if not news:
            raise CoverageError(
                "NEWS_COVERAGE_UNVERIFIED: an empty news response cannot establish "
                "historical coverage or the absence of a catalyst"
            )
        bar_timestamps: list[datetime] = []
        for bar_record in bars:
            timestamp_value = bar_record.get("t")
            if not isinstance(timestamp_value, str):
                raise CoverageError("BAR_INVALID: required timestamp field 't' is missing")
            timestamp = parse_utc(timestamp_value)
            if not start_utc <= timestamp < end_utc:
                raise CoverageError("BAR_OUT_OF_RANGE: returned bar is outside the requested range")
            bar_timestamps.append(timestamp)
        for article_record in news:
            timestamp_value = article_record.get("created_at")
            if "id" not in article_record or not isinstance(timestamp_value, str):
                raise CoverageError("NEWS_INVALID: required article ID or created_at is missing")
            timestamp = parse_utc(timestamp_value)
            if not start_utc <= timestamp < end_utc:
                raise CoverageError("NEWS_OUT_OF_RANGE: returned article is outside the requested range")
        return CoverageReport(tuple(bar_timestamps), len(news))

    def _pages(
        self, endpoint: str, params: Mapping[str, str | int], collection: str
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
            if not isinstance(page, list):
                raise CoverageError(
                    f"RESPONSE_INVALID: {urlsplit(endpoint).path} omitted the {collection!r} list"
                )
            for record in page:
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
