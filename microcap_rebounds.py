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
* Bar timestamps must parse and strictly increase through the first incomplete
  bar at ``as_of``. Its timestamp determines the cutoff boundary, but later
  records are ignored. Values are validated only for complete historical bars;
  invalid values, unadjusted bars, out-of-session bars, or a bar-to-bar/intra-bar
  price ratio above ``max_jump_ratio`` (split-like) raise ``CoverageError``.
* ``min_amplitude`` is in price units (dollars per share) and, with
  ``min_separation_minutes``, must be chosen on training data before evaluation.
* The confirming bar's close is a signal reference, not a fill. Historical event
  entries use the first valid post-decision ask quote; outcomes start with the
  first full minute bar strictly after that quote. The stop is the confirmed
  trough's low. Spread and fees are never invented: they must be observed/supplied.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from numbers import Real
from statistics import fmean
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


def catalyst_episodes(
    news: Sequence[Mapping[str, object]], symbol: str, company: str, as_of: object
) -> list[dict[str, object]]:
    """Group primary-news articles into connected, overlapping 48-hour episodes."""
    decision = _as_utc(as_of, "as_of")
    articles: dict[str, tuple[datetime, Mapping[str, object]] | None] = {}
    for item in news:
        if not isinstance(item, Mapping):
            continue
        try:
            created = parse_utc(item.get("created_at"))  # type: ignore[arg-type]
        except CoverageError:
            continue
        if created > decision:
            continue
        eligible = eligible_news([item], symbol, company, created)
        if eligible:
            article_id = str(eligible[0]["id"])
            previous = articles.get(article_id)
            if previous is None and article_id not in articles:
                articles[article_id] = (created, eligible[0])
            elif previous is not None and (
                previous[0] != created or previous[1].get("headline") != eligible[0].get("headline")
            ):
                articles[article_id] = None

    episodes: list[dict[str, object]] = []
    valid_articles = [
        (article_id, value)
        for article_id, value in articles.items()
        if value is not None
    ]
    for article_id, (created, article) in sorted(valid_articles, key=lambda pair: pair[1][0]):
        article_end = created + _NEWS_WINDOW
        if episodes and created <= episodes[-1]["end"]:
            episode = episodes[-1]
            episode["end"] = max(episode["end"], article_end)  # type: ignore[arg-type]
            episode["articles"].append(article)  # type: ignore[union-attr]
            episode["article_ids"].add(article_id)  # type: ignore[union-attr]
        else:
            episodes.append({
                "episode_id": f"{symbol.strip().upper()}:{created.isoformat()}",
                "start": created,
                "end": article_end,
                "articles": [article],
                "article_ids": {article_id},
            })
    return episodes


# ---------------------------------------------------------------------------
# Bars and cycles
# ---------------------------------------------------------------------------

def _known_bars(bars: Sequence[Mapping[str, object]], as_of: datetime, params: CycleParams) -> list[_Bar]:
    known: list[_Bar] = []
    previous: datetime | None = None
    for raw in bars:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("t"), str):
            raise CoverageError("BAR_INVALID: every bar needs a string timestamp 't'")
        stamp = parse_utc(raw["t"])  # type: ignore[arg-type]
        if previous is not None and stamp <= previous:
            raise CoverageError("BAR_UNORDERED: historical bar timestamps must strictly increase")
        previous = stamp
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


def _first_post_decision_ask(
    quotes: Sequence[Mapping[str, object]],
    decision: datetime,
    cutoff: datetime,
    max_age: timedelta,
) -> tuple[datetime, float] | None:
    first: tuple[datetime, float] | None = None
    for item in quotes:
        stamp = _as_utc(item.get("t"), "quote t") if isinstance(item, Mapping) else None
        if stamp is None:
            raise CoverageError("QUOTE_INVALID: quotes must be mappings")
        if stamp <= decision or stamp > cutoff or stamp - decision > max_age:
            continue
        bid, ask = item.get("bid"), item.get("ask")
        if not _finite(bid) or not _finite(ask) or bid <= 0 or ask <= bid:  # type: ignore[operator]
            continue
        if first is None or stamp < first[0]:
            first = (stamp, float(ask))  # type: ignore[arg-type]
    return first


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
    horizon_minutes: int,
    max_quote_age_seconds: float,
) -> list[dict[str, object]]:
    """Label historical setup decisions whose outcome window ends strictly before ``cutoff``.

    A decision is the confirmation of a cycle's closing low that is a valid
    ``current_setup`` at that instant. Each needs an eligible catalyst known at the
    decision (the earliest eligible article is the catalyst ID, with its merged
    overlapping-news episode ID), a fresh quote at
    or before the decision for spread, the first usable quote after the decision
    for a simulated ask fill, and supplied fees. Missing costs or fills yield no
    event. The integer-minute outcome window starts at the first full minute bar
    strictly after the fill quote and must be covered by contiguous complete
    one-minute bars; any missing outcome bar (e.g. an IEX minute with no trades)
    skips the event. The stop must be below the simulated fill. At most one event
    per ``(symbol, catalyst_id, session_date)``, the earliest. Through the first
    incomplete bar at ``cutoff``, timestamps must be valid and ordered; that bar's
    OHLCV and later records are ignored. Invalid historical-prefix data raises
    ``CoverageError``.
    """
    end = _as_utc(cutoff, "cutoff")
    if (isinstance(horizon_minutes, bool) or not isinstance(horizon_minutes, int)
            or horizon_minutes <= 0):
        raise CoverageError("PARAMS_INVALID: horizon_minutes must be a positive integer")
    if not _finite(max_quote_age_seconds) or max_quote_age_seconds < 0:  # type: ignore[operator]
        raise CoverageError("PARAMS_INVALID: max_quote_age_seconds must be non-negative")
    if not _finite(fees) or fees < 0:  # type: ignore[operator]
        return []
    horizon_bars = horizon_minutes
    max_age = timedelta(seconds=max_quote_age_seconds)
    symbol = symbol.strip().upper()
    known = _known_bars(bars, end, params)
    episodes = catalyst_episodes(news, symbol, company, end)
    episode_by_article = {
        article_id: str(episode["episode_id"])
        for episode in episodes
        for article_id in episode["article_ids"]  # type: ignore[union-attr]
    }
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
            fill = _first_post_decision_ask(quotes, decision, end, max_age)
            if fill is None:
                continue
            fill_at, entry = fill
            if entry <= setup["stop"]:
                continue
            first_outcome_at = fill_at.replace(second=0, microsecond=0) + _BAR
            horizon_seconds = horizon_bars * 60
            if (horizon_seconds >= (end - first_outcome_at).total_seconds()
                    or horizon_seconds > (session_end - first_outcome_at).total_seconds()):
                continue
            outcome_end = first_outcome_at + horizon_bars * _BAR
            expected = [first_outcome_at + k * _BAR for k in range(horizon_bars)]
            outcome = [b for b in segment if b.t > fill_at and b.t + _BAR <= outcome_end]
            if not expected or [b.t for b in outcome] != expected:
                continue
            seen.add(key)
            age = decision - parse_utc(catalyst["created_at"])  # type: ignore[arg-type]
            events.append({
                "symbol": symbol,
                "catalyst_id": catalyst["id"],
                "episode_id": episode_by_article[str(catalyst["id"])],
                "session_date": session_date,
                "session": session,
                "decision_at": decision,
                "entry": entry,
                "fill_at": fill_at,
                "signal_reference_price": setup["entry"],
                "stop": setup["stop"],
                "spread": spread,
                "fees": fees,
                "horizon_minutes": horizon_minutes,
                "future_bars": [dict(b.raw) for b in outcome],
                "feature_bucket": (
                    f"{session}|price:{_price_band(setup['entry'])}|news_age:{_age_band(age)}"
                ),
            })
    return events


_NET_TARGETS = (0.50, 1.00, 1.50, 2.00)
_TARGET_KEYS = tuple(f"{target:.2f}" for target in _NET_TARGETS)
_MIN_TARGET_SAMPLE = 50
_WILSON_Z_95 = 1.959963984540054
_NORMAL_Z_95_ONE_SIDED = 1.6448536269514722


def _target_bars(
    bars: object, *, fill_at: datetime | None = None
) -> list[tuple[datetime, float, float, float, float]]:
    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)) or not bars:
        raise CoverageError("TARGET_BARS_INVALID: future_bars must be a non-empty sequence")
    checked: list[tuple[datetime, float, float, float, float]] = []
    for raw in bars:
        if not isinstance(raw, Mapping):
            raise CoverageError("TARGET_BARS_INVALID: every future bar must be a mapping")
        stamp = _as_utc(raw.get("t"), "future bar t")
        high, low, close = raw.get("h"), raw.get("l"), raw.get("c")
        open_price = raw.get("o")
        if not all(_finite(value) for value in (open_price, high, low, close)):
            raise CoverageError("TARGET_BARS_INVALID: OHLC fields must be finite numbers")
        o, h, l, c = (float(value) for value in (open_price, high, low, close))  # type: ignore[arg-type]
        if min(o, h, l, c) <= 0 or l > min(o, c) or h < max(o, c):
            raise CoverageError("TARGET_BARS_INVALID: OHLC values are inconsistent")
        if checked and stamp - checked[-1][0] != _BAR:
            raise CoverageError("TARGET_BARS_GAP: future_bars must be contiguous one-minute bars")
        checked.append((stamp, o, h, l, c))
    if fill_at is not None:
        first_expected = fill_at.replace(second=0, microsecond=0) + _BAR
        if checked[0][0] != first_expected:
            raise CoverageError("TARGET_BARS_GAP: future_bars must start after the executable fill")
    return checked


def first_touch(
    bars: Sequence[Mapping[str, object]], stop: object, target: object
) -> str:
    """Return the first OHLC stop/target touch; ambiguous bars conservatively stop."""
    if not _finite(stop) or not _finite(target) or stop <= 0 or target <= stop:  # type: ignore[operator]
        raise CoverageError("TARGET_LEVEL_INVALID: stop and target must be positive with target above stop")
    for _, _, high, low, _ in _target_bars(bars):
        if low <= float(stop):
            return "STOP"
        if high >= float(target):
            return "TARGET"
    return "TIMEOUT"


def _wilson_lower_bound(successes: int, sample_size: int) -> float:
    proportion = successes / sample_size
    z_squared = _WILSON_Z_95 ** 2
    denominator = 1 + z_squared / sample_size
    center = proportion + z_squared / (2 * sample_size)
    margin = _WILSON_Z_95 * math.sqrt(
        proportion * (1 - proportion) / sample_size
        + z_squared / (4 * sample_size**2)
    )
    return max(0.0, (center - margin) / denominator)


def _mean_lower_bound(values: Sequence[float]) -> float:
    count = len(values)
    mean = fmean(values)
    if count < 2:
        return mean
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    degrees = count - 1
    z = _NORMAL_Z_95_ONE_SIDED
    t_critical = (
        z
        + (z**3 + z) / (4 * degrees)
        + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * degrees**2)
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * degrees**3)
    )
    return mean - t_critical * math.sqrt(variance / count)


def _event_date(value: object) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CoverageError("EVENT_INVALID: session_date must identify a valid date")
        return value.astimezone(_MARKET_TZ).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CoverageError("EVENT_INVALID: session_date must be a date or ISO date")


def _validate_target_event(event: object) -> dict[str, object]:
    required = (
        "symbol", "catalyst_id", "session_date", "session", "decision_at", "fill_at",
        "entry", "stop", "spread", "fees", "feature_bucket", "horizon_minutes", "future_bars",
    )
    if not isinstance(event, Mapping) or any(field not in event for field in required):
        raise CoverageError("EVENT_INVALID: historical event is missing required fields")
    symbol, catalyst_id, session = event["symbol"], event["catalyst_id"], event["session"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise CoverageError("EVENT_INVALID: symbol must be a non-empty string")
    if (isinstance(catalyst_id, bool) or not isinstance(catalyst_id, (str, int))
            or catalyst_id == ""):
        raise CoverageError("EVENT_INVALID: catalyst_id must be a non-empty string or integer")
    episode_id = event.get("episode_id", catalyst_id)
    if (isinstance(episode_id, bool) or not isinstance(episode_id, (str, int))
            or episode_id == ""):
        raise CoverageError("EVENT_INVALID: episode_id must be a non-empty string or integer")
    if session not in {name for name, _, _ in _SESSIONS}:
        raise CoverageError("EVENT_INVALID: session must be a known trading session")
    if not isinstance(event["feature_bucket"], str) or not event["feature_bucket"]:
        raise CoverageError("EVENT_INVALID: feature_bucket must be a non-empty string")
    horizon_minutes = event["horizon_minutes"]
    if (isinstance(horizon_minutes, bool) or not isinstance(horizon_minutes, int)
            or horizon_minutes <= 0):
        raise CoverageError("EVENT_INVALID: horizon_minutes must be a positive integer")
    event_day = _event_date(event["session_date"])
    decision_at = _as_utc(event["decision_at"], "event decision_at")
    fill_at = _as_utc(event["fill_at"], "event fill_at")
    if decision_at >= fill_at:
        raise CoverageError("EVENT_INVALID: fill_at must be strictly after decision_at")
    if ((event_day, session) not in (
            _session_of(decision_at), _session_of(decision_at, end_inclusive=True))
            or _session_of(fill_at) != (event_day, session)):
        raise CoverageError("EVENT_INVALID: session_date/session must match decision_at and fill_at")
    entry, stop = event["entry"], event["stop"]
    spread, fees = event["spread"], event["fees"]
    if not _finite(entry) or not _finite(stop) or entry <= stop or stop <= 0:  # type: ignore[operator]
        raise CoverageError("EVENT_INVALID: entry and stop must be finite positive prices with entry above stop")
    if not _finite(spread) or spread <= 0:  # type: ignore[operator]
        raise CoverageError("EVENT_INVALID: event spread must be positive and finite")
    if not _finite(fees) or fees < 0:  # type: ignore[operator]
        raise CoverageError("EVENT_INVALID: event fees must be non-negative and finite")
    return {
        "symbol": symbol.strip().upper(),
        "catalyst_id": catalyst_id,
        "episode_id": episode_id,
        "session_date": event_day,
        "session": session,
        "decision_at": decision_at,
        "fill_at": fill_at,
        "entry": float(entry),  # type: ignore[arg-type]
        "stop": float(stop),  # type: ignore[arg-type]
        "spread": float(spread),  # type: ignore[arg-type]
        "fees": float(fees),  # type: ignore[arg-type]
        "feature_bucket": event["feature_bucket"],
        "horizon_minutes": horizon_minutes,
        "future_bars": event["future_bars"],
    }


def estimate_targets(
    events: Sequence[Mapping[str, object]],
    entry: object,
    stop: object,
    costs: object,
    as_of: object,
    *,
    feature_bucket: str,
) -> dict[str, object]:
    """Estimate net target outcomes from >=50 independent, fully observed prior episodes.

    Events must carry their own fill, stop, observed spread and fees, exact known-as-of
    feature bucket, and contiguous future 1-minute bars. A target price includes the
    greater of historical and candidate exit spread and fees, so reaching it represents
    the requested net gain without charging spread twice on the ask entry. Timeout
    outcomes are liquidated at the final close less those exit costs. Targets whose
    candidate costs equal or exceed the requested gain are ineligible. Candidate
    selection requires both a 95% Wilson hit-rate lower bound >=.60 and a positive
    one-sided 95% t lower bound on mean net outcome. Eligible targets are ranked by
    that lower bound divided by mean downside plus candidate per-share risk; ties favor
    the lower target. These frozen cutoffs are research-only and not trading advice.
    """
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise CoverageError("EVENTS_INVALID: events must be a sequence of historical event mappings")
    decision_at = _as_utc(as_of, "as_of")
    if not isinstance(feature_bucket, str) or not feature_bucket:
        raise CoverageError("FEATURE_BUCKET_INVALID: feature_bucket must be a non-empty string")
    if not _finite(entry) or not _finite(stop) or entry <= stop or stop <= 0:  # type: ignore[operator]
        raise CoverageError("ENTRY_INVALID: entry and stop must be finite positive prices with entry above stop")
    if not isinstance(costs, Mapping) or any(key not in costs for key in ("spread", "fees")):
        raise CoverageError("COSTS_MISSING: candidate spread and fees are required")
    candidate_spread, candidate_fees = costs["spread"], costs["fees"]
    if not _finite(candidate_spread) or candidate_spread <= 0:  # type: ignore[operator]
        raise CoverageError("COSTS_INVALID: candidate spread must be positive and finite")
    if not _finite(candidate_fees) or candidate_fees < 0:  # type: ignore[operator]
        raise CoverageError("COSTS_INVALID: candidate fees must be non-negative and finite")

    candidate_day = decision_at.astimezone(_MARKET_TZ).date()
    independent: dict[
        tuple[str, str],
        tuple[dict[str, object], list[tuple[datetime, float, float, float, float]]],
    ] = {}
    catalyst_days: set[date] = set()
    for raw_event in events:
        event = _validate_target_event(raw_event)
        future_bars = _target_bars(event["future_bars"], fill_at=event["fill_at"])  # type: ignore[arg-type]
        if len(future_bars) != event["horizon_minutes"]:
            raise CoverageError(
                "TARGET_BARS_HORIZON: future_bars must contain exactly horizon_minutes bars"
            )
        if any(
            _session_of(stamp) != (event["session_date"], event["session"])
            or _session_of(stamp + _BAR, end_inclusive=True) != (event["session_date"], event["session"])
            for stamp, _, _, _, _ in future_bars
        ):
            raise CoverageError("EVENT_INVALID: future_bars must complete within session_date/session")
        if event["feature_bucket"] != feature_bucket or event["session_date"] >= candidate_day:
            continue
        if future_bars[-1][0] + _BAR > decision_at:
            continue
        event_day = event["session_date"]
        if isinstance(event_day, date):
            catalyst_days.add(event_day)
        key = (event["symbol"], str(event["episode_id"]))  # type: ignore[arg-type]
        previous = independent.get(key)
        if previous is None or event["decision_at"] < previous[0]["decision_at"]:
            independent[key] = (event, future_bars)

    independent_events = sorted(
        independent.values(),
        key=lambda pair: (
            pair[0]["session_date"], pair[0]["decision_at"],
            pair[0]["symbol"], str(pair[0]["episode_id"]),
        ),
    )
    sample_size = len(independent_events)
    probabilities: dict[str, float | None] = {key: None for key in _TARGET_KEYS}
    empty_metrics = {
        "wilson_lower_bound": None,
        "net_mean_lower_bound": None,
        "expected_time_to_target_minutes": None,
        "failure_rate": None,
    }
    statistics: dict[str, dict[str, float | None]] = {
        key: dict(empty_metrics) for key in _TARGET_KEYS
    }
    reasons: list[str] = []
    if sample_size == 0:
        reasons.append("NO_MATCHING_EVENTS")
    if sample_size < _MIN_TARGET_SAMPLE:
        reasons.append("INSUFFICIENT_SAMPLE")

    selected_target: float | None = None
    if sample_size >= _MIN_TARGET_SAMPLE:
        evaluations: list[tuple[float, float, float]] = []
        for target, target_key in zip(_NET_TARGETS, _TARGET_KEYS):
            hit_count = 0
            net_outcomes: list[float] = []
            success_minutes: list[float] = []
            for event, future_bars in independent_events:
                event_entry = event["entry"]
                event_stop = event["stop"]
                event_spread = max(event["spread"], candidate_spread)
                event_fees = max(event["fees"], candidate_fees)
                target_price = event_entry + target + event_spread + event_fees  # type: ignore[operator]
                touch = first_touch(event["future_bars"], event_stop, target_price)  # type: ignore[arg-type]
                if touch == "TARGET":
                    hit_count += 1
                    touch_at = next(
                        stamp + _BAR for stamp, _, high, low, _ in future_bars
                        if low > event_stop and high >= target_price  # type: ignore[operator]
                    )
                    success_minutes.append(
                        (touch_at - event["fill_at"]).total_seconds() / 60  # type: ignore[union-attr]
                    )
                    net_outcomes.append(target)
                elif touch == "STOP":
                    stop_open = next(
                        open_price for _, open_price, _, low, _ in future_bars
                        if low <= event_stop  # type: ignore[operator]
                    )
                    net_outcomes.append(min(event_stop, stop_open) - event_entry - event_spread - event_fees)  # type: ignore[operator]
                else:
                    final_close = future_bars[-1][4]
                    net_outcomes.append(final_close - event_entry - event_spread - event_fees)  # type: ignore[operator]

            probability = hit_count / sample_size
            wilson_lower = _wilson_lower_bound(hit_count, sample_size)
            net_lower = _mean_lower_bound(net_outcomes)
            failure_rate = (sample_size - hit_count) / sample_size
            expected_time = fmean(success_minutes) if success_minutes else None
            probabilities[target_key] = probability
            statistics[target_key] = {
                "wilson_lower_bound": wilson_lower,
                "net_mean_lower_bound": net_lower,
                "expected_time_to_target_minutes": expected_time,
                "failure_rate": failure_rate,
            }
            if (float(candidate_spread) + float(candidate_fees) < target
                    and wilson_lower >= 0.60 and net_lower > 0):
                downside = fmean(max(0.0, -value) for value in net_outcomes)
                risk_adjusted = net_lower / (
                    downside + float(entry) - float(stop) + float(candidate_spread) + float(candidate_fees)  # type: ignore[arg-type]
                )
                evaluations.append((risk_adjusted, -target, target))

        if evaluations:
            selected_target = max(evaluations)[2]
            reasons.append("TARGET_MEETS_FROZEN_THRESHOLDS")
        elif any(
            item["net_mean_lower_bound"] is not None and item["net_mean_lower_bound"] > 0
            for item in statistics.values()
        ):
            reasons.append("NO_TARGET_MEETS_THRESHOLDS")
        else:
            reasons.append("NO_POSITIVE_TARGET")
    return {
        "decision": "RESEARCH_ELIGIBLE" if selected_target is not None else "NO_TRADE",
        "target_probabilities": probabilities,
        "target_statistics": statistics,
        "selected_target": selected_target,
        "sample_size": sample_size,
        "independent_episodes": sample_size,
        "catalyst_days": len(catalyst_days),
        "reasons": reasons,
    }
