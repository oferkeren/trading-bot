"""Bounded, read-only Marketaux news coverage probe.

Verified against https://www.marketaux.com/documentation (fetched 2026-10-05):
``GET https://api.marketaux.com/v1/news/all`` authenticated by the ``api_token`` query
parameter; ``symbols``, ``filter_entities``, ``must_have_entities``, ``group_similar``,
``published_after``/``published_before`` (``Y-m-d\\TH:i:s``, all dates UTC) and ``page``.
``meta`` carries ``found``/``returned``/``limit``/``page``; the per-request limit is plan
dependent (so it is measured from ``meta.limit``, never assumed) and a result set cannot
exceed 20,000. Documented errors: 400 malformed_parameters, 401 invalid_api_token,
402 usage_limit_reached, 403 endpoint_access_restricted, 404 resource_not_found /
invalid_api_endpoint, 429 rate_limit_reached, 500 server_error, 503 maintenance_mode.

``timeout`` supplies a best-effort deadline across pages, measured on a monotonic clock.
The deadline is checked between blocking operations; ``urlopen`` receives the remaining
time and, when available, the socket idle timeout is set to the remaining time before
each body read. This is not a total-duration guarantee. Socket idle timeouts can restart
as bytes arrive, so a peer that slowly trickles bytes during a blocking body read (for
example, while HTTP chunk framing is read) can keep that operation blocked past the
deadline. DNS resolution can also block beyond it. Because the deadline is checked only
after blocking operations return, total duration and deadline overrun are not bounded,
including not by one configured timeout.

Results are news-coverage evidence only: an empty result is ``NEWS_COVERAGE_UNVERIFIED``
(never "no catalyst") and Marketaux is never a substitute for IBKR quotes.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone

from microcap_history import CoverageError, parse_utc


PROVIDER = "marketaux"
_TRUSTED_BASE_URL = "https://api.marketaux.com"
_TRUSTED_HOST = "api.marketaux.com"
_NEWS_PATH = "/v1/news/all"
_MAX_PAGES = 10
_MAX_ARTICLES = 500
_MAX_RESULT_SET = 20_000
_MAX_WINDOW = timedelta(days=31)
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_FALLBACK_CHUNK_BYTES = 1024
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_UTC_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_DOCUMENTED_ERROR_CODES = {
    "malformed_parameters",
    "invalid_api_token",
    "usage_limit_reached",
    "endpoint_access_restricted",
    "resource_not_found",
    "invalid_api_endpoint",
    "rate_limit_reached",
    "server_error",
    "maintenance_mode",
}
_ENTITY_FIELDS = ("symbol", "name", "exchange", "exchange_long", "country", "type", "industry")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the token-bearing URL is never replayed elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def urlopen(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


_monotonic = time.monotonic


def _remaining(deadline: float) -> float:
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise _unavailable("best-effort deadline exceeded")
    return remaining


def _set_socket_timeout(response: object, seconds: float) -> None:
    """Best effort: lower the live socket's idle timeout to the remaining budget."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        try:
            settimeout(seconds)
        except (OSError, ValueError):
            pass


def _read_bounded(response: object, limit: int, deadline: float) -> bytes:
    """Read bounded chunks, checking the best-effort deadline around blocking reads."""
    read1 = getattr(response, "read1", None)
    reader = read1 if callable(read1) else response.read
    chunk_cap = _READ_CHUNK_BYTES if callable(read1) else _FALLBACK_CHUNK_BYTES
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        _set_socket_timeout(response, _remaining(deadline))
        chunk = reader(min(chunk_cap, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        _remaining(deadline)
    return b"".join(chunks)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unavailable(detail: str) -> CoverageError:
    return CoverageError(f"NEWS_PROVIDER_UNAVAILABLE: {detail}")


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class MarketauxProbe:
    """Read-only Marketaux news probe; the API token is never exposed in output."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = _TRUSTED_BASE_URL,
        timeout: float = 15.0,
        country: str = "us",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise CoverageError("MARKETAUX_TOKEN_MISSING")
        if base_url != _TRUSTED_BASE_URL:
            raise CoverageError("PARAMS_INVALID: only https://api.marketaux.com is accepted")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= _MAX_TIMEOUT_SECONDS
        ):
            raise CoverageError("PARAMS_INVALID: timeout must be within (0, 60] seconds")
        if not isinstance(country, str) or not re.fullmatch(r"[a-z]{2}", country):
            raise CoverageError("PARAMS_INVALID: country must be a lowercase ISO code")
        self.__token = token.strip()
        self._timeout = float(timeout)
        self._country = country
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"MarketauxProbe(base_url={_TRUSTED_BASE_URL!r}, token=<redacted>)"

    __str__ = __repr__

    @classmethod
    def from_environment(cls, **kwargs: object) -> "MarketauxProbe":
        token = os.environ.get("MARKETAUX_API_TOKEN", "").strip()
        if not token:
            raise CoverageError("MARKETAUX_TOKEN_MISSING")
        return cls(token, **kwargs)

    # ------------------------------------------------------------------ HTTP
    def _request(self, query: Mapping[str, str]) -> urllib.request.Request:
        params = dict(query)
        params["api_token"] = self.__token
        url = f"{_TRUSTED_BASE_URL}{_NEWS_PATH}?{urllib.parse.urlencode(params)}"
        return urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})

    def _get_page(self, query: Mapping[str, str], deadline: float) -> dict:
        request = self._request(query)
        failure: CoverageError | None = None
        body = b""
        try:
            with urlopen(request, timeout=_remaining(deadline)) as response:
                final = urllib.parse.urlsplit(response.geturl() or "")
                if final.scheme != "https" or final.hostname != _TRUSTED_HOST:
                    failure = _unavailable("response from untrusted host refused")
                else:
                    body = _read_bounded(response, _MAX_RESPONSE_BYTES + 1, deadline)
        except CoverageError as exc:
            failure = CoverageError(str(exc))
        except urllib.error.HTTPError as exc:
            failure = self._http_failure(exc, deadline)
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ValueError,
        ) as exc:
            failure = _unavailable(f"network error ({type(exc).__name__})")
        # Raised outside the except blocks so no token-bearing exception is chained.
        if failure is not None:
            raise failure
        if len(body) > _MAX_RESPONSE_BYTES:
            raise _unavailable("response exceeds size bound")
        payload: object = None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        if not isinstance(payload, dict):
            raise _unavailable("malformed payload (not a JSON object)")
        if "error" in payload:
            code = self._error_code(payload)
            safe_code = self._safe_provider_text(code) or "unknown"
            if code == "rate_limit_reached":
                raise CoverageError(f"NEWS_RATE_LIMITED: provider error ({safe_code})")
            raise _unavailable(f"provider error ({safe_code})")
        return payload

    @staticmethod
    def _error_code(payload: object) -> str:
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            code = payload["error"].get("code")
            if isinstance(code, str) and code in _DOCUMENTED_ERROR_CODES:
                return code
        return "unknown"

    def _safe_provider_text(self, value: str) -> str:
        safe = value
        for credential in {
            self.__token,
            urllib.parse.quote(self.__token, safe=""),
            urllib.parse.quote_plus(self.__token),
        }:
            if credential:
                safe = safe.replace(credential, "<redacted>")
        return safe

    def _http_failure(self, exc: urllib.error.HTTPError, deadline: float) -> CoverageError:
        status = exc.code if isinstance(exc.code, int) else 0
        code = "unknown"
        try:
            raw = _read_bounded(exc, 64 * 1024, deadline) if exc.fp is not None else b""
            code = self._error_code(json.loads(raw.decode("utf-8")))
        except Exception:  # noqa: BLE001 - error body is optional diagnostics only
            pass
        finally:
            exc.close()
        code = self._safe_provider_text(code)
        if 300 <= status < 400:
            return _unavailable(f"HTTP {status} redirect refused")
        if status == 429:
            return CoverageError(f"NEWS_RATE_LIMITED: HTTP 429 ({code})")
        return _unavailable(f"HTTP {status} ({code})")

    # -------------------------------------------------------------- parsing
    def _article(
        self,
        item: object,
        symbol: str,
        start: datetime,
        end: datetime,
        fetched_at: datetime,
        page_no: int,
    ) -> dict[str, object]:
        if not isinstance(item, dict):
            raise _unavailable("malformed article record")
        article_id = item.get("uuid")
        if not isinstance(article_id, str) or not article_id.strip():
            raise _unavailable("malformed article id")
        article_id = self._safe_provider_text(article_id)
        published = item.get("published_at")
        if not isinstance(published, str) or not _UTC_Z_RE.fullmatch(published):
            raise _unavailable("malformed article timestamp")
        try:
            published_at = parse_utc(published)
        except (CoverageError, ValueError, OverflowError):
            raise _unavailable("malformed article timestamp") from None
        if published_at > fetched_at:
            raise _unavailable("future timestamp")
        if not start <= published_at < end:
            raise _unavailable("timestamp outside request window")
        source = item.get("source")
        if not isinstance(source, str) or not source.strip():
            raise _unavailable("malformed article source")
        source = self._safe_provider_text(source)
        entities = item.get("entities")
        if not isinstance(entities, list) or not entities:
            raise _unavailable("entity mismatch (no entities)")
        matches = [
            entity
            for entity in entities
            if isinstance(entity, dict)
            and isinstance(entity.get("symbol"), str)
            and entity["symbol"].upper() == symbol
            and entity.get("country") == self._country
        ]
        if not matches:
            raise _unavailable("entity mismatch")
        entity = matches[0]
        match_score = entity.get("match_score")
        similar = item.get("similar")
        return {
            "provider": PROVIDER,
            "id": article_id,
            "published_at": _utc_text(published_at),
            "source": source,
            "title": self._safe_provider_text(item["title"])
            if isinstance(item.get("title"), str)
            else None,
            "entity": {
                **{
                    field: self._safe_provider_text(entity[field])
                    if isinstance(entity.get(field), str)
                    else None
                    for field in _ENTITY_FIELDS
                },
                "match_score": match_score
                if isinstance(match_score, (int, float)) and not isinstance(match_score, bool)
                else None,
            },
            "similar_count": len(similar) if isinstance(similar, list) else 0,
            "page": page_no,
            "fetched_at": _utc_text(fetched_at),
            "request_start": _utc_text(start),
            "request_end": _utc_text(end),
        }

    # ----------------------------------------------------------------- probe
    def fetch_articles(
        self, symbol: str, start: datetime, end: datetime, *, max_pages: int = 2
    ) -> dict[str, object]:
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            raise CoverageError("PARAMS_INVALID: symbol must be one uppercase ticker")
        for name, value in (("start", start), ("end", end)):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise CoverageError(f"PARAMS_INVALID: {name} must be a timezone-aware datetime")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= _MAX_PAGES:
            raise CoverageError(f"PARAMS_INVALID: max_pages must be within 1..{_MAX_PAGES}")
        # The API accepts second resolution only; refuse rather than silently truncate.
        for name, value in (("start", start), ("end", end)):
            if value.microsecond or value.utcoffset().microseconds:
                raise CoverageError(
                    f"TIMESTAMP_INVALID: {name} must be a whole-second UTC bound"
                )
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        fetched_at = self._clock().astimezone(timezone.utc)
        if start >= end:
            raise CoverageError("PARAMS_INVALID: start must be before end")
        if end > fetched_at:
            raise CoverageError("PARAMS_INVALID: end must not be in the future")
        if end - start > _MAX_WINDOW:
            raise CoverageError("PARAMS_INVALID: window exceeds 31 days")

        base_query = {
            "symbols": symbol,
            "countries": self._country,
            "filter_entities": "true",
            "must_have_entities": "true",
            "group_similar": "false",
            "published_after": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "published_before": end.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        articles: list[dict[str, object]] = []
        seen: set[str] = set()
        pages: list[dict[str, int]] = []
        found_total: int | None = None
        deadline = _monotonic() + self._timeout
        for page_no in range(1, max_pages + 1):
            payload = self._get_page({**base_query, "page": str(page_no)}, deadline)
            meta, data = payload.get("meta"), payload.get("data")
            if not isinstance(meta, dict) or not isinstance(data, list):
                raise _unavailable("malformed payload (meta/data)")
            found, returned, limit, meta_page = (meta.get(k) for k in ("found", "returned", "limit", "page"))
            if not all(_is_count(v) for v in (found, returned, limit, meta_page)):
                raise _unavailable("malformed payload (meta counts)")
            if meta_page != page_no or limit < 1 or returned != len(data) or returned > limit:
                raise _unavailable("malformed payload (inconsistent meta)")
            if found_total is None:
                found_total = found
                if found > min(_MAX_RESULT_SET, _MAX_ARTICLES, max_pages * limit):
                    raise _unavailable(
                        f"truncated (found {found} exceeds bound of {max_pages} pages x {limit})"
                    )
            elif found != found_total:
                raise _unavailable("truncated (result set changed during pagination)")
            pages.append({"page": page_no, "found": found, "returned": returned, "limit": limit})
            for item in data:
                record = self._article(item, symbol, start, end, fetched_at, page_no)
                if record["id"] in seen:
                    raise _unavailable("duplicate article id")
                seen.add(record["id"])
                articles.append(record)
            if len(articles) >= found_total:
                break
            if returned < limit:
                raise _unavailable(
                    f"truncated (collected {len(articles)} of {found_total} found)"
                )
        if found_total is None or len(articles) != found_total:
            raise _unavailable(f"truncated (collected {len(articles)} of {found_total} found)")

        return {
            "provider": PROVIDER,
            "endpoint": f"{_TRUSTED_BASE_URL}{_NEWS_PATH}",
            "symbol": symbol,
            "country": self._country,
            "request_start": _utc_text(start),
            "request_end": _utc_text(end),
            "fetched_at": _utc_text(fetched_at),
            "status": "NEWS_ARTICLES_OBSERVED" if articles else "NEWS_COVERAGE_UNVERIFIED",
            "found": found_total,
            "collected": len(articles),
            "measured_page_limit": pages[0]["limit"],
            "pages": pages,
            "articles": articles,
        }
