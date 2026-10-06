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

    def test_stale_last_closed_bar_is_rejected(self):
        bars = make_bars(TWO_CYCLES + [TRIGGER])
        stale_now = datetime.fromtimestamp(bars[-1]["timestamp"] + 30 * 60, timezone.utc)
        self.assertEqual(self.skip(self.run_case(bars=bars, now=stale_now)), "SKIP_BARS_STALE")

    def test_gap_between_prior_and_trigger_is_rejected(self):
        bars = make_bars(TWO_CYCLES + [TRIGGER])
        bars[-1]["timestamp"] = bars[-2]["timestamp"] + 3 * rs.BAR_SECONDS
        self.assertEqual(self.skip(self.run_case(bars=bars, now=now_after(bars))),
                         "SKIP_BARS_STALE")


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
                 (self.ai(event_type="DELISTING"), "NEWS_NEGATIVE_EVENT"),
                 (self.ai(event_type="REVERSE_SPLIT"), "NEWS_NEGATIVE_EVENT"),
                 (self.ai(event_type="TRADING_HALT"), "NEWS_NEGATIVE_EVENT"),
                 (self.ai(ai=None), "NEWS_GATE_UNAVAILABLE")]
        for value, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(rs.news_verdict(value), (False, reason))


if __name__ == "__main__":
    unittest.main()
