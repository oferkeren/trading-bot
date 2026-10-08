import unittest
from unittest.mock import patch
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
    def setUp(self):
        patcher = patch.object(rs, "MIN_SWING_USD", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_case(self, closes=None, q=None, env=PAPER, bars=None, now=None):
        bars = bars if bars is not None else make_bars((closes or TWO_CYCLES) + [TRIGGER])
        return rs.analyze({"symbol": "abcd"}, bars, q or quote(), now or now_after(bars), env)

    def test_qualified_signal(self):
        result = self.run_case()
        self.assertTrue(result["qualified"], result)
        self.assertEqual((result["symbol"], result["action"], result["strategy"]),
                         ("ABCD", "BUY", "microcap_rebound_v1"))
        self.assertEqual(result["entry"], 1.151)
        # swing 0.14 < $1 -> target = +swing, stop = half of that below entry
        self.assertEqual(result["target"], round(1.151 + 0.14, 4))
        self.assertEqual(result["stop"], round(1.151 - 0.07, 4))
        self.assertEqual(result["rebound"]["risk_per_share"], 0.07)
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

    def test_zero_cycles_is_not_enough(self):
        closes = [1.00] * 11
        self.assertEqual(self.skip(self.run_case(closes=closes)), "SKIP_CYCLES")

    def test_one_cycle_is_enough(self):
        closes = [1.00, 1.00, 1.00, 1.04, 1.08, 1.12, 1.10, 1.08, 1.06, 1.06, 1.06]
        bars = make_bars(closes + [1.08])
        result = rs.analyze({"symbol": "x"}, bars, quote(1.081, 1.079), now_after(bars), PAPER)
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["rebound"]["cycles"], 1)

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
        rth_mid = self.run_case(q=quote(1.151, 1.137))  # ~1.2% now allowed in RTH
        self.assertNotEqual(rth_mid.get("skip_reason"), "SKIP_SPREAD")
        bars = make_bars(TWO_CYCLES + [TRIGGER], start=datetime(2026, 10, 6, 7, 0, tzinfo=NY))
        result = self.run_case(bars=bars, q=quote(1.151, 1.137))
        self.assertTrue(result["qualified"], result)  # 1.2% allowed pre-market

    def test_exit_levels(self):
        self.assertEqual(rs.exit_levels(11.51, 1.4), (11.01, 12.51))
        self.assertEqual(rs.exit_levels(3.451, 0.42), (3.241, 3.871))
        self.assertIsNone(rs.exit_levels(0.50, 1.2))  # stop would be 0

    def test_price_bounds_and_invalid_inputs(self):
        self.assertEqual(self.skip(self.run_case(q=quote(80.5, 80.49))), "SKIP_PRICE")
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
                         (8.0, 15, 1.5))
        self.assertEqual(rs.profile_for(self.at(17, 0)).max_spread_pct, 1.5)

    def test_detect_cycles_uses_profile(self):
        bars = make_bars(EARLY_CLOSES, start=EARLY)
        clean = rs._clean_bars(bars, now_after(bars))
        self.assertEqual(len(rs.detect_cycles(clean, rs.profile_for(EARLY))), 2)
        self.assertEqual(rs.detect_cycles(clean), [])  # default profile = 8%/15 bars


class EarlyPremarketAnalyzeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(rs, "MIN_SWING_USD", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_at(self, start, q):
        bars = make_bars(EARLY_CLOSES + [EARLY_TRIGGER], start=start)
        return rs.analyze({"symbol": "abcd"}, bars, q, now_after(bars), PAPER)

    def test_fast_small_cycles_qualify_early(self):
        result = self.run_at(EARLY, quote(1.061, 1.059))
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["rebound"]["cycles"], 2)
        self.assertEqual(result["rebound"]["session"], "EARLY_PRE")
        swing = result["rebound"]["swing"]
        self.assertEqual(result["target"], round(1.061 + swing, 4))
        self.assertEqual(result["stop"], round(1.061 - swing / 2, 4))

    def test_same_bars_in_regular_hours_are_not_cycles(self):
        result = self.run_at(datetime(2026, 10, 6, 10, 0, tzinfo=NY), quote(1.061, 1.059))
        self.assertEqual(result["skip_reason"], "SKIP_CYCLES")
        self.assertEqual(result["rebound"]["session"], "RTH")

    @patch.object(rs, "MIN_STOP_SPREAD_MULT", 0.0)  # isolate the spread cap from the stop guard
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
        last_cycle = rs.detect_cycles(clean)[-1]
        after_peak = clean[last_cycle.peak_index + 1:]
        dip_low_bar = min(after_peak, key=lambda bar: (bar["low"], bar["timestamp"]))
        result = self.run_case(DOLLAR, 11.5, quote(11.51, 11.50),
                               symbol_state={"losses": 0,
                                             "last_exit_ts": dip_low_bar["timestamp"]})
        self.assertEqual(result["skip_reason"], "SKIP_SAME_CYCLE")
        result = self.run_case(DOLLAR, 11.5, quote(11.51, 11.50),
                               symbol_state={"losses": 0,
                                             "last_exit_ts": dip_low_bar["timestamp"] - 1})
        self.assertTrue(result["qualified"], result)

    def test_fade_after_winning_exit_can_start_next_cycle(self):
        closes = [10.00, 10.00, 10.50, 11.20, 10.60] + [11.20] * 16 + [
            12.30, 11.75, 11.75
        ]
        trigger = 11.90
        bars = make_bars(closes + [trigger])
        clean = rs._clean_bars(bars, now_after(bars))
        last_cycle = rs.detect_cycles(clean)[-1]
        peak_ts = clean[last_cycle.peak_index]["timestamp"]
        dip_low_ts = clean[last_cycle.fade_index]["timestamp"]

        result = self.run_case(closes, trigger, quote(11.91, 11.90))
        self.assertTrue(result["qualified"], result)

        result = self.run_case(closes, trigger, quote(11.91, 11.90),
                               symbol_state={"losses": 0, "last_exit_ts": peak_ts})
        self.assertTrue(result["qualified"], result)

        result = self.run_case(closes, trigger, quote(11.91, 11.90),
                               symbol_state={"losses": 0, "last_exit_ts": dip_low_ts})
        self.assertEqual(result["skip_reason"], "SKIP_SAME_CYCLE")


    def test_mid_swing_scales_target_and_stop(self):
        closes = [round(c * 3, 4) for c in TWO_CYCLES]  # ~$3 stock, last swing $0.42
        result = self.run_case(closes, 3.45, quote(3.451, 3.449))
        self.assertTrue(result["qualified"], result)
        self.assertEqual((result["stop"], result["target"]), (3.241, 3.871))
        self.assertEqual(result["rebound"]["risk_per_share"], 0.21)

    def test_stop_inside_spread_is_skipped(self):
        closes = [round(c * 10, 4) for c in EARLY_CLOSES]  # ~$10, swing $0.5 -> stop $0.25
        bars = make_bars(closes + [10.6], start=EARLY)
        tight = rs.analyze({"symbol": "x"}, bars, quote(10.61, 10.59), now_after(bars), PAPER)
        self.assertTrue(tight["qualified"], tight)
        wide = rs.analyze({"symbol": "x"}, bars, quote(10.61, 10.35), now_after(bars), PAPER)
        self.assertEqual(wide["skip_reason"], "SKIP_STOP_IN_SPREAD")

    def test_price_cap_is_eighty_dollars(self):
        import scanner
        self.assertEqual(rs.MAX_PRICE, 80.0)
        self.assertGreaterEqual(scanner.MAX_PRICE, rs.MAX_PRICE)

    def test_swing_floor_is_twenty_cents(self):
        self.assertEqual(rs.MIN_SWING_USD, 0.20)


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

    def test_reduce_with_strong_news_passes_at_reduced_size(self):
        self.assertEqual(rs.news_verdict(self.ai(status="REDUCE")), (True, "NEWS_STRONG_REDUCED"))
        self.assertEqual(rs.REDUCED_SIZE_FACTOR, 0.5)

    def test_fail_closed(self):
        cases = [(None, "NEWS_GATE_UNAVAILABLE"), (self.ai(status="BLOCK"), "AI_BLOCK"),
                 (self.ai(status="SKIP"), "AI_SKIP"),
                 (self.ai(status="REDUCE", news_count=0), "NO_RECENT_NEWS"),
                 (self.ai(status="REDUCE", news_score=0.4), "NEWS_NOT_STRONG"),
                 (self.ai(status="REDUCE", event_type="OFFERING"), "NEWS_NEGATIVE_EVENT"),
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
