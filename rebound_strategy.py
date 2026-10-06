"""Micro-cap rebound strategy (paper only): buy the dip after >= 2 spike-and-fade cycles."""

import math
import os
import threading
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

STRATEGY_NAME = "microcap_rebound_v1"
TIMEFRAME = "1m"

MIN_PRICE = 0.50
MAX_PRICE = 20.00
SPIKE_MIN_PCT = 8.0
SPIKE_MAX_BARS = 15
FADE_MIN_RETRACE = 0.40
MIN_CYCLES = 2
ENTRY_RETRACE_MIN = 0.40
ENTRY_RETRACE_MAX = 0.70
MIN_ROOM_TO_PEAK = 0.25
MAX_SPREAD_PCT_RTH = 0.8
MAX_SPREAD_PCT_EXT = 1.5
MAX_POSITION_USD = 1000.0
MAX_RISK_USD = 55.0  # headroom under the server's $60 MAX_RISK_PER_TRADE_USD
STOP_BUFFER_PCT = 0.005
MAX_STOP_PCT = 0.06
TARGET_R = 3.0
NEWS_MIN_SCORE = 0.5
NEGATIVE_EVENTS = frozenset({
    "OFFERING",
    "DILUTION",
    "LAWSUIT",
    "BANKRUPTCY",
    "DELISTING",
    "REVERSE_SPLIT",
    "TRADING_HALT",
})

NEW_YORK = ZoneInfo("America/New_York")
ENTRY_START = dtime(4, 0)
ENTRY_END = dtime(19, 30)
RTH_START = dtime(9, 30)
RTH_END = dtime(16, 0)
BAR_SECONDS = 60
HISTORY_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class Cycle:
    low_index: int
    peak_index: int
    fade_index: int
    low: float
    peak: float


def _num(bar, key):
    value = bar.get(key) if isinstance(bar, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("BAR_INVALID")
    value = float(value)
    if not math.isfinite(value) or (key != "volume" and value <= 0) or value < 0:
        raise ValueError("BAR_INVALID")
    return value


def _find_spike(bars, start):
    for j in range(start, len(bars)):
        window_start = max(start, j - SPIKE_MAX_BARS)
        low_index = min(range(window_start, j + 1), key=lambda k: (bars[k]["low"], k))
        if low_index < j and bars[j]["high"] >= bars[low_index]["low"] * (1 + SPIKE_MIN_PCT / 100):
            return low_index, j
    return None


def _find_fade(bars, low_index, spike_index):
    low = bars[low_index]["low"]
    peak_index = spike_index
    for m in range(spike_index + 1, len(bars)):
        if bars[m]["high"] > bars[peak_index]["high"]:
            peak_index = m
            continue
        spike_range = bars[peak_index]["high"] - low
        if bars[peak_index]["high"] - bars[m]["low"] >= FADE_MIN_RETRACE * spike_range:
            return peak_index, m
    return peak_index, None


def detect_cycles(bars):
    """Return completed spike-and-fade cycles, oldest first. Bars must be validated."""
    cycles = []
    start = 0
    while start < len(bars):
        spike = _find_spike(bars, start)
        if spike is None:
            break
        peak_index, fade_index = _find_fade(bars, *spike)
        if fade_index is None:
            break
        low_index = spike[0]
        cycles.append(Cycle(low_index, peak_index, fade_index,
                            bars[low_index]["low"], bars[peak_index]["high"]))
        start = fade_index
    return cycles


def _clean_bars(bars, now):
    today = now.astimezone(NEW_YORK).date()
    cutoff = now.timestamp() - BAR_SECONDS
    cleaned = []
    for bar in bars or []:
        stamp = bar.get("timestamp") if isinstance(bar, dict) else None
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise ValueError("BAR_INVALID")
        if stamp > cutoff:
            continue
        if datetime.fromtimestamp(stamp, timezone.utc).astimezone(NEW_YORK).date() != today:
            continue
        values = {key: _num(bar, key) for key in ("open", "high", "low", "close", "volume")}
        if values["low"] > min(values["open"], values["close"]) or \
                values["high"] < max(values["open"], values["close"]):
            raise ValueError("BAR_INVALID")
        cleaned.append({"timestamp": stamp, **values})
    cleaned.sort(key=lambda item: item["timestamp"])
    return cleaned


def paper_guard(env=None):
    env = os.environ if env is None else env
    return (str(env.get("IB_PORT", "")).strip() == "7497"
            and str(env.get("IB_ACCOUNT", "")).strip().upper().startswith("DU"))


def news_verdict(ai_result):
    """Strict news gate: (passed, reason). Anything unexpected fails closed."""
    if not isinstance(ai_result, dict):
        return False, "NEWS_GATE_UNAVAILABLE"
    status = str(ai_result.get("status") or "").strip().upper()
    if status != "PASS":
        return False, f"AI_{status or 'NONE'}"
    count = ai_result.get("news_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return False, "NO_RECENT_NEWS"
    ai = ai_result.get("ai")
    if not isinstance(ai, dict):
        return False, "NEWS_GATE_UNAVAILABLE"
    score = ai.get("news_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) \
            or not math.isfinite(score) or score < NEWS_MIN_SCORE:
        return False, "NEWS_NOT_STRONG"
    if str(ai.get("event_type") or "").strip().upper() in NEGATIVE_EVENTS:
        return False, "NEWS_NEGATIVE_EVENT"
    return True, "NEWS_STRONG_POSITIVE"


def _skip(symbol, reason, **diagnostics):
    return {"symbol": symbol, "strategy": STRATEGY_NAME, "timeframe": TIMEFRAME,
            "qualified": False, "skip_reason": reason, "rebound": diagnostics}


def _spread_limit(now):
    local = now.astimezone(NEW_YORK).time()
    return MAX_SPREAD_PCT_RTH if RTH_START <= local < RTH_END else MAX_SPREAD_PCT_EXT


def analyze(candidate, bars, quote, now, env=None):
    """Return a qualified BUY signal dict or a skip dict with ``skip_reason``."""
    symbol = str((candidate or {}).get("symbol") or "").strip().upper()
    if not symbol:
        return _skip("", "SKIP_INPUT_INVALID")
    if not paper_guard(env):
        return _skip(symbol, "SKIP_NOT_PAPER")
    local = now.astimezone(NEW_YORK)
    if local.weekday() >= 5 or not (ENTRY_START <= local.time() < ENTRY_END):
        return _skip(symbol, "SKIP_SESSION")
    try:
        bid, ask = float((quote or {}).get("bid")), float((quote or {}).get("ask"))
    except (TypeError, ValueError):
        return _skip(symbol, "SKIP_QUOTE_INVALID")
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask < bid:
        return _skip(symbol, "SKIP_QUOTE_INVALID")
    if not (MIN_PRICE <= ask <= MAX_PRICE):
        return _skip(symbol, "SKIP_PRICE", ask=ask)
    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    if spread_pct > _spread_limit(now):
        return _skip(symbol, "SKIP_SPREAD", spread_pct=round(spread_pct, 3))
    try:
        clean = _clean_bars(bars, now)
    except ValueError:
        return _skip(symbol, "SKIP_BARS_INVALID")
    if len(clean) < 7:
        return _skip(symbol, "SKIP_BARS_INSUFFICIENT", bars=len(clean))
    if clean[-1]["timestamp"] < now.timestamp() - 2 * BAR_SECONDS:
        return _skip(symbol, "SKIP_BARS_STALE")
    cycles = detect_cycles(clean)
    if len(cycles) < MIN_CYCLES:
        return _skip(symbol, "SKIP_CYCLES", cycles=len(cycles))
    last_cycle, previous = cycles[-1], cycles[-2]
    after_peak = clean[last_cycle.peak_index + 1:]
    if _find_spike(clean, last_cycle.fade_index) is not None:
        return _skip(symbol, "SKIP_NEW_SPIKE", cycles=len(cycles))
    spike_range = last_cycle.peak - last_cycle.low
    dip_low = min(bar["low"] for bar in after_peak)
    retrace = (last_cycle.peak - dip_low) / spike_range
    diagnostics = {"cycles": len(cycles), "retrace": round(retrace, 4),
                   "peak": last_cycle.peak, "spike_low": last_cycle.low, "dip_low": dip_low}
    if not (ENTRY_RETRACE_MIN <= retrace <= ENTRY_RETRACE_MAX):
        return _skip(symbol, "SKIP_RETRACE", **diagnostics)
    previous_fade_low = min(bar["low"] for bar in
                            clean[previous.peak_index + 1:last_cycle.low_index + 1])
    if dip_low <= previous_fade_low:
        return _skip(symbol, "SKIP_LOWER_LOW", previous_fade_low=previous_fade_low,
                     **diagnostics)
    trigger, prior = clean[-1], clean[-2]
    if trigger["timestamp"] - prior["timestamp"] != BAR_SECONDS:
        return _skip(symbol, "SKIP_BARS_STALE")
    average_volume = sum(bar["volume"] for bar in clean[-6:-1]) / 5
    if not (trigger["close"] > trigger["open"] and trigger["close"] > prior["high"]
            and trigger["volume"] >= average_volume):
        return _skip(symbol, "SKIP_NO_TRIGGER", **diagnostics)
    if last_cycle.peak - ask < MIN_ROOM_TO_PEAK * spike_range:
        return _skip(symbol, "SKIP_CHASE", **diagnostics)
    entry = round(ask, 4)
    stop = round(dip_low * (1 - STOP_BUFFER_PCT), 4)
    risk = entry - stop
    if risk <= 0:
        return _skip(symbol, "SKIP_STOP_INVALID", **diagnostics)
    if risk / entry > MAX_STOP_PCT:
        return _skip(symbol, "SKIP_STOP_TOO_WIDE", stop_pct=round(risk / entry * 100, 3),
                     **diagnostics)
    if math.floor(MAX_POSITION_USD / entry) < 1:
        return _skip(symbol, "SKIP_SIZE", **diagnostics)
    return {"symbol": symbol, "action": "BUY", "entry": entry, "stop": stop,
            "target": round(entry + TARGET_R * risk, 4), "strategy": STRATEGY_NAME,
            "timeframe": TIMEFRAME, "qualified": True, "hard_pass": True,
            "hard_failures": [], "rebound": {**diagnostics, "risk_per_share": round(risk, 4)}}


def request_history(app, candidate, req_id, make_contract):
    """1-minute TRADES bars for the last day incl. extended hours (bars include open-bar)."""
    event = threading.Event()
    app.historical_events[req_id] = event
    app.historical_bars[req_id] = []
    app.reqHistoricalData(req_id, make_contract(candidate), "", "1 D", "1 min", "TRADES",
                          0, 2, False, [])
    if not event.wait(timeout=HISTORY_TIMEOUT_SECONDS):
        try:
            app.cancelHistoricalData(req_id)
        except Exception:
            pass
        return []
    return list(app.historical_bars.get(req_id, []))
