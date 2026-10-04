"""Pure, point-in-time micro-cap rebound labelling for historical research.

No broker, network, or clock access: every decision is a function of the inputs
and an explicit ``as_of``/``cutoff``. Nothing here makes a statistical claim; it
only defines which historical decisions are labelable without lookahead.

Assumptions (documented, fail-closed):

* Bars are 1-minute, split-adjusted OHLCV mappings with an ISO-8601 ``t`` bar
  *start*. A bar is known only once complete, i.e. when ``t + 1 minute <= as_of``.
* Sessions are New York premarket [04:00, 09:30), regular [09:30, 16:00) and
  after-hours [16:00, 20:00), by bar start. Cycles never span sessions, and a gap
  of more than ``max_gap_minutes`` between bars starts a new detection segment.
* Bar timestamps must parse and strictly increase across the whole input
  (structural integrity). Values are validated for every bar known at ``as_of``;
  invalid values, unadjusted bars, out-of-session bars, or a bar-to-bar/intra-bar
  price ratio above ``max_jump_ratio`` (split-like) raise ``CoverageError``.
* ``min_amplitude`` is in price units (dollars per share) and, with
  ``min_separation_minutes``, must be chosen on training data before evaluation.
* Entry is the close of the bar that confirms the third trough; the stop is that
  trough's low. Spread and fees are never invented: they must be observed/supplied.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from numbers import Real
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from microcap_history import CoverageError, parse_utc


_MARKET_TZ = ZoneInfo("America/New_York")
_BAR = timedelta(minutes=1)
_NEWS_WINDOW = timedelta(hours=48)
_SESSIONS = (
    ("premarket", time(4), time(9, 30)),
    ("regular", time(9, 30), time(16)),
    ("afterhours", time(16), time(20)),
)
_GENERIC_COMPANY_WORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "holdings", "holding", "group", "sa", "nv", "ag", "llc", "lp", "the",
    "class", "a", "b", "common", "stock", "shares", "ordinary", "ads",
}
_LEAD_IN = re.compile(
    r"^(?:(?:update\s*\d*|exclusive|brief|breaking|press\s+release)\s*[:\-\u2013]\s*)*"
    r"(?:(?:why|here'?s\s+why|what'?s\s+going\s+on\s+with|shares\s+of)\s+)?",
    re.IGNORECASE,
)
_ROUNDUP = re.compile(
    r"\b(?:movers|gainers|losers|stocks?\s+moving|stocks?\s+to\s+watch|among|round-?up|"
    r"mid-?day|top\s+stocks|trending\s+stocks|market\s+wrap)\b",
    re.IGNORECASE,
)
_COMPARISON = re.compile(r"\b(?:vs\.?|versus|compared\s+(?:with|to))(?=\s|$)", re.IGNORECASE)

READY = "SETUP_READY"


@dataclass(frozen=True)
class CycleParams:
    """Detection thresholds; preselect on training data, never tune on holdout."""

    min_amplitude: float
    min_separation_minutes: float
    max_gap_minutes: float = 1
    max_jump_ratio: float = 3.0

    def __post_init__(self) -> None:
        if not _finite(self.min_amplitude) or self.min_amplitude <= 0:
            raise CoverageError("PARAMS_INVALID: min_amplitude must be a positive finite number")
        if not _finite(self.min_separation_minutes) or self.min_separation_minutes < 0:
            raise CoverageError("PARAMS_INVALID: min_separation_minutes must be >= 0")
        if not _finite(self.max_gap_minutes) or self.max_gap_minutes < 1:
            raise CoverageError("PARAMS_INVALID: max_gap_minutes must be >= 1")
        if not _finite(self.max_jump_ratio) or self.max_jump_ratio <= 1:
            raise CoverageError("PARAMS_INVALID: max_jump_ratio must be > 1")


@dataclass(frozen=True)
class Extremum:
    kind: str  # "low" or "high"
    price: float
    at: datetime  # start of the bar that printed the extremum
    confirmed_at: datetime  # end of the bar that confirmed it


@dataclass(frozen=True)
class Cycle:
    session_date: date
    session: str
    start_low: Extremum
    high: Extremum
    end_low: Extremum

    @property
    def confirmed_at(self) -> datetime:
        return self.end_low.confirmed_at


@dataclass(frozen=True)
class _Bar:
    t: datetime
    o: float
    h: float
    l: float
    c: float
    raw: Mapping[str, object]


def _finite(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _as_utc(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CoverageError(f"TIMESTAMP_INVALID: {name} must be timezone-aware")
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        return parse_utc(value)
    raise CoverageError(f"TIMESTAMP_INVALID: {name} must be an aware datetime or ISO-8601 string")


def _session_of(moment: datetime, *, end_inclusive: bool = False) -> tuple[date, str] | None:
    local = moment.astimezone(_MARKET_TZ)
    clock = local.time()
    for name, start, end in _SESSIONS:
        inside = start < clock <= end if end_inclusive else start <= clock < end
        if inside:
            return local.date(), name
    return None


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _ticker_pattern(symbol: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9])\$?" + re.escape(symbol) + r"(?![A-Za-z0-9])")


def _is_primary_subject(headline: str, symbol: str, company: str, symbols: Sequence[str]) -> bool:
    if _ROUNDUP.search(headline) or _COMPARISON.search(headline):
        return False
    others = [s for s in symbols if s != symbol and len(s) >= 2 and _ticker_pattern(s).search(headline)]
    if len(others) >= 2:
        return False
    subject = headline[_LEAD_IN.match(headline).end():].lstrip()
    ticker_start = re.compile(
        r"^\(?(?:(?:NASDAQ|NYSE(?:\s+AMERICAN)?|AMEX|OTC[A-Z]*)\s*:\s*)?\$?"
        + re.escape(symbol) + r"(?![A-Za-z0-9])"
    )
    if ticker_start.match(subject):
        return True
    core = " ".join(w for w in _normalize(company).split() if w not in _GENERIC_COMPANY_WORDS)
    normalized = _normalize(subject)
    return bool(core) and (normalized == core or normalized.startswith(core + " "))


def eligible_news(
    news: Sequence[Mapping[str, object]], symbol: str, company: str, as_of: object
) -> list[Mapping[str, object]]:
    """Articles known at ``as_of`` (published in [as_of-48h, as_of]) primarily about the company.

    Articles with missing/invalid IDs, malformed or naive dates, a symbol list not
    containing ``symbol``, or a non-primary headline are skipped. Duplicate IDs keep
    one copy; IDs whose copies disagree on time or headline are dropped entirely.
    An invalid ``as_of`` raises ``CoverageError`` (never falls back to "now").
    """
    decision = _as_utc(as_of, "as_of")
    if not isinstance(symbol, str) or not symbol.strip():
        raise CoverageError("SYMBOLS_INVALID: symbol must be a non-empty string")
    symbol = symbol.strip().upper()
    company = company if isinstance(company, str) else ""
    by_id: dict[str, tuple[datetime, Mapping[str, object]] | None] = {}
    for item in news:
        if not isinstance(item, Mapping):
            continue
        article_id = item.get("id")
        if isinstance(article_id, bool) or not isinstance(article_id, (str, int)) or article_id == "":
            continue
        try:
            created = parse_utc(item.get("created_at"))  # type: ignore[arg-type]
        except CoverageError:
            continue
        headline = item.get("headline")
        symbols = item.get("symbols")
        if not isinstance(headline, str) or not isinstance(symbols, list):
            continue
        listed = [s.strip().upper() for s in symbols if isinstance(s, str)]
        if symbol not in listed:
            continue
        if not decision - _NEWS_WINDOW <= created <= decision:
            continue
        if not _is_primary_subject(headline, symbol, company, listed):
            continue
        key = str(article_id)
        if key in by_id:
            previous = by_id[key]
            if previous is not None and (previous[0] != created or previous[1].get("headline") != headline):
                by_id[key] = None
            continue
        by_id[key] = (created, item)
    kept = [entry for entry in by_id.values() if entry is not None]
    kept.sort(key=lambda entry: (entry[0], str(entry[1]["id"])))
    return [entry[1] for entry in kept]


# ---------------------------------------------------------------------------
# Bars and cycles
# ---------------------------------------------------------------------------

def _known_bars(bars: Sequence[Mapping[str, object]], as_of: datetime, params: CycleParams) -> list[_Bar]:
    stamps: list[datetime] = []
    for raw in bars:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("t"), str):
            raise CoverageError("BAR_INVALID: every bar needs a string timestamp 't'")
        stamps.append(parse_utc(raw["t"]))  # type: ignore[arg-type]
    if any(later <= earlier for earlier, later in zip(stamps, stamps[1:])):
        raise CoverageError("BAR_UNORDERED: bar timestamps must strictly increase")
    known: list[_Bar] = []
    for raw, stamp in zip(bars, stamps):
        if stamp + _BAR > as_of:
            break
        values = {}
        for field in ("o", "h", "l", "c", "v"):
            value = raw.get(field)
            if not _finite(value):
                raise CoverageError(f"BAR_INVALID: field {field!r} must be a finite number")
            values[field] = float(value)  # type: ignore[arg-type]
        if min(values["o"], values["h"], values["l"], values["c"]) <= 0 or values["v"] < 0:
            raise CoverageError("BAR_INVALID: prices must be positive and volume non-negative")
        if values["l"] > min(values["o"], values["c"]) or values["h"] < max(values["o"], values["c"]):
            raise CoverageError("BAR_INVALID: OHLC values are inconsistent")
        if values["h"] / values["l"] > params.max_jump_ratio:
            raise CoverageError("BAR_SUSPICIOUS_JUMP: intra-bar range is split-like")
        if raw.get("request_adjustment", "split") != "split":
            raise CoverageError("BAR_UNADJUSTED: bars must be split-adjusted")
        if _session_of(stamp) is None:
            raise CoverageError("BAR_OUTSIDE_SESSION: bar is outside 04:00-20:00 New York")
        bar = _Bar(stamp, values["o"], values["h"], values["l"], values["c"], raw)
        if known and _session_of(known[-1].t) == _session_of(stamp):
            ratio = bar.o / known[-1].c
            if ratio > params.max_jump_ratio or ratio < 1 / params.max_jump_ratio:
                raise CoverageError("BAR_SUSPICIOUS_JUMP: bar-to-bar move is split-like")
        known.append(bar)
    return known


def _segments(bars: list[_Bar], params: CycleParams) -> list[list[_Bar]]:
    segments: list[list[_Bar]] = []
    max_gap = timedelta(minutes=params.max_gap_minutes)
    for bar in bars:
        if (segments and _session_of(segments[-1][-1].t) == _session_of(bar.t)
                and bar.t - segments[-1][-1].t <= max_gap):
            segments[-1].append(bar)
        else:
            segments.append([bar])
    return segments


def _zigzag(segment: list[_Bar], params: CycleParams) -> list[Extremum]:
    """Causal swing detection: each extremum is confirmed by a strictly later bar.

    A bar that sets a new candidate extreme never also confirms it (intra-bar
    order is unknown). Confirmed extrema are never revised.
    """
    amplitude = params.min_amplitude
    separation = timedelta(minutes=params.min_separation_minutes)
    extrema: list[Extremum] = []
    low = high = segment[0]
    direction: str | None = None
    for bar in segment[1:]:
        end = bar.t + _BAR
        if direction is None:
            rose = bar.h - low.l >= amplitude
            fell = high.h - bar.l >= amplitude
            if rose and (not fell or low.t <= high.t):
                extrema.append(Extremum("low", low.l, low.t, end))
                direction, high = "up", bar
            elif fell:
                extrema.append(Extremum("high", high.h, high.t, end))
                direction, low = "down", bar
            else:
                if bar.l < low.l:
                    low = bar
                if bar.h > high.h:
                    high = bar
        elif direction == "up":
            if bar.h > high.h:
                high = bar
            elif high.h - bar.l >= amplitude and high.t - extrema[-1].at >= separation:
                extrema.append(Extremum("high", high.h, high.t, end))
                direction, low = "down", bar
        else:
            if bar.l < low.l:
                low = bar
            elif bar.h - low.l >= amplitude and low.t - extrema[-1].at >= separation:
                extrema.append(Extremum("low", low.l, low.t, end))
                direction, high = "up", bar
    return extrema


def _segment_cycles(
    segment: list[_Bar], params: CycleParams, extrema: list[Extremum] | None = None
) -> list[Cycle]:
    session_date, session = _session_of(segment[0].t)  # type: ignore[misc]
    extrema = _zigzag(segment, params) if extrema is None else extrema
    return [
        Cycle(session_date, session, a, b, c)
        for a, b, c in zip(extrema, extrema[1:], extrema[2:])
        if (a.kind, b.kind, c.kind) == ("low", "high", "low")
    ]


def confirmed_cycles(
    bars: Sequence[Mapping[str, object]], as_of: object, *, params: CycleParams
) -> tuple[Cycle, ...]:
    """Completed low->high->low cycles confirmed by bars complete at ``as_of``.

    Raises ``CoverageError`` on malformed, unordered, unadjusted, out-of-session,
    or split-like bars.
    """
    decision = _as_utc(as_of, "as_of")
    cycles: list[Cycle] = []
    for segment in _segments(_known_bars(bars, decision, params), params):
        cycles.extend(_segment_cycles(segment, params))
    return tuple(sorted(cycles, key=lambda cycle: cycle.confirmed_at))


def _setup_from_segment(
    segment: list[_Bar], params: CycleParams
) -> tuple[dict[str, float] | None, str]:
    extrema = _zigzag(segment, params)
    cycles = _segment_cycles(segment, params, extrema)
    if len(cycles) < 2:
        return None, "INSUFFICIENT_CYCLES"
    if extrema[-1] is not cycles[-1].end_low:
        return None, "NO_FRESH_LOW"
    entry, stop = segment[-1].c, cycles[-1].end_low.price
    if entry <= stop:
        return None, "BELOW_STOP"
    return {"entry": entry, "stop": stop}, READY


def current_setup(
    bars: Sequence[Mapping[str, object]], spread: object, fees: object, as_of: object,
    *, params: CycleParams,
) -> tuple[dict[str, float] | None, str]:
    """Entry setup at ``as_of`` or ``(None, REASON)``; never raises on bad data.

    Requires, within the current session's latest gap-free segment, at least two
    confirmed cycles whose latest confirmed extremum is the closing low (no high
    confirmed since), last close above that low, fresh bars, an observed positive
    spread and non-negative fees.
    """
    try:
        decision = _as_utc(as_of, "as_of")
        known = _known_bars(bars, decision, params)
    except CoverageError as error:
        return None, f"BAD_DATA: {error}"
    current = _session_of(decision, end_inclusive=True)
    if current is None:
        return None, "OUTSIDE_SESSION"
    if not known:
        return None, "NO_BARS"
    if not _finite(spread) or spread <= 0:  # type: ignore[operator]
        return None, "SPREAD_INVALID"
    if not _finite(fees) or fees < 0:  # type: ignore[operator]
        return None, "FEES_INVALID"
    last = known[-1]
    if (_session_of(last.t) != current
            or decision - (last.t + _BAR) >= timedelta(minutes=params.max_gap_minutes)):
        return None, "STALE_BARS"
    setup, reason = _setup_from_segment(_segments(known, params)[-1], params)
    if setup is None:
        return None, reason
    return {**setup, "spread": spread, "fees": fees}, READY  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Historical events
# ---------------------------------------------------------------------------

def _price_band(price: float) -> str:
    for upper, label in ((1, "0-1"), (5, "1-5"), (10, "5-10"), (20, "10-20")):
        if price < upper:
            return label
    return "20+"


def _age_band(age: timedelta) -> str:
    hours = age.total_seconds() / 3600
    return "0-6h" if hours < 6 else "6-24h" if hours < 24 else "24-48h"


def _as_of_spread(
    quotes: Sequence[Mapping[str, object]], decision: datetime, max_age: timedelta
) -> float | None:
    latest: tuple[datetime, Mapping[str, object]] | None = None
    for item in quotes:
        stamp = _as_utc(item.get("t"), "quote t") if isinstance(item, Mapping) else None
        if stamp is None:
            raise CoverageError("QUOTE_INVALID: quotes must be mappings")
        if stamp <= decision and (latest is None or stamp > latest[0]):
            latest = (stamp, item)
    if latest is None or decision - latest[0] > max_age:
        return None
    bid, ask = latest[1].get("bid"), latest[1].get("ask")
    if not _finite(bid) or not _finite(ask) or bid <= 0 or ask <= bid:  # type: ignore[operator]
        return None
    return float(ask) - float(bid)  # type: ignore[arg-type]


def extract_events(
    bars: Sequence[Mapping[str, object]],
    news: Sequence[Mapping[str, object]],
    symbol: str,
    company: str,
    cutoff: object,
    *,
    params: CycleParams,
    quotes: Sequence[Mapping[str, object]],
    fees: object,
    horizon_minutes: float,
    max_quote_age_seconds: float,
) -> list[dict[str, object]]:
    """Label historical setup decisions whose outcome window ends strictly before ``cutoff``.

    A decision is the confirmation of a cycle's closing low that is a valid
    ``current_setup`` at that instant. Each needs an eligible catalyst known at the
    decision (the earliest eligible article is the catalyst ID), an as-of quote no
    older than ``max_quote_age_seconds`` with ``0 < bid < ask``, and supplied fees.
    Missing costs yield no event. The outcome window [decision, decision+horizon)
    must fit inside the decision's session. At most one event per
    ``(symbol, catalyst_id, session_date)``, the earliest. Bars complete after
    ``cutoff`` are never read; bad bars before it raise ``CoverageError``.
    """
    end = _as_utc(cutoff, "cutoff")
    if not _finite(horizon_minutes) or horizon_minutes <= 0:  # type: ignore[operator]
        raise CoverageError("PARAMS_INVALID: horizon_minutes must be positive")
    if not _finite(max_quote_age_seconds) or max_quote_age_seconds < 0:  # type: ignore[operator]
        raise CoverageError("PARAMS_INVALID: max_quote_age_seconds must be non-negative")
    if not _finite(fees) or fees < 0:  # type: ignore[operator]
        return []
    horizon = timedelta(minutes=horizon_minutes)
    max_age = timedelta(seconds=max_quote_age_seconds)
    symbol = symbol.strip().upper()
    known = _known_bars(bars, end, params)
    events: list[dict[str, object]] = []
    seen: set[tuple[str, str, date]] = set()
    for segment in _segments(known, params):
        session_date, session = _session_of(segment[0].t)  # type: ignore[misc]
        session_end = next(
            datetime.combine(session_date, close, _MARKET_TZ).astimezone(timezone.utc)
            for name, _, close in _SESSIONS if name == session
        )
        for cycle in _segment_cycles(segment, params):
            decision = cycle.confirmed_at
            if decision + horizon >= end or decision + horizon > session_end:
                continue
            setup, _ = _setup_from_segment([b for b in segment if b.t + _BAR <= decision], params)
            if setup is None:
                continue
            catalysts = eligible_news(news, symbol, company, decision)
            if not catalysts:
                continue
            catalyst = catalysts[0]
            key = (symbol, str(catalyst["id"]), session_date)
            if key in seen:
                continue
            spread = _as_of_spread(quotes, decision, max_age)
            if spread is None:
                continue
            seen.add(key)
            age = decision - parse_utc(catalyst["created_at"])  # type: ignore[arg-type]
            events.append({
                "symbol": symbol,
                "catalyst_id": catalyst["id"],
                "session_date": session_date,
                "session": session,
                "decision_at": decision,
                "entry": setup["entry"],
                "stop": setup["stop"],
                "spread": spread,
                "fees": fees,
                "future_bars": [
                    dict(b.raw) for b in segment if decision <= b.t and b.t + _BAR <= decision + horizon
                ],
                "feature_bucket": f"{session}|price:{_price_band(setup['entry'])}|news_age:{_age_band(age)}",
            })
    return events
