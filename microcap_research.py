"""Research-only historical study of news-driven micro-cap rebounds.

Nothing here connects to a broker, places, approves, or simulates orders. The CLI
reads Alpaca *Market Data API* history (``data.alpaca.markets``), never the paper or
live trading API, and prints one JSON report. ``RESEARCH_ELIGIBLE`` only means the
frozen research gates passed for a historical replay; it is not an order approval.

Point-in-time rules:

* ``start < end <= as_of <= now``: nothing after ``as_of`` is requested or used.
* Parameters (cycle thresholds, horizon, train-objective target) are chosen from a
  predeclared grid on the *train* catalyst episodes only; validation confirms them and
  the final chronological holdout is evaluated afterwards, never used for choice.
* Spreads come only from observed historical bid/ask quotes. Alpaca quotes are raw
  (unadjusted) while bars are split-adjusted, so estimates require proof that raw
  and split-adjusted daily bars agree over the window; otherwise no estimate.
* A candidate estimate needs the as-of state. For a same-day (live) ``as_of`` that
  state must come from IBKR live data, which is not part of this phase, so the
  report is ``NO_TRADE``; delayed Alpaca data is never substituted for it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from statistics import fmean
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from microcap_history import AlpacaHistory, CoverageError, parse_utc
from microcap_rebounds import (
    CycleParams,
    _age_band,
    _as_of_spread,
    _mean_lower_bound,
    _price_band,
    _session_of,
    catalyst_episodes,
    confirmed_cycles,
    current_setup,
    eligible_news,
    estimate_targets,
    extract_events,
    first_touch,
)


MODEL_VERSION = "microcap-rebound-research-v2"
TARGETS = (0.50, 1.00, 1.50, 2.00)
TARGET_KEYS = tuple(f"{target:.2f}" for target in TARGETS)
PARAM_GRID: tuple[tuple[CycleParams, int], ...] = tuple(
    (CycleParams(amplitude, separation), horizon)
    for amplitude in (0.10, 0.25, 0.50)
    for separation in (3.0, 10.0)
    for horizon in (30, 60)
)
MIN_CATALYST_EPISODES = 64
MIN_SPLIT_EPISODES = 10
TRAIN_FRACTION = 0.6
VALIDATION_FRACTION = 0.2
MAX_QUOTE_AGE_SECONDS = 10.0
DEFAULT_FEES_PER_SHARE = 0.02
MAX_QUOTE_WINDOWS = 2000
QUOTE_REQUEST_PAUSE_SECONDS = 0.35

_MARKET_TZ = ZoneInfo("America/New_York")
_NEWS_WINDOW = timedelta(hours=48)
_QUOTE_AGE = timedelta(seconds=MAX_QUOTE_AGE_SECONDS)
_NEWS_COMPLETENESS = (
    "UNVERIFIED_BY_PROVIDER: Alpaca news has no completeness guarantee; observed "
    "articles are evidence, but absent articles are never treated as absence of a catalyst"
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unavailable(reason: str) -> dict[str, object]:
    return {"status": "unavailable", "reason": reason}


def _validate_inputs(
    symbol: object, company: object, as_of: object, start: object, end: object,
    feed: object, costs: object, now: object,
) -> tuple[str, str, datetime, datetime, datetime, float, datetime]:
    if not isinstance(symbol, str) or not symbol.strip():
        raise CoverageError("SYMBOLS_INVALID: symbol must be a non-empty string")
    if not isinstance(company, str) or not company.strip():
        raise CoverageError("COMPANY_INVALID: company must be a non-empty string")
    if feed != "iex":
        raise CoverageError(f"FEED_UNSUPPORTED: only the free iex feed is supported, not {feed!r}")
    for name, value in (("as_of", as_of), ("start", start), ("end", end)):
        if not isinstance(value, str):
            raise CoverageError(f"TIMESTAMP_INVALID: {name} must be an RFC-3339 string")
    decision, start_utc, end_utc = parse_utc(as_of), parse_utc(start), parse_utc(end)  # type: ignore[arg-type]
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise CoverageError("TIMESTAMP_INVALID: now must be a timezone-aware datetime")
    if start_utc >= end_utc:
        raise CoverageError("RANGE_INVALID: start must be earlier than end")
    if end_utc > decision:
        raise CoverageError("RANGE_INVALID: end must not be after as_of (no lookahead)")
    if decision > now:
        raise CoverageError("AS_OF_IN_FUTURE: as_of must not be after the current time")
    fees = costs.get("fees_per_share") if isinstance(costs, Mapping) else None
    if isinstance(fees, bool) or not isinstance(fees, (int, float)) or not math.isfinite(fees) or fees < 0:
        raise CoverageError("COSTS_INVALID: costs.fees_per_share must be a finite non-negative number")
    return (symbol.strip().upper(), company.strip(), decision, start_utc, end_utc,
            float(fees), now.astimezone(timezone.utc))


def price_basis_check(
    split_daily: Sequence[Mapping[str, object]], raw_daily: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Verify raw quotes and split-adjusted bars share a basis: identical daily OHLC."""
    if not split_daily and not raw_daily:
        return {"status": "UNVERIFIED", "detail": "NO_DAILY_BARS_TO_COMPARE"}
    split = {bar.get("t"): bar for bar in split_daily}
    raw = {bar.get("t"): bar for bar in raw_daily}
    if set(split) != set(raw):
        return {"status": "MISMATCH", "detail": "DAILY_BAR_SETS_DIFFER"}
    for stamp in sorted(split, key=str):
        for field in ("o", "h", "l", "c"):
            left, right = split[stamp].get(field), raw[stamp].get(field)
            if (not isinstance(left, (int, float)) or not isinstance(right, (int, float))
                    or abs(float(left) - float(right)) > 1e-9 * max(1.0, abs(float(left)))):
                return {"status": "MISMATCH",
                        "detail": f"SPLIT_ADJUSTED_DIFFERS_FROM_RAW at {stamp} field {field}"}
    return {"status": "VERIFIED", "detail": "split-adjusted and raw daily OHLC identical",
            "days_compared": len(split)}


def _catalyst_days(
    news: Sequence[Mapping[str, object]], symbol: str, company: str, start: datetime, as_of: datetime
) -> list[date]:
    return sorted({
        day
        for episode in _catalyst_episodes(news, symbol, company, start, as_of)
        for day in episode["days"]  # type: ignore[union-attr]
    })


def _catalyst_episodes(
    news: Sequence[Mapping[str, object]], symbol: str, company: str, start: datetime, as_of: datetime
) -> list[dict[str, object]]:
    """Independent primary-news episodes and their associated catalyst session days."""
    first_day = start.astimezone(_MARKET_TZ).date()
    last_day = as_of.astimezone(_MARKET_TZ).date()
    result: list[dict[str, object]] = []
    for episode in catalyst_episodes(news, symbol, company, as_of):
        days: set[date] = set()
        for item in episode["articles"]:  # type: ignore[union-attr]
            created = parse_utc(item["created_at"])  # type: ignore[index,arg-type]
            day = created.astimezone(_MARKET_TZ).date()
            while day <= (created + _NEWS_WINDOW).astimezone(_MARKET_TZ).date():
                opens = datetime.combine(day, time(4), _MARKET_TZ).astimezone(timezone.utc)
                closes = datetime.combine(day, time(20), _MARKET_TZ).astimezone(timezone.utc)
                if (day.weekday() < 5 and first_day <= day < last_day
                        and created < closes and created + _NEWS_WINDOW >= opens):
                    days.add(day)
                day += timedelta(days=1)
        if days:
            result.append({**episode, "days": sorted(days)})
    return result


def plan_quote_windows(
    bars: Sequence[Mapping[str, object]], news: Sequence[Mapping[str, object]],
    symbol: str, company: str, as_of: object, *, include_as_of: bool, include_history: bool = True,
) -> list[tuple[datetime, datetime]]:
    """Merged [start, end) quote windows around every possible catalyst decision before ``as_of``.

    Only quotes within ``MAX_QUOTE_AGE_SECONDS`` of a decision can affect a label, so these
    windows reproduce the full-stream result without downloading every quote.
    """
    decision_time = parse_utc(as_of) if isinstance(as_of, str) else as_of
    as_of_day = decision_time.astimezone(_MARKET_TZ).date()
    windows: list[tuple[datetime, datetime]] = []
    if include_history:
        decisions: set[datetime] = set()
        for params in {params for params, _ in PARAM_GRID}:
            for cycle in confirmed_cycles(bars, decision_time, params=params):
                if cycle.session_date < as_of_day:
                    decisions.add(cycle.confirmed_at)
        for decision in sorted(decisions):
            if eligible_news(news, symbol, company, decision):
                windows.append((decision - _QUOTE_AGE,
                                min(decision + _QUOTE_AGE + timedelta(microseconds=1), decision_time)))
    if include_as_of:
        windows.append((decision_time - _QUOTE_AGE, decision_time))
    merged: list[tuple[datetime, datetime]] = []
    for window_start, window_end in sorted(windows):
        if merged and window_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], window_end))
        else:
            merged.append((window_start, window_end))
    return merged


def _bar_coverage(
    bars: Sequence[Mapping[str, object]], start: datetime, end: datetime, as_of: datetime
) -> dict[str, object]:
    stamps = sorted(parse_utc(bar["t"]) for bar in bars)  # type: ignore[arg-type]
    observed = {stamp.astimezone(_MARKET_TZ).date() for stamp in stamps}
    missing_minutes = 0
    for previous, following in zip(stamps, stamps[1:]):
        if _session_of(previous) == _session_of(following):
            missing_minutes += max(0, int((following - previous).total_seconds() // 60) - 1)
    weekdays_without_bars, day = 0, start.astimezone(_MARKET_TZ).date()
    while day <= (end - timedelta(microseconds=1)).astimezone(_MARKET_TZ).date():
        if day.weekday() < 5 and day not in observed:
            weekdays_without_bars += 1
        day += timedelta(days=1)
    return {
        "status": "observed" if stamps else "unavailable",
        "count": len(stamps),
        "first": _iso(stamps[0]) if stamps else None,
        "last": _iso(stamps[-1]) if stamps else None,
        "observed_days": len(observed),
        "weekdays_without_bars": weekdays_without_bars,
        "missing_minutes_within_sessions": missing_minutes,
        "last_bar_age_seconds_at_as_of": (
            (as_of - stamps[-1] - timedelta(minutes=1)).total_seconds() if stamps else None
        ),
        "note": "IEX-only bars: a missing minute may mean no IEX trade, not missing data",
    }


def _context(bars: Sequence[Mapping[str, object]] | None, as_of: datetime, daily: bool) -> dict[str, object]:
    if not bars:
        return _unavailable("NO_BARS_RETURNED")
    as_of_day = as_of.astimezone(_MARKET_TZ).date()
    complete = []
    for bar in bars:
        stamp = parse_utc(bar["t"])  # type: ignore[arg-type]
        done = stamp.astimezone(_MARKET_TZ).date() < as_of_day if daily else stamp + timedelta(hours=1) <= as_of
        if done:
            complete.append((stamp, bar))
    if not complete:
        return _unavailable("NO_COMPLETE_BARS_BEFORE_AS_OF")
    complete.sort(key=lambda pair: pair[0])
    last_stamp, last = complete[-1]
    return {"status": "available", "bars_used": len(complete), "last_t": _iso(last_stamp),
            "last_close": last.get("c"), "adjustment": "split"}


def _net_outcome(event: Mapping[str, object], target: float | None) -> tuple[float, bool]:
    """Per-share net outcome after the event's own observed spread and fees."""
    entry, stop = float(event["entry"]), float(event["stop"])  # type: ignore[arg-type]
    costs = float(event["spread"]) + float(event["fees"])  # type: ignore[arg-type]
    bars: list[Mapping[str, object]] = event["future_bars"]  # type: ignore[assignment]
    final_close = float(bars[-1]["c"])  # type: ignore[arg-type]
    if target is None:
        return final_close - entry - costs, False
    touch = first_touch(bars, stop, entry + target + costs)
    if touch == "TARGET":
        return target, True
    if touch == "STOP":
        stop_open = next(float(bar["o"]) for bar in bars if float(bar["l"]) <= stop)  # type: ignore[arg-type]
        return min(stop, stop_open) - entry - costs, False
    return final_close - entry - costs, False


def _max_drawdown(outcomes: Sequence[float]) -> float:
    peak = equity = drawdown = 0.0
    for value in outcomes:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _metrics(
    events: Sequence[Mapping[str, object]], target: float | None,
    cache: dict[tuple[int, float | None], tuple[float, bool]] | None = None,
) -> dict[str, object]:
    cache = {} if cache is None else cache
    results = []
    for event in events:
        key = (id(event), target)
        if key not in cache:
            cache[key] = _net_outcome(event, target)
        results.append(cache[key])
    outcomes = [value for value, _ in results]
    return {
        "event_days": len({event["session_date"] for event in events}),
        "independent_episodes": len(outcomes),
        "hit_rate": (sum(hit for _, hit in results) / len(results)) if target is not None else None,
        "mean_net": fmean(outcomes),
        "total_net": sum(outcomes),
        "net_mean_lower_bound": _mean_lower_bound(outcomes),
        "max_drawdown": _max_drawdown(outcomes),
    }


def _config_label(params: CycleParams, horizon: int) -> dict[str, object]:
    return {"min_amplitude": params.min_amplitude, "min_separation_minutes": params.min_separation_minutes,
            "max_gap_minutes": params.max_gap_minutes, "horizon_minutes": horizon}


def _study(
    bars: Sequence[Mapping[str, object]], news: Sequence[Mapping[str, object]],
    quotes: Sequence[Mapping[str, object]], symbol: str, company: str, as_of: datetime,
    fees: float, episodes: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object] | None, list[str]]:
    """Chronological train/validation/holdout study; returns (study, frozen choice, blocking reasons)."""
    blocking: list[str] = []
    train_count = int(len(episodes) * TRAIN_FRACTION)
    validation_count = int(len(episodes) * VALIDATION_FRACTION)
    parts = {
        "train": episodes[:train_count],
        "validation": episodes[train_count:train_count + validation_count],
        "holdout": episodes[train_count + validation_count:],
    }
    split_of_episode = {
        str(episode["episode_id"]): name
        for name, members in parts.items()
        for episode in members
    }
    study: dict[str, object] = {
        "catalyst_days": len({day for episode in episodes for day in episode["days"]}),  # type: ignore[union-attr]
        "independent_episodes": len(episodes),
        "split": {
            name: {
                "independent_episodes": len(members),
                "days": len({day for episode in members for day in episode["days"]}),  # type: ignore[union-attr]
                "first": next((day.isoformat() for episode in members for day in episode["days"]), None),
                "last": next((day.isoformat() for episode in reversed(members)
                              for day in reversed(episode["days"])), None),  # type: ignore[arg-type]
            }
            for name, members in parts.items()
        },
        "grid_size": len(PARAM_GRID),
        "min_independent_episodes_per_split": MIN_SPLIT_EPISODES,
        "boundary_policy": "overlapping 48-hour primary-news windows are merged and assigned to one split",
    }
    by_config: list[dict[str, list[dict[str, object]]]] = []
    for params, horizon in PARAM_GRID:
        events = extract_events(bars, news, symbol, company, as_of, params=params, quotes=quotes,
                                fees=fees, horizon_minutes=horizon,
                                max_quote_age_seconds=MAX_QUOTE_AGE_SECONDS)
        first_per_episode: dict[str, dict[str, object]] = {}
        for event in sorted(events, key=lambda item: item["decision_at"]):  # type: ignore[arg-type,return-value]
            first_per_episode.setdefault(str(event["episode_id"]), event)
        grouped: dict[str, list[dict[str, object]]] = {name: [] for name in parts}
        all_events: dict[str, list[dict[str, object]]] = {name: [] for name in parts}
        for episode_id, event in first_per_episode.items():
            if episode_id in split_of_episode:
                grouped[split_of_episode[episode_id]].append(event)
        for event in events:
            episode_id = str(event["episode_id"])
            if episode_id in split_of_episode:
                all_events[split_of_episode[episode_id]].append(event)
        by_config.append({**grouped, **{f"all_{name}": items for name, items in all_events.items()}})

    cache: dict[tuple[int, float | None], tuple[float, bool]] = {}
    best: tuple[float, int, float] | None = None
    for index, grouped in enumerate(by_config):
        if len(grouped["train"]) < MIN_SPLIT_EPISODES:
            continue
        for target in TARGETS:
            score = _metrics(grouped["train"], target, cache)["net_mean_lower_bound"]
            if best is None or score > best[0]:  # type: ignore[operator]
                best = (score, index, target)  # type: ignore[assignment]
    if best is None:
        blocking.append("INSUFFICIENT_TRAIN_EPISODES")
        for name in ("selection", "validation", "holdout"):
            study[name] = _unavailable("INSUFFICIENT_TRAIN_EPISODES")
        return study, None, blocking
    _, index, train_target = best
    params, horizon = PARAM_GRID[index]
    chosen = by_config[index]
    study["selection"] = {
        "status": "available",
        "objective": "max one-sided 95% lower bound of mean net per-share outcome on train episodes only",
        "config": _config_label(params, horizon),
        "train_target": train_target,
        "train": _metrics(chosen["train"], train_target, cache),
    }
    if best[0] <= 0:
        blocking.append("TRAIN_NOT_POSITIVE")
    if len(chosen["validation"]) < MIN_SPLIT_EPISODES:
        blocking.append("INSUFFICIENT_VALIDATION_EPISODES")
        study["validation"] = _unavailable("INSUFFICIENT_VALIDATION_EPISODES")
    else:
        validation = _metrics(chosen["validation"], train_target, cache)
        study["validation"] = {"status": "available", **validation}
        if validation["mean_net"] <= 0:  # type: ignore[operator]
            blocking.append("VALIDATION_NOT_POSITIVE")
    holdout_events = chosen["holdout"]
    if len(holdout_events) < MIN_SPLIT_EPISODES:
        study["holdout"] = _unavailable("INSUFFICIENT_HOLDOUT_EPISODES")
    else:
        prior = chosen["train"] + chosen["validation"]
        study["holdout"] = {
            "status": "available",
            "evaluated_after_freezing": True,
            "first": holdout_events[0]["session_date"].isoformat(),  # type: ignore[union-attr]
            "last": holdout_events[-1]["session_date"].isoformat(),  # type: ignore[union-attr]
            "targets": {key: _metrics(holdout_events, target, cache) for key, target in zip(TARGET_KEYS, TARGETS)},
            "baseline_hold_to_horizon": _metrics(holdout_events, None, cache),
            "baseline_no_trade": {"mean_net": 0.0, "total_net": 0.0, "max_drawdown": 0.0},
            "calibration": {
                key: {"predicted_hit_rate_train_validation": _metrics(prior, target, cache)["hit_rate"],
                      "holdout_hit_rate": _metrics(holdout_events, target, cache)["hit_rate"],
                      "holdout_event_days": len(holdout_events),
                      "holdout_independent_episodes": len(holdout_events)}
                for key, target in zip(TARGET_KEYS, TARGETS)
            },
        }
    frozen = {"params": params, "horizon": horizon, "train_target": train_target,
              "estimation_events": chosen["all_train"] + chosen["all_validation"]}
    return study, frozen, blocking


def report(
    *, symbol: str, company: str, as_of: str, start: str, end: str, feed: str,
    bars: Sequence[Mapping[str, object]], news: Sequence[Mapping[str, object]],
    quotes: Sequence[Mapping[str, object]] | None, costs: Mapping[str, object], now: datetime,
    price_basis: Mapping[str, object] | None = None,
    context: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> dict[str, object]:
    """Pure research report from injected data; raises ``CoverageError`` only on invalid input."""
    symbol, company, decision, start_utc, end_utc, fees, now_utc = _validate_inputs(
        symbol, company, as_of, start, end, feed, costs, now)
    live = decision.astimezone(_MARKET_TZ).date() >= now_utc.astimezone(_MARKET_TZ).date()
    session = _session_of(decision, end_inclusive=True)
    blocking: list[str] = []
    targets: dict[str, dict[str, object]] = {key: _unavailable("NO_ESTIMATE") for key in TARGET_KEYS}
    result: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "research_only": True,
        "order_approval": False,
        "orders": "NONE: research only; no broker connection; paper execution is a later phase",
        "symbol": symbol,
        "company": company,
        "session": {"as_of": _iso(decision), "date": session[0].isoformat() if session else None,
                    "name": session[1] if session else None,
                    "mode": "LIVE_REQUIRED" if live else "HISTORICAL_REPLAY"},
        "history": {"start": _iso(start_utc), "end": _iso(end_utc)},
        "feed": feed,
        "adjustment": {"bars": "split", "quotes": "raw",
                       "basis_check": dict(price_basis) if price_basis else {"status": "UNVERIFIED"}},
        "costs": {"fees_per_share": fees, "candidate_spread": None,
                  "max_quote_age_seconds": MAX_QUOTE_AGE_SECONDS,
                  "fill_model": "historical events fill at the first IEX ask after the decision; "
                                "spread from the latest IEX quote at or before it"},
        "context": {"daily": _context((context or {}).get("1Day"), decision, True),
                    "hourly": _context((context or {}).get("1Hour"), decision, False)},
        "catalyst": {"status": "unavailable", "reason": "NEWS_COVERAGE_UNVERIFIED", "articles": []},
        "cycle_counts_by_grid": {},
        "cycle_count": None,
        "signal_reference": _unavailable("NOT_EVALUATED"),
        "feature_bucket": None,
        "target_probabilities": targets,
        "selected_target": None,
        "sample_size": 0,
        "uncertainty": None,
        "study": {"catalyst_days": 0, "independent_episodes": 0},
    }
    bars_ok = bool(bars)
    try:
        bar_coverage = _bar_coverage(bars, start_utc, end_utc, decision)
        if bars:
            confirmed_cycles(bars, decision, params=PARAM_GRID[0][0])
    except (CoverageError, KeyError, TypeError) as error:
        blocking.append(f"BAR_DATA_REJECTED: {error}")
        bar_coverage, bars_ok = _unavailable("BAR_DATA_REJECTED"), False
    if not bars:
        blocking.append("BAR_COVERAGE_UNAVAILABLE")
    news_stamps = sorted(parse_utc(item["created_at"]) for item in news  # type: ignore[arg-type]
                         if isinstance(item, Mapping) and isinstance(item.get("created_at"), str))
    quote_list = list(quotes) if quotes else []
    quote_stamps = sorted(parse_utc(item["t"]) for item in quote_list)  # type: ignore[arg-type]
    result["coverage"] = {
        "minute_bars": bar_coverage,
        "news": {"status": "observed" if news else "unavailable", "count": len(news),
                 "first": _iso(news_stamps[0]) if news_stamps else None,
                 "last": _iso(news_stamps[-1]) if news_stamps else None,
                 "sources": sorted({str(item.get("source")) for item in news if isinstance(item, Mapping)}),
                 "completeness": _NEWS_COMPLETENESS},
        "quotes": {"status": "observed" if quote_list else "unavailable", "count": len(quote_list),
                   "first": _iso(quote_stamps[0]) if quote_stamps else None,
                   "last": _iso(quote_stamps[-1]) if quote_stamps else None,
                   "source": "IEX top-of-book via Alpaca Market Data API (not consolidated NBBO)",
                   "price_basis": "raw"},
    }
    if not news:
        blocking.append("NEWS_COVERAGE_UNVERIFIED")
    if not quote_list:
        blocking.append("QUOTE_COVERAGE_UNAVAILABLE")
    basis_status = price_basis.get("status") if price_basis else None
    if basis_status == "MISMATCH":
        blocking.append("PRICE_BASIS_MISMATCH")
    elif basis_status != "VERIFIED":
        blocking.append("PRICE_BASIS_UNVERIFIED")

    catalysts: list[Mapping[str, object]] = []
    if news:
        catalysts = eligible_news(news, symbol, company, decision)
        result["catalyst"] = {
            "status": "available" if catalysts else "none_eligible",
            "articles": [{
                "id": item["id"], "source": item.get("source"), "headline": item.get("headline"),
                "created_at": _iso(parse_utc(item["created_at"])),  # type: ignore[arg-type]
                "age_minutes": (decision - parse_utc(item["created_at"])).total_seconds() / 60,  # type: ignore[arg-type]
                "relevance": "PRIMARY_SUBJECT_HEADLINE_WITH_SYMBOL_TAG",
            } for item in catalysts],
            "sentiment": _unavailable("NOT_SCORED_IN_RESEARCH_PHASE"),
            "impact": _unavailable("NOT_SCORED_IN_RESEARCH_PHASE"),
        }
        if not catalysts:
            blocking.append("NO_ELIGIBLE_CATALYST_AT_AS_OF")
        episodes = _catalyst_episodes(news, symbol, company, start_utc, decision)
        result["study"] = {
            "catalyst_days": len({day for episode in episodes for day in episode["days"]}),  # type: ignore[union-attr]
            "independent_episodes": len(episodes),
        }

    if bars_ok and session is not None:
        counts = {}
        for params in dict.fromkeys(params for params, _ in PARAM_GRID):
            counts[f"amp={params.min_amplitude}|sep={params.min_separation_minutes}"] = sum(
                1 for cycle in confirmed_cycles(bars, decision, params=params)
                if (cycle.session_date, cycle.session) == session)
        result["cycle_counts_by_grid"] = counts
    if session is None:
        blocking.append("OUTSIDE_SESSION")

    frozen = None
    research_ready = bars_ok and bool(news) and bool(quote_list) and basis_status == "VERIFIED"
    episodes = _catalyst_episodes(news, symbol, company, start_utc, decision) if news else []
    if research_ready and len(episodes) < MIN_CATALYST_EPISODES:
        blocking.append("INSUFFICIENT_INDEPENDENT_EPISODES")
    if not research_ready or len(episodes) < MIN_CATALYST_EPISODES:
        reason = "INSUFFICIENT_INDEPENDENT_EPISODES" if research_ready else "INPUT_COVERAGE_INSUFFICIENT"
        result["study"].update({name: _unavailable(reason)  # type: ignore[union-attr]
                                for name in ("selection", "validation", "holdout")})
    else:
        study, frozen, study_blocking = _study(
            bars, news, quote_list, symbol, company, decision, fees, episodes)
        result["study"] = study
        blocking.extend(study_blocking)

    spread = None
    if live:
        blocking.append("LIVE_IBKR_STATE_REQUIRED")
        result["signal_reference"] = _unavailable("LIVE_IBKR_STATE_REQUIRED")
    elif quote_list:
        spread = _as_of_spread(quote_list, decision, _QUOTE_AGE)
        if spread is None:
            blocking.append("CANDIDATE_QUOTE_UNAVAILABLE")
    result["costs"]["candidate_spread"] = spread  # type: ignore[index]

    if frozen is not None:
        params = frozen["params"]
        result["cycle_count"] = result["cycle_counts_by_grid"].get(  # type: ignore[union-attr]
            f"amp={params.min_amplitude}|sep={params.min_separation_minutes}")
    if frozen is None and not live:
        result["signal_reference"] = _unavailable("NO_FROZEN_PARAMETERS")
    elif frozen is not None and not live and spread is None:
        result["signal_reference"] = _unavailable("CANDIDATE_QUOTE_UNAVAILABLE")
    elif frozen is not None and spread is not None:
        setup, why = current_setup(bars, spread, fees, decision, params=frozen["params"])
        if setup is None:
            blocking.append(f"NO_SETUP: {why}")
            result["signal_reference"] = _unavailable(why)
        else:
            result["signal_reference"] = {
                "status": "available",
                "entry_signal_reference": setup["entry"],
                "stop": setup["stop"],
                "buy_zone": {"above_stop": setup["stop"], "at_or_below": setup["entry"]},
                "note": "completed-bar close reference at as_of; not a fill, quote, or live price",
            }
            if catalysts and session is not None:
                age = decision - parse_utc(catalysts[0]["created_at"])  # type: ignore[arg-type]
                bucket = f"{session[1]}|price:{_price_band(setup['entry'])}|news_age:{_age_band(age)}"
                result["feature_bucket"] = bucket
                estimate = estimate_targets(
                    frozen["estimation_events"], setup["entry"], setup["stop"],
                    {"spread": spread, "fees": fees}, decision, feature_bucket=bucket)
                _apply_estimate(result, targets, estimate, frozen, blocking)

    decision_label = "NO_TRADE"
    if not blocking and result["selected_target"] is not None:
        decision_label = "RESEARCH_ELIGIBLE"
    if decision_label == "NO_TRADE" and not blocking:
        blocking.append("NO_TARGET_SELECTED")
    for key, value in targets.items():
        if value.get("status") == "unavailable" and value.get("reason") == "NO_ESTIMATE":
            targets[key] = _unavailable(blocking[0] if blocking else "NO_ESTIMATE")
    result["decision"] = decision_label
    result["reasons"] = blocking if blocking else [
        "RESEARCH_GATES_PASSED_NOT_AN_ORDER_APPROVAL", "TARGET_MEETS_FROZEN_THRESHOLDS"]
    result["freshness"] = {
        "now": _iso(now_utc),
        "as_of": _iso(decision),
        "minute_bar_age_seconds_at_as_of": (
            bar_coverage.get("last_bar_age_seconds_at_as_of") if isinstance(bar_coverage, dict) else None),
        "latest_news_age_minutes_at_as_of": (
            (decision - max(s for s in news_stamps if s <= decision)).total_seconds() / 60
            if any(s <= decision for s in news_stamps) else None),
        "latest_quote_age_seconds_at_as_of": (
            (decision - max(s for s in quote_stamps if s <= decision)).total_seconds()
            if any(s <= decision for s in quote_stamps) else None),
        "data_fetched_at": max((str(bar.get("fetched_at")) for bar in bars if bar.get("fetched_at")), default=None),
    }
    return result


def _apply_estimate(
    result: dict[str, object], targets: dict[str, dict[str, object]], estimate: Mapping[str, object],
    frozen: Mapping[str, object], blocking: list[str],
) -> None:
    probabilities: Mapping[str, float | None] = estimate["target_probabilities"]  # type: ignore[assignment]
    statistics: Mapping[str, Mapping[str, object]] = estimate["target_statistics"]  # type: ignore[assignment]
    sample = estimate["sample_size"]
    reasons: list[str] = list(estimate["reasons"])  # type: ignore[arg-type]
    result["sample_size"] = sample
    result["uncertainty"] = {
        "method": "95% Wilson lower bound on hit rate; one-sided 95% t lower bound on mean net",
        "estimation_split": "train+validation independent episodes only (holdout excluded)",
    }
    for key in TARGET_KEYS:
        probability = probabilities.get(key)
        if probability is None:
            targets[key] = _unavailable(reasons[0] if reasons else "NO_ESTIMATE")
        else:
            targets[key] = {"status": "available", "probability": probability,
                            "sample_size": sample, **statistics[key]}
    selected = estimate["selected_target"]
    if estimate["decision"] != "RESEARCH_ELIGIBLE" or selected is None:
        blocking.extend(reason for reason in reasons if reason != "TARGET_MEETS_FROZEN_THRESHOLDS")
        if not reasons:
            blocking.append("NO_TARGET_SELECTED")
        return
    key = f"{selected:.2f}"
    result["selected_target"] = {
        "net_target": selected,
        "expected_time_to_target_minutes": statistics[key]["expected_time_to_target_minutes"],
        "horizon_minutes": frozen["horizon"],
    }
    holdout = result["study"].get("holdout", {})  # type: ignore[union-attr]
    if holdout.get("status") != "available":
        blocking.append("HOLDOUT_UNAVAILABLE")
        return
    target_metrics = holdout["targets"][key]
    if target_metrics["mean_net"] <= 0:
        blocking.append("HOLDOUT_NOT_POSITIVE")
    if target_metrics["mean_net"] <= holdout["baseline_hold_to_horizon"]["mean_net"]:
        blocking.append("HOLDOUT_NOT_BETTER_THAN_BASELINE")


def _fetch_and_report(
    client: object, args: argparse.Namespace, now: datetime, sleep: Callable[[float], None]
) -> dict[str, object]:
    symbol = args.symbol.strip().upper()
    symbols = [symbol]
    start, end, decision = parse_utc(args.start), parse_utc(args.end), parse_utc(args.as_of)
    minute = client.fetch_bars(symbols, args.start, args.end, timeframe="1Min", feed=args.feed)  # type: ignore[attr-defined]
    hourly = client.fetch_bars(symbols, args.start, args.end, timeframe="1Hour", feed=args.feed)  # type: ignore[attr-defined]
    daily = client.fetch_bars(symbols, args.start, args.end, timeframe="1Day", feed=args.feed)  # type: ignore[attr-defined]
    daily_raw = client.fetch_bars(  # type: ignore[attr-defined]
        symbols, args.start, args.end, timeframe="1Day", feed=args.feed, adjustment="raw")
    news = client.fetch_news(symbols, _iso(start - _NEWS_WINDOW), args.end)  # type: ignore[attr-defined]
    live = decision.astimezone(_MARKET_TZ).date() >= now.astimezone(_MARKET_TZ).date()
    enough_episodes = len(_catalyst_episodes(news, symbol, args.company, start, decision)) >= MIN_CATALYST_EPISODES
    try:
        windows = plan_quote_windows(minute, news, symbol, args.company, decision,
                                     include_as_of=not live, include_history=enough_episodes)
    except CoverageError:
        windows = plan_quote_windows([], news, symbol, args.company, decision,
                                     include_as_of=not live, include_history=False)
    quotes: list[Mapping[str, object]] | None = []
    budget_note = None
    if len(windows) > MAX_QUOTE_WINDOWS:
        quotes, budget_note = None, "QUOTE_REQUEST_BUDGET_EXCEEDED"
    else:
        seen: set[tuple[object, object, object]] = set()
        for index, (window_start, window_end) in enumerate(windows):
            if index:
                sleep(QUOTE_REQUEST_PAUSE_SECONDS)
            for item in client.fetch_quotes(symbols, _iso(window_start), _iso(window_end), feed=args.feed):  # type: ignore[attr-defined]
                key = (item.get("t"), item.get("bid"), item.get("ask"))
                if key not in seen:
                    seen.add(key)
                    quotes.append(item)  # type: ignore[union-attr]
        quotes.sort(key=lambda item: parse_utc(item["t"]))  # type: ignore[union-attr,arg-type]
    result = report(
        symbol=symbol, company=args.company, as_of=args.as_of, start=args.start, end=args.end,
        feed=args.feed, bars=minute, news=news, quotes=quotes,
        costs={"fees_per_share": args.fees_per_share}, now=now,
        price_basis=price_basis_check(daily, daily_raw), context={"1Hour": hourly, "1Day": daily},
    )
    quote_coverage = result["coverage"]["quotes"]  # type: ignore[index]
    quote_coverage["windows_requested"] = len(windows)
    quote_coverage["window_seconds_each_side"] = MAX_QUOTE_AGE_SECONDS
    if budget_note:
        quote_coverage["note"] = budget_note
    return result


def _error(message: str) -> None:
    print(json.dumps({"status": "ERROR", "decision": "NO_TRADE", "error": message}), file=sys.stderr)


def main(
    argv: Sequence[str] | None = None, *,
    client_factory: Callable[[], object] = AlpacaHistory.from_environment,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time_module.sleep,
) -> int:
    parser = argparse.ArgumentParser(description="Research-only micro-cap rebound report (no orders).")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--as-of", required=True, help="RFC-3339 timestamp with timezone, e.g. 2024-05-15T14:31:00Z")
    parser.add_argument("--start", required=True, help="RFC-3339 history start")
    parser.add_argument("--end", required=True, help="RFC-3339 history end (exclusive, <= --as-of)")
    parser.add_argument("--feed", default="iex", help="only the free iex feed is supported")
    parser.add_argument("--fees-per-share", type=float, default=DEFAULT_FEES_PER_SHARE,
                        help="predeclared round-trip fees per share in USD")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:
        return int(exit_request.code or 0)
    try:
        current = now()
        _validate_inputs(args.symbol, args.company, args.as_of, args.start, args.end, args.feed,
                         {"fees_per_share": args.fees_per_share}, current)
    except CoverageError as error:
        _error(str(error))
        return 2
    try:
        result = _fetch_and_report(client_factory(), args, current, sleep)
    except CoverageError as error:
        _error(str(error))
        return 3
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
