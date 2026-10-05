"""Mechanical micro-cap runner-day selection for a coverage-only pilot; always NO_TRADE."""

from __future__ import annotations

import math
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from microcap_batch_schema import (
    BIAS, MAX_CALENDAR_DAYS, SYMBOL_PATTERN, manifest_hash, session_window,
    validate_manifest, validate_rule,
)
from microcap_history import CoverageError


_NEW_YORK = ZoneInfo("America/New_York")
_TICKER = re.compile(SYMBOL_PATTERN + r"\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


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
