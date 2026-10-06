# Micro-cap Rebound Strategy — Paper Trading Design

Status: approved (2026-10-06)

## Goal

Trade the user's micro-cap scalping idea on the IBKR **paper** account. The idea: buy the dip on a micro-cap that has fresh, strongly positive news and has already done at least two spike-and-fade cycles today, then exit before it fades again. Every signal, skip and trade is journaled. After 30–50 paper trades we judge whether the rules have an edge.

The paper results are an experiment, not calibration. A separate later spec covers an unbiased historical dataset and backtest (track A). Live trading is out of scope and needs separate approval.

## Decisions

| Topic | Decision |
|---|---|
| Cycle | Intraday spike-and-fade on 1-minute bars |
| Direction | Long only. Buy the dip after a fade |
| News | The existing AI gate scores headlines from the last 48 h as strongly positive |
| Exit | Initial stop, then a ratcheting stop (breakeven lock, then trail) plus a max hold time |
| Size | At most $1000 per trade, at most 1 open position |
| Daily stop | None (user choice; paper only). The kill switch stays available |
| Session | Extended hours: entries 04:00–19:30 ET, everything flat by 19:55 ET |
| Integration | New strategy module in the existing runner. The old paper strategies are disabled |

## Rules (config constants; defaults below)

### Universe and news
- The existing scanner's candidates, price $0.50–$20.00.
- The news gate passes only when the AI gate scores the company's headlines from the last 48 h as strongly positive. Offering, dilution, lawsuit, bankruptcy, delisting, reverse split, and trading halt events are treated as negative and skipped. If the AI or news is unavailable or ambiguous, the candidate is skipped (`SKIP_NEWS_GATE`).

### Cycle detection (today's 1-minute bars, including pre-market)
- **Spike:** a rise of at least `SPIKE_MIN_PCT = 8.0` from a local low to a local high within at most `SPIKE_MAX_BARS = 15` bars.
- **Fade:** after the spike high, a pullback of at least `FADE_MIN_RETRACE = 0.40` of the spike's range.
- **Cycle:** a spike followed by its fade. Cycles don't overlap: the next spike starts at or after the previous fade low.
- At least `MIN_CYCLES = 2` completed cycles are required (`SKIP_CYCLES`).

### Entry (long the dip)
- The current pullback from the latest spike high has retraced between `ENTRY_RETRACE_MIN = 0.40` and `ENTRY_RETRACE_MAX = 0.70` of that spike (`SKIP_RETRACE`).
- The current dip low must be above the previous completed cycle's fade low (a higher low; otherwise `SKIP_LOWER_LOW`).
- **Trigger:** the last *closed* 1-minute bar is green, closes above the previous bar's high, and has volume at least the average of the prior 5 bars (`SKIP_NO_TRIGGER`).
- **Spread limit:** `(ask − bid) / mid` must be ≤ 0.8% in regular hours and ≤ 1.5% in extended hours (`SKIP_SPREAD`).
- **Order:** a limit buy at the ask, sized at `min(floor(MAX_POSITION_USD / ask), floor(MAX_RISK_USD / (entry - stop)))` shares with `MAX_POSITION_USD = 1000` and `MAX_RISK_USD = 55` (`SKIP_SIZE` if that comes to 0 or the stop is not below entry).
- **Time:** no new entries outside 04:00–19:30 ET (`SKIP_SESSION`).
- **Position limit:** no entry while any `microcap_rebound_v1` position or open entry order exists (`SKIP_BUSY`).

### Exits
- **Initial stop:** `dip_low × (1 − 0.005)`. If `(entry − stop) / entry > MAX_STOP_PCT = 0.06`, the trade is skipped (`SKIP_STOP_TOO_WIDE`). Define `R = entry − stop`.
- **Bracket target:** `entry + 3R`, a cap in case a spike runs.
- **Ratchet** (the stop never moves down):
  - Once `high_since_entry ≥ entry + 1R`, the stop is at least `entry + 0.2R` (breakeven lock).
  - After the breakeven lock, the trailing stop is `max(locked_stop, high_since_entry − 1R)`.
  - The stop only moves when the new level is at least one tick higher than the current one.
- **Max hold:** `MAX_HOLD_MINUTES = 20`, then close with a marketable limit order.
- **End of session:** close any open position at 19:55 ET. Nothing is held overnight.

## Components

1. **`rebound_strategy.py`** (new, pure functions, no I/O apart from the existing `request_history` pattern):
   - `detect_cycles(bars) -> list[Cycle]`
   - `analyze(candidate, bars, quote, news_verdict, now) -> Signal | Skip`
   - Signal fields: `symbol`, `action=BUY`, `entry`, `stop`, `target`, `strategy="microcap_rebound_v1"`, `timeframe="1m"`, plus diagnostics (cycles, retrace, R).
2. **`signal_bridge.py`:** a new `STRATEGY_MODE=REBOUND`. In that mode only the rebound strategy runs. Signals use the existing `build_payload`, webhook and worker bracket path. The runner's service drop-in sets `STRATEGY_MODE=REBOUND`, which disables the old scalp and momentum strategies.
3. **News gate:** reuse `ai_gate` / `ai_market_intelligence`, requiring a strongly positive verdict for headlines in the last 48 h. It fails closed.
4. **`rebound_exits.py`** (new, pure):
   - `next_action(entry, initial_stop, current_stop, high_since_entry, opened_at, now, tick)` returns `HOLD`, `MOVE_STOP(price)` or `CLOSE(reason)`.
5. **Worker hook (`worker_core.py`):** IBKR only allows the client that placed an order to modify it, so the worker runs the stop manager. Every few seconds, for each open `microcap_rebound_v1` position:
   - track `high_since_entry`;
   - call `next_action`;
   - modify only the stop child order (same order id, new `auxPrice`/`lmtPrice`) and wait for IBKR's acknowledgement.
   - If the modify is rejected or not acknowledged within a timeout, close the position with a marketable limit order.
6. **Journal:**
   - A new `rebound_journal` table in `trading.db` with columns ts, symbol, event (`SIGNAL`, `SKIP`, `ENTRY`, `STOP_MOVE`, `EXIT`), reason, prices, R, and pnl.
   - An authenticated `GET /rebound-journal` endpoint.
   - A "Paper trades" panel on the dashboard Research tab: trade count, win rate, average R, and the recent trades and skips. All rendering uses `textContent`.

## Safety and error handling

- **Paper only:** signals are emitted only when trading mode is PAPER, the port is 7497, and the account id starts with `DU`. Otherwise there is no signal (`SKIP_NOT_PAPER`).
- **Fail closed:** any missing or invalid bars, quote, news or AI verdict causes a skip with a reason; no exceptions reach the runner loop.
- **Kill switch:** it blocks new entries only. Stop raises and protective closes (max hold, end of session, failed stop modify) only reduce risk, so they still go through.
- **Unprotected position:** a rebound position with no live stop order is closed immediately.
- Existing protection and recovery logic in the worker stays unchanged for the other order paths.

## Testing

- **`test_rebound_strategy.py`:** synthetic bar series covering 0, 1, 2 and 3 cycles; a lower low; retrace out of range; no trigger; a spread that is too wide; a stop that is too wide; the session window; size 0; and fail-closed handling of a missing news verdict.
- **`test_rebound_exits.py`:** the ratchet never moves down, the breakeven lock fires at +1R, trailing works, the one-tick minimum step holds, max hold closes, and the 19:55 close works.
- **Worker stop-manager tests** with a fake IB client: modify acknowledged; modify rejected leads to a close; a missing stop leads to a close; kill switch on still allows stop raises and closes.
- **`signal_bridge` test:** `REBOUND` mode runs only the rebound strategy.
- **Journal endpoint route test:** 401 without authentication and 200 with it. Plus a dashboard panel Node test.

## Rollout

1. Merge with `STRATEGY_MODE=REBOUND` and `BRIDGE_DRY_RUN=true` for one session. Signals and skips are journaled, and no orders are placed.
2. The user reviews the journal on the dashboard. With the user's OK, set `BRIDGE_DRY_RUN=false` on the paper account.
3. After 30–50 trades, review the win rate, average R and skip distribution, and tune the constants.
