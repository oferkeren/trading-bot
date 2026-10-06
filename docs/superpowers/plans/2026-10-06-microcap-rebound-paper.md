# Micro-cap Rebound Paper Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paper-trade `microcap_rebound_v1` (buy the dip after ≥2 intraday spike-and-fade cycles on a stock with strong positive news, then exit with a ratcheting stop) on the IBKR paper account, and show every decision and trade on the dashboard.

**Architecture:** Four pure, unit-tested modules (`rebound_strategy`, `rebound_exits`, `rebound_journal`, `rebound_stop_manager`) plus one thin IBKR adapter (`rebound_ib_broker`). The existing signal runner gets a `REBOUND` strategy mode that analyzes candidates and sends qualified BUY signals through a strict news gate. The existing worker places the bracket as today and, every ~10 s, lets the stop manager ratchet the stop child or move it through the market to close. The dashboard Research tab gets a "Paper trades" card fed by `/rebound-journal`.

**Tech Stack:** Python 3 (unittest, sqlite3, ibapi), FastAPI, vanilla JS dashboard (Node test harness).

**Spec:** `docs/superpowers/specs/2026-10-06-microcap-rebound-paper-design.md`

---

## Ground rules for every task

- Work in the worktree `.worktrees/rebound` on branch `rebound-paper` (Task 0 creates it). Never edit `/home/oferke/trading-bot` directly; never restart services; never place orders. Rollout (Task 8, part 2) is done by the controller with the user's explicit consent.
- Python tests: `venv/bin/python -m unittest <module> -v` from the worktree. Use the live venv: `/home/oferke/trading-bot/venv/bin/python`.
- Full suite: `/home/oferke/trading-bot/venv/bin/python -m unittest discover -s . -p 'test_*.py'` (baseline at plan time: 520 OK). JS: `node test_dashboard_app.js`.
- Never read or print credential files (`.env`, `~/.config/...`).
- Commit after each task with the trailer `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.

## File structure

| File | Responsibility |
|---|---|
| `rebound_strategy.py` (new) | Pure entry rules: cycle detection, filters, stop/target, strict news verdict, paper guard, 1-min history request |
| `rebound_exits.py` (new) | Pure exit rule: HOLD / MOVE_STOP / CLOSE |
| `rebound_journal.py` (new) | SQLite tables `rebound_journal`, `rebound_positions`; `record`, `record_skip`, `is_busy`, `summary` |
| `rebound_stop_manager.py` (new) | `tick(db_file, broker, now)`: manage open rebound positions; `has_work(db_file)` |
| `rebound_ib_broker.py` (new) | IBKR broker for the manager (bars, modify stop child, close via stop child) |
| `worker_core.py` (modify) | IBApp historical callbacks; `run_rebound_manager()`; 10 s hook in `main()` |
| `signal_bridge.py` (modify) | `REBOUND` mode dispatch + skip journaling |
| `ai_signal_bridge.py` (modify) | Strict rebound branch in `process_candidate` (busy check, gate, news verdict) |
| `signal_server_core.py` (modify) | `REBOUND` in valid strategy modes |
| `signal_server.py` (modify) | `GET /rebound-journal` |
| `dashboard.html`, `dashboard_app.js` (modify) | "Paper trades" card |
| `OPERATIONS.md` (modify) | Rebound operations section |
| `test_rebound_*.py`, `test_dashboard_app.js` | Tests |

---

### Task 0: Snapshot live edits and create the worktree (controller only)

The live checkout has uncommitted user edits in tracked files (`ai_gate.py`, `ai_signal_bridge.py`, `signal_bridge.py`, `signal_server.py`, `signal_server_core.py`, `worker_core.py`, …). The integration tasks modify those files, so the worktree must start from exactly what is running.

- [ ] **Step 1: Ask the user for consent** to commit their uncommitted tracked edits as-is ("Snapshot live edits before rebound work"). Do not proceed without it.
- [ ] **Step 2: Commit and push**

```bash
cd /home/oferke/trading-bot
git status --short | grep -v '^??'          # review the list
git add -u
git commit -q -m "Snapshot live edits before rebound work" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
GH_TOKEN="$(gh auth token --user oferkeren)" git -c credential.helper= -c credential.helper='!gh auth git-credential' push -q origin master
```

- [ ] **Step 3: Create the worktree and record the baseline**

```bash
git worktree add -q .worktrees/rebound -b rebound-paper
cd .worktrees/rebound
/home/oferke/trading-bot/venv/bin/python -m unittest discover -s . -p 'test_*.py' 2>&1 | tail -3
node test_dashboard_app.js
```

Expected: the same pass count as the live checkout (record it), `dashboard_app tests passed`.

---

### Task 1: `rebound_strategy.py` — entry rules

**Files:**
- Create: `rebound_strategy.py`
- Test: `test_rebound_strategy.py`

- [ ] **Step 1: Write the failing tests**

```python
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import rebound_strategy as rs

NY = ZoneInfo("America/New_York")
PAPER = {"IB_PORT": "7497", "IB_ACCOUNT": "DUQ569670"}
START = datetime(2026, 10, 6, 10, 0, tzinfo=NY)  # a Tuesday, regular hours

# Two spike-and-fade cycles with a higher low, then a green trigger bar.
TWO_CYCLES = [1.00, 1.00, 1.00, 1.04, 1.08, 1.12, 1.10, 1.08, 1.06,
              1.10, 1.15, 1.20, 1.17, 1.15, 1.13, 1.13, 1.13, 1.13, 1.13]
TRIGGER = 1.15


def make_bars(closes, start=START, volumes=None, trigger_volume=5000.0):
    bars, previous = [], closes[0]
    for index, close in enumerate(closes):
        volume = volumes[index] if volumes else 1000.0
        if index == len(closes) - 1 and trigger_volume is not None:
            volume = trigger_volume
        stamp = int((start + timedelta(minutes=index)).timestamp())
        bars.append({"timestamp": stamp, "open": previous, "close": close,
                     "high": max(previous, close), "low": min(previous, close),
                     "volume": volume})
        previous = close
    return bars


def now_after(bars):
    return datetime.fromtimestamp(bars[-1]["timestamp"] + 61, timezone.utc)


def quote(ask=1.151, bid=1.149):
    return {"bid": bid, "ask": ask, "last": ask}


class DetectCyclesTests(unittest.TestCase):
    def clean(self, closes):
        bars = make_bars(closes)
        return rs._clean_bars(bars, now_after(bars))

    def test_counts_zero_one_two_three_cycles(self):
        self.assertEqual(rs.detect_cycles(self.clean([1.0] * 10)), [])
        one = [1.00, 1.00, 1.04, 1.09, 1.07, 1.05, 1.05]
        self.assertEqual(len(rs.detect_cycles(self.clean(one))), 1)
        self.assertEqual(len(rs.detect_cycles(self.clean(TWO_CYCLES))), 2)
        three = TWO_CYCLES + [1.18, 1.23, 1.28, 1.24, 1.21, 1.20]
        self.assertEqual(len(rs.detect_cycles(self.clean(three))), 3)

    def test_spike_without_fade_is_not_a_cycle(self):
        self.assertEqual(rs.detect_cycles(self.clean([1.0, 1.0, 1.05, 1.10, 1.12])), [])

    def test_slow_rise_beyond_max_bars_is_not_a_spike(self):
        slow = [1.00 + 0.003 * i for i in range(30)] + [1.05, 1.02]
        self.assertEqual(rs.detect_cycles(self.clean(slow)), [])

    def test_cycle_fields(self):
        first = rs.detect_cycles(self.clean(TWO_CYCLES))[0]
        self.assertEqual((first.low, first.peak), (1.00, 1.12))


class AnalyzeTests(unittest.TestCase):
    def run_case(self, closes=None, q=None, env=PAPER, bars=None, now=None):
        bars = bars if bars is not None else make_bars((closes or TWO_CYCLES) + [TRIGGER])
        return rs.analyze({"symbol": "abcd"}, bars, q or quote(), now or now_after(bars), env)

    def test_qualified_signal(self):
        result = self.run_case()
        self.assertTrue(result["qualified"], result)
        self.assertEqual((result["symbol"], result["action"], result["strategy"]),
                         ("ABCD", "BUY", "microcap_rebound_v1"))
        self.assertEqual(result["entry"], 1.151)
        self.assertEqual(result["stop"], round(1.13 * 0.995, 4))
        risk = result["entry"] - result["stop"]
        self.assertAlmostEqual(result["target"], round(result["entry"] + 3 * risk, 4))
        self.assertEqual(result["rebound"]["cycles"], 2)
        self.assertTrue(result["hard_pass"])

    def skip(self, result):
        self.assertFalse(result["qualified"])
        return result["skip_reason"]

    def test_not_paper(self):
        for env in ({"IB_PORT": "7496", "IB_ACCOUNT": "DUQ1"},
                    {"IB_PORT": "7497", "IB_ACCOUNT": "U123"}, {}):
            self.assertEqual(self.skip(self.run_case(env=env)), "SKIP_NOT_PAPER")

    def test_session_window(self):
        bars = make_bars(TWO_CYCLES + [TRIGGER])
        late = datetime(2026, 10, 6, 19, 31, tzinfo=NY)
        saturday = datetime(2026, 10, 10, 10, 30, tzinfo=NY)
        for now in (late, saturday):
            self.assertEqual(self.skip(self.run_case(bars=bars, now=now)), "SKIP_SESSION")

    def test_one_cycle_is_not_enough(self):
        closes = [1.00, 1.00, 1.00, 1.04, 1.08, 1.12, 1.10, 1.08, 1.06, 1.06, 1.06]
        self.assertEqual(self.skip(self.run_case(closes=closes)), "SKIP_CYCLES")

    def test_dip_below_previous_fade_low_is_rejected(self):
        # A retrace <= 70% already implies a higher low, so a deeper dip fails as RETRACE.
        closes = TWO_CYCLES[:12] + [1.15, 1.10, 1.05, 1.05, 1.05, 1.05, 1.05]
        bars = make_bars(closes + [1.07])
        result = rs.analyze({"symbol": "X"}, bars, quote(1.071, 1.069), now_after(bars), PAPER)
        self.assertEqual(self.skip(result), "SKIP_RETRACE")

    def test_new_spike_after_last_fade_is_not_chased(self):
        bars = make_bars(TWO_CYCLES + [1.23])
        result = rs.analyze({"symbol": "X"}, bars, quote(1.231, 1.229), now_after(bars), PAPER)
        self.assertEqual(self.skip(result), "SKIP_NEW_SPIKE")

    def test_retrace_too_deep(self):
        closes = [1.00, 1.00, 1.00, 1.04, 1.08, 1.12, 1.10, 1.08, 1.06,
                  1.10, 1.15, 1.25, 1.20, 1.14, 1.10, 1.10, 1.10, 1.10, 1.10]
        self.assertEqual(self.skip(self.run_case(closes=closes + [])), "SKIP_RETRACE")

    def test_no_trigger(self):
        bars = make_bars(TWO_CYCLES + [1.125])
        self.assertEqual(self.skip(self.run_case(bars=bars)), "SKIP_NO_TRIGGER")
        bars = make_bars(TWO_CYCLES + [TRIGGER], trigger_volume=10.0)
        self.assertEqual(self.skip(self.run_case(bars=bars)), "SKIP_NO_TRIGGER")

    def test_chase(self):
        self.assertEqual(self.skip(self.run_case(q=quote(1.17, 1.169))), "SKIP_CHASE")

    def test_spread(self):
        self.assertEqual(self.skip(self.run_case(q=quote(1.151, 1.13))), "SKIP_SPREAD")
        bars = make_bars(TWO_CYCLES + [TRIGGER], start=datetime(2026, 10, 6, 7, 0, tzinfo=NY))
        result = self.run_case(bars=bars, q=quote(1.151, 1.137))
        self.assertTrue(result["qualified"], result)  # 1.2% allowed pre-market

    def test_stop_too_wide(self):
        closes = [1.00, 1.00, 1.00, 1.10, 1.20, 1.30, 1.25, 1.20, 1.16,
                  1.30, 1.45, 1.60, 1.50, 1.40, 1.33, 1.33, 1.33, 1.33, 1.33]
        bars = make_bars(closes + [1.42])
        result = self.run_case(bars=bars, q=quote(1.421, 1.419))
        self.assertEqual(self.skip(result), "SKIP_STOP_TOO_WIDE")

    def test_price_bounds_and_invalid_inputs(self):
        self.assertEqual(self.skip(self.run_case(q=quote(25.0, 24.99))), "SKIP_PRICE")
        self.assertEqual(self.skip(self.run_case(q={"bid": None, "ask": 1})), "SKIP_QUOTE_INVALID")
        self.assertEqual(self.skip(self.run_case(q=quote(1.10, 1.20))), "SKIP_QUOTE_INVALID")
        bad = make_bars(TWO_CYCLES + [TRIGGER])
        bad[3]["high"] = "x"
        self.assertEqual(self.skip(self.run_case(bars=bad)), "SKIP_BARS_INVALID")
        self.assertEqual(self.skip(rs.analyze({}, [], quote(), START, PAPER)), "SKIP_INPUT_INVALID")

    def test_ignores_open_bar_and_previous_day(self):
        bars = make_bars(TWO_CYCLES + [TRIGGER])
        still_open = datetime.fromtimestamp(bars[-1]["timestamp"] + 30, timezone.utc)
        self.assertEqual(self.skip(self.run_case(bars=bars, now=still_open)), "SKIP_NO_TRIGGER")
        yesterday = make_bars([1.0] * 5, start=START - timedelta(days=1))
        result = self.run_case(bars=yesterday + bars)
        self.assertTrue(result["qualified"], result)


class NewsVerdictTests(unittest.TestCase):
    def ai(self, **overrides):
        value = {"status": "PASS", "news_count": 2,
                 "ai": {"news_score": 0.7, "event_type": "FDA"}}
        for key, item in overrides.items():
            if key in ("news_score", "event_type"):
                value["ai"][key] = item
            else:
                value[key] = item
        return value

    def test_pass(self):
        self.assertEqual(rs.news_verdict(self.ai()), (True, "NEWS_STRONG_POSITIVE"))

    def test_fail_closed(self):
        cases = [(None, "NEWS_GATE_UNAVAILABLE"), (self.ai(status="BLOCK"), "AI_BLOCK"),
                 (self.ai(status="REDUCE"), "AI_REDUCE"), (self.ai(status="SKIP"), "AI_SKIP"),
                 (self.ai(news_count=0), "NO_RECENT_NEWS"),
                 (self.ai(news_count=True), "NO_RECENT_NEWS"),
                 (self.ai(news_score=0.49), "NEWS_NOT_STRONG"),
                 (self.ai(news_score=None), "NEWS_NOT_STRONG"),
                 (self.ai(event_type="OFFERING"), "NEWS_NEGATIVE_EVENT"),
                 (self.ai(ai=None), "NEWS_GATE_UNAVAILABLE")]
        for value, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(rs.news_verdict(value), (False, reason))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `venv/bin/python -m unittest test_rebound_strategy -v`
Expected: `ModuleNotFoundError: No module named 'rebound_strategy'`

- [ ] **Step 3: Implement**

```python
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
STOP_BUFFER_PCT = 0.005
MAX_STOP_PCT = 0.06
TARGET_R = 3.0
NEWS_MIN_SCORE = 0.5
NEGATIVE_EVENTS = frozenset({"OFFERING", "DILUTION", "LAWSUIT", "BANKRUPTCY"})

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
```

Notes: a retrace ≤ 70% of the last spike already implies a higher low than the previous fade low (spike 2 starts at that low), so `SKIP_LOWER_LOW` is a defensive guard and a deeper dip reports `SKIP_RETRACE`.

- [ ] **Step 4: Verify** — `venv/bin/python -m unittest test_rebound_strategy -v` → 19 tests OK.
- [ ] **Step 5: Commit** — `git add rebound_strategy.py test_rebound_strategy.py && git commit -m "Add microcap rebound entry rules"`

---

### Task 2: `rebound_exits.py` — ratcheting stop and forced exits

**Files:**
- Create: `rebound_exits.py`
- Test: `test_rebound_exits.py`

- [ ] **Step 1: Write the failing tests**

```python
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rebound_exits import next_action, tick_for

NY = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 6, 10, 0, tzinfo=NY)


def act(high, current=1.90, minutes=1, entry=2.00, initial=1.90, opened=OPEN):
    return next_action(entry=entry, initial_stop=initial, current_stop=current,
                       high_since_entry=high, opened_at=opened,
                       now=opened + timedelta(minutes=minutes))


class NextActionTests(unittest.TestCase):
    def test_hold_below_one_r(self):
        self.assertEqual(act(2.09), {"action": "HOLD"})

    def test_breakeven_lock_at_one_r(self):
        self.assertEqual(act(2.10), {"action": "MOVE_STOP", "stop": 2.02})

    def test_trails_one_r_below_high(self):
        self.assertEqual(act(2.25, current=2.02), {"action": "MOVE_STOP", "stop": 2.15})

    def test_never_moves_down(self):
        self.assertEqual(act(2.12, current=2.15), {"action": "HOLD"})

    def test_requires_one_tick_step(self):
        self.assertEqual(act(2.255, current=2.15), {"action": "HOLD"})
        self.assertEqual(act(2.26, current=2.15), {"action": "MOVE_STOP", "stop": 2.16})

    def test_sub_dollar_tick(self):
        result = next_action(entry=0.5000, initial_stop=0.4800, current_stop=0.4800,
                             high_since_entry=0.5200, opened_at=OPEN,
                             now=OPEN + timedelta(minutes=1))
        self.assertEqual(result, {"action": "MOVE_STOP", "stop": 0.504})
        self.assertEqual((tick_for(0.99), tick_for(1.0)), (0.0001, 0.01))

    def test_max_hold(self):
        self.assertEqual(act(2.05, minutes=20), {"action": "CLOSE", "reason": "MAX_HOLD"})
        self.assertEqual(act(2.05, minutes=19)["action"], "HOLD")

    def test_end_of_session(self):
        late = datetime(2026, 10, 6, 19, 50, tzinfo=NY)
        self.assertEqual(act(2.05, minutes=5, opened=late),
                         {"action": "CLOSE", "reason": "END_OF_SESSION"})
        result = next_action(entry=2.0, initial_stop=1.9, current_stop=1.9,
                             high_since_entry=2.0, opened_at=datetime(2026, 10, 5, 19, 50, tzinfo=NY),
                             now=datetime(2026, 10, 6, 4, 1, tzinfo=NY))
        self.assertEqual(result, {"action": "CLOSE", "reason": "END_OF_SESSION"})

    def test_invalid_risk_closes(self):
        self.assertEqual(act(2.0, initial=2.0)["reason"], "INVALID_RISK")
        self.assertEqual(act(float("nan"))["reason"], "INVALID_RISK")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — `venv/bin/python -m unittest test_rebound_exits -v` → `ModuleNotFoundError`.
- [ ] **Step 3: Implement**

```python
"""Pure exit rules for microcap_rebound_v1: ratcheting stop, max hold, end of session."""

import math
from datetime import time as dtime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
BREAKEVEN_TRIGGER_R = 1.0
BREAKEVEN_LOCK_R = 0.2
TRAIL_R = 1.0
MAX_HOLD_MINUTES = 20
SESSION_CLOSE = dtime(19, 55)


def tick_for(price):
    return 0.0001 if price < 1.0 else 0.01


def _floor_to_tick(price, tick):
    return round(math.floor(price / tick + 1e-9) * tick, 6)


def next_action(*, entry, initial_stop, current_stop, high_since_entry, opened_at, now):
    """Return {"action": "HOLD"} | {"action": "MOVE_STOP", "stop": p} | {"action": "CLOSE", "reason": r}.

    The stop never moves down. Datetimes must be timezone-aware.
    """
    risk = entry - initial_stop
    if not all(math.isfinite(v) for v in (entry, initial_stop, current_stop, high_since_entry)) \
            or risk <= 0:
        return {"action": "CLOSE", "reason": "INVALID_RISK"}
    local_now, local_open = now.astimezone(NEW_YORK), opened_at.astimezone(NEW_YORK)
    if local_now.date() != local_open.date() or local_now.time() >= SESSION_CLOSE:
        return {"action": "CLOSE", "reason": "END_OF_SESSION"}
    if now - opened_at >= timedelta(minutes=MAX_HOLD_MINUTES):
        return {"action": "CLOSE", "reason": "MAX_HOLD"}
    desired = current_stop
    if high_since_entry >= entry + BREAKEVEN_TRIGGER_R * risk:
        desired = max(desired, entry + BREAKEVEN_LOCK_R * risk, high_since_entry - TRAIL_R * risk)
    tick = tick_for(entry)
    desired = _floor_to_tick(desired, tick)
    if desired >= current_stop + tick - 1e-9:
        return {"action": "MOVE_STOP", "stop": desired}
    return {"action": "HOLD"}
```

- [ ] **Step 4: Verify** → 9 tests OK.
- [ ] **Step 5: Commit** — `git commit -m "Add rebound exit rules"` (both files).

---

### Task 3 + 4: `rebound_journal.py` and `rebound_stop_manager.py`

These share one test file, so they are one task.

**Files:**
- Create: `rebound_journal.py`, `rebound_stop_manager.py`
- Test: `test_rebound_journal_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import rebound_journal as journal
import rebound_stop_manager as manager

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=NY)


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE signals (signal_id TEXT PRIMARY KEY, symbol TEXT, quantity INTEGER,
        stop REAL, status TEXT, strategy TEXT, entry_fill_price REAL, exit_fill_price REAL,
        entry_time TEXT, exit_reason TEXT, realized_pnl REAL, net_realized_pnl REAL,
        stop_order_id INTEGER, exit_time TEXT)""")
    conn.commit()
    conn.close()


def add_signal(path, sid="s1", status="OPEN_POSITION", strategy=journal.STRATEGY,
               fill=2.00, stop=1.90, entry_time="20261006 10:00:00 US/Eastern"):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO signals (signal_id, symbol, quantity, stop, status, strategy, "
                     "entry_fill_price, entry_time, stop_order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                     (sid, "ABC", 500, stop, status, strategy, fill, entry_time, 7))


def set_status(path, sid, status, exit_price=None, pnl=None):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE signals SET status=?, exit_fill_price=?, realized_pnl=? WHERE signal_id=?",
                     (status, exit_price, pnl, sid))


class FakeBroker:
    def __init__(self, highs=()):
        self.highs = list(highs)
        self.calls = []
        self.fail_bars = False

    def bars_since(self, symbol, since):
        if self.fail_bars:
            raise RuntimeError("no data")
        return [{"high": h} for h in self.highs]

    def modify_stop(self, signal, trigger):
        self.calls.append(("modify_stop", signal["signal_id"], trigger))

    def close(self, signal, reason):
        self.calls.append(("close", signal["signal_id"], reason))


def events(path):
    with sqlite3.connect(path) as conn:
        return [r[0] for r in conn.execute("SELECT event FROM rebound_journal ORDER BY id")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "trading.db")
        make_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()


class JournalTests(Base):
    def test_is_busy(self):
        self.assertFalse(journal.is_busy(self.db))
        add_signal(self.db, "old", strategy="scalp_pingpong_v1")
        self.assertFalse(journal.is_busy(self.db))
        add_signal(self.db, "a", status="SUBMITTED")
        self.assertTrue(journal.is_busy(self.db))
        set_status(self.db, "a", "CLOSED_TP")
        self.assertFalse(journal.is_busy(self.db))
        add_signal(self.db, "b", status=None)
        self.assertTrue(journal.is_busy(self.db))

    def test_missing_db_or_table_is_busy(self):
        self.assertTrue(journal.is_busy(os.path.join(self.tmp.name, "missing.db")))
        empty = os.path.join(self.tmp.name, "empty.db")
        sqlite3.connect(empty).close()
        self.assertTrue(journal.is_busy(empty))

    def test_record_skip_dedupes_for_ten_minutes(self):
        self.assertTrue(journal.record_skip(self.db, "ABC", "SKIP_SPREAD", now=T0))
        self.assertFalse(journal.record_skip(self.db, "ABC", "SKIP_SPREAD", now=T0 + timedelta(minutes=9)))
        self.assertTrue(journal.record_skip(self.db, "ABC", "SKIP_NEWS", now=T0 + timedelta(minutes=9)))
        self.assertTrue(journal.record_skip(self.db, "ABC", "SKIP_SPREAD", now=T0 + timedelta(minutes=11)))

    def test_summary(self):
        add_signal(self.db, "w")
        set_status(self.db, "w", "CLOSED_TP", 2.3, 150.0)
        add_signal(self.db, "l")
        set_status(self.db, "l", "CLOSED_SL", 1.9, -50.0)
        journal.record(self.db, "SKIP", symbol="X", reason="R", detail={"a": 1}, now=T0)
        result = journal.summary(self.db)
        self.assertEqual(result["stats"], {"trades": 2, "wins": 1, "losses": 1, "total_pnl": 100.0})
        self.assertEqual(result["events"][0]["detail"], {"a": 1})


class ManagerTests(Base):
    def test_entry_then_ratchet(self):
        add_signal(self.db)
        broker = FakeBroker(highs=[2.05])
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [])
        broker.highs = [2.05, 2.25]
        manager.tick(self.db, broker, T0 + timedelta(minutes=2))
        self.assertEqual(broker.calls, [("modify_stop", "s1", 2.15)])
        broker.highs = [2.10]  # the stored high is kept
        manager.tick(self.db, broker, T0 + timedelta(minutes=3))
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(events(self.db), ["ENTRY", "STOP_MOVE"])

    def test_max_hold_close_and_retry(self):
        add_signal(self.db)
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=20))
        self.assertEqual(broker.calls, [("close", "s1", "MAX_HOLD")])
        manager.tick(self.db, broker, T0 + timedelta(minutes=20, seconds=30))
        self.assertEqual(len(broker.calls), 1)
        manager.tick(self.db, broker, T0 + timedelta(minutes=21, seconds=1))
        self.assertEqual(broker.calls[-1], ("close", "s1", "MAX_HOLD"))

    def test_exit_recorded_once(self):
        add_signal(self.db)
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        set_status(self.db, "s1", "CLOSED_SL", 1.9, -50.0)
        manager.tick(self.db, broker, T0 + timedelta(minutes=2))
        manager.tick(self.db, broker, T0 + timedelta(minutes=3))
        self.assertEqual(events(self.db), ["ENTRY", "EXIT"])

    def test_bar_failure_still_enforces_time_exit(self):
        add_signal(self.db)
        broker = FakeBroker()
        broker.fail_bars = True
        manager.tick(self.db, broker, T0 + timedelta(minutes=25))
        self.assertEqual(broker.calls, [("close", "s1", "MAX_HOLD")])
        self.assertIn("ERROR", events(self.db))

    def test_broker_error_is_journaled_and_other_positions_continue(self):
        add_signal(self.db, "a")
        add_signal(self.db, "b")
        broker = FakeBroker(highs=[2.25])
        original = broker.modify_stop

        def flaky(signal, trigger):
            if signal["signal_id"] == "a":
                raise RuntimeError("ib down")
            original(signal, trigger)
        broker.modify_stop = flaky
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [("modify_stop", "b", 2.15)])
        self.assertIn("ERROR", events(self.db))

    def test_ignores_other_strategies(self):
        add_signal(self.db, strategy="scalp_pingpong_v1")
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=30))
        self.assertEqual(broker.calls, [])

    def test_has_work(self):
        self.assertFalse(manager.has_work(os.path.join(self.tmp.name, "missing.db")))
        self.assertFalse(manager.has_work(self.db))
        add_signal(self.db, strategy="scalp_pingpong_v1")
        self.assertFalse(manager.has_work(self.db))
        add_signal(self.db, "r1")
        self.assertTrue(manager.has_work(self.db))
        manager.tick(self.db, FakeBroker(), T0 + timedelta(minutes=1))
        set_status(self.db, "r1", "CLOSED_SL")
        self.assertTrue(manager.has_work(self.db))  # position row still needs its EXIT record
        manager.tick(self.db, FakeBroker(), T0 + timedelta(minutes=2))
        self.assertFalse(manager.has_work(self.db))

    def test_parse_ib_time(self):
        self.assertEqual(manager.parse_ib_time("20261006 10:00:00 US/Eastern"), T0)
        self.assertIsNone(manager.parse_ib_time("garbage"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** → `ModuleNotFoundError`.
- [ ] **Step 3: Implement `rebound_journal.py`**

```python
"""SQLite journal for microcap_rebound_v1 decisions and managed positions."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

STRATEGY = "microcap_rebound_v1"
DEFAULT_DB = str(Path(__file__).resolve().with_name("trading.db"))
TERMINAL_STATUSES = frozenset({
    "CLOSED_SL", "CLOSED_TP", "CLOSED", "CANCELLED", "REJECTED",
    "BLOCKED", "TESTED", "ERROR", "EXPIRED",
})
SKIP_DEDUPE_MINUTES = 10


def connect(db_file):
    conn = sqlite3.connect(db_file, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS rebound_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, event TEXT NOT NULL, symbol TEXT,
        signal_id TEXT, reason TEXT, detail TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS rebound_journal_ts ON rebound_journal(ts)")
    conn.execute("""CREATE TABLE IF NOT EXISTS rebound_positions (
        signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
        entry REAL NOT NULL, initial_stop REAL NOT NULL, current_stop REAL NOT NULL,
        high_since_entry REAL NOT NULL, opened_at TEXT NOT NULL,
        state TEXT NOT NULL, close_reason TEXT, updated_at TEXT NOT NULL)""")
    return conn


def _iso(now):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def record(db_file, event, *, symbol=None, signal_id=None, reason=None, detail=None, now=None):
    with connect(db_file) as conn:
        conn.execute(
            "INSERT INTO rebound_journal (ts, event, symbol, signal_id, reason, detail) VALUES (?,?,?,?,?,?)",
            (_iso(now), event, symbol, signal_id, reason,
             json.dumps(detail, sort_keys=True, default=str) if detail is not None else None),
        )


def record_skip(db_file, symbol, reason, detail=None, now=None):
    """Record SKIP unless the same symbol/reason was recorded in the last 10 minutes."""
    now = now or datetime.now(timezone.utc)
    cutoff = _iso(now - timedelta(minutes=SKIP_DEDUPE_MINUTES))
    with connect(db_file) as conn:
        seen = conn.execute(
            "SELECT 1 FROM rebound_journal WHERE event='SKIP' AND symbol=? AND reason=? AND ts>=? LIMIT 1",
            (symbol, reason, cutoff),
        ).fetchone()
    if seen:
        return False
    record(db_file, "SKIP", symbol=symbol, reason=reason, detail=detail, now=now)
    return True


def is_busy(db_file):
    """True when any rebound signal is not terminal. Any read failure counts as busy."""
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=10)
        try:
            placeholders = ",".join("?" * len(TERMINAL_STATUSES))
            row = conn.execute(
                f"SELECT COUNT(*) FROM signals WHERE strategy=? "
                f"AND COALESCE(status,'') NOT IN ({placeholders})",
                (STRATEGY, *sorted(TERMINAL_STATUSES)),
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row[0])
    except Exception:
        return True


def summary(db_file, limit=50):
    with connect(db_file) as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT ts, event, symbol, signal_id, reason, detail FROM rebound_journal "
            "ORDER BY id DESC LIMIT ?", (int(limit),))]
        positions = [dict(r) for r in conn.execute(
            "SELECT * FROM rebound_positions WHERE state != 'CLOSED' ORDER BY opened_at")]
        try:
            trades = [dict(r) for r in conn.execute(
                "SELECT signal_id, symbol, status, entry_fill_price, exit_fill_price, "
                "COALESCE(net_realized_pnl, realized_pnl) AS pnl, exit_reason, entry_time, exit_time "
                "FROM signals WHERE strategy=? AND status IN ('CLOSED_SL','CLOSED_TP','CLOSED') "
                "ORDER BY rowid DESC LIMIT ?", (STRATEGY, int(limit)))]
        except sqlite3.OperationalError:
            trades = []
    for event in events:
        event["detail"] = json.loads(event["detail"]) if event["detail"] else None
    pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
    stats = {
        "trades": len(trades),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p <= 0),
        "total_pnl": round(sum(pnls), 2),
    }
    return {"strategy": STRATEGY, "stats": stats, "open_positions": positions,
            "trades": trades, "events": events}
```

- [ ] **Step 4: Implement `rebound_stop_manager.py`**

```python
"""Manage open microcap_rebound_v1 positions: ratchet the stop and force exits.

broker must provide:
  bars_since(symbol, since) -> list of bar dicts with "high"
  modify_stop(signal, trigger) -> None
  close(signal, reason) -> None
Not affected by the kill switch: it only raises stops and closes positions.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import rebound_exits
import rebound_journal as journal

CLOSE_RETRY_SECONDS = 60
_EASTERN = ZoneInfo("America/New_York")


def parse_ib_time(value):
    """Parse '20261001 07:43:43 US/Eastern' (or ISO) to an aware datetime; None if invalid."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if text[:8].isdigit() and len(text) >= 17:
            return datetime.strptime(text[:17], "%Y%m%d %H:%M:%S").replace(tzinfo=_EASTERN)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _open_signals(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM signals WHERE strategy=? AND status='OPEN_POSITION' "
        "AND entry_fill_price IS NOT NULL AND stop IS NOT NULL", (journal.STRATEGY,))]


def _ensure_position(db_file, conn, signal, now):
    row = conn.execute("SELECT * FROM rebound_positions WHERE signal_id=?",
                       (signal["signal_id"],)).fetchone()
    if row:
        return dict(row)
    entry = float(signal["entry_fill_price"])
    stop = float(signal["stop"])
    opened = parse_ib_time(signal.get("entry_time")) or now
    position = {
        "signal_id": signal["signal_id"], "symbol": signal["symbol"], "entry": entry,
        "initial_stop": stop, "current_stop": stop, "high_since_entry": entry,
        "opened_at": opened.isoformat(), "state": "OPEN", "close_reason": None,
        "updated_at": now.isoformat(),
    }
    conn.execute(
        "INSERT INTO rebound_positions VALUES (:signal_id,:symbol,:entry,:initial_stop,:current_stop,"
        ":high_since_entry,:opened_at,:state,:close_reason,:updated_at)", position)
    conn.commit()
    journal.record(db_file, "ENTRY", symbol=signal["symbol"], signal_id=signal["signal_id"],
                   detail={"entry": entry, "stop": stop, "quantity": signal.get("quantity")}, now=now)
    return position


def _update(conn, signal_id, now, **fields):
    fields["updated_at"] = now.isoformat()
    assignments = ",".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE rebound_positions SET {assignments} WHERE signal_id=?",
                 (*fields.values(), signal_id))
    conn.commit()


def _record_exits(db_file, conn, now):
    rows = conn.execute(
        "SELECT p.signal_id, p.symbol, s.status, s.exit_fill_price, s.exit_reason, "
        "COALESCE(s.net_realized_pnl, s.realized_pnl) AS pnl, p.close_reason "
        "FROM rebound_positions p LEFT JOIN signals s ON s.signal_id = p.signal_id "
        "WHERE p.state != 'CLOSED'").fetchall()
    for row in rows:
        if row["status"] in journal.TERMINAL_STATUSES:
            _update(conn, row["signal_id"], now, state="CLOSED")
            journal.record(db_file, "EXIT", symbol=row["symbol"], signal_id=row["signal_id"],
                           reason=row["close_reason"] or row["status"],
                           detail={"status": row["status"], "exit_price": row["exit_fill_price"],
                                   "pnl": row["pnl"], "exit_reason": row["exit_reason"]}, now=now)


def tick(db_file, broker, now=None):
    now = now or datetime.now(timezone.utc)
    conn = journal.connect(db_file)
    try:
        for signal in _open_signals(conn):
            try:
                _manage(db_file, conn, broker, signal, now)
            except Exception as exc:
                journal.record(db_file, "ERROR", symbol=signal.get("symbol"),
                               signal_id=signal.get("signal_id"), reason="MANAGE_FAILED",
                               detail={"error": repr(exc)}, now=now)
        _record_exits(db_file, conn, now)
    finally:
        conn.close()


def _manage(db_file, conn, broker, signal, now):
    position = _ensure_position(db_file, conn, signal, now)
    sid, symbol = position["signal_id"], position["symbol"]
    if position["state"] == "CLOSING":
        updated = datetime.fromisoformat(position["updated_at"])
        if now - updated >= timedelta(seconds=CLOSE_RETRY_SECONDS):
            _update(conn, sid, now)
            broker.close(signal, position["close_reason"])
            journal.record(db_file, "CLOSE_REQUEST", symbol=symbol, signal_id=sid,
                           reason=position["close_reason"], detail={"retry": True}, now=now)
        return
    opened = datetime.fromisoformat(position["opened_at"])
    high = position["high_since_entry"]
    try:
        bars = broker.bars_since(symbol, opened)
        high = max([high] + [float(b["high"]) for b in bars if b.get("high") is not None])
    except Exception as exc:
        journal.record(db_file, "ERROR", symbol=symbol, signal_id=sid, reason="BARS_FAILED",
                       detail={"error": repr(exc)}, now=now)
    if high > position["high_since_entry"]:
        _update(conn, sid, now, high_since_entry=high)
    decision = rebound_exits.next_action(
        entry=position["entry"], initial_stop=position["initial_stop"],
        current_stop=position["current_stop"], high_since_entry=high,
        opened_at=opened, now=now)
    if decision["action"] == "MOVE_STOP":
        broker.modify_stop(signal, decision["stop"])
        _update(conn, sid, now, current_stop=decision["stop"])
        journal.record(db_file, "STOP_MOVE", symbol=symbol, signal_id=sid,
                       detail={"from": position["current_stop"], "to": decision["stop"], "high": high},
                       now=now)
    elif decision["action"] == "CLOSE":
        broker.close(signal, decision["reason"])
        _update(conn, sid, now, state="CLOSING", close_reason=decision["reason"])
        journal.record(db_file, "CLOSE_REQUEST", symbol=symbol, signal_id=sid,
                       reason=decision["reason"], detail={"high": high}, now=now)


def has_work(db_file):
    """Cheap read-only check so the worker only connects to IBKR when needed."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return False
    try:
        open_signals = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE strategy=? AND status='OPEN_POSITION'",
            (journal.STRATEGY,)).fetchone()[0]
        try:
            open_positions = conn.execute(
                "SELECT COUNT(*) FROM rebound_positions WHERE state != 'CLOSED'").fetchone()[0]
        except sqlite3.OperationalError:
            open_positions = 0
        return bool(open_signals or open_positions)
    except sqlite3.Error:
        return False
    finally:
        conn.close()
```

- [ ] **Step 5: Verify** — `venv/bin/python -m unittest test_rebound_journal_manager -v` → 12 tests OK.
- [ ] **Step 6: Commit** — `git commit -m "Add rebound journal and stop manager"`.

---

### Task 5: IBKR broker adapter and worker hook

**Files:**
- Create: `rebound_ib_broker.py`, `test_rebound_ib_broker.py`, `test_rebound_worker_hook.py`
- Modify: `worker_core.py` (IBApp `__init__` ≈ line 870–960, new IBApp methods, new module function before `def main():` ≈ 6730, loop in `main()` ≈ 6800)

- [ ] **Step 1: Write the broker tests**

```python
import threading
import types
import unittest
from datetime import datetime, timezone

import rebound_ib_broker as rib


def fake_wc():
    wc = types.SimpleNamespace(IB_ACCOUNT="DU1", ALLOW_OUTSIDE_RTH=True)
    wc.stock_contract = lambda symbol: ("contract", symbol)
    wc.load_order_state = lambda ib: None
    wc.check_market_session = lambda ib, symbol: {"market_rule": [], "min_tick": 0.01}
    wc.normalize_price_to_market_rule = lambda p, rule, tick, rounding: round(p, 2)
    wc.build_stop_limit_price = lambda action, trig, rule, tick: round(trig * 0.99, 2)
    return wc


class FakeIB:
    def __init__(self, bars=(), reject=False):
        self.historical_events, self.historical_bars = {}, {}
        self.bars = list(bars)
        self.reject = reject
        self.placed = []
        self.fatal_order_error = threading.Event()
        self.expected_order_ids, self.reject_messages = set(), []
        self.open_orders = [{"order_id": 12, "status": "PreSubmitted", "action": "SELL",
                             "order_type": "STP LMT", "total_quantity": 500.0, "parent_id": 10,
                             "order_ref": "ref-sl", "symbol": "ABC"}]

    def reqHistoricalData(self, req_id, *args):
        self.historical_bars[req_id] = list(self.bars)
        self.historical_events[req_id].set()

    def cancelHistoricalData(self, req_id):
        pass

    def placeOrder(self, order_id, contract, order):
        self.placed.append((order_id, contract, order))
        if self.reject:
            self.reject_messages.append("IBKR 201: rejected")
            self.fatal_order_error.set()


SIGNAL = {"signal_id": "s1", "symbol": "ABC", "stop_order_id": 12}


class BrokerTests(unittest.TestCase):
    def test_bars_since_filters_and_cleans_up(self):
        since = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
        t = int(since.timestamp())
        ib = FakeIB(bars=[{"timestamp": t - 120, "high": 9}, {"timestamp": t - 60, "high": 2},
                          {"timestamp": t + 60, "high": 3}, {"timestamp": None, "high": 99}])
        bars = rib.IBReboundBroker(ib, fake_wc()).bars_since("ABC", since)
        self.assertEqual([b["high"] for b in bars], [2, 3])
        self.assertEqual((ib.historical_events, ib.historical_bars), ({}, {}))

    def test_modify_stop_reuses_child_identity(self):
        ib = FakeIB()
        rib.IBReboundBroker(ib, fake_wc()).modify_stop(SIGNAL, 2.157)
        order_id, contract, order = ib.placed[0]
        self.assertEqual((order_id, order.parentId, order.action, order.orderType), (12, 10, "SELL", "STP LMT"))
        self.assertEqual((order.auxPrice, order.lmtPrice, order.totalQuantity), (2.16, 2.14, 500.0))
        self.assertEqual((order.tif, order.outsideRth, order.orderRef), ("GTC", True, "ref-sl"))

    def test_close_moves_stop_through_market(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")
        order = ib.placed[0][2]
        self.assertEqual((order.auxPrice, order.lmtPrice), (2.02, 1.94))

    def test_missing_stop_and_reject_raise(self):
        ib = FakeIB()
        ib.open_orders[0]["status"] = "Filled"
        with self.assertRaises(LookupError):
            rib.IBReboundBroker(ib, fake_wc()).modify_stop(SIGNAL, 2.1)
        with self.assertRaises(RuntimeError):
            rib.IBReboundBroker(FakeIB(reject=True), fake_wc()).modify_stop(SIGNAL, 2.1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** → `ModuleNotFoundError`.
- [ ] **Step 3: Implement the broker**

```python
"""IBKR implementation of the rebound stop-manager broker, used inside the worker.

`wc` is the worker_core module (passed in to avoid a circular import).
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR

from ibapi.order import Order

HISTORY_TIMEOUT_SECONDS = 10
REJECT_WAIT_SECONDS = 3
CLOSE_TRIGGER_UP = 1.01
CLOSE_LIMIT_DOWN = 0.97
_ACTIVE_STATUSES = {"PreSubmitted", "Submitted", "PendingSubmit", "ApiPending", "PendingCancel"}


class IBReboundBroker:
    def __init__(self, ib, wc, req_id_base=91000):
        self.ib = ib
        self.wc = wc
        self._next_req = req_id_base

    def bars_since(self, symbol, since):
        self._next_req += 1
        req_id = self._next_req
        event = threading.Event()
        self.ib.historical_events[req_id] = event
        self.ib.historical_bars[req_id] = []
        self.ib.reqHistoricalData(req_id, self.wc.stock_contract(symbol), "", "1 D", "1 min",
                                  "TRADES", 0, 2, False, [])
        try:
            if not event.wait(timeout=HISTORY_TIMEOUT_SECONDS):
                try:
                    self.ib.cancelHistoricalData(req_id)
                except Exception:
                    pass
                raise TimeoutError(f"historical bars timeout for {symbol}")
            cutoff = since.timestamp() - 60
            return [bar for bar in self.ib.historical_bars.get(req_id, [])
                    if bar.get("timestamp") is not None and bar["timestamp"] >= cutoff]
        finally:
            self.ib.historical_events.pop(req_id, None)
            self.ib.historical_bars.pop(req_id, None)

    def _stop_order(self, signal):
        stop_id = signal.get("stop_order_id")
        if not stop_id:
            raise LookupError(f"{signal['signal_id']}: no stop_order_id")
        self.wc.load_order_state(self.ib)
        for item in self.ib.open_orders:
            if int(item.get("order_id") or 0) == int(stop_id) \
                    and item.get("status") in _ACTIVE_STATUSES:
                return item
        raise LookupError(f"{signal['signal_id']}: stop order {stop_id} not active")

    def _rules(self, symbol):
        session = self.wc.check_market_session(self.ib, symbol)
        return session.get("market_rule") or [], float(session.get("min_tick") or 0.01)

    def _place_stop(self, item, trigger, limit):
        order = Order()
        order.orderId = int(item["order_id"])
        order.account = self.wc.IB_ACCOUNT
        order.action = item["action"]
        order.orderType = item["order_type"]
        order.totalQuantity = item["total_quantity"]
        order.auxPrice = trigger
        if item["order_type"] == "STP LMT":
            order.lmtPrice = limit
        order.parentId = int(item.get("parent_id") or 0)
        order.tif = "GTC"
        order.outsideRth = self.wc.ALLOW_OUTSIDE_RTH
        order.transmit = True
        order.orderRef = item.get("order_ref") or ""
        self.ib.expected_order_ids = {order.orderId}
        self.ib.reject_messages = []
        self.ib.fatal_order_error.clear()
        self.ib.placeOrder(order.orderId, self.wc.stock_contract(item["symbol"]), order)
        if self.ib.fatal_order_error.wait(timeout=REJECT_WAIT_SECONDS):
            raise RuntimeError("; ".join(self.ib.reject_messages) or "stop modify rejected")

    def modify_stop(self, signal, trigger):
        item = self._stop_order(signal)
        rule, tick = self._rules(signal["symbol"])
        trigger = self.wc.normalize_price_to_market_rule(trigger, rule, tick, ROUND_FLOOR)
        limit = self.wc.build_stop_limit_price("BUY", trigger, rule, tick)
        self._place_stop(item, trigger, limit)

    def close(self, signal, reason):
        """Move the stop child to the market so the bracket closes as a stop exit."""
        bars = self.bars_since(signal["symbol"],
                               datetime.now(timezone.utc) - timedelta(minutes=10))
        if not bars:
            raise LookupError(f"{signal['symbol']}: no recent bars to price the close")
        last = float(bars[-1]["close"])
        item = self._stop_order(signal)
        rule, tick = self._rules(signal["symbol"])
        trigger = self.wc.normalize_price_to_market_rule(last * CLOSE_TRIGGER_UP, rule, tick,
                                                         ROUND_CEILING)
        limit = self.wc.normalize_price_to_market_rule(last * CLOSE_LIMIT_DOWN, rule, tick,
                                                       ROUND_FLOOR)
        self._place_stop(item, trigger, limit)
```

Design notes: modifying the existing SL child (same order id, parent id, quantity, order ref, `STP LMT`, GTC, outsideRth) keeps the position protected at all times, and a close fills as the bracket's own stop, so the existing monitor records it as `CLOSED_SL`. The TP sibling is cancelled by OCA. A successful modify waits 3 s for an IBKR reject.

- [ ] **Step 4: Verify** → 4 tests OK.
- [ ] **Step 5: Write the worker hook test**

```python
import inspect
import threading
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import worker_core


class FakeBar:
    date = "1791216000"
    open, high, low, close = 1.0, 1.1, 0.9, 1.05
    volume = Decimal("1500")


class HistoricalCallbackTests(unittest.TestCase):
    def test_bars_are_collected_and_end_sets_event(self):
        ib = worker_core.IBApp()
        event = threading.Event()
        ib.historical_events[5] = event
        ib.historicalData(5, FakeBar())
        ib.historicalDataEnd(5, "", "")
        self.assertTrue(event.is_set())
        self.assertEqual(ib.historical_bars[5], [{"timestamp": 1791216000, "open": 1.0, "high": 1.1,
                                                  "low": 0.9, "close": 1.05, "volume": 1500.0}])

    def test_unknown_end_is_ignored(self):
        worker_core.IBApp().historicalDataEnd(99, "", "")


class RunReboundManagerTests(unittest.TestCase):
    def test_no_work_does_not_connect(self):
        with patch("rebound_stop_manager.has_work", return_value=False), \
                patch.object(worker_core, "connect_ibkr") as connect:
            worker_core.run_rebound_manager()
        connect.assert_not_called()

    def test_live_account_does_not_connect(self):
        with patch("rebound_stop_manager.has_work", return_value=True), \
                patch.object(worker_core, "IB_PORT", 7496), \
                patch.object(worker_core, "connect_ibkr") as connect:
            worker_core.run_rebound_manager()
        connect.assert_not_called()

    def test_paper_ticks_and_disconnects(self):
        fake_ib = MagicMock()
        with patch("rebound_stop_manager.has_work", return_value=True), \
                patch.object(worker_core, "IB_PORT", 7497), \
                patch.object(worker_core, "IB_ACCOUNT", "DU123"), \
                patch.object(worker_core, "connect_ibkr", return_value=fake_ib), \
                patch("rebound_stop_manager.tick", side_effect=RuntimeError("boom")) as tick:
            with self.assertRaises(RuntimeError):
                worker_core.run_rebound_manager()
        db_file, broker = tick.call_args.args
        self.assertEqual(db_file, worker_core.DB_FILE)
        self.assertIs(broker.ib, fake_ib)
        self.assertIs(broker.wc, worker_core)
        fake_ib.disconnect.assert_called_once()

    def test_main_loop_runs_manager(self):
        source = inspect.getsource(worker_core.main)
        self.assertIn("run_rebound_manager()", source)
        self.assertIn("REBOUND_MANAGER_INTERVAL_SECONDS", source)


if __name__ == "__main__":
    unittest.main()
```

If `import worker_core` fails in the worktree because of missing environment (no `.env`), set the required non-secret env vars at the top of the test with `os.environ.setdefault(...)`; never copy `.env`.

- [ ] **Step 6: Run to verify failure** → `AttributeError: ... has no attribute 'run_rebound_manager'` / `historicalData`.
- [ ] **Step 7: Modify `worker_core.py`**

(a) In `IBApp.__init__`, after `self.cancelled_ids = set()`, add:

```python
        self.historical_bars = {}
        self.historical_events = {}
```

(b) Add these methods to `IBApp` (directly after `openOrder`):

```python
    def historicalData(self, reqId, bar):
        try:
            timestamp = int(str(bar.date))
        except (TypeError, ValueError):
            timestamp = None
        self.historical_bars.setdefault(reqId, []).append({
            "timestamp": timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume or 0),
        })

    def historicalDataEnd(self, reqId, start, end):
        event = self.historical_events.get(reqId)
        if event is not None:
            event.set()
```

(c) Add `import sys` to the imports if missing, and this block immediately before `def main():`:

```python
REBOUND_MANAGER_INTERVAL_SECONDS = float(
    os.getenv("REBOUND_MANAGER_INTERVAL_SECONDS", "10")
)


def run_rebound_manager():
    """Ratchet/close open microcap_rebound_v1 positions (paper account only)."""
    import rebound_stop_manager

    if not rebound_stop_manager.has_work(DB_FILE):
        return
    if not (str(IB_PORT) == "7497" and str(IB_ACCOUNT).upper().startswith("DU")):
        print("REBOUND MANAGER SKIP | not the paper account", flush=True)
        return
    import rebound_ib_broker

    ib = connect_ibkr()
    try:
        rebound_stop_manager.tick(
            DB_FILE, rebound_ib_broker.IBReboundBroker(ib, sys.modules[__name__])
        )
    finally:
        ib.disconnect()
```

(d) In `main()`, set `last_rebound = 0.0` right after `last_reconcile = (time.monotonic())`, and inside `while True: try:` immediately after the reconcile `if` block (before `cancel_request = ...`) add:

```python
            if (
                time.monotonic() - last_rebound
                >= REBOUND_MANAGER_INTERVAL_SECONDS
            ):
                last_rebound = time.monotonic()
                try:
                    run_rebound_manager()
                except Exception as exc:
                    print(
                        f"REBOUND MANAGER ERROR | {type(exc).__name__}: {exc}",
                        flush=True,
                    )
```

The manager is deliberately not gated by the kill switch: it only raises stops and closes positions.

- [ ] **Step 8: Verify** — `venv/bin/python -m unittest test_rebound_ib_broker test_rebound_worker_hook -v` → OK, then the full suite → baseline + new tests, no failures.
- [ ] **Step 9: Commit** — `git commit -m "Manage rebound positions from the worker"`.

---

### Task 6: `REBOUND` mode in the runner and strict news gate

**Files:**
- Modify: `signal_bridge.py` (imports ≈ line 14; `valid_modes` ≈ 1809; per-candidate loop ≈ 1955–2075)
- Modify: `ai_signal_bridge.py` (imports ≈ 27; `process_candidate` ≈ 580)
- Modify: `signal_server_core.py` (`VALID_STRATEGY_MODES` ≈ 1830; `valid_modes` list ≈ 1885)
- Test: `test_rebound_bridge.py`

- [ ] **Step 1: Write the failing tests**

```python
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import ai_signal_bridge
import rebound_journal
import signal_bridge

CANDIDATE = {"symbol": "ABC", "action": "BUY", "entry": 2.0, "stop": 1.9, "target": 2.3,
             "qualified": True, "strategy": "microcap_rebound_v1", "timeframe": "1m"}
STRONG = {"status": "PASS", "news_count": 2, "ai": {"news_score": 0.8, "event_type": "CONTRACT"}}


class ProcessReboundCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "trading.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE signals (signal_id TEXT, strategy TEXT, status TEXT)")
        self.original = patch.object(ai_signal_bridge, "_original_process_candidate",
                                     return_value={"signal_id": "sig-1", "status": "SENT"})
        self.post = self.original.start()
        self.paper = patch("rebound_strategy.paper_guard", return_value=True)
        self.paper.start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def run_with(self, gate):
        with patch.object(ai_signal_bridge.ai_gate, "evaluate_trade_candidate", gate):
            return ai_signal_bridge.process_rebound_candidate(dict(CANDIDATE), "s", db_file=self.db)

    def journal(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT event, reason FROM rebound_journal ORDER BY id").fetchall()

    def test_strong_news_posts_and_journals_signal(self):
        outcome = self.run_with(MagicMock(return_value=STRONG))
        self.assertEqual(outcome["signal_id"], "sig-1")
        self.post.assert_called_once()
        self.assertEqual(self.journal(), [("SIGNAL", "SENT")])

    def test_weak_news_is_skipped(self):
        weak = {**STRONG, "ai": {"news_score": 0.2, "event_type": "CONTRACT"}}
        outcome = self.run_with(MagicMock(return_value=weak))
        self.assertEqual(outcome["reason"], "NEWS_NOT_STRONG")
        self.post.assert_not_called()
        self.assertEqual(self.journal(), [("SKIP", "NEWS_NOT_STRONG")])

    def test_gate_error_fails_closed(self):
        outcome = self.run_with(MagicMock(side_effect=TimeoutError("news timeout")))
        self.assertEqual(outcome["reason"], "NEWS_GATE_ERROR")
        self.post.assert_not_called()

    def test_busy_skips_before_gate(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO signals VALUES ('x', 'microcap_rebound_v1', 'SUBMITTED')")
        gate = MagicMock(return_value=STRONG)
        outcome = self.run_with(gate)
        self.assertEqual(outcome["reason"], "SKIP_BUSY")
        gate.assert_not_called()

    def test_not_paper_skips(self):
        with patch("rebound_strategy.paper_guard", return_value=False):
            outcome = self.run_with(MagicMock(return_value=STRONG))
        self.assertEqual(outcome["reason"], "SKIP_NOT_PAPER")
        self.post.assert_not_called()

    def test_process_candidate_routes_rebound(self):
        with patch.object(ai_signal_bridge, "process_rebound_candidate",
                          return_value={"status": "X"}) as strict:
            self.assertEqual(ai_signal_bridge.process_candidate(dict(CANDIDATE), "s"), {"status": "X"})
        strict.assert_called_once()


class ReboundModeTests(unittest.TestCase):
    def test_collect_dispatches_rebound(self):
        with tempfile.TemporaryDirectory() as home:
            mode_dir = Path(home) / ".cache" / "tradingmax"
            mode_dir.mkdir(parents=True)
            (mode_dir / "strategy_mode.txt").write_text("REBOUND\n", encoding="utf-8")
            app = MagicMock()
            app.market_data = {1: {"bid": 1.99, "ask": 2.0}}
            app.isConnected.return_value = False
            skip = {"symbol": "ABC", "qualified": False, "skip_reason": "SKIP_CYCLES",
                    "rebound": {"cycles": 1}, "strategy": "microcap_rebound_v1"}
            with patch.object(signal_bridge.Path, "home", return_value=Path(home)), \
                    patch.object(signal_bridge, "mark_phase"), \
                    patch.object(signal_bridge, "record_bridge_mode"), \
                    patch.object(signal_bridge, "record_analysis"), \
                    patch.object(signal_bridge.strategy_engine, "get_candidates",
                                 return_value=[{"symbol": "ABC"}]), \
                    patch.object(signal_bridge.strategy_engine, "TradingMaxStrategy", return_value=app), \
                    patch.object(signal_bridge.strategy_engine, "connect_strategy"), \
                    patch.object(signal_bridge.strategy_engine, "start_live", return_value={"ABC": 1}), \
                    patch.object(signal_bridge.strategy_engine, "stop_live"), \
                    patch.object(signal_bridge.strategy_engine, "request_history") as momentum, \
                    patch.object(signal_bridge.scalp_strategy, "request_history") as scalp, \
                    patch.object(signal_bridge.rebound_strategy, "request_history", return_value=[]), \
                    patch.object(signal_bridge.rebound_strategy, "analyze", return_value=skip) as analyze, \
                    patch.object(signal_bridge.rebound_journal, "record_skip") as record_skip, \
                    patch.object(signal_bridge.time, "sleep"):
                results = signal_bridge.collect_strategy_results()
        self.assertEqual(results, [skip])
        self.assertEqual(analyze.call_args.args[2], {"bid": 1.99, "ask": 2.0})
        record_skip.assert_called_once_with(rebound_journal.DEFAULT_DB, "ABC", "SKIP_CYCLES", {"cycles": 1})
        momentum.assert_not_called()
        scalp.assert_not_called()

    def test_server_accepts_rebound_mode(self):
        source = Path(signal_bridge.__file__).with_name("signal_server_core.py").read_text(encoding="utf-8")
        start = source.index("VALID_STRATEGY_MODES = {")
        self.assertIn('"REBOUND"', source[start:source.index("}", start)])
        listing = source.index('"valid_modes":')
        self.assertIn('"REBOUND"', source[listing:source.index("]", listing)])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure** — `venv/bin/python -m unittest test_rebound_bridge -v` → failures (no `process_rebound_candidate`, `REBOUND` not valid).
- [ ] **Step 3: `signal_bridge.py`**

(a) Imports, after `import scalp_strategy`:

```python
import rebound_journal
import rebound_strategy
```

(b) Add `"REBOUND",` to the local `valid_modes` set in `collect_strategy_results()`.

(c) In the per-candidate loop, after the `if mode in {"SCALP", "AUTO", "BOTH"}:` block and before `if mode == "MOMENTUM":`, add:

```python
            rebound_result = None

            if mode == "REBOUND":
                rebound_bars = rebound_strategy.request_history(
                    app,
                    candidate,
                    60000 + index,
                    strategy_engine.make_contract,
                )
                rebound_result = rebound_strategy.analyze(
                    candidate,
                    rebound_bars,
                    quote,
                    datetime.now(timezone.utc),
                )
                if not rebound_result.get("qualified"):
                    try:
                        rebound_journal.record_skip(
                            rebound_journal.DEFAULT_DB,
                            rebound_result.get("symbol") or candidate.get("symbol"),
                            rebound_result.get("skip_reason"),
                            rebound_result.get("rebound"),
                        )
                    except Exception as exc:
                        print(
                            f"REBOUND JOURNAL ERROR | {type(exc).__name__}: {exc}",
                            flush=True,
                        )
```

(d) Change `if mode == "MOMENTUM":` to start a chain that handles REBOUND first:

```python
            if mode == "REBOUND":
                results.append(rebound_result)

            elif mode == "MOMENTUM":
```

(keep the rest of the chain unchanged).

- [ ] **Step 4: `ai_signal_bridge.py`**

(a) Imports, after `import ai_gate`:

```python
import rebound_journal
import rebound_strategy
```

(b) Add this function above `def process_candidate(`:

```python
def process_rebound_candidate(candidate, secret, db_file=None):
    """Strict path for microcap_rebound_v1: no bypasses, everything fails closed."""
    db_file = db_file or rebound_journal.DEFAULT_DB
    symbol = str(candidate.get("symbol") or "").strip().upper()

    def skip(reason, detail=None):
        rebound_journal.record_skip(db_file, symbol, reason, detail)
        print(f"REBOUND SKIP | {symbol} | {reason}", flush=True)
        return {"status": "REBOUND_SKIP", "symbol": symbol, "reason": reason}

    if not rebound_strategy.paper_guard():
        return skip("SKIP_NOT_PAPER")
    if rebound_journal.is_busy(db_file):
        return skip("SKIP_BUSY")
    try:
        ai_result = ai_gate.evaluate_trade_candidate(candidate)
    except Exception as exc:
        return skip("NEWS_GATE_ERROR", {"error": f"{type(exc).__name__}: {exc}"})
    passed, reason = rebound_strategy.news_verdict(ai_result)
    ai = ai_result.get("ai") if isinstance(ai_result, dict) else None
    news = {
        "status": ai_result.get("status") if isinstance(ai_result, dict) else None,
        "news_count": ai_result.get("news_count") if isinstance(ai_result, dict) else None,
        "news_score": ai.get("news_score") if isinstance(ai, dict) else None,
        "event_type": ai.get("event_type") if isinstance(ai, dict) else None,
    }
    if not passed:
        return skip(reason, news)
    outcome = _original_process_candidate(candidate, secret)
    rebound_journal.record(
        db_file, "SIGNAL", symbol=symbol,
        signal_id=(outcome or {}).get("signal_id"),
        reason=(outcome or {}).get("status"),
        detail={"entry": candidate.get("entry"), "stop": candidate.get("stop"),
                "target": candidate.get("target"), "news": news,
                "rebound": candidate.get("rebound")},
    )
    return outcome
```

(c) In `process_candidate`, immediately after `signal_bridge.validate_candidate(candidate)`:

```python
    if candidate.get("strategy") == rebound_strategy.STRATEGY_NAME:
        return process_rebound_candidate(candidate, secret)
```

- [ ] **Step 5: `signal_server_core.py`** — add `"REBOUND",` to `VALID_STRATEGY_MODES` and to the `valid_modes` list returned by `GET /strategy-mode`.
- [ ] **Step 6: Verify** — `venv/bin/python -m unittest test_rebound_bridge -v` → OK; full suite → no regressions. Also `venv/bin/python signal_bridge.py --self-test` must still pass.
- [ ] **Step 7: Commit** — `git commit -m "Add REBOUND strategy mode with strict news gate"`.

---

### Task 7: `/rebound-journal` route and dashboard "Paper trades" card

**Files:**
- Modify: `signal_server.py` (next to `/microcap-research-status` ≈ line 1590)
- Modify: `dashboard.html` (top of `<section id="tab-research">`), `dashboard_app.js`
- Test: `test_rebound_route.py`, `test_dashboard_app.js`

- [ ] **Step 1: Write the failing route test**

```python
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_microcap_readiness_route import TestClient, isolated_server, request


class ReboundRouteTests(unittest.TestCase):
    def test_requires_auth(self):
        server, _ = isolated_server()
        status, _ = request(server.app, "/rebound-journal")
        self.assertEqual(status, 401)

    def test_returns_summary(self):
        server, _ = isolated_server()
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "trading.db")
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE signals (signal_id TEXT, symbol TEXT, status TEXT, "
                             "strategy TEXT, entry_fill_price REAL, exit_fill_price REAL, "
                             "realized_pnl REAL, net_realized_pnl REAL, exit_reason TEXT, "
                             "entry_time TEXT, exit_time TEXT)")
            with patch.dict(os.environ, {"REBOUND_DB_FILE": db}):
                status, body = request(server.app, "/rebound-journal", auth=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["strategy"], "microcap_rebound_v1")
        self.assertEqual(body["stats"]["trades"], 0)

    def test_failure_is_503(self):
        server, _ = isolated_server()
        with patch.object(server.rebound_journal, "summary", side_effect=sqlite3.OperationalError("x")):
            status, body = request(server.app, "/rebound-journal", auth=True)
        self.assertEqual((status, body["error"]), (503, "REBOUND_JOURNAL_UNAVAILABLE"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Add the route** to `signal_server.py` (add `from fastapi.responses import FileResponse, JSONResponse` and `import rebound_journal`):

```python
@app.get("/rebound-journal")
def rebound_journal_status(user=Depends(dashboard_auth)):
    db_file = os.environ.get("REBOUND_DB_FILE") or rebound_journal.DEFAULT_DB
    try:
        return rebound_journal.summary(db_file)
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"error": "REBOUND_JOURNAL_UNAVAILABLE", "detail": type(exc).__name__},
        )
```

- [ ] **Step 3: Verify route** — `venv/bin/python -m unittest test_rebound_route -v` → OK.
- [ ] **Step 4: Write the failing JS tests** — add to `test_dashboard_app.js` (and call both from the runner block at the bottom, `await` for the async one):

```javascript
function testRenderRebound() {
  const doc = fakeDoc();
  app.renderRebound(doc, {
    stats: { trades: 3, wins: 2, losses: 1, total_pnl: 42.5 },
    open_positions: [{ symbol: ATTACK, entry: 2, initial_stop: 1.9, current_stop: 2.02,
      high_since_entry: 2.1, opened_at: "2026-10-06T14:00:00+00:00", state: "OPEN" }],
    trades: [{ exit_time: "x", symbol: "ABC", status: "CLOSED_TP", entry_fill_price: 2,
      exit_fill_price: 2.3, pnl: -5 }],
    events: [{ ts: "t", event: "SKIP", symbol: "ABC", reason: "SKIP_CYCLES" }],
  });
  assert.equal(doc.nodes.reboundTrades.textContent, "3");
  assert.equal(doc.nodes.reboundWinLoss.textContent, "2 / 1");
  assert.equal(doc.nodes.reboundPnl.textContent, "+42.50");
  assert.equal(doc.nodes.reboundPnl.className, "pos");
  assert.deepEqual(cells(doc.nodes.reboundOpenRows.children[0]),
    [ATTACK, "2.0000", "1.9000", "2.0200", "2.1000", "2026-10-06T14:00:00+00:00", "OPEN"]);
  assert.equal(doc.nodes.reboundTradeRows.children[0].children[5].className, "num neg");
  assert.deepEqual(cells(doc.nodes.reboundEventRows.children[0]), ["t", "SKIP", "ABC", "SKIP_CYCLES"]);
  app.renderRebound(doc, null);
  assert.equal(doc.nodes.reboundTrades.textContent, "-");
  assert.equal(cells(doc.nodes.reboundOpenRows.children[0])[0], "No open rebound position");
}

async function testReboundFetchFailureShowsUnavailable() {
  const { doc, instance } = harness({ "/rebound-journal": new Error("down") });
  await instance.refreshRebound();
  assert.equal(doc.nodes.reboundTrades.textContent, "UNAVAILABLE");
  assert.equal(cells(doc.nodes.reboundEventRows.children[0])[0], "UNAVAILABLE");
}
```

Run `node test_dashboard_app.js` → fails (`renderRebound` is not a function).

- [ ] **Step 5: Implement in `dashboard_app.js`**

(a) Constants: `const REBOUND_POLL_MS = 15000;` and
`const REBOUND_KPI_IDS = ["reboundTrades", "reboundWinLoss", "reboundPnl"];`

(b) Pure renderer (after `renderHealth`):

```javascript
  function renderRebound(doc, data) {
    const body = object(data);
    const stats = object(body.stats);
    setText(doc, "reboundTrades", plain(stats.trades));
    setText(doc, "reboundWinLoss", `${plain(stats.wins)} / ${plain(stats.losses)}`);
    setText(doc, "reboundPnl", signed(stats.total_pnl), pnlClass(stats.total_pnl));
    fillRows(doc, "reboundOpenRows", list(body.open_positions).map(item => [
      [str(item.symbol)], [num(item.entry, 4), "num"], [num(item.initial_stop, 4), "num"],
      [num(item.current_stop, 4), "num"], [num(item.high_since_entry, 4), "num"],
      [str(item.opened_at)], [str(item.state)],
    ]), "No open rebound position", 7);
    fillRows(doc, "reboundTradeRows", list(body.trades).map(item => [
      [str(item.exit_time)], [str(item.symbol)], [str(item.status)],
      [num(item.entry_fill_price, 4), "num"], [num(item.exit_fill_price, 4), "num"],
      [signed(item.pnl), `num ${pnlClass(item.pnl)}`.trim()],
    ]), "No closed rebound trades yet", 6);
    fillRows(doc, "reboundEventRows", list(body.events).slice(0, 20).map(item => [
      [str(item.ts)], [str(item.event)], [str(item.symbol)], [str(item.reason)],
    ]), "No decisions yet", 4);
  }
```

(c) In `createApp`, add:

```javascript
    async function refreshRebound() {
      try {
        renderRebound(doc, await fetchJson(fetchImpl, "/rebound-journal", {}, win));
      } catch (error) {
        markUnavailable(doc, REBOUND_KPI_IDS);
        fillRows(doc, "reboundOpenRows", [], "UNAVAILABLE", 7);
        fillRows(doc, "reboundTradeRows", [], "UNAVAILABLE", 6);
        fillRows(doc, "reboundEventRows", [], "UNAVAILABLE", 4);
      }
    }
```

In `start()`: `const pollRebound = guarded("rebound", refreshRebound);`, call `pollRebound();` with the other initial polls, and `win.setInterval(pollRebound, REBOUND_POLL_MS);`. Export `refreshRebound` from the `createApp` return object and `renderRebound` from `api`.

- [ ] **Step 6: Add the card** to `dashboard.html` as the first child of `<section id="tab-research">`, right after `<h1>Micro-cap research</h1>`:

```html
      <div class="card">
        <h2>Paper trades · microcap_rebound_v1</h2>
        <div class="kpis">
          <div class="kpi"><div class="kpi-label">Closed trades</div><div id="reboundTrades" class="kpi-value">-</div></div>
          <div class="kpi"><div class="kpi-label">Wins / losses</div><div id="reboundWinLoss" class="kpi-value">-</div></div>
          <div class="kpi"><div class="kpi-label">Net P&amp;L</div><div id="reboundPnl" class="kpi-value">-</div></div>
        </div>
        <h3>Open position</h3>
        <table>
          <thead><tr><th>Symbol</th><th class="num">Entry</th><th class="num">Initial stop</th><th class="num">Stop now</th><th class="num">High</th><th>Opened</th><th>State</th></tr></thead>
          <tbody id="reboundOpenRows"></tbody>
        </table>
        <h3>Closed trades</h3>
        <table>
          <thead><tr><th>Exit time</th><th>Symbol</th><th>Result</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">P&amp;L</th></tr></thead>
          <tbody id="reboundTradeRows"></tbody>
        </table>
        <h3>Recent decisions</h3>
        <table>
          <thead><tr><th>Time (UTC)</th><th>Event</th><th>Symbol</th><th>Reason</th></tr></thead>
          <tbody id="reboundEventRows"></tbody>
        </table>
      </div>
```

- [ ] **Step 7: Verify** — `node test_dashboard_app.js` → `dashboard_app tests passed`; full Python suite → OK.
- [ ] **Step 8: Commit** — `git commit -m "Show rebound paper trades on the dashboard"`.

---

### Task 8: Docs, merge, and rollout

**Part 1 (implementer):**

- [ ] **Step 1: Add a "Micro-cap rebound paper trading" section to `OPERATIONS.md`** covering: what the strategy does (one paragraph, link the spec); how to enable (`echo REBOUND > ~/.cache/tradingmax/strategy_mode.txt` or `POST /strategy-mode {"mode":"REBOUND"}`); runner env `NEWS_LOOKBACK_HOURS=48`; dry-run first via `BRIDGE_DRY_RUN=true`; sizing via `.env` `MAX_POSITION_USD=1000` and `MAX_RISK_PER_TRADE_USD=60`; the worker manager env `REBOUND_MANAGER_INTERVAL_SECONDS` (default 10); where to look (dashboard Research → Paper trades, `rebound_journal` / `rebound_positions` tables, `journalctl -u trading-worker` lines `REBOUND MANAGER`); how to stop (set mode back, or kill switch — note the kill switch blocks new entries only, the manager keeps protecting open positions); paper-only guards (`IB_PORT=7497`, account `DU…`).
- [ ] **Step 2: Full verification** — full Python suite and `node test_dashboard_app.js`, both green.
- [ ] **Step 3: Commit** — `git commit -m "Document rebound paper trading operations"`.

**Part 2 (controller, each step needs the user's explicit OK):**

- [ ] **Step 4: Merge** — final code review of the branch, fast-forward `master` (stash/restore any new live dirty files first), push with the `GH_TOKEN` workaround, remove the worktree.
- [ ] **Step 5: Dry-run session** — write a runner drop-in `/etc/systemd/system/trading-signal-runner.service.d/zzzzzzzzzzzzzz-rebound.conf`:

```ini
[Service]
Environment=STRATEGY_MODE=REBOUND
Environment=NEWS_LOOKBACK_HOURS=48
Environment=BRIDGE_DRY_RUN=true
```

set the mode file to `REBOUND`, `systemctl daemon-reload`, restart `trading-bot`, `trading-signal-runner`, and the worker service. Check: runner log shows `STRATEGY_MODE = REBOUND`, the dashboard Paper trades card shows SKIP decisions, no POSTs.
- [ ] **Step 6: Paper** — after one session of reasonable dry-run decisions: `.env` `MAX_POSITION_USD=1000`, `MAX_RISK_PER_TRADE_USD=60`; remove `BRIDGE_DRY_RUN=true` from the drop-in; daemon-reload; restart `trading-bot` and the runner. Watch the first trade end-to-end (ENTRY → STOP_MOVE/CLOSE_REQUEST → EXIT on the dashboard and in TWS).

