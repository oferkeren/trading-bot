# Rebound "dollar scalp" exits and same-stock re-entry — design

Date: 2026-10-06
Strategy: `microcap_rebound_v1` (paper only), changed in place.

## Goal

Match the user's manual style: take a fixed **+$1.00/share** profit with a fixed
**-$0.50/share** stop, then wait for the stock's **next** spike-and-fade cycle and
buy the dip again. Stop trading a stock for the day after **2 losses** on it.

## Rules

1. **Entry pattern and news gate** — unchanged (≥2 cycles, 40–70% retrace, higher low,
   trigger bar, chase guard, session profiles, strong recent news).
2. **Swing filter** — the cycle being traded (last completed cycle) must swing
   `peak - low >= MIN_SWING_USD` (1.00). Otherwise skip `SKIP_SWING`
   (diagnostic `swing`).
3. **Fixed exits** — `stop = entry - STOP_USD` (0.50), `target = entry + TARGET_USD`
   (1.00), both rounded to 4 decimals. If `stop <= 0` skip `SKIP_STOP_INVALID`.
   The old dip-low stop, `STOP_BUFFER_PCT`, `MAX_STOP_PCT`/`SKIP_STOP_TOO_WIDE`
   and `TARGET_R` are removed. `risk_per_share` diagnostic = 0.50.
4. **Trailing / time exits** — unchanged (`rebound_exits`): breakeven lock after +1R
   (+$0.50 → stop to entry + $0.10), trail 1R ($0.50) below the high, 20-min max hold,
   end-of-session close. The IBKR target order sits at +$1.00.
5. **Sizing** — unchanged formula `min(floor(1000/entry), floor(55/(entry-stop)))`,
   i.e. at most 110 shares.
6. **Same-stock re-entry**
   - New `rebound_journal.symbol_day_state(db_file, symbol, now)` returns
     `{"losses": int, "last_exit_ts": float | None}` from today's (New York date)
     `EXIT` journal rows for that symbol. A loss is finite numeric `detail.pnl < 0`;
     additionally, `reason == "FLAT_AT_BROKER"` with missing, non-numeric, or non-finite
     `detail.pnl` counts as a loss (fail closed because the flatten P/L is not known yet).
     Other exits with missing, non-numeric, or non-finite P/L do not count as losses.
   - `analyze(..., symbol_state=None)`:
     - `losses >= MAX_SYMBOL_LOSSES` (2) → `SKIP_SYMBOL_LOSSES` (checked right after
       the session check, before quotes/bars).
     - Compute the current entry dip-low from the bars after the latest cycle peak
       (`clean[last_cycle.peak_index + 1:]`), using the lowest `low` and the earliest
       bar on ties. If `last_exit_ts` is set and that dip-low bar timestamp is
       `<= last_exit_ts` → `SKIP_SAME_CYCLE`. This requires price to come down after
       the last exit while still allowing a prior winning entry dip to become the next
       cycle's spike low.
   - `signal_bridge` REBOUND dispatch passes
     `symbol_state=rebound_journal.symbol_day_state(DEFAULT_DB, symbol, now)`; if that
     lookup raises, the symbol is skipped with `SKIP_SYMBOL_STATE` (fail closed).
   - The bridge's per-symbol cooldown for `microcap_rebound_v1` drops from 900 s to the
     75 s already used by `scalp_*` strategies, so re-entry is gated by the cycle rule,
     not a 15-min timer. `is_busy` (one position at a time) is unchanged.

## Consequences (accepted by user)

- Cheap stocks / small early-premarket cycles (4–8%) rarely swing $1, so far fewer
  setups will qualify, mostly on higher-priced or very volatile names.
- A +$1 target can sit above the cycle peak; the trailing stop and max hold handle it.

## Testing

Unit tests: swing filter pass/skip; fixed stop/target values; stop ≤ 0 skip;
`SKIP_SYMBOL_LOSSES`; `SKIP_SAME_CYCLE` vs new-cycle qualify; `symbol_day_state`
(today only, per symbol, loss counting, latest exit); bridge passes symbol state and
fails closed; rebound cooldown is 75 s. Existing tests that assumed dip-low stops/3R
targets are updated. Docs: `OPERATIONS.md` rebound section.

## Amendment 2026-10-07: one cycle is enough

User request (no trades with 2 cycles + $1 swing): `MIN_CYCLES = 1`. With a single
cycle the higher-low check (`SKIP_LOWER_LOW`) is skipped because there is no previous
fade; with 2+ cycles it still applies. All other rules are unchanged.

## Amendment 2026-10-07: swing-scaled exits

User request (still no trades; most movers swing < $1): `MIN_SWING_USD = 0.20`;
reward = `min($1.00, swing)`; target = `entry + reward`; stop = `entry - reward / 2`
(`rebound_strategy.exit_levels`). `risk_per_share` = `entry - stop`. Trailing stays
R-based (`rebound_exits`), so it scales with the smaller stop. Sizing is unchanged:
`min(floor(1000/entry), floor(55/(entry-stop)))`.

Review follow-ups: skip `SKIP_STOP_IN_SPREAD` when `entry - stop < 1.5 * (ask - bid)`
(early-premarket spreads up to 3% could put the half-swing stop inside the spread);
the stop manager ignores bars that started before the fill, so pre-fill highs can't
trigger an immediate trail exit with the smaller R.
