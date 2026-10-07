# Rebound Dollar-Scalp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fixed +$1.00 / -$0.50 per-share exits, a ≥$1 cycle-swing filter, and same-stock re-entry on the next cycle with a 2-losses-per-day stop for `microcap_rebound_v1`.

**Architecture:** `rebound_strategy.analyze` gains the swing filter, fixed exits and an optional `symbol_state` argument; `rebound_journal.symbol_day_state` derives that state from today's `EXIT` journal rows; `signal_bridge`'s REBOUND dispatch passes it in and uses a 75 s cooldown for rebound signals.

**Tech Stack:** Python 3.10, unittest, sqlite3. Run tests with `venv/bin/python -m unittest <module>` from the repo root (full suite: `venv/bin/python -m unittest discover -s . -p 'test_*.py'`). Spec: `docs/superpowers/specs/2026-10-06-rebound-dollar-scalp-design.md`.

---

### Task 1: Fixed exits and swing filter in `rebound_strategy`

**Files:**
- Modify: `rebound_strategy.py` (constants block lines ~12-36; `analyze` tail lines ~230-272)
- Test: `test_rebound_strategy.py`

Existing fixtures are ~$1 stocks with ~$0.12–0.20 swings. Pattern tests are price-scale independent, so the existing `AnalyzeTests` and `EarlyPremarketAnalyzeTests` classes get a `setUp` that patches `rs.MIN_SWING_USD` to `0.0`; the swing filter itself is tested separately with a $10-scale fixture.

- [ ] **Step 1: Write the failing tests**

Add near the top of `test_rebound_strategy.py`:

```python
from unittest.mock import patch
```

Add to `AnalyzeTests` and to `EarlyPremarketAnalyzeTests`:

```python
    def setUp(self):
        patcher = patch.object(rs, "MIN_SWING_USD", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)
```

Replace the stop/target assertions in `AnalyzeTests.test_qualified_signal` with:

```python
        self.assertEqual(result["entry"], 1.151)
        self.assertEqual(result["stop"], round(1.151 - 0.50, 4))
        self.assertEqual(result["target"], round(1.151 + 1.00, 4))
        self.assertEqual(result["rebound"]["risk_per_share"], 0.5)
```

Replace `test_stop_too_wide` with:

```python
    def test_stop_must_stay_above_zero(self):
        with patch.object(rs, "STOP_USD", 2.0):
            self.assertEqual(self.skip(self.run_case()), "SKIP_STOP_INVALID")
```

In `EarlyPremarketAnalyzeTests.test_fast_small_cycles_qualify_early` replace the stop assertion with:

```python
        self.assertEqual(result["stop"], round(1.061 - 0.50, 4))
```

Add a new class at the end of the file:

```python
DOLLAR = [c * 10 for c in TWO_CYCLES]  # same pattern on a ~$10 stock: swings $1.2 and $1.4


class DollarScalpTests(unittest.TestCase):
    def run_case(self, closes, trigger, q, symbol_state=None):
        bars = make_bars(closes + [trigger])
        return rs.analyze({"symbol": "abcd"}, bars, q, now_after(bars), PAPER,
                          symbol_state=symbol_state)

    def test_dollar_swing_qualifies_with_fixed_exits(self):
        result = self.run_case(DOLLAR, 11.5, quote(11.51, 11.50))
        self.assertTrue(result["qualified"], result)
        self.assertEqual((result["entry"], result["stop"], result["target"]),
                         (11.51, 11.01, 12.51))
        self.assertAlmostEqual(result["rebound"]["swing"], 1.4)

    def test_small_swing_is_skipped(self):
        bars = make_bars(TWO_CYCLES + [TRIGGER])
        result = rs.analyze({"symbol": "abcd"}, bars, quote(), now_after(bars), PAPER)
        self.assertEqual(result["skip_reason"], "SKIP_SWING")
        self.assertAlmostEqual(result["rebound"]["swing"], 0.14)
```

(Verified: `TWO_CYCLES` cycle swings are $0.12 and $0.14; `DOLLAR` swings are $1.2 and $1.4, and `DOLLAR` + trigger 11.5 with quote 11.51/11.50 qualifies today.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m unittest test_rebound_strategy 2>&1 | tail -5`
Expected: FAIL/ERROR (no `MIN_SWING_USD`, `STOP_USD`, `symbol_state` kwarg; old stop values).

- [ ] **Step 3: Implement**

In `rebound_strategy.py` constants, remove `STOP_BUFFER_PCT`, `MAX_STOP_PCT`, `TARGET_R` and add:

```python
STOP_USD = 0.50
TARGET_USD = 1.00
MIN_SWING_USD = 1.00
MAX_SYMBOL_LOSSES = 2
```

Change the signature to `def analyze(candidate, bars, quote, now, env=None, symbol_state=None):` (the `symbol_state` logic is Task 2; accept and ignore it here).

After `spike_range = last_cycle.peak - last_cycle.low` add `"swing": round(spike_range, 4)` to `diagnostics`, and immediately after the `diagnostics = {...}` line insert:

```python
    if spike_range < MIN_SWING_USD:
        return skip("SKIP_SWING", **diagnostics)
```

Replace the exit block (from `entry = round(ask, 4)` to the end of `analyze`) with:

```python
    entry = round(ask, 4)
    stop = round(entry - STOP_USD, 4)
    if stop <= 0:
        return skip("SKIP_STOP_INVALID", **diagnostics)
    if math.floor(MAX_POSITION_USD / entry) < 1:
        return skip("SKIP_SIZE", **diagnostics)
    return {"symbol": symbol, "action": "BUY", "entry": entry, "stop": stop,
            "target": round(entry + TARGET_USD, 4), "strategy": STRATEGY_NAME,
            "timeframe": TIMEFRAME, "qualified": True, "hard_pass": True,
            "hard_failures": [],
            "rebound": {**diagnostics, "risk_per_share": STOP_USD,
                        "session": profile.session}}
```

`grep -n "STOP_BUFFER_PCT\|MAX_STOP_PCT\|TARGET_R\|SKIP_STOP_TOO_WIDE" *.py` must return nothing outside backups (`*.bak*`, `*_candidate.py`); fix any remaining references (e.g. dashboard/summary code) by removing them.

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m unittest test_rebound_strategy test_rebound_bridge test_rebound_route 2>&1 | tail -3`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add rebound_strategy.py test_rebound_strategy.py
git commit -m "Rebound: fixed +\$1/-\$0.50 exits and \$1 swing filter"
```

### Task 2: Symbol day state and re-entry rules

**Files:**
- Modify: `rebound_journal.py` (add `symbol_day_state` after `is_busy`)
- Modify: `rebound_strategy.py` (`analyze`)
- Test: `test_rebound_journal_manager.py`, `test_rebound_strategy.py`

- [ ] **Step 1: Write failing journal tests** (add a class to `test_rebound_journal_manager.py`; reuse its existing temp-DB helper pattern — look at how other tests in that file create a temp db and call `rebound_journal.record`)

```python
class SymbolDayStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "t.db")

    def exit(self, symbol, pnl, when):
        rebound_journal.record(self.db, "EXIT", symbol=symbol, signal_id=f"{symbol}-{when}",
                               reason="MAX_HOLD", detail={"pnl": pnl}, now=when)

    def test_counts_losses_and_latest_exit_today_only(self):
        ny = ZoneInfo("America/New_York")
        now = datetime(2026, 10, 6, 12, 0, tzinfo=ny)
        self.exit("ABC", -10.0, now - timedelta(days=1))
        self.exit("ABC", -5.0, now - timedelta(hours=2))
        self.exit("ABC", 20.0, now - timedelta(hours=1))
        self.exit("XYZ", -1.0, now - timedelta(minutes=5))
        state = rebound_journal.symbol_day_state(self.db, "abc", now)
        self.assertEqual(state["losses"], 1)
        self.assertEqual(state["last_exit_ts"], (now - timedelta(hours=1)).timestamp())

    def test_empty_or_missing_table(self):
        now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(rebound_journal.symbol_day_state(self.db, "ABC", now),
                         {"losses": 0, "last_exit_ts": None})

    def test_null_or_bad_pnl_is_not_a_loss(self):
        now = datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
        self.exit("ABC", None, now - timedelta(minutes=10))
        self.assertEqual(rebound_journal.symbol_day_state(self.db, "ABC", now)["losses"], 0)
```

Add any missing imports (`os`, `tempfile`, `datetime`, `timedelta`, `timezone`, `ZoneInfo`).

- [ ] **Step 2: Run and confirm failure**

Run: `venv/bin/python -m unittest test_rebound_journal_manager 2>&1 | tail -3` → AttributeError `symbol_day_state`.

- [ ] **Step 3: Implement in `rebound_journal.py`**

Check `record()`'s storage format first: `ts` is an ISO-8601 UTC string from `_iso(now)` and `detail` is JSON text. Then add:

```python
def symbol_day_state(db_file, symbol, now):
    """Today's (New York date) losses and latest exit time for ``symbol`` from EXIT rows."""
    symbol = str(symbol or "").strip().upper()
    today = now.astimezone(NEW_YORK).date()
    losses, last_exit = 0, None
    conn = connect(db_file)
    try:
        rows = conn.execute(
            "SELECT ts, detail FROM rebound_journal WHERE event='EXIT' AND symbol=?",
            (symbol,)).fetchall()
    finally:
        conn.close()
    for ts, detail in rows:
        stamp = datetime.fromisoformat(ts)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp.astimezone(NEW_YORK).date() != today:
            continue
        last_exit = max(last_exit or stamp.timestamp(), stamp.timestamp())
        try:
            pnl = json.loads(detail or "{}").get("pnl")
        except (TypeError, ValueError, AttributeError):
            pnl = None
        if isinstance(pnl, (int, float)) and not isinstance(pnl, bool) and pnl < 0:
            losses += 1
    return {"losses": losses, "last_exit_ts": last_exit}
```

Use the module's existing imports/`NEW_YORK` constant if present (add `from zoneinfo import ZoneInfo`, `NEW_YORK = ZoneInfo("America/New_York")`, `json`, `datetime`, `timezone` only if missing). `connect()` already creates the table, so a fresh DB returns zero state. Note `_iso` may truncate to seconds — compare against the stored precision in the test (`(now - timedelta(hours=1)).timestamp()` is a whole second, so it matches).

- [ ] **Step 4: Write failing strategy tests** (append to `DollarScalpTests`)

```python
    def test_two_losses_block_symbol(self):
        result = self.run_case(DOLLAR, 11.5, quote(11.51, 11.50),
                               symbol_state={"losses": 2, "last_exit_ts": None})
        self.assertEqual(result["skip_reason"], "SKIP_SYMBOL_LOSSES")
        result = self.run_case(DOLLAR, 11.5, quote(11.51, 11.50),
                               symbol_state={"losses": 1, "last_exit_ts": None})
        self.assertTrue(result["qualified"], result)

    def test_same_cycle_after_exit_is_skipped(self):
        bars = make_bars(DOLLAR + [11.5])
        clean = rs._clean_bars(bars, now_after(bars))
        last_low_ts = clean[rs.detect_cycles(clean)[-1].low_index]["timestamp"]
        result = self.run_case(DOLLAR, 11.5, quote(11.51, 11.50),
                               symbol_state={"losses": 0, "last_exit_ts": last_low_ts})
        self.assertEqual(result["skip_reason"], "SKIP_SAME_CYCLE")
        result = self.run_case(DOLLAR, 11.5, quote(11.51, 11.50),
                               symbol_state={"losses": 0, "last_exit_ts": last_low_ts - 1})
        self.assertTrue(result["qualified"], result)
```

- [ ] **Step 5: Implement in `analyze`**

Right after the `SKIP_SESSION` return (before `def skip`):

```python
    state = symbol_state or {}
    if (state.get("losses") or 0) >= MAX_SYMBOL_LOSSES:
        return _skip(symbol, "SKIP_SYMBOL_LOSSES", session=profile.session,
                     losses=state.get("losses"))
```

Right after the `SKIP_SWING` check:

```python
    last_exit_ts = state.get("last_exit_ts")
    if last_exit_ts is not None and clean[last_cycle.low_index]["timestamp"] <= last_exit_ts:
        return skip("SKIP_SAME_CYCLE", last_exit_ts=last_exit_ts, **diagnostics)
```

- [ ] **Step 6: Run tests**

Run: `venv/bin/python -m unittest test_rebound_strategy test_rebound_journal_manager 2>&1 | tail -3` → `OK`.

- [ ] **Step 7: Commit**

```bash
git add rebound_journal.py rebound_strategy.py test_rebound_strategy.py test_rebound_journal_manager.py
git commit -m "Rebound: per-symbol day state, 2-loss block and next-cycle re-entry"
```

### Task 3: Bridge wiring and rebound cooldown

**Files:**
- Modify: `signal_bridge.py` (REBOUND dispatch ~line 2040-2065; cooldown ~line 1603-1625)
- Test: `test_rebound_route.py` (or `test_rebound_bridge.py`, whichever already tests the REBOUND dispatch / cooldown — check with `grep -n "REBOUND\|cooldown" test_rebound_*.py`)

- [ ] **Step 1: Write failing tests**

1. Dispatch passes symbol state: patch `rebound_journal.symbol_day_state` to return `{"losses": 2, "last_exit_ts": None}` and assert the `analyze` call received `symbol_state=` that dict (patch `rebound_strategy.analyze` with a `MagicMock(return_value={"qualified": False, "symbol": "X", "skip_reason": "SKIP_SYMBOL_LOSSES", "rebound": {}})` and inspect `call_args.kwargs["symbol_state"]`). Follow the existing REBOUND-dispatch test's setup for faking `app`, `request_history` and quotes.
2. Fail closed: `symbol_day_state` raising `sqlite3.OperationalError` → `analyze` is NOT called and the result for that candidate is a skip with `skip_reason == "SKIP_SYMBOL_STATE"` (also journaled via `record_skip` like other skips).
3. Cooldown: `symbol_action_in_cooldown` is called with `75` for a candidate whose `strategy` is `microcap_rebound_v1` (same way `scalp_*` is tested, if a test exists; otherwise patch `symbol_action_in_cooldown` and assert its third positional arg).

- [ ] **Step 2: Run, confirm failure.**

- [ ] **Step 3: Implement**

In the REBOUND dispatch, replace the `rebound_result = rebound_strategy.analyze(...)` call with:

```python
                now_utc = datetime.now(timezone.utc)
                try:
                    symbol_state = rebound_journal.symbol_day_state(
                        rebound_journal.DEFAULT_DB, candidate.get("symbol"), now_utc)
                except Exception as exc:
                    symbol_state = None
                    rebound_result = {
                        "symbol": str(candidate.get("symbol") or "").strip().upper(),
                        "strategy": rebound_strategy.STRATEGY_NAME,
                        "timeframe": rebound_strategy.TIMEFRAME,
                        "qualified": False, "skip_reason": "SKIP_SYMBOL_STATE",
                        "rebound": {"error": f"{type(exc).__name__}: {exc}"}}
                if symbol_state is not None:
                    rebound_result = rebound_strategy.analyze(
                        candidate, rebound_bars, quote, now_utc, symbol_state=symbol_state)
```

(Keep `request_history` before it and the existing `if not rebound_result.get("qualified"):` journaling after it unchanged.)

In the cooldown call, change the condition so rebound also gets 75 s:

```python
        (
            75
            if str(candidate.get("strategy", "")).startswith(("scalp_", "microcap_rebound_"))
            else
            COOLDOWN_SECONDS
        ),
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m unittest test_rebound_route test_rebound_bridge test_rebound_strategy 2>&1 | tail -3` → `OK`.

- [ ] **Step 5: Commit**

```bash
git add signal_bridge.py test_rebound_route.py test_rebound_bridge.py
git commit -m "Bridge: pass rebound symbol day state, fail closed, 75s rebound cooldown"
```

### Task 4: Docs and full verification

**Files:**
- Modify: `OPERATIONS.md` (rebound section, ~line 931+)

- [ ] **Step 1:** In the rebound section, replace the description of dip-low stop / 3R target / 6% max stop with: fixed stop entry − $0.50, target entry + $1.00, cycle swing ≥ $1 (`SKIP_SWING`), re-entry only on a cycle whose low is after the last exit (`SKIP_SAME_CYCLE`), 2 losses per symbol per New York day blocks it (`SKIP_SYMBOL_LOSSES`; `SKIP_SYMBOL_STATE` if the journal lookup fails), rebound cooldown 75 s. Trailing/max-hold text stays.
- [ ] **Step 2:** Run the full suite: `venv/bin/python -m unittest discover -s . -p 'test_*.py' 2>&1 | tail -3` → `OK`; and `node test_dashboard_app.js` → passes.
- [ ] **Step 3: Commit**

```bash
git add OPERATIONS.md
git commit -m "Docs: rebound dollar-scalp exits and re-entry rules"
```
