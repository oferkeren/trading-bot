"""Mechanical micro-cap runner-day selection for a coverage-only pilot; always NO_TRADE."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from microcap_batch_schema import (
    BIAS, DEFAULT_RULE, MAX_CALENDAR_DAYS, SYMBOL_PATTERN, manifest_hash, session_window,
    validate_manifest, validate_rule,
)
from microcap_history import CoverageError
from microcap_readiness import _external, _reject_constant, _save, _unique_object


_NEW_YORK = ZoneInfo("America/New_York")
_TICKER = re.compile(SYMBOL_PATTERN + r"\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MASSIVE = "https://api.massive.com"
_SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_GROUPED_LIMIT = 16 * 1024 * 1024
_TYPE_LIMIT = 1024 * 1024
_SEC_LIMIT = 4 * 1024 * 1024
_TIMEOUT = 30


class SelectionError(RuntimeError):
    """Fixed-code selection failure; message is a code, never provider text."""


class ProviderError(RuntimeError):
    """Fixed-code provider failure."""


class RateLimiter:
    def __init__(self, *, limit: int = 5, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()

    def wait(self) -> None:
        now = self._clock()
        while self._starts and now - self._starts[0] >= self._window:
            self._starts.popleft()
        if len(self._starts) >= self._limit:
            self._sleep(self._starts[0] + self._window - now)
            now = self._clock()
            self._starts.popleft()
        self._starts.append(now)


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def screen(rows: list, rule: Mapping) -> list[tuple[str, float]]:
    ranked = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ticker, open_, high, volume = row.get("T"), row.get("o"), row.get("h"), row.get("v")
        if not (isinstance(ticker, str) and _TICKER.fullmatch(ticker)):
            continue
        if not (_finite(open_) and _finite(high) and _finite(volume)):
            continue
        if not rule["min_open"] <= open_ <= rule["max_open"] or open_ <= 0:
            continue
        if volume < rule["min_volume"]:
            continue
        move = high / open_ - 1
        if move < rule["min_move"]:
            continue
        ranked.append((ticker, round(move * 100, 1), move))
    ranked.sort(key=lambda item: (-item[2], item[0]))
    return [(ticker, move_pct) for ticker, move_pct, _ in ranked]


def parse_sec_tickers(payload: object) -> dict[str, tuple[str, str]]:
    if not isinstance(payload, Mapping):
        raise SelectionError("PROVIDER_ERROR")
    seen: dict[str, set[str]] = {}
    titles: dict[str, str] = {}
    for entry in payload.values():
        if not isinstance(entry, Mapping):
            continue
        cik, ticker, title = entry.get("cik_str"), entry.get("ticker"), entry.get("title")
        if type(cik) is not int or not 0 < cik < 10**10:
            continue
        if not (isinstance(ticker, str) and _TICKER.fullmatch(ticker)):
            continue
        if not (isinstance(title, str) and 0 < len(title) <= 120 and title.isprintable()):
            continue
        cik10 = f"{cik:010d}"
        seen.setdefault(ticker, set()).add(cik10)
        titles[ticker] = title
    return {ticker: (next(iter(ciks)), titles[ticker])
            for ticker, ciks in seen.items() if len(ciks) == 1}


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_batch(provider, sec_map: Mapping[str, tuple[str, str]], *, as_of: date,
                 rule: Mapping, now: datetime) -> dict[str, object]:
    try:
        validate_rule(dict(rule))
    except CoverageError:
        raise SelectionError("INPUT_INVALID") from None
    if now.tzinfo is None or as_of >= now.astimezone(_NEW_YORK).date():
        raise SelectionError("INPUT_INVALID")
    trading_days, candidates, samples = [], [], []
    selected_ciks: set[str] = set()
    for offset in range(MAX_CALENDAR_DAYS):
        if len(trading_days) >= rule["days"]:
            break
        day = as_of - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            request_id, rows = provider.grouped(day)
        except ProviderError:
            raise SelectionError("PROVIDER_ERROR") from None
        if not rows:
            continue
        if not (isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)):
            raise SelectionError("PROVIDER_ERROR")
        trading_days.append({"date": day.isoformat(), "request_id": request_id})
        accepted = 0
        for ticker, move_pct in screen(rows, rule)[: rule["max_candidates_per_day"]]:
            if accepted >= rule["per_day"]:
                break
            mapped = sec_map.get(ticker)
            if mapped is None:
                outcome = "NO_SEC_CIK"
            elif mapped[0] in selected_ciks:
                outcome = "DUPLICATE_ISSUER"
            else:
                try:
                    outcome = ("SELECTED" if provider.ticker_type(ticker, day) == "CS"
                               else "NOT_COMMON_STOCK")
                except ProviderError:
                    outcome = "PROVIDER_ERROR"
            candidates.append({"date": day.isoformat(), "ticker": ticker,
                               "move_pct": move_pct, "outcome": outcome})
            if outcome == "SELECTED":
                accepted += 1
                selected_ciks.add(mapped[0])
                start, end = session_window(day)
                samples.append({"issuer_id": mapped[0], "symbol": ticker,
                                "company": mapped[1], "date": day.isoformat(),
                                "move_pct": move_pct, "start_utc": start, "end_utc": end})
    if not samples:
        raise SelectionError("NO_SAMPLES_SELECTED")
    manifest = {
        "schema_version": 1, "kind": "microcap_batch_manifest",
        "created_at": _utc_text(now), "as_of": as_of.isoformat(), "rule": dict(rule),
        "trading_days": trading_days, "candidates": candidates, "samples": samples,
        "bias": list(BIAS),
    }
    manifest["sha256"] = manifest_hash(manifest)
    try:
        validate_manifest(manifest)
    except CoverageError:
        raise SelectionError("INPUT_INVALID") from None
    return manifest


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_json(url: str, *, limit: int, headers: Mapping[str, str] | None = None) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   **(headers or {})})
    try:
        with _open(request, _TIMEOUT) as response:
            if getattr(response, "status", 200) != 200:
                raise ProviderError("PROVIDER_ERROR")
            chunks, total = [], 0
            while chunk := response.read(65536):
                total += len(chunk)
                if total > limit:
                    raise ProviderError("PROVIDER_ERROR")
                chunks.append(chunk)
        return json.loads(b"".join(chunks), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except ProviderError:
        raise
    except Exception:
        raise ProviderError("PROVIDER_ERROR") from None


class MassiveDaily:
    def __init__(self, api_key: str, limiter: RateLimiter) -> None:
        self._key = api_key
        self._limiter = limiter

    def _url(self, path: str, params: Mapping[str, str]) -> str:
        query = urllib.parse.urlencode({**params, "apiKey": self._key})
        return f"{_MASSIVE}{path}?{query}"

    def grouped(self, day: date) -> tuple[str, list]:
        self._limiter.wait()
        body = _get_json(self._url(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            {"adjusted": "false"}), limit=_GROUPED_LIMIT)
        if not isinstance(body, Mapping) or body.get("status") not in ("OK", "DELAYED"):
            raise ProviderError("PROVIDER_ERROR")
        count, results, request_id = (body.get("resultsCount"), body.get("results"),
                                      body.get("request_id"))
        if type(count) is not int or count < 0 or not isinstance(request_id, str):
            raise ProviderError("PROVIDER_ERROR")
        if results is None and count == 0:
            results = []
        if not isinstance(results, list):
            raise ProviderError("PROVIDER_ERROR")
        return request_id, results

    def ticker_type(self, ticker: str, day: date) -> str:
        if not _TICKER.fullmatch(ticker):
            raise ProviderError("PROVIDER_ERROR")
        self._limiter.wait()
        body = _get_json(self._url(f"/v3/reference/tickers/{ticker}",
                                   {"date": day.isoformat()}), limit=_TYPE_LIMIT)
        results = body.get("results") if isinstance(body, Mapping) else None
        kind = results.get("type") if isinstance(results, Mapping) else None
        if not isinstance(kind, str):
            raise ProviderError("PROVIDER_ERROR")
        return kind


def fetch_sec_tickers(user_agent: str) -> dict[str, tuple[str, str]]:
    try:
        payload = _get_json(_SEC_TICKERS, limit=_SEC_LIMIT,
                            headers={"User-Agent": user_agent})
    except ProviderError:
        raise SelectionError("PROVIDER_ERROR") from None
    return parse_sec_tickers(payload)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SelectionError("INPUT_INVALID")


def _fail(code: str) -> int:
    print(json.dumps({"error": code}), file=sys.stderr)
    return 2 if code in ("INPUT_INVALID", "ENVIRONMENT_INCOMPLETE") else 3


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Select micro-cap runner days for a coverage-only pilot; "
                                 "always NO_TRADE")
    parser.add_argument("--as-of", help="latest US trading date to consider (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=DEFAULT_RULE["days"])
    parser.add_argument("--per-day", type=int, default=DEFAULT_RULE["per_day"])
    parser.add_argument("--output", required=True, help="absolute external manifest JSON")
    try:
        args = parser.parse_args(argv)
        api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if not api_key or not user_agent:
            return _fail("ENVIRONMENT_INCOMPLETE")
        now = _now()
        as_of = (date.fromisoformat(args.as_of) if args.as_of
                 else now.astimezone(_NEW_YORK).date() - timedelta(days=1))
        rule = {**DEFAULT_RULE, "days": args.days, "per_day": args.per_day}
        try:
            output = _external(Path(args.output), existing=False)
        except CoverageError:
            return _fail("INPUT_INVALID")
        sec_map = fetch_sec_tickers(user_agent)
        manifest = select_batch(MassiveDaily(api_key, RateLimiter()), sec_map,
                                as_of=as_of, rule=rule, now=now)
        _save(output, manifest)
    except SelectionError as error:
        return _fail(str(error))
    except (CoverageError, ValueError):
        return _fail("INPUT_INVALID")
    print(json.dumps({"output": str(output), "samples": len(manifest["samples"]),
                      "sha256": manifest["sha256"],
                      "trading_days": len(manifest["trading_days"])},
                     sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
