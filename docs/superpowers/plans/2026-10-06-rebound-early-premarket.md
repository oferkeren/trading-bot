# Rebound Early Pre-market Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `microcap_rebound_v1` detect small fast cycles (≥4% in ≤5 bars) and accept spreads up to 3% during 04:00-07:00 ET, tag every decision with its session, and report results per session.

**Architecture:** A frozen `Profile` dataclass in `rebound_strategy.py` holds the per-session spike/spread parameters; `profile_for(now)` picks one. `detect_cycles`/`_find_spike` take the profile; `analyze` tags every post-session-check result with `rebound.session`. `rebound_journal.summary` adds `stats_by_session` derived from `signals.entry_time`; the dashboard card shows it.

**Tech Stack:** Python 3 stdlib (`unittest`, `sqlite3`, `zoneinfo`), vanilla JS dashboard with `node test_dashboard_app.js`.

**Spec:** `docs/superpowers/specs/2026-10-06-rebound-early-premarket-design.md`

## Ground rules

- Work in `/home/oferke/trading-bot` on `master` (small change; the live services run from this checkout, so do NOT restart any service — the controller does rollout).
- Never read or modify `.env` or credential files. No IBKR connections.
- Python tests: `/home/oferke/trading-bot/venv/bin/python -m unittest <module>` from the repo root. Full suite: `venv/bin/python -m unittest discover -s . -p 'test_*.py'` (632 tests OK before this plan). JS: `node test_dashboard_app.js`.
- Do NOT set `TMPDIR` inside the repo.
- Commit messages end with the trailer `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`. Do not push.
- Only `git add` the files you changed (the checkout has many untracked files that must stay untracked).

## File map

- Modify `rebound_strategy.py`: `Profile`, `PROFILES`, `profile_for`, profile-aware `_find_spike`/`detect_cycles`, `analyze` uses profile and tags `session`; remove `_spread_limit`.
- Modify `test_rebound_strategy.py`: profile and early-session tests.
- Modify `rebound_journal.py`: `session_of_entry_time`, `stats_by_session` in `summary`.
- Modify `test_rebound_journal_manager.py`: per-session stats test.
- Modify `dashboard.html`, `dashboard_app.js`, `test_dashboard_app.js`: per-session line.
- Modify `OPERATIONS.md`: document the profile.

---

### Task 1: Session profiles in the strategy

**Files:**
- Modify: `rebound_strategy.py` (constants ~lines 13-45, `_find_spike` ~68, `detect_cycles` ~90, `_spread_limit` ~162, `analyze` ~167-237)
- Test: `test_rebound_strategy.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_rebound_strategy.py` before `class NewsVerdictTests`:

```python
EARLY = datetime(2026, 10, 6, 5, 0, tzinfo=NY)
# Two fast ~5% spike-and-fade cycles (each spike within 5 bars), then a 50% dip and trigger.
EARLY_CLOSES = [1.00, 1.00, 1.00, 1.025, 1.05, 1.04, 1.03, 1.025,
                1.045, 1.075, 1.06, 1.05, 1.05, 1.05, 1.05]
EARLY_TRIGGER = 1.06


class ProfileTests(unittest.TestCase):
    def at(self, hour, minute):
        return datetime(2026, 10, 6, hour, minute, tzinfo=NY)

    def test_profile_boundaries(self):
        self.assertIsNone(rs.profile_for(self.at(3, 59)))
        self.assertEqual(rs.profile_for(self.at(4, 0)).session, "EARLY_PRE")
        self.assertEqual(rs.profile_for(self.at(6, 59)).session, "EARLY_PRE")
        self.assertEqual(rs.profile_for(self.at(7, 0)).session, "PRE")
        self.assertEqual(rs.profile_for(self.at(9, 29)).session, "PRE")
        self.assertEqual(rs.profile_for(self.at(9, 30)).session, "RTH")
        self.assertEqual(rs.profile_for(self.at(16, 0)).session, "POST")
        self.assertEqual(rs.profile_for(self.at(19, 29)).session, "POST")
        self.assertIsNone(rs.profile_for(self.at(19, 30)))

    def test_profile_parameters(self):
        early = rs.profile_for(self.at(5, 0))
        self.assertEqual((early.spike_min_pct, early.spike_max_bars, early.max_spread_pct),
                         (4.0, 5, 3.0))
        self.assertEqual(rs.profile_for(self.at(8, 0)).max_spread_pct, 1.5)
        rth = rs.profile_for(self.at(10, 0))
        self.assertEqual((rth.spike_min_pct, rth.spike_max_bars, rth.max_spread_pct),
                         (8.0, 15, 0.8))
        self.assertEqual(rs.profile_for(self.at(17, 0)).max_spread_pct, 1.5)

    def test_detect_cycles_uses_profile(self):
        bars = make_bars(EARLY_CLOSES, start=EARLY)
        clean = rs._clean_bars(bars, now_after(bars))
        self.assertEqual(len(rs.detect_cycles(clean, rs.profile_for(EARLY))), 2)
        self.assertEqual(rs.detect_cycles(clean), [])  # default profile = 8%/15 bars


class EarlyPremarketAnalyzeTests(unittest.TestCase):
    def run_at(self, start, q):
        bars = make_bars(EARLY_CLOSES + [EARLY_TRIGGER], start=start)
        return rs.analyze({"symbol": "abcd"}, bars, q, now_after(bars), PAPER)

    def test_fast_small_cycles_qualify_early(self):
        result = self.run_at(EARLY, quote(1.061, 1.059))
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["rebound"]["cycles"], 2)
        self.assertEqual(result["rebound"]["session"], "EARLY_PRE")
        self.assertEqual(result["stop"], round(1.05 * 0.995, 4))

    def test_same_bars_in_regular_hours_are_not_cycles(self):
        result = self.run_at(datetime(2026, 10, 6, 10, 0, tzinfo=NY), quote(1.061, 1.059))
        self.assertEqual(result["skip_reason"], "SKIP_CYCLES")
        self.assertEqual(result["rebound"]["session"], "RTH")

    def test_early_spread_limit_is_three_percent(self):
        wide = quote(1.061, 1.035)  # ~2.5%
        self.assertTrue(self.run_at(EARLY, wide)["qualified"])
        late = self.run_at(datetime(2026, 10, 6, 8, 0, tzinfo=NY), wide)
        self.assertEqual(late["skip_reason"], "SKIP_SPREAD")
        self.assertEqual(late["rebound"]["session"], "PRE")
        too_wide = self.run_at(EARLY, quote(1.061, 1.025))  # ~3.5%
        self.assertEqual(too_wide["skip_reason"], "SKIP_SPREAD")

    def test_existing_regular_hours_signal_is_tagged(self):
        bars = make_bars(TWO_CYCLES + [TRIGGER])
        result = rs.analyze({"symbol": "abcd"}, bars, quote(), now_after(bars), PAPER)
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["rebound"]["session"], "RTH")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m unittest test_rebound_strategy 2>&1 | grep -E '^(FAIL|ERROR|Ran|OK)'`
Expected: ERRORs with `AttributeError: module 'rebound_strategy' has no attribute 'profile_for'` and FAILs for `session`.

- [ ] **Step 3: Implement**

In `rebound_strategy.py`, after the line `RTH_END = dtime(16, 0)` add:

```python
EARLY_PRE_END = dtime(7, 0)
EARLY_SPIKE_MIN_PCT = 4.0
EARLY_SPIKE_MAX_BARS = 5
MAX_SPREAD_PCT_EARLY = 3.0


@dataclass(frozen=True)
class Profile:
    session: str
    spike_min_pct: float
    spike_max_bars: int
    max_spread_pct: float


EARLY_PRE_PROFILE = Profile("EARLY_PRE", EARLY_SPIKE_MIN_PCT, EARLY_SPIKE_MAX_BARS,
                            MAX_SPREAD_PCT_EARLY)
PRE_PROFILE = Profile("PRE", SPIKE_MIN_PCT, SPIKE_MAX_BARS, MAX_SPREAD_PCT_EXT)
RTH_PROFILE = Profile("RTH", SPIKE_MIN_PCT, SPIKE_MAX_BARS, MAX_SPREAD_PCT_RTH)
POST_PROFILE = Profile("POST", SPIKE_MIN_PCT, SPIKE_MAX_BARS, MAX_SPREAD_PCT_EXT)


def profile_for(now):
    """Session profile for ``now`` (New York time), or None outside 04:00-19:30."""
    local = now.astimezone(NEW_YORK).time()
    if not (ENTRY_START <= local < ENTRY_END):
        return None
    if local < EARLY_PRE_END:
        return EARLY_PRE_PROFILE
    if local < RTH_START:
        return PRE_PROFILE
    if local < RTH_END:
        return RTH_PROFILE
    return POST_PROFILE
```

(`Profile` must be defined after `ENTRY_START`, `ENTRY_END`, `RTH_START`, `RTH_END`, `NEW_YORK` and the spike/spread constants; `dataclass` is already imported.)

Replace `_find_spike` and `detect_cycles` with:

```python
def _find_spike(bars, start, profile=RTH_PROFILE):
    for j in range(start, len(bars)):
        window_start = max(start, j - profile.spike_max_bars)
        low_index = min(range(window_start, j + 1), key=lambda k: (bars[k]["low"], k))
        if low_index < j and \
                bars[j]["high"] >= bars[low_index]["low"] * (1 + profile.spike_min_pct / 100):
            return low_index, j
    return None
```

```python
def detect_cycles(bars, profile=RTH_PROFILE):
    """Return completed spike-and-fade cycles, oldest first. Bars must be validated."""
    cycles = []
    start = 0
    while start < len(bars):
        spike = _find_spike(bars, start, profile)
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
```

Note: `_find_spike`/`detect_cycles` are defined above `profile_for` in the file but reference `RTH_PROFILE` as a default argument, so the `Profile` block must be placed **above** `_find_spike` (put it right after the constants block, before `_num`). `Cycle` stays where it is.

Delete the `_spread_limit` function.

In `analyze`, replace from the session check through the end of the function with:

```python
    local = now.astimezone(NEW_YORK)
    profile = profile_for(now)
    if local.weekday() >= 5 or profile is None:
        return _skip(symbol, "SKIP_SESSION")

    def skip(reason, **diagnostics):
        return _skip(symbol, reason, session=profile.session, **diagnostics)

    try:
        bid, ask = float((quote or {}).get("bid")), float((quote or {}).get("ask"))
    except (TypeError, ValueError):
        return skip("SKIP_QUOTE_INVALID")
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask < bid:
        return skip("SKIP_QUOTE_INVALID")
    if not (MIN_PRICE <= ask <= MAX_PRICE):
        return skip("SKIP_PRICE", ask=ask)
    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    if spread_pct > profile.max_spread_pct:
        return skip("SKIP_SPREAD", spread_pct=round(spread_pct, 3))
    try:
        clean = _clean_bars(bars, now)
    except ValueError:
        return skip("SKIP_BARS_INVALID")
    if len(clean) < 7:
        return skip("SKIP_BARS_INSUFFICIENT", bars=len(clean))
    if clean[-1]["timestamp"] < now.timestamp() - 2 * BAR_SECONDS:
        return skip("SKIP_BARS_STALE")
    cycles = detect_cycles(clean, profile)
    if len(cycles) < MIN_CYCLES:
        return skip("SKIP_CYCLES", cycles=len(cycles))
    last_cycle, previous = cycles[-1], cycles[-2]
    after_peak = clean[last_cycle.peak_index + 1:]
    if _find_spike(clean, last_cycle.fade_index, profile) is not None:
        return skip("SKIP_NEW_SPIKE", cycles=len(cycles))
    spike_range = last_cycle.peak - last_cycle.low
    dip_low = min(bar["low"] for bar in after_peak)
    retrace = (last_cycle.peak - dip_low) / spike_range
    diagnostics = {"cycles": len(cycles), "retrace": round(retrace, 4),
                   "peak": last_cycle.peak, "spike_low": last_cycle.low, "dip_low": dip_low}
    if not (ENTRY_RETRACE_MIN <= retrace <= ENTRY_RETRACE_MAX):
        return skip("SKIP_RETRACE", **diagnostics)
    previous_fade_low = min(bar["low"] for bar in
                            clean[previous.peak_index + 1:last_cycle.low_index + 1])
    if dip_low <= previous_fade_low:
        return skip("SKIP_LOWER_LOW", previous_fade_low=previous_fade_low, **diagnostics)
    trigger, prior = clean[-1], clean[-2]
    if trigger["timestamp"] - prior["timestamp"] != BAR_SECONDS:
        return skip("SKIP_BARS_STALE")
    average_volume = sum(bar["volume"] for bar in clean[-6:-1]) / 5
    if not (trigger["close"] > trigger["open"] and trigger["close"] > prior["high"]
            and trigger["volume"] >= average_volume):
        return skip("SKIP_NO_TRIGGER", **diagnostics)
    if last_cycle.peak - ask < MIN_ROOM_TO_PEAK * spike_range:
        return skip("SKIP_CHASE", **diagnostics)
    entry = round(ask, 4)
    stop = round(dip_low * (1 - STOP_BUFFER_PCT), 4)
    risk = entry - stop
    if risk <= 0:
        return skip("SKIP_STOP_INVALID", **diagnostics)
    if risk / entry > MAX_STOP_PCT:
        return skip("SKIP_STOP_TOO_WIDE", stop_pct=round(risk / entry * 100, 3), **diagnostics)
    if math.floor(MAX_POSITION_USD / entry) < 1:
        return skip("SKIP_SIZE", **diagnostics)
    return {"symbol": symbol, "action": "BUY", "entry": entry, "stop": stop,
            "target": round(entry + TARGET_R * risk, 4), "strategy": STRATEGY_NAME,
            "timeframe": TIMEFRAME, "qualified": True, "hard_pass": True,
            "hard_failures": [],
            "rebound": {**diagnostics, "risk_per_share": round(risk, 4),
                        "session": profile.session}}
```

Check nothing else uses `_spread_limit`: `grep -rn _spread_limit --include='*.py' .` must print nothing.

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m unittest test_rebound_strategy test_rebound_bridge 2>&1 | grep -E '^(FAIL|ERROR|Ran|OK)'`
Expected: `OK`. If an existing test asserts an exact `rebound` dict (e.g. `test_collect_dispatches_rebound` passes a mocked dict — unaffected), update only assertions that compare the full diagnostics dict to include `"session"`.

- [ ] **Step 5: Commit**

```bash
git add rebound_strategy.py test_rebound_strategy.py
git commit -m "Rebound: session profiles with early pre-market spike and spread rules

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Per-session stats in the journal summary

**Files:**
- Modify: `rebound_journal.py` (imports; new `session_of_entry_time`; `summary`)
- Test: `test_rebound_journal_manager.py` (`JournalTests`)

- [ ] **Step 1: Write the failing test**

First read the helpers `add_signal`/`set_status`/`make_db` at the top of `test_rebound_journal_manager.py` to see how `entry_time` is stored (`add_signal` writes an `entry_time`; if it has no parameter for it, set it with a direct `UPDATE`). Add to `JournalTests`:

```python
    def set_entry_time(self, sid, value):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE signals SET entry_time=? WHERE signal_id=?", (value, sid))

    def test_session_of_entry_time(self):
        self.assertEqual(journal.session_of_entry_time("20261006 05:12:00 US/Eastern"), "EARLY_PRE")
        self.assertEqual(journal.session_of_entry_time("20261006 08:00:00 US/Eastern"), "PRE")
        self.assertEqual(journal.session_of_entry_time("20261006 10:00:00 US/Eastern"), "RTH")
        self.assertEqual(journal.session_of_entry_time("20261006 17:00:00 US/Eastern"), "POST")
        self.assertEqual(journal.session_of_entry_time("20261006 14:00:00 Asia/Jerusalem"), "PRE")
        for bad in (None, "", "garbage", "20261006 02:00:00 US/Eastern"):
            self.assertEqual(journal.session_of_entry_time(bad), "UNKNOWN")

    def test_summary_stats_by_session(self):
        rows = [("e1", "20261006 05:10:00 US/Eastern", "CLOSED_TP", 40.0),
                ("e2", "20261006 06:30:00 US/Eastern", "CLOSED_SL", -20.0),
                ("r1", "20261006 10:00:00 US/Eastern", "CLOSED_SL", -10.0),
                ("u1", None, "CLOSED_TP", 5.0)]
        for sid, entry_time, status, pnl in rows:
            add_signal(self.db, sid)
            set_status(self.db, sid, status, 2.0, pnl)
            self.set_entry_time(sid, entry_time)
        by_session = journal.summary(self.db)["stats_by_session"]
        self.assertEqual(by_session["EARLY_PRE"],
                         {"trades": 2, "wins": 1, "losses": 1, "total_pnl": 20.0})
        self.assertEqual(by_session["RTH"],
                         {"trades": 1, "wins": 0, "losses": 1, "total_pnl": -10.0})
        self.assertEqual(by_session["UNKNOWN"]["trades"], 1)
        self.assertNotIn("POST", by_session)
```

(`set_status(db, sid, status, exit_price, pnl)` is the existing helper used by `test_summary`. If `add_signal` already sets `entry_time`, the `UPDATE` overrides it.)

- [ ] **Step 2: Run to verify failure**

Run: `venv/bin/python -m unittest test_rebound_journal_manager 2>&1 | grep -E '^(FAIL|ERROR|Ran|OK)'`
Expected: 2 ERRORs (`session_of_entry_time` missing / `KeyError: 'stats_by_session'`).

- [ ] **Step 3: Implement**

In `rebound_journal.py` add imports:

```python
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import rebound_strategy
```

Add after `_iso`:

```python
def session_of_entry_time(value):
    """Session label for an IB ``"YYYYMMDD HH:MM:SS Zone"`` entry time, else UNKNOWN."""
    try:
        date_part, time_part, *zone = str(value or "").split(" ")
        tz = ZoneInfo(zone[0]) if zone else rebound_strategy.NEW_YORK
        moment = datetime.strptime(f"{date_part} {time_part}", "%Y%m%d %H:%M:%S").replace(tzinfo=tz)
    except (ValueError, ZoneInfoNotFoundError):
        return "UNKNOWN"
    profile = rebound_strategy.profile_for(moment)
    return profile.session if profile else "UNKNOWN"
```

In `summary`, inside the existing `try:` block after `stats = {...}`, add:

```python
            by_session = {}
            for row in conn.execute(
                    "SELECT entry_time, COALESCE(net_realized_pnl, realized_pnl) AS pnl "
                    "FROM signals WHERE strategy=? AND status IN ('CLOSED_SL','CLOSED_TP','CLOSED')",
                    (STRATEGY,)):
                bucket = by_session.setdefault(session_of_entry_time(row["entry_time"]),
                                               {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
                pnl = row["pnl"]
                bucket["trades"] += 1
                if pnl is not None and float(pnl) > 0:
                    bucket["wins"] += 1
                elif pnl is not None:
                    bucket["losses"] += 1
                bucket["total_pnl"] = round(bucket["total_pnl"] + float(pnl or 0.0), 2)
```

(A NULL P&L counts as neither win nor loss, matching the existing SQL aggregate.)

In the `except sqlite3.OperationalError:` branch add `by_session = {}`. Add `"stats_by_session": by_session` to the returned dict (after `"stats": stats`).

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m unittest test_rebound_journal_manager test_rebound_route 2>&1 | grep -E '^(FAIL|ERROR|Ran|OK)'`
Expected: `OK`. Also `venv/bin/python -c "import rebound_stop_manager, ai_signal_bridge, signal_bridge"` must succeed (no circular import).

- [ ] **Step 5: Commit**

```bash
git add rebound_journal.py test_rebound_journal_manager.py
git commit -m "Rebound journal: closed-trade stats by session

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Dashboard per-session line

**Files:**
- Modify: `dashboard.html` (Paper trades card, after the `.kpis` div ~line 95)
- Modify: `dashboard_app.js` (`renderRebound` ~line 269, refresh failure ~line 404)
- Test: `test_dashboard_app.js` (`testRenderRebound` ~line 360)

- [ ] **Step 1: Write the failing test**

In `test_dashboard_app.js` `testRenderRebound`, add to the object passed to the first `app.renderRebound` call:

```js
    stats_by_session: {
      EARLY_PRE: { trades: 2, wins: 1, losses: 1, total_pnl: 20 },
      RTH: { trades: 1, wins: 0, losses: 1, total_pnl: -10 },
    },
```

and after the existing `reboundEventRows` assertion add:

```js
  assert.equal(doc.nodes.reboundSessions.textContent,
    "EARLY_PRE: 2 trades, 1W/1L, +20.00 · RTH: 1 trades, 0W/1L, -10.00");
```

and after `app.renderRebound(doc, null);` add:

```js
  assert.equal(doc.nodes.reboundSessions.textContent, "No closed trades by session yet");
```

Check that the fake document (`fakeDoc`) creates nodes on demand for any id (look at its definition near the top of the test file); if it needs ids registered, add `"reboundSessions"` where the other rebound ids are listed.

- [ ] **Step 2: Run to verify failure**

Run: `node test_dashboard_app.js`
Expected: assertion failure on `reboundSessions`.

- [ ] **Step 3: Implement**

`dashboard.html`, directly after the closing `</div>` of the Paper trades `.kpis` block:

```html
        <p id="reboundSessions" class="muted">-</p>
```

(If no `.muted` class exists in the page CSS, use no class.)

`dashboard_app.js`, in `renderRebound` after the `reboundPnl` line:

```js
    const order = ["EARLY_PRE", "PRE", "RTH", "POST", "UNKNOWN"];
    const sessions = object(body.stats_by_session);
    const parts = order.filter(name => sessions[name]).map(name => {
      const s = object(sessions[name]);
      return `${name}: ${plain(s.trades)} trades, ${plain(s.wins)}W/${plain(s.losses)}L, ${signed(s.total_pnl)}`;
    });
    setText(doc, "reboundSessions", parts.length ? parts.join(" · ") : "No closed trades by session yet");
```

In `refreshRebound`'s catch block (next to `markUnavailable(doc, REBOUND_KPI_IDS);`) add:

```js
        setText(doc, "reboundSessions", "UNAVAILABLE");
```

Verify `signed(20)` renders `+20.00` and `signed(-10)` renders `-10.00` (it does for `total_pnl` in the existing test: `+42.50`).

- [ ] **Step 4: Run tests**

Run: `node test_dashboard_app.js`
Expected: `dashboard_app tests passed`.

- [ ] **Step 5: Commit**

```bash
git add dashboard.html dashboard_app.js test_dashboard_app.js
git commit -m "Dashboard: rebound results by session

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Docs and full verification

**Files:**
- Modify: `OPERATIONS.md` (section `## Micro-cap Rebound Paper Trading`, ~line 931)

- [ ] **Step 1: Document**

In `OPERATIONS.md`, after the paragraph that begins `Paper-only rebound strategy for the approved`, insert:

```markdown
Session profiles (New York time, picked from the decision time): `EARLY_PRE` 04:00-07:00 counts a spike at >=4% within 5 bars and allows spreads up to 3.0%; `PRE` 07:00-09:30 and `POST` 16:00-19:30 use >=8% within 15 bars and 1.5%; `RTH` 09:30-16:00 uses >=8% within 15 bars and 0.8%. All other rules are the same in every session. Every skip and signal records `rebound.session`, and `/rebound-journal` returns `stats_by_session` (closed trades grouped by the session of `entry_time`), shown under the Paper trades KPIs. See the [early pre-market design](docs/superpowers/specs/2026-10-06-rebound-early-premarket-design.md).
```

- [ ] **Step 2: Full verification**

Run: `venv/bin/python -m unittest discover -s . -p 'test_*.py' 2>&1 | grep -E '^(FAIL|ERROR|Ran|OK)'` → `OK` (632 + new tests).
Run: `node test_dashboard_app.js` → `dashboard_app tests passed`.

- [ ] **Step 3: Commit**

```bash
git add OPERATIONS.md
git commit -m "Docs: rebound session profiles

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Rollout (controller only, after review)

Push `master`, then `sudo systemctl restart trading-signal-runner trading-bot` (runner uses the strategy; the server serves `/rebound-journal` and the dashboard). Verify `systemctl is-active`, then within ~2 minutes check `sqlite3 trading.db "select detail from rebound_journal order by id desc limit 3"` shows `"session"`.
