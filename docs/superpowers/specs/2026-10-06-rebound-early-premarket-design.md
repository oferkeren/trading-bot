# Rebound early pre-market profile — design

**Status:** approved 2026-10-06
**Extends:** [micro-cap rebound paper design](2026-10-06-microcap-rebound-paper-design.md)

## Why

The user's manual edge is in early pre-market (04:00-07:00 ET), where cycles are small and fast (4-8% within a few minutes). `microcap_rebound_v1` uses one rule set for all sessions: a spike must be ≥8% within 15 one-minute bars and the extended-hours spread cap is 1.5%. On the first pre-market run (2026-10-06) the main skips were `SKIP_CYCLES` (53 symbols, mostly 0 cycles) and `SKIP_SPREAD` (43 symbols, most between 1.55% and 3%).

## What changes

`rebound_strategy` picks a **profile** from the decision time `now` in New York time (the trigger bar is always within 2 minutes of `now`, so this equals the trigger bar's session except at the exact boundary minute):

| Session label | Window (ET, weekdays) | `spike_min_pct` | `spike_max_bars` | `max_spread_pct` |
|---|---|---|---|---|
| `EARLY_PRE` | 04:00 ≤ t < 07:00 | 4.0 | 5 | 3.0 |
| `PRE` | 07:00 ≤ t < 09:30 | 8.0 | 15 | 1.5 |
| `RTH` | 09:30 ≤ t < 16:00 | 8.0 | 15 | 0.8 |
| `POST` | 16:00 ≤ t < 19:30 | 8.0 | 15 | 1.5 |

Everything else is unchanged in all sessions: fade ≥40%, ≥2 cycles, entry retrace 40-70%, trigger bar rules, room-to-peak, stop buffer, 6% max stop, 3R target, strict news gate, sizing `min(floor($1000/entry), floor($55/(entry-stop)))`, one position at a time, exits and the stop manager.

- The spread check and the cycle detector both use the profile for `now`. The cycle detector (`_find_spike`, used both for counting cycles and for `SKIP_NEW_SPIKE`) uses the profile's spike parameters for the whole day's bars. Cycles from earlier sessions are counted with the current profile's thresholds; this is intentional and simple.
- Every analyze result (skip or signal) carries `session` in its `rebound` diagnostics, so the journal records it.

## Reporting

`rebound_journal.summary` adds `stats_by_session`: for each session label, trades / wins / losses / total P&L over all closed rebound trades, using the signal's `entry_time` (`"YYYYMMDD HH:MM:SS US/Eastern"`) to derive the session. Trades whose `entry_time` can't be parsed go under `UNKNOWN`. The dashboard "Paper trades" card shows a small per-session line under the overall stats.

## Error handling

Profile selection is a pure function of time; outside 04:00-19:30 ET the existing `SKIP_SESSION` applies before any profile is used. No new failure modes.

## Testing

- `profile_for` boundaries: 03:59 → none/SKIP_SESSION path, 04:00 and 06:59 → `EARLY_PRE`, 07:00 → `PRE`, 09:30 → `RTH`, 16:00 → `POST`.
- A synthetic day with two fast ~5% spike-and-fade cycles (≤5 bars each) then a valid dip and trigger qualifies at 05:00 ET and is skipped with `SKIP_CYCLES` when the same bars are shifted to 10:00 ET.
- Spread 2.5% passes at 05:00 and is `SKIP_SPREAD` at 08:00; spread 3.5% is `SKIP_SPREAD` at 05:00.
- Results include `rebound.session`.
- `summary` returns per-session stats from `entry_time`, including `UNKNOWN`.
- Existing rebound tests still pass unchanged.

## Rollout

Restart `trading-signal-runner` only. No `.env` change.
