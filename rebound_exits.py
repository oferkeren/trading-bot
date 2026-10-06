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


def next_action(*, entry, initial_stop, current_stop, high_since_entry, opened_at, now,
                last_price=None):
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
    has_last_price = (not isinstance(last_price, bool)
                      and isinstance(last_price, (int, float))
                      and math.isfinite(last_price))
    if has_last_price and last_price <= current_stop:
        return {"action": "CLOSE", "reason": "STOP_BREACHED"}
    desired = current_stop
    if high_since_entry >= entry + BREAKEVEN_TRIGGER_R * risk:
        desired = max(desired, entry + BREAKEVEN_LOCK_R * risk, high_since_entry - TRAIL_R * risk)
    tick = tick_for(entry)
    desired = _floor_to_tick(desired, tick)
    if has_last_price and desired >= last_price:
        return {"action": "CLOSE", "reason": "TRAIL_HIT"}
    if desired >= current_stop + tick - 1e-9:
        return {"action": "MOVE_STOP", "stop": desired}
    return {"action": "HOLD"}
