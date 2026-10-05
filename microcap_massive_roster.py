"""Fail-closed, read-only Massive reference-ticker snapshots."""

from __future__ import annotations

import http.client
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date as calendar_date
from datetime import datetime, timezone
from typing import cast

from microcap_history import CoverageError, parse_utc


_ENDPOINT = "https://api.massive.com/v3/reference/tickers"
_HOST = "api.massive.com"
_PATH = "/v3/reference/tickers"
_MAX_PAGES = 10
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TIMEOUT_SECONDS = 15.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not replay a credential-bearing request through redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def urlopen(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _unverified(detail: str) -> CoverageError:
    return CoverageError(f"ROSTER_COVERAGE_UNVERIFIED: {detail}")


def _params_invalid(detail: str) -> CoverageError:
    return CoverageError(f"ROSTER_PARAMS_INVALID: {detail}")


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _unverified("clock must return a timezone-aware timestamp")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _pagination_key(url: str) -> tuple[object, ...]:
    parsed = urllib.parse.urlsplit(url)
    query = tuple(sorted(
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() != "apikey"
    ))
    return (parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.path, query)


def _trusted_next_url(
    value: object, credential: str, expected_filters: Mapping[str, str]
) -> str:
    if not isinstance(value, str) or not value:
        raise _unverified("pagination URL is malformed")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _unverified("pagination URL is malformed") from None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != _HOST
        or parsed.path != _PATH
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise _unverified("pagination URL is outside the trusted endpoint")

    cursor: str | None = None
    seen: set[str] = set()
    for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        name = key.casefold()
        if name in seen:
            raise _unverified("pagination URL filters are invalid")
        seen.add(name)
        if name == "apikey":
            continue
        if key in expected_filters:
            if item != expected_filters[key]:
                raise _unverified("pagination URL filters are invalid")
        elif key == "cursor" and item:
            cursor = item
        else:
            raise _unverified("pagination URL filters are invalid")
    if cursor is None:
        raise _unverified("pagination URL filters are invalid")
    query = list(expected_filters.items())
    query.append(("cursor", cursor))
    query.append(("apiKey", credential))
    return urllib.parse.urlunsplit((
        "https",
        _HOST,
        _PATH,
        urllib.parse.urlencode(query),
        "",
    ))


def _read_json(response: object) -> object:
    reader = getattr(response, "read", None)
    if not callable(reader):
        raise _unverified("response body is unreadable")
    try:
        body = reader(_MAX_RESPONSE_BYTES + 1)
    except (http.client.HTTPException, OSError, ValueError):
        raise _unverified("response body could not be read") from None
    if not isinstance(body, bytes) or len(body) > _MAX_RESPONSE_BYTES:
        raise _unverified("response body is truncated or exceeds the size limit")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise _unverified("response body is not valid JSON") from None


def _required_text(record: Mapping[str, object], field: str, index: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _unverified(f"result {index} has invalid {field}")
    return value


def _normalize_result(
    raw: object,
    *,
    index: int,
    requested_active: bool,
    requested_ticker: str | None,
    snapshot_date: str,
    fetched_at: str,
) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise _unverified(f"result {index} is not an object")

    ticker = _required_text(raw, "ticker", index)
    name = _required_text(raw, "name", index)
    active = raw.get("active")
    market = raw.get("market")
    locale = raw.get("locale")
    if not isinstance(active, bool) or active is not requested_active:
        raise _unverified(f"result {index} conflicts with the requested active filter")
    if market != "stocks" or locale != "us":
        raise _unverified(f"result {index} conflicts with the requested market or locale")
    if ticker != ticker.strip() or (
        requested_ticker is not None and ticker.casefold() != requested_ticker.casefold()
    ):
        raise _unverified(f"result {index} conflicts with the requested ticker")

    identifiers: dict[str, str | None] = {}
    for field in ("cik", "composite_figi"):
        value = raw.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _unverified(f"result {index} has invalid {field}")
        identifiers[field] = value

    optional_text: dict[str, str | None] = {}
    for field in ("primary_exchange", "type"):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise _unverified(f"result {index} has invalid {field}")
        optional_text[field] = value

    delisted = raw.get("delisted_utc")
    if delisted is not None:
        if not isinstance(delisted, str):
            raise _unverified(f"result {index} has invalid delisted_utc")
        try:
            if "T" in delisted:
                parse_utc(delisted)
            else:
                calendar_date.fromisoformat(delisted)
        except (CoverageError, ValueError, OverflowError):
            raise _unverified(f"result {index} has invalid delisted_utc") from None

    return {
        "ticker": ticker,
        "active": active,
        "name": name,
        "cik": identifiers["cik"],
        "composite_figi": identifiers["composite_figi"],
        "primary_exchange": optional_text["primary_exchange"],
        "type": optional_text["type"],
        "delisted_utc": delisted,
        "snapshot_date": snapshot_date,
        "fetched_at": fetched_at,
        "source": "massive",
        "identity_status": "verified",
    }


def _mark_ambiguous_identities(records: list[dict[str, object]]) -> None:
    by_ticker: dict[str, list[dict[str, object]]] = {}
    for record in records:
        by_ticker.setdefault(cast(str, record["ticker"]).casefold(), []).append(record)

    for same_ticker in by_ticker.values():
        ambiguous = False
        for index, record in enumerate(same_ticker):
            for other in same_ticker[index + 1:]:
                shared_identifiers = [
                    field for field in ("cik", "composite_figi")
                    if record[field] is not None and other[field] is not None
                ]
                if not shared_identifiers or any(
                    record[field] != other[field] for field in shared_identifiers
                ):
                    ambiguous = True
        for record in same_ticker:
            if ambiguous or (record["cik"] is None and record["composite_figi"] is None):
                record["identity_status"] = "unverified"


class MassiveRoster:
    """Read-only Massive ticker snapshot client; credentials are kept private."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = _TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise CoverageError("MASSIVE_KEY_MISSING")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise _params_invalid("timeout must be within (0, 60] seconds")
        self.__api_key = api_key.strip()
        self._timeout = float(timeout)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"MassiveRoster(endpoint={_ENDPOINT!r}, api_key=<redacted>)"

    __str__ = __repr__

    @classmethod
    def from_environment(cls) -> MassiveRoster:
        api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
        if not api_key:
            raise CoverageError("MASSIVE_KEY_MISSING")
        return cls(api_key)

    def fetch_snapshot(
        self,
        date: str,
        *,
        active: bool,
        ticker: str | None = None,
        max_pages: int = 2,
    ) -> dict[str, object]:
        if not isinstance(date, str):
            raise _params_invalid("date must be an ISO calendar date")
        try:
            parsed_date = calendar_date.fromisoformat(date)
        except (ValueError, OverflowError):
            raise _params_invalid("date must be an ISO calendar date") from None
        if parsed_date.isoformat() != date:
            raise _params_invalid("date must be an ISO calendar date")
        if not isinstance(active, bool):
            raise _params_invalid("active must be a boolean")
        if ticker is not None and (
            not isinstance(ticker, str) or not ticker or ticker != ticker.strip()
        ):
            raise _params_invalid("ticker must be a non-empty trimmed string")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= _MAX_PAGES
        ):
            raise _params_invalid(f"max_pages must be between 1 and {_MAX_PAGES}")

        fetched_at = _timestamp_text(self._clock())
        query = [
            ("market", "stocks"),
            ("locale", "us"),
            ("date", parsed_date.isoformat()),
            ("active", str(active).lower()),
            ("limit", "1000"),
        ]
        if ticker is not None:
            query.append(("ticker", ticker))
        expected_filters = dict(query)
        query.append(("apiKey", self.__api_key))
        current_url = f"{_ENDPOINT}?{urllib.parse.urlencode(query)}"

        records: list[dict[str, object]] = []
        visited: set[tuple[object, ...]] = set()
        page_count = 0
        for page_index in range(max_pages):
            page_key = _pagination_key(current_url)
            if page_key in visited:
                raise _unverified("pagination URL repeated")
            visited.add(page_key)

            request = urllib.request.Request(
                current_url,
                headers={"Accept": "application/json"},
                method="GET",
            )
            try:
                with urlopen(request, timeout=self._timeout) as response:
                    document = _read_json(response)
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    raise CoverageError("ROSTER_RATE_LIMITED") from None
                raise _unverified(f"provider returned HTTP {error.code}") from None
            except urllib.error.URLError:
                raise _unverified("provider request failed") from None
            except (http.client.HTTPException, TimeoutError, OSError):
                raise _unverified("provider request timed out or failed") from None

            if not isinstance(document, Mapping):
                raise _unverified("response must be an object")
            if document.get("status") != "OK" or not isinstance(document.get("results"), list):
                raise _unverified("response status or results are missing or invalid")
            page_count += 1
            page_results = cast(list[object], document["results"])
            count = document.get("count")
            if (
                count is not None
                and (isinstance(count, bool) or not isinstance(count, int) or count != len(page_results))
            ):
                raise _unverified("response count does not match its results")

            page_records = [
                _normalize_result(
                    result,
                    index=len(records) + index,
                    requested_active=active,
                    requested_ticker=ticker,
                    snapshot_date=parsed_date.isoformat(),
                    fetched_at=fetched_at,
                )
                for index, result in enumerate(page_results)
            ]
            records.extend(page_records)

            next_value = document.get("next_url")
            if next_value is None:
                next_url = None
            else:
                next_url = _trusted_next_url(next_value, self.__api_key, expected_filters)
                if _pagination_key(next_url) in visited:
                    raise _unverified("pagination URL repeated")
            if next_url is None:
                break
            if page_index == max_pages - 1:
                raise _unverified("maximum page count reached before pagination completed")
            current_url = next_url
        else:
            raise _unverified("maximum page count reached before pagination completed")

        _mark_ambiguous_identities(records)
        return {
            "snapshot_date": parsed_date.isoformat(),
            "fetched_at": fetched_at,
            "source": "massive",
            "page_count": page_count,
            "pagination_complete": True,
            "records": records,
        }
