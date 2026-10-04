import unittest
from datetime import datetime, timedelta, timezone

from microcap_history import CoverageError
from microcap_rebounds import (
    CycleParams,
    confirmed_cycles,
    current_setup,
    eligible_news,
    extract_events,
)


AS_OF = "2024-01-03T15:00:00Z"
T0 = datetime(2024, 1, 3, 15, 0, tzinfo=timezone.utc)  # 10:00 New York, regular session
PARAMS = CycleParams(min_amplitude=1.0, min_separation_minutes=2)
# Two complete low->high->low cycles; the third trough (index 14) is confirmed by index 15.
TWO_CYCLES = [10, 9, 8, 9, 10, 11, 10, 9, 8.5, 9.5, 10.5, 11.5, 10.5, 9.5, 9, 10]
# A third cycle closes at index 20 (confirmed by index 21), followed by flat bars.
THREE_CYCLES = TWO_CYCLES + [11, 12, 11, 10.5, 10, 11] + [11] * 10


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def minute(index: int, start: datetime = T0) -> str:
    return iso(start + timedelta(minutes=index))


def bars_from(prices, start: datetime = T0) -> list[dict[str, object]]:
    return [
        {"t": minute(index, start), "o": price, "h": price, "l": price, "c": price, "v": 100}
        for index, price in enumerate(prices)
    ]


def article(article_id=1, created_at="2024-01-03T12:00:00Z",
            headline="ABCD Therapeutics announces FDA approval", symbols=("ABCD",)):
    return {
        "id": article_id,
        "created_at": created_at,
        "headline": headline,
        "source": "Example",
        "symbols": list(symbols),
        "summary": "",
    }


def eligible(news, as_of=AS_OF):
    return eligible_news(news, "ABCD", "ABCD Therapeutics Inc.", as_of)


class EligibleNewsTests(unittest.TestCase):
    def test_fresh_company_headline_is_eligible(self) -> None:
        self.assertEqual([item["id"] for item in eligible([article()])], [1])

    def test_article_after_as_of_is_excluded(self) -> None:
        self.assertEqual(eligible([article(created_at="2024-01-03T15:01:00Z")]), [])

    def test_48_hour_window_is_inclusive_and_49_hours_is_stale(self) -> None:
        self.assertEqual(len(eligible([article(created_at="2024-01-01T15:00:00Z")])), 1)
        self.assertEqual(eligible([article(created_at="2024-01-01T14:00:00Z")]), [])

    def test_article_exactly_at_as_of_is_eligible(self) -> None:
        self.assertEqual(len(eligible([article(created_at=AS_OF)])), 1)

    def test_malformed_naive_or_missing_dates_are_rejected(self) -> None:
        for created_at in ("not-a-date", "2024-01-03T12:00:00", None, ""):
            with self.subTest(created_at=created_at):
                self.assertEqual(eligible([article(created_at=created_at)]), [])
        missing = article()
        del missing["created_at"]
        self.assertEqual(eligible([missing]), [])

    def test_invalid_as_of_raises_instead_of_using_current_time(self) -> None:
        for as_of in ("2024-01-03T15:00:00", "bad", None, datetime(2024, 1, 3, 15)):
            with self.subTest(as_of=as_of):
                with self.assertRaises(CoverageError):
                    eligible([article()], as_of=as_of)

    def test_aware_datetime_as_of_is_accepted(self) -> None:
        self.assertEqual(len(eligible([article()], as_of=T0)), 1)

    def test_symbol_must_be_listed_by_article(self) -> None:
        self.assertEqual(eligible([article(symbols=("WXYZ",))]), [])

    def test_wrong_company_headline_is_excluded(self) -> None:
        news = [article(headline="Peer Bio Corp announces FDA approval", symbols=("ABCD", "PEER"))]
        self.assertEqual(eligible(news), [])

    def test_symbol_mentioned_only_as_comparison_is_rejected(self) -> None:
        for headline in (
            "Peer Bio beats ABCD Therapeutics in head-to-head trial",
            "Peer Bio shares outpace ABCD after data",
            "Peer Bio vs. ABCD: which biotech is the better buy?",
        ):
            with self.subTest(headline=headline):
                self.assertEqual(eligible([article(headline=headline, symbols=("ABCD", "PEER"))]), [])

    def test_generic_roundups_are_rejected(self) -> None:
        for headline in (
            "ABCD, PEER, WXYZ among top premarket movers",
            "12 Health Care Stocks Moving In Wednesday's Pre-Market Session",
            "ABCD and other mid-day gainers",
        ):
            with self.subTest(headline=headline):
                self.assertEqual(
                    eligible([article(headline=headline, symbols=("ABCD", "PEER", "WXYZ"))]), []
                )

    def test_ticker_substring_is_not_an_explicit_mention(self) -> None:
        news = [article(headline="ABCDE Corp announces offering", symbols=("ABCD", "ABCDE"))]
        self.assertEqual(eligible(news), [])
        lowercase = [article(headline="Abcd-like rally lifts sector", symbols=("ABCD",))]
        self.assertEqual(eligible(lowercase), [])

    def test_explicit_ticker_and_why_lead_in_are_primary_subject(self) -> None:
        for headline in (
            "ABCD shares jump after FDA approval",
            "$ABCD: FDA approves lead drug",
            "Why ABCD Therapeutics Shares Are Trading Higher",
            "UPDATE: ABCD Therapeutics (NASDAQ: ABCD) prices offering",
        ):
            with self.subTest(headline=headline):
                self.assertEqual(len(eligible([article(headline=headline)])), 1)

    def test_duplicate_article_ids_are_deduplicated(self) -> None:
        self.assertEqual(len(eligible([article(7), article(7)])), 1)

    def test_conflicting_duplicate_ids_are_dropped(self) -> None:
        news = [article(7), article(7, created_at="2024-01-03T13:00:00Z")]
        self.assertEqual(eligible(news), [])

    def test_missing_or_invalid_ids_are_rejected(self) -> None:
        for article_id in (None, "", True):
            with self.subTest(article_id=article_id):
                self.assertEqual(eligible([article(article_id)]), [])


class ConfirmedCyclesTests(unittest.TestCase):
    def test_flat_bars_have_no_cycles(self) -> None:
        bars = bars_from([10.0] * 20)
        self.assertEqual(confirmed_cycles(bars, minute(20), params=PARAMS), ())

    def test_two_cycles_report_confirmation_bar_end_not_extremum_time(self) -> None:
        cycles = confirmed_cycles(bars_from(TWO_CYCLES), minute(16), params=PARAMS)
        self.assertEqual(len(cycles), 2)
        second = cycles[1]
        self.assertEqual(second.end_low.at, T0 + timedelta(minutes=14))
        self.assertEqual(second.end_low.price, 9)
        self.assertEqual(second.confirmed_at, T0 + timedelta(minutes=16))
        self.assertEqual(second.session, "regular")
        self.assertEqual([cycle.start_low.price for cycle in cycles], [8, 8.5])
        self.assertEqual([cycle.high.price for cycle in cycles], [11, 11.5])

    def test_third_trough_known_only_after_confirmation_bar_completes(self) -> None:
        bars = bars_from(TWO_CYCLES)
        self.assertEqual(len(confirmed_cycles(bars, minute(15), params=PARAMS)), 1)
        self.assertEqual(len(confirmed_cycles(bars, iso(T0 + timedelta(minutes=15, seconds=59)), params=PARAMS)), 1)
        self.assertEqual(len(confirmed_cycles(bars, minute(16), params=PARAMS)), 2)

    def test_bars_after_decision_do_not_change_earlier_decision(self) -> None:
        base = confirmed_cycles(bars_from(TWO_CYCLES), minute(16), params=PARAMS)
        extended = confirmed_cycles(bars_from(TWO_CYCLES + [3, 20, 2, 25]), minute(16), params=PARAMS)
        self.assertEqual(base, extended)

    def test_amplitude_and_separation_are_explicit_and_respected(self) -> None:
        with self.assertRaises(TypeError):
            confirmed_cycles(bars_from(TWO_CYCLES), minute(16))  # type: ignore[call-arg]
        wide = CycleParams(min_amplitude=5.0, min_separation_minutes=2)
        self.assertEqual(confirmed_cycles(bars_from(TWO_CYCLES), minute(16), params=wide), ())
        slow = CycleParams(min_amplitude=1.0, min_separation_minutes=10)
        self.assertEqual(confirmed_cycles(bars_from(TWO_CYCLES), minute(16), params=slow), ())

    def test_invalid_params_raise(self) -> None:
        for kwargs in (
            {"min_amplitude": 0, "min_separation_minutes": 2},
            {"min_amplitude": float("nan"), "min_separation_minutes": 2},
            {"min_amplitude": 1.0, "min_separation_minutes": -1},
            {"min_amplitude": 1.0, "min_separation_minutes": 2, "max_gap_minutes": 0},
            {"min_amplitude": 1.0, "min_separation_minutes": 2, "max_jump_ratio": 1.0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CoverageError):
                    CycleParams(**kwargs)

    def test_cycles_do_not_mix_premarket_and_regular_sessions(self) -> None:
        premarket_start = datetime(2024, 1, 3, 14, 22, tzinfo=timezone.utc)  # 09:22 New York
        bars = bars_from(TWO_CYCLES, premarket_start)
        cycles = confirmed_cycles(bars, iso(premarket_start + timedelta(minutes=16)), params=PARAMS)
        regular_open = datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc)
        self.assertEqual(len(cycles), 1)
        self.assertGreaterEqual(cycles[0].start_low.at, regular_open)
        contiguous = confirmed_cycles(bars_from(TWO_CYCLES), minute(16), params=PARAMS)
        self.assertEqual(len(contiguous), 2)

    def test_gap_larger_than_limit_resets_cycle_detection(self) -> None:
        bars = bars_from(TWO_CYCLES)
        shifted = bars[:8] + [dict(item, t=minute(index + 30)) for index, item in enumerate(bars[8:], 8)]
        cycles = confirmed_cycles(shifted, minute(46), params=PARAMS)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].start_low.at, T0 + timedelta(minutes=38))

    def test_missing_fields_fail_closed(self) -> None:
        bars = bars_from(TWO_CYCLES)
        del bars[3]["l"]
        with self.assertRaises(CoverageError):
            confirmed_cycles(bars, minute(16), params=PARAMS)

    def test_unordered_or_duplicate_bars_fail_closed(self) -> None:
        bars = bars_from(TWO_CYCLES)
        for broken in ([bars[1], bars[0]] + bars[2:], [bars[0], bars[0]] + bars[1:]):
            with self.subTest():
                with self.assertRaises(CoverageError):
                    confirmed_cycles(broken, minute(16), params=PARAMS)

    def test_inconsistent_ohlc_or_nonpositive_prices_fail_closed(self) -> None:
        for change in ({"h": 5}, {"o": 0, "l": 0}, {"v": -1}, {"c": float("nan")}, {"o": True}):
            bars = bars_from(TWO_CYCLES)
            bars[3].update(change)
            with self.subTest(change=change):
                with self.assertRaises(CoverageError):
                    confirmed_cycles(bars, minute(16), params=PARAMS)

    def test_split_like_jump_fails_closed(self) -> None:
        prices = TWO_CYCLES[:6] + [price / 10 for price in TWO_CYCLES[6:]]
        with self.assertRaises(CoverageError):
            confirmed_cycles(bars_from(prices), minute(16), params=PARAMS)

    def test_unadjusted_bars_fail_closed(self) -> None:
        bars = bars_from(TWO_CYCLES)
        bars[0]["request_adjustment"] = "raw"
        with self.assertRaises(CoverageError):
            confirmed_cycles(bars, minute(16), params=PARAMS)

    def test_bars_outside_extended_hours_fail_closed(self) -> None:
        overnight = datetime(2024, 1, 3, 2, 0, tzinfo=timezone.utc)  # 21:00 New York
        with self.assertRaises(CoverageError):
            confirmed_cycles(bars_from(TWO_CYCLES, overnight), iso(overnight + timedelta(minutes=16)), params=PARAMS)


class CurrentSetupTests(unittest.TestCase):
    def test_setup_after_two_cycles_and_confirmed_low(self) -> None:
        setup, reason = current_setup(bars_from(TWO_CYCLES), 0.02, 0.005, minute(16), params=PARAMS)
        self.assertEqual(reason, "SETUP_READY")
        self.assertEqual(setup, {"entry": 10, "stop": 9, "spread": 0.02, "fees": 0.005})

    def test_no_setup_before_third_trough_confirmation(self) -> None:
        setup, reason = current_setup(bars_from(TWO_CYCLES), 0.02, 0.005, minute(15), params=PARAMS)
        self.assertIsNone(setup)
        self.assertEqual(reason, "INSUFFICIENT_CYCLES")

    def test_flat_bars_have_no_setup(self) -> None:
        self.assertEqual(
            current_setup(bars_from([10.0] * 20), 0.02, 0.005, minute(20), params=PARAMS),
            (None, "INSUFFICIENT_CYCLES"),
        )

    def test_missing_or_invalid_spread_and_fees_block_setup(self) -> None:
        bars = bars_from(TWO_CYCLES)
        for spread in (None, 0, -0.01, float("nan"), True):
            with self.subTest(spread=spread):
                self.assertEqual(
                    current_setup(bars, spread, 0.005, minute(16), params=PARAMS), (None, "SPREAD_INVALID")
                )
        for fees in (None, -0.01, float("inf")):
            with self.subTest(fees=fees):
                self.assertEqual(
                    current_setup(bars, 0.02, fees, minute(16), params=PARAMS), (None, "FEES_INVALID")
                )

    def test_bad_data_returns_reason_instead_of_raising(self) -> None:
        bars = bars_from(TWO_CYCLES)
        del bars[3]["c"]
        setup, reason = current_setup(bars, 0.02, 0.005, minute(16), params=PARAMS)
        self.assertIsNone(setup)
        self.assertTrue(reason.startswith("BAD_DATA"))
        self.assertEqual(current_setup(bars_from(TWO_CYCLES), 0.02, 0.005, "bad", params=PARAMS)[1][:8], "BAD_DATA")

    def test_stale_bars_block_setup(self) -> None:
        self.assertEqual(
            current_setup(bars_from(TWO_CYCLES), 0.02, 0.005, minute(18), params=PARAMS), (None, "STALE_BARS")
        )

    def test_no_bars_and_outside_session(self) -> None:
        self.assertEqual(current_setup([], 0.02, 0.005, minute(16), params=PARAMS), (None, "NO_BARS"))
        self.assertEqual(
            current_setup(bars_from(TWO_CYCLES), 0.02, 0.005, "2024-01-03T03:00:00Z", params=PARAMS),
            (None, "OUTSIDE_SESSION"),
        )

    def test_confirmed_high_after_third_trough_ends_setup(self) -> None:
        prices = TWO_CYCLES + [11, 12, 11]
        self.assertEqual(
            current_setup(bars_from(prices), 0.02, 0.005, minute(19), params=PARAMS), (None, "NO_FRESH_LOW")
        )

    def test_price_at_or_below_stop_blocks_setup(self) -> None:
        bars = bars_from(TWO_CYCLES + [9])
        self.assertEqual(current_setup(bars, 0.02, 0.005, minute(17), params=PARAMS), (None, "BELOW_STOP"))


def quote(at: datetime, bid=9.99, ask=10.01):
    return {"t": iso(at), "bid": bid, "ask": ask}


class ExtractEventsTests(unittest.TestCase):
    def extract(self, bars=None, news=None, cutoff=None, quotes=None, fees=0.005, horizon=5):
        return extract_events(
            bars_from(THREE_CYCLES) if bars is None else bars,
            [article()] if news is None else news,
            "ABCD",
            "ABCD Therapeutics Inc.",
            minute(40) if cutoff is None else cutoff,
            params=PARAMS,
            quotes=[quote(T0 + timedelta(minutes=15, seconds=30)), quote(T0 + timedelta(minutes=21, seconds=30))]
            if quotes is None else quotes,
            fees=fees,
            horizon_minutes=horizon,
            max_quote_age_seconds=60,
        )

    def test_one_event_per_symbol_catalyst_session(self) -> None:
        events = self.extract()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["decision_at"], T0 + timedelta(minutes=16))
        self.assertEqual(event["entry"], 10)
        self.assertEqual(event["stop"], 9)
        self.assertAlmostEqual(event["spread"], 0.02)
        self.assertEqual(event["fees"], 0.005)
        self.assertEqual(event["catalyst_id"], 1)
        self.assertEqual(event["symbol"], "ABCD")
        self.assertEqual(event["feature_bucket"], "regular|price:10-20|news_age:0-6h")
        self.assertEqual([item["t"] for item in event["future_bars"]], [minute(index) for index in range(16, 21)])

    def test_event_matches_current_setup_at_decision_time(self) -> None:
        event = self.extract()[0]
        setup, _ = current_setup(
            bars_from(THREE_CYCLES), event["spread"], event["fees"], event["decision_at"], params=PARAMS
        )
        self.assertEqual((setup["entry"], setup["stop"]), (event["entry"], event["stop"]))

    def test_outcome_window_must_finish_strictly_before_cutoff(self) -> None:
        self.assertEqual(self.extract(cutoff=minute(21)), [])
        self.assertEqual(len(self.extract(cutoff=iso(T0 + timedelta(minutes=21, seconds=1)))), 1)

    def test_bars_after_cutoff_are_ignored(self) -> None:
        bars = bars_from(THREE_CYCLES)
        bars[-1]["l"] = None
        self.assertEqual(len(self.extract(bars=bars, cutoff=minute(30))), 1)

    def first_decision_only(self, bars):
        # Only the decision at minute 16 has an as-of quote, isolating its outcome window.
        return self.extract(bars=bars, quotes=[quote(T0 + timedelta(minutes=15, seconds=30))])

    def test_complete_outcome_window_produces_event(self) -> None:
        events = self.first_decision_only(bars_from(THREE_CYCLES))
        self.assertEqual(len(events), 1)
        self.assertEqual([item["t"] for item in events[0]["future_bars"]], [minute(index) for index in range(16, 21)])

    def test_missing_interior_outcome_bar_skips_event(self) -> None:
        bars = [bar for bar in bars_from(THREE_CYCLES) if bar["t"] != minute(18)]
        self.assertEqual(self.first_decision_only(bars), [])

    def test_missing_final_outcome_bar_skips_event(self) -> None:
        bars = [bar for bar in bars_from(THREE_CYCLES) if bar["t"] != minute(20)]
        self.assertEqual(self.first_decision_only(bars), [])

    def test_missing_first_outcome_bar_skips_event(self) -> None:
        bars = [bar for bar in bars_from(THREE_CYCLES) if bar["t"] != minute(16)]
        self.assertEqual(self.first_decision_only(bars), [])

    def test_missing_quote_spread_or_fees_produce_no_event(self) -> None:
        self.assertEqual(self.extract(quotes=[]), [])
        self.assertEqual(self.extract(fees=None), [])
        stale = [quote(T0 + timedelta(minutes=14))]
        self.assertEqual(self.extract(quotes=stale), [])
        future_only = [quote(T0 + timedelta(minutes=16, seconds=1))]
        self.assertEqual(self.extract(quotes=future_only), [])
        crossed = [quote(T0 + timedelta(minutes=15, seconds=30), bid=10.02, ask=10.01)]
        self.assertEqual(self.extract(quotes=crossed), [])

    def test_no_event_without_as_of_catalyst(self) -> None:
        self.assertEqual(self.extract(news=[article(created_at="2024-01-03T15:23:00Z")]), [])
        self.assertEqual(self.extract(news=[]), [])

    def test_later_catalyst_can_create_independent_event(self) -> None:
        news = [article(created_at="2023-12-31T12:00:00Z"), article(2, created_at="2024-01-03T15:18:00Z")]
        # Article 1 is stale at both decisions; article 2 is known only for the second decision.
        events = self.extract(news=news)
        self.assertEqual([event["catalyst_id"] for event in events], [2])
        self.assertEqual(events[0]["decision_at"], T0 + timedelta(minutes=22))


if __name__ == "__main__":
    unittest.main()
