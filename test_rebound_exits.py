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

    def test_trail_hit_closes_when_new_stop_is_above_market(self):
        try:
            result = next_action(entry=2.00, initial_stop=1.90, current_stop=1.90,
                                 high_since_entry=2.25, opened_at=OPEN,
                                 now=OPEN + timedelta(minutes=1), last_price=2.12)
        except TypeError as exc:
            self.fail(f"next_action should accept last_price: {exc}")
        self.assertEqual(result, {"action": "CLOSE", "reason": "TRAIL_HIT"})

    def test_stop_breached_closes_even_without_stop_move(self):
        try:
            result = next_action(entry=2.00, initial_stop=1.90, current_stop=2.15,
                                 high_since_entry=2.12, opened_at=OPEN,
                                 now=OPEN + timedelta(minutes=1), last_price=2.14)
        except TypeError as exc:
            self.fail(f"next_action should accept last_price: {exc}")
        self.assertEqual(result, {"action": "CLOSE", "reason": "STOP_BREACHED"})

    def test_missing_last_price_keeps_move_stop_behavior(self):
        try:
            result = next_action(entry=2.00, initial_stop=1.90, current_stop=2.02,
                                 high_since_entry=2.25, opened_at=OPEN,
                                 now=OPEN + timedelta(minutes=1), last_price=None)
        except TypeError as exc:
            self.fail(f"next_action should accept last_price: {exc}")
        self.assertEqual(result, {"action": "MOVE_STOP", "stop": 2.15})

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
