import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import microcap_history
import microcap_research
from microcap_history import CoverageError
from microcap_research import (
    MODEL_VERSION, TARGET_KEYS, main, plan_quote_windows, price_basis_check, report,
)


NY = ZoneInfo("America/New_York")
SYMBOL = "ACME"
COMPANY = "Acme Corp"
FIRST_DAY = date(2024, 1, 10)  # Wednesday
FEES = {"fees_per_share": 0.02}
DECISION_MINUTES = (53, 56, 60)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def weekdays(first: date, count: int) -> list[date]:
    days, day = [], first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def price(minute: int, crash: bool = False) -> float:
    legs = ((0, 8.0), (10, 7.0), (20, 8.0), (30, 7.0), (40, 8.0), (50, 7.0))
    if minute <= 50:
        for (m0, p0), (m1, p1) in zip(legs, legs[1:]):
            if m0 <= minute <= m1:
                return round(p0 + (p1 - p0) * (minute - m0) / (m1 - m0), 4)
    if minute <= 60:
        return round(7.0 + 0.06 * (minute - 50), 4)
    if crash:
        return round(max(6.6, 7.6 - 0.1 * (minute - 60)), 4)
    if minute <= 78:
        return round(7.6 + 0.15 * (minute - 60), 4)
    if minute <= 88:
        return round(10.3 - 0.3 * (minute - 78), 4)
    return 7.3


def open_at(day: date) -> datetime:
    return datetime.combine(day, time(9, 30), NY).astimezone(timezone.utc)


def day_bars(day: date, last_minute: int = 130, crash: bool = False) -> list[dict]:
    bars = []
    for minute in range(last_minute + 1):
        o = price(max(minute - 1, 0), crash)
        c = price(minute, crash)
        bars.append({
            "symbol": SYMBOL, "t": iso(open_at(day) + timedelta(minutes=minute)),
            "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 1000,
            "request_feed": "iex", "request_adjustment": "split",
        })
    return bars


def day_quotes(day: date, crash: bool = False, extra_minutes: tuple[int, ...] = ()) -> list[dict]:
    quotes = []
    for minute in DECISION_MINUTES + extra_minutes:
        boundary = open_at(day) + timedelta(minutes=minute)
        for offset, mid in ((-2, price(minute - 1, crash)), (2, price(minute, crash))):
            quotes.append({"symbol": SYMBOL, "t": iso(boundary + timedelta(seconds=offset)),
                           "bid": round(mid - 0.01, 4), "ask": round(mid + 0.01, 4),
                           "price_basis": "raw", "request_feed": "iex"})
    return quotes


def article(day: date, index: int) -> dict:
    created = datetime.combine(day, time(8), NY)
    return {"id": f"n{index}", "created_at": iso(created), "headline": "Acme announces new contract",
            "source": "benzinga", "symbols": [SYMBOL], "summary": "", "fetched_at": "2024-06-01T00:00:00Z"}


def dataset(history_days: int, crash_from: int | None = None, *, independent_news: bool = False):
    days = weekdays(FIRST_DAY, history_days + 1)
    history, candidate = days[:-1], days[-1]
    bars, quotes, news = [], [], []
    for index, day in enumerate(history):
        crash = crash_from is not None and index >= crash_from
        bars.extend(day_bars(day, crash=crash))
        quotes.extend(day_quotes(day, crash=crash))
        if not independent_news or index % 3 == 0:
            news.append(article(day, index))
    as_of = open_at(candidate) + timedelta(minutes=61)
    bars.extend(day_bars(candidate, last_minute=60))
    quotes.extend(day_quotes(candidate, extra_minutes=(61,)))  # 61:02 is after as_of: must be ignored
    news.append(article(candidate, len(history)))
    start = iso(datetime.combine(days[0], time(4), NY))
    return {"bars": bars, "news": news, "quotes": quotes, "as_of": iso(as_of),
            "start": start, "end": iso(as_of), "candidate_day": candidate}


VERIFIED_BASIS = {"status": "VERIFIED", "detail": "fixture"}


def run_report(data: dict, **overrides) -> dict:
    arguments = dict(
        symbol=SYMBOL, company=COMPANY, as_of=data["as_of"], start=data["start"], end=data["end"],
        feed="iex", bars=data["bars"], news=data["news"], quotes=data["quotes"], costs=FEES,
        now=datetime(2025, 6, 1, 12, tzinfo=timezone.utc), price_basis=VERIFIED_BASIS,
    )
    arguments.update(overrides)
    return report(**arguments)


class InvalidInputTests(unittest.TestCase):
    def test_invalid_timestamps_and_ranges_raise(self) -> None:
        data = dataset(3)
        for overrides in ({"as_of": "2024-01-03T15:00:00"}, {"as_of": "garbage"},
                          {"start": data["end"], "end": data["start"]},
                          {"end": "2030-01-01T00:00:00Z"},
                          {"as_of": "2030-01-01T00:00:00Z", "end": "2030-01-01T00:00:00Z"},
                          {"feed": "sip"}, {"feed": ""}, {"symbol": " "}, {"company": ""},
                          {"costs": {}}, {"costs": {"fees_per_share": -1}},
                          {"now": datetime(2024, 6, 1)}):
            with self.subTest(overrides=overrides), self.assertRaises(CoverageError):
                run_report(data, **overrides)


class RefusalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.small = dataset(49)

    def assert_no_trade(self, result: dict, reason: str) -> None:
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertTrue(any(item.startswith(reason) for item in result["reasons"]), result["reasons"])
        self.assertFalse(result["order_approval"])
        self.assertEqual(set(result["target_probabilities"]), set(TARGET_KEYS))

    def test_missing_history_fails_closed_with_unavailable_targets(self) -> None:
        data = dataset(3)
        result = run_report(data, bars=[], news=[], quotes=[])
        self.assert_no_trade(result, "BAR_COVERAGE_UNAVAILABLE")
        self.assertIn("NEWS_COVERAGE_UNVERIFIED", result["reasons"])
        self.assertIn("QUOTE_COVERAGE_UNAVAILABLE", result["reasons"])
        for key in TARGET_KEYS:
            self.assertEqual(result["target_probabilities"][key]["status"], "unavailable")
            self.assertTrue(result["target_probabilities"][key]["reason"])

    def test_no_news_is_unverified_coverage_not_absence_of_catalyst(self) -> None:
        result = run_report(self.small, news=[])
        self.assert_no_trade(result, "NEWS_COVERAGE_UNVERIFIED")
        self.assertEqual(result["catalyst"]["status"], "unavailable")
        self.assertNotIn("NO_CATALYST", result["reasons"])

    def test_missing_quotes_never_invent_spread_from_bars(self) -> None:
        for quotes in (None, []):
            with self.subTest(quotes=quotes):
                result = run_report(self.small, quotes=quotes)
                self.assert_no_trade(result, "QUOTE_COVERAGE_UNAVAILABLE")
                self.assertIsNone(result["costs"]["candidate_spread"])

    def test_forty_nine_catalyst_days_cannot_emit_probabilities(self) -> None:
        result = run_report(self.small)
        self.assert_no_trade(result, "INSUFFICIENT_INDEPENDENT_EPISODES")
        self.assertEqual(result["study"]["catalyst_days"], 49)
        self.assertEqual(result["study"]["independent_episodes"], 11)
        for key in TARGET_KEYS:
            self.assertEqual(result["target_probabilities"][key]["status"], "unavailable")
        self.assertEqual(result["study"]["holdout"]["status"], "unavailable")
        self.assertIsNone(result["selected_target"])

    def test_one_rolling_news_episode_can_cover_multiple_catalyst_days(self) -> None:
        result = run_report(dataset(3))
        self.assertEqual(result["study"]["catalyst_days"], 3)
        self.assertEqual(result["study"]["independent_episodes"], 1)
        self.assert_no_trade(result, "INSUFFICIENT_INDEPENDENT_EPISODES")
        for key in TARGET_KEYS:
            self.assertEqual(result["target_probabilities"][key]["status"], "unavailable")

    def test_price_basis_mismatch_or_unverified_blocks_estimates(self) -> None:
        for basis, reason in (({"status": "MISMATCH", "detail": "split"}, "PRICE_BASIS_MISMATCH"),
                              (None, "PRICE_BASIS_UNVERIFIED")):
            with self.subTest(reason=reason):
                self.assert_no_trade(run_report(self.small, price_basis=basis), reason)

    def test_bad_bar_data_is_rejected_without_estimate(self) -> None:
        bars = [dict(bar) for bar in self.small["bars"]]
        bars[5]["o"] = 80.0
        bars[5]["h"] = 80.0
        self.assert_no_trade(run_report(self.small, bars=bars), "BAR_DATA_REJECTED")

    def test_report_never_touches_network(self) -> None:
        with patch.object(microcap_history, "urlopen", side_effect=AssertionError("network")):
            run_report(self.small)


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = dataset(192, independent_news=True)
        cls.result = run_report(cls.data)
        cls.crash = dataset(192, crash_from=150, independent_news=True)
        cls.crash_result = run_report(cls.crash)

    def test_supported_history_is_research_eligible_not_order_approval(self) -> None:
        result = self.result
        self.assertEqual(result["decision"], "RESEARCH_ELIGIBLE", result["reasons"])
        self.assertFalse(result["order_approval"])
        self.assertEqual(result["model_version"], MODEL_VERSION)
        self.assertNotIn("BUY_ELIGIBLE", json.dumps(result))
        self.assertEqual(result["session"]["mode"], "HISTORICAL_REPLAY")
        self.assertEqual(result["session"]["name"], "regular")

    def test_report_includes_targets_coverage_costs_and_study(self) -> None:
        result = self.result
        json.dumps(result)
        for key in TARGET_KEYS:
            entry = result["target_probabilities"][key]
            self.assertEqual(entry["status"], "available")
            self.assertGreaterEqual(entry["sample_size"], 50)
            self.assertIsNotNone(entry["wilson_lower_bound"])
        self.assertEqual(result["sample_size"], 50)
        self.assertEqual(result["selected_target"]["net_target"], 2.0)
        self.assertEqual(result["feed"], "iex")
        self.assertEqual(result["adjustment"]["bars"], "split")
        self.assertEqual(result["adjustment"]["quotes"], "raw")
        self.assertGreater(result["coverage"]["minute_bars"]["count"], 0)
        self.assertGreater(result["coverage"]["quotes"]["count"], 0)
        self.assertIn("completeness", result["coverage"]["news"])
        self.assertEqual(result["costs"]["fees_per_share"], 0.02)
        self.assertAlmostEqual(result["costs"]["candidate_spread"], 0.02)
        self.assertEqual(result["catalyst"]["articles"][0]["source"], "benzinga")
        self.assertIn("age_minutes", result["catalyst"]["articles"][0])
        self.assertGreaterEqual(result["cycle_count"], 2)
        reference = result["signal_reference"]
        self.assertEqual(reference["status"], "available")
        self.assertLess(reference["stop"], reference["entry_signal_reference"])
        study = result["study"]
        self.assertGreater(study["catalyst_days"], 50)
        self.assertEqual(study["independent_episodes"], 64)
        self.assertEqual(
            [study["split"][name]["independent_episodes"] for name in ("train", "validation", "holdout")],
            [38, 12, 14],
        )
        self.assertEqual(sum(study["split"][name]["days"] for name in ("train", "validation", "holdout")),
                         study["catalyst_days"])
        self.assertLess(study["split"]["train"]["last"], study["split"]["validation"]["first"])
        self.assertLess(study["split"]["validation"]["last"], study["split"]["holdout"]["first"])
        holdout = study["holdout"]
        self.assertEqual(holdout["status"], "available")
        self.assertGreater(holdout["targets"]["2.00"]["mean_net"], holdout["baseline_hold_to_horizon"]["mean_net"])
        self.assertIn("max_drawdown", holdout["targets"]["2.00"])
        self.assertEqual(holdout["baseline_no_trade"]["mean_net"], 0.0)
        self.assertIn("calibration", holdout)
        self.assertIn("freshness", result)

    def test_holdout_never_influences_parameter_selection(self) -> None:
        self.assertEqual(self.result["study"]["selection"], self.crash_result["study"]["selection"])
        self.assertEqual(self.result["study"]["validation"], self.crash_result["study"]["validation"])
        self.assertNotEqual(self.result["study"]["holdout"], self.crash_result["study"]["holdout"])
        self.assertEqual(self.crash_result["decision"], "NO_TRADE")
        self.assertIn("HOLDOUT_NOT_POSITIVE", self.crash_result["reasons"])

    def test_live_as_of_requires_ibkr_state_and_ignores_alpaca_quotes(self) -> None:
        small = dataset(49)
        as_of = microcap_history.parse_utc(small["as_of"])
        result = run_report(small, now=as_of + timedelta(minutes=5))
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertIn("LIVE_IBKR_STATE_REQUIRED", result["reasons"])
        self.assertEqual(result["session"]["mode"], "LIVE_REQUIRED")
        self.assertIsNone(result["costs"]["candidate_spread"])
        self.assertEqual(result["signal_reference"]["status"], "unavailable")

    def test_future_quotes_are_never_used_for_candidate_costs(self) -> None:
        small = dataset(49)
        as_of = microcap_history.parse_utc(small["as_of"])
        quotes = [q for q in small["quotes"] if microcap_history.parse_utc(q["t"]) <= as_of - timedelta(seconds=30)]
        quotes.append({"t": iso(as_of + timedelta(seconds=1)), "bid": 7.59, "ask": 7.61})
        result = run_report(small, quotes=quotes)
        self.assertIsNone(result["costs"]["candidate_spread"])
        self.assertIn("CANDIDATE_QUOTE_UNAVAILABLE", result["reasons"])
        self.assertEqual(result["decision"], "NO_TRADE")


class HelperTests(unittest.TestCase):
    def test_price_basis_check(self) -> None:
        daily = [{"t": "2024-01-10T05:00:00Z", "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10}]
        self.assertEqual(price_basis_check(daily, [dict(daily[0])])["status"], "VERIFIED")
        self.assertEqual(price_basis_check(daily, [dict(daily[0], c=3.0)])["status"], "MISMATCH")
        self.assertEqual(price_basis_check([], [])["status"], "UNVERIFIED")
        self.assertEqual(price_basis_check(daily, [])["status"], "MISMATCH")

    def test_quote_windows_cover_candidate_decisions_and_as_of(self) -> None:
        data = dataset(2)
        windows = plan_quote_windows(data["bars"], data["news"], SYMBOL, COMPANY, data["as_of"],
                                     include_as_of=True)
        as_of = microcap_history.parse_utc(data["as_of"])
        for day in weekdays(FIRST_DAY, 2):
            for minute in DECISION_MINUTES:
                decision = open_at(day) + timedelta(minutes=minute)
                self.assertTrue(any(start <= decision - timedelta(seconds=10) and decision + timedelta(seconds=10) <= end
                                    for start, end in windows), (day, minute))
        self.assertTrue(any(start <= as_of - timedelta(seconds=10) and as_of <= end for start, end in windows))
        self.assertTrue(all(end <= as_of for _, end in windows))
        self.assertEqual(windows, sorted(windows))
        for (_, first_end), (second_start, _) in zip(windows, windows[1:]):
            self.assertLess(first_end, second_start)


class FakeClient:
    def __init__(self, data: dict, fail: str | None = None) -> None:
        self.data, self.fail, self.calls = data, fail, []

    @staticmethod
    def _within(records, start, end, key="t"):
        lo, hi = microcap_history.parse_utc(start), microcap_history.parse_utc(end)
        return [r for r in records if lo <= microcap_history.parse_utc(r[key]) < hi]

    def fetch_bars(self, symbols, start, end, *, timeframe="1Min", feed="iex", adjustment="split"):
        self.calls.append(("bars", timeframe, adjustment))
        if self.fail == "bars":
            raise CoverageError("HTTP_ERROR: status 500 from /v2/stocks/bars (HTTPError)")
        if timeframe == "1Min":
            return self._within(self.data["bars"], start, end)
        if timeframe == "1Day":
            return [{"symbol": SYMBOL, "t": iso(datetime.combine(d, time(0), NY)), "o": 8.0, "h": 8.0,
                     "l": 7.0, "c": 7.3, "v": 1, "request_adjustment": adjustment}
                    for d in weekdays(FIRST_DAY, 3)]
        return []

    def fetch_news(self, symbols, start, end):
        self.calls.append(("news",))
        return self._within(self.data["news"], start, end, "created_at")

    def fetch_quotes(self, symbols, start, end, *, feed="iex"):
        self.calls.append(("quotes",))
        if self.fail == "quotes":
            raise CoverageError("QUOTE_COVERAGE_UNAVAILABLE: HTTP_ERROR: status 403 from /v2/stocks/quotes")
        return self._within(self.data["quotes"], start, end)


class CliTests(unittest.TestCase):
    SECRET = "never-print-this-secret"

    def run_cli(self, argv, client=None, factory=None):
        out, err = io.StringIO(), io.StringIO()
        factory = factory or (lambda: client)
        with patch.dict(os.environ, {"APCA_API_KEY_ID": "key-id-value", "APCA_API_SECRET_KEY": self.SECRET}), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(argv, client_factory=factory,
                        now=lambda: datetime(2024, 6, 1, 12, tzinfo=timezone.utc), sleep=lambda _: None)
        for stream in (out.getvalue(), err.getvalue()):
            self.assertNotIn(self.SECRET, stream)
            self.assertNotIn("key-id-value", stream)
        return code, out.getvalue(), err.getvalue()

    def argv(self, data, **extra):
        values = {"--symbol": SYMBOL, "--company": COMPANY, "--as-of": data["as_of"],
                  "--start": data["start"], "--end": data["end"]}
        values.update(extra)
        return [item for pair in values.items() for item in pair]

    def test_invalid_as_of_exits_nonzero_before_any_client_call(self) -> None:
        data = dataset(2)
        client = FakeClient(data)
        code, out, err = self.run_cli(self.argv(data, **{"--as-of": "2024-01-03T15:00:00"}), client)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("TIMESTAMP_INVALID", err)
        self.assertEqual(client.calls, [])

    def test_unsupported_feed_exits_nonzero(self) -> None:
        data = dataset(2)
        code, _, _ = self.run_cli(self.argv(data, **{"--feed": "sip"}), FakeClient(data))
        self.assertNotEqual(code, 0)

    def test_api_errors_exit_nonzero_with_reason(self) -> None:
        data = dataset(2)
        for fail, reason in (("bars", "HTTP_ERROR"), ("quotes", "QUOTE_COVERAGE_UNAVAILABLE")):
            with self.subTest(fail=fail):
                code, out, err = self.run_cli(self.argv(data), FakeClient(data, fail))
                self.assertNotEqual(code, 0)
                self.assertEqual(out, "")
                self.assertIn(reason, err)

    def test_missing_credentials_exit_nonzero(self) -> None:
        def factory():
            raise CoverageError("CREDENTIALS_MISSING: APCA_API_KEY_ID and APCA_API_SECRET_KEY are required")
        data = dataset(2)
        code, out, err = self.run_cli(self.argv(data), factory=factory)
        self.assertNotEqual(code, 0)
        self.assertIn("CREDENTIALS_MISSING", err)

    def test_insufficient_evidence_prints_no_trade_json_and_exits_zero(self) -> None:
        data = dataset(3)
        client = FakeClient(data)
        code, out, err = self.run_cli(self.argv(data), client)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertIn("INSUFFICIENT_INDEPENDENT_EPISODES", result["reasons"])
        self.assertFalse(result["order_approval"])
        self.assertEqual(result["adjustment"]["basis_check"]["status"], "VERIFIED")
        self.assertNotIn("order_id", out)
        self.assertIn(("bars", "1Day", "raw"), client.calls)
        self.assertIn(("bars", "1Hour", "split"), client.calls)
        self.assertGreater(result["coverage"]["quotes"]["windows_requested"], 0)
        self.assertEqual(result["context"]["daily"]["status"], "available")
        self.assertEqual(result["context"]["hourly"]["status"], "unavailable")

    def test_module_has_no_order_or_trading_api_path(self) -> None:
        with open(microcap_research.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("paper-api.alpaca.markets", "api.alpaca.markets/v2/orders", "submit_order",
                          "placeOrder", "ib_insync", "BUY_ELIGIBLE\"", "print(os.environ"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
