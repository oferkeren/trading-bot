import json
import unittest
from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.contract import ContractDetails
from ibapi.common import BarData, HistoricalTickBidAsk
from ibapi.wrapper import EWrapper

from microcap_history import CoverageError
from microcap_ibkr_probe import (
    _ProbeApp,
    _bar_observation,
    _daily_bar_observation,
    probe_ibkr,
    summarize_observations,
)


def utc_epoch(value: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class OfflineIBKRDriver:
    """Patch the installed EClient transport with deterministic EWrapper callbacks."""

    def __init__(self, *, scenario: str = "complete") -> None:
        self.scenario = scenario
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.app = None
        self.network_runs = 0

    def connect(self, app, host, port, client_id):
        self.app = app
        if self.scenario == "connect_error":
            raise ConnectionError("offline test connection failure")
        app._offline_connected = True

    def is_connected(self, app):
        return getattr(app, "_offline_connected", False)

    def run(self, app):
        self.network_runs += 1
        if self.scenario == "network_error" and self.network_runs > 1:
            raise RuntimeError("offline test network loop failure")
        return None

    def disconnect(self, app):
        self.calls.append(("disconnect", ()))
        app._offline_connected = False
        app.connectionClosed()

    def req_contract_details(self, app, request_id, query):
        self.calls.append(("reqContractDetails", (request_id, query)))
        if self.scenario == "contract_timeout":
            return
        if self.scenario in ("unknown", "delisted"):
            app.contractDetailsEnd(request_id)
            return
        contracts = [self.contract(12345)]
        if self.scenario == "ambiguous":
            contracts.append(self.contract(23456))
        for resolved in contracts:
            details = ContractDetails()
            details.contract = resolved
            details.validExchanges = "SMART,NYSE"
            app.contractDetails(request_id, details)
        app.contractDetailsEnd(request_id)

    @staticmethod
    def contract(con_id: int) -> Contract:
        contract = Contract()
        contract.conId = con_id
        contract.symbol = "ABC"
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.primaryExchange = "NYSE"
        contract.currency = "USD"
        return contract

    def req_historical_data(self, app, request_id, contract, end, duration, bar_size,
                            what_to_show, use_rth, format_date, keep_up_to_date, options):
        self.calls.append(("reqHistoricalData", (request_id, bar_size, what_to_show,
                                                 format_date)))
        if self.scenario == "disconnect":
            app.connectionClosed()
            return
        if self.scenario == "network_error":
            app.run_network_loop()
            return
        if self.scenario == "permission" and what_to_show == "TRADES":
            app.error(request_id, 0, 354, "Not subscribed to requested market data", "")
            return
        if self.scenario == "global_pacing":
            app.error(-1, 0, 420, "Historical data pacing violation", "")
            return
        if self.scenario == "timeout":
            return
        # formatDate=2: intraday bars carry epoch seconds as str, daily bars a session date.
        if bar_size == "1 day":
            dates = ["2024-05-1x" if self.scenario == "malformed_daily" else "20240515"]
        elif self.scenario == "ambiguous_time":
            dates = ["20240515"]
        elif self.scenario == "flood_ambiguous" and bar_size == "1 min":
            dates = ["20240515 10:30:00"] * 300
        elif bar_size == "1 min":
            dates = [str(utc_epoch("2024-05-15T14:30:00Z"))]
        else:
            dates = [str(utc_epoch("2024-05-15T14:00:00Z"))]
        for bar_date in dates:
            bar = BarData()
            bar.date = bar_date
            bar.open = 1.0
            bar.high = 1.2
            bar.low = 0.9
            bar.close = 1.1
            bar.volume = Decimal(1000)
            app.historicalData(request_id, bar)
        app.historicalDataEnd(request_id, "", "")

    def req_historical_ticks(self, app, request_id, contract, start, end,
                             number_of_ticks, what_to_show, use_rth, ignore_size, options):
        self.calls.append(("reqHistoricalTicks", (
            request_id, contract.conId, start, end, number_of_ticks, what_to_show,
            use_rth, ignore_size, options,
        )))
        if self.scenario == "pacing" and what_to_show == "BID_ASK":
            app.error(request_id, 0, 420, "Historical data pacing violation", "")
            return
        first = utc_epoch("2024-05-15T14:30:01Z")
        if self.scenario == "tick_limit":
            times = [utc_epoch("2024-05-15T14:00:00Z") + offset for offset in range(1000)]
        elif self.scenario == "tick_limit_past_end":
            times = [first] * 999 + [utc_epoch("2024-05-15T15:00:00Z")]
        elif self.scenario == "ticks_past_end":
            times = [first, utc_epoch("2024-05-15T15:00:00Z")]
        else:
            times = [first]
        ticks = []
        for at in times:
            tick = HistoricalTickBidAsk()
            tick.time = at
            tick.priceBid = 1.09
            tick.priceAsk = 1.11
            tick.sizeBid = 100
            tick.sizeAsk = 200
            ticks.append(tick)
        app.historicalTicksBidAsk(request_id, ticks, True)

    def req_news_providers(self, app):
        self.calls.append(("reqNewsProviders", ()))
        providers = [SimpleNamespace(code="BRFG", name="Briefing")]
        if self.scenario == "multi_provider":
            providers.append(SimpleNamespace(code="DJNL", name="Dow Jones Newsletters"))
        app.newsProviders(providers)

    def req_historical_news(self, app, request_id, con_id, provider_codes, start, end,
                            total_results, options):
        self.calls.append(("reqHistoricalNews", (
            request_id, con_id, provider_codes, start, end, total_results, options,
        )))
        if self.scenario == "empty_news":
            app.historicalNewsEnd(request_id, False)
            return
        article_time = {
            "ambiguous_news": "20240515 14:29:00 EST",
            "news_naive": "20240515 14:29:00",
            "malformed_news": "20240515T14:29",
            "news_epoch": str(utc_epoch("2024-05-15T14:29:00Z")),
            "news_epoch_millis": str(utc_epoch("2024-05-15T14:29:00Z") * 1000),
            "news_dashed": "2024-05-15 14:29:00.0",
            "news_utc_suffix": "20240515 14:29:00 UTC",
            "news_iso_zoned": "2024-05-15T10:29:00-04:00",
            "news_at_end": str(utc_epoch("2024-05-15T15:00:00Z")),
            "news_past_end": str(utc_epoch("2024-05-15T15:00:01Z")),
        }.get(self.scenario, str(utc_epoch("2024-05-15T14:29:00Z")))
        provider = "DJNL" if self.scenario == "multi_provider" else "BRFG"
        app.historicalNews(
            request_id, article_time, provider, "article-1",
            "Company announces a product update",
        )
        app.historicalNewsEnd(request_id, False)

    def patches(self):
        def connect(app, host, port, client_id):
            return self.connect(app, host, port, client_id)

        def is_connected(app):
            return self.is_connected(app)

        def run(app):
            return self.run(app)

        def disconnect(app):
            return self.disconnect(app)

        def req_contract_details(app, request_id, query):
            return self.req_contract_details(app, request_id, query)

        def req_historical_data(
            app, request_id, contract, end, duration, bar_size, what_to_show,
            use_rth, format_date, keep_up_to_date, options,
        ):
            return self.req_historical_data(
                app, request_id, contract, end, duration, bar_size, what_to_show,
                use_rth, format_date, keep_up_to_date, options,
            )

        def req_historical_ticks(
            app, request_id, contract, start, end, number_of_ticks,
            what_to_show, use_rth, ignore_size, options,
        ):
            return self.req_historical_ticks(
                app, request_id, contract, start, end, number_of_ticks,
                what_to_show, use_rth, ignore_size, options,
            )

        def req_news_providers(app):
            return self.req_news_providers(app)

        def req_historical_news(
            app, request_id, con_id, provider_codes, start, end, total_results, options,
        ):
            return self.req_historical_news(
                app, request_id, con_id, provider_codes, start, end, total_results, options,
            )

        return (
            patch.object(EClient, "connect", connect),
            patch.object(EClient, "isConnected", is_connected),
            patch.object(EClient, "run", run),
            patch.object(EClient, "disconnect", disconnect),
            patch.object(EClient, "reqContractDetails", req_contract_details),
            patch.object(EClient, "reqHistoricalData", req_historical_data),
            patch.object(EClient, "reqHistoricalTicks", req_historical_ticks),
            patch.object(EClient, "reqNewsProviders", req_news_providers),
            patch.object(EClient, "reqHistoricalNews", req_historical_news),
            patch.object(EClient, "placeOrder", side_effect=AssertionError("order path invoked")),
            patch.object(EClient, "reqPositions", side_effect=AssertionError("account API invoked")),
            patch.object(EClient, "reqOpenOrders", side_effect=AssertionError("order API invoked")),
            patch.object(EClient, "reqAllOpenOrders", side_effect=AssertionError("order API invoked")),
            patch.object(EClient, "reqAccountSummary", side_effect=AssertionError("account API invoked")),
            patch.object(EClient, "reqAccountUpdates", side_effect=AssertionError("account API invoked")),
            patch.object(EClient, "reqMktData", side_effect=AssertionError("live quote API invoked")),
            patch.object(EClient, "reqExecutions", side_effect=AssertionError("account API invoked")),
            patch.object(EClient, "reqGlobalCancel", side_effect=AssertionError("order API invoked")),
            patch.object(EClient, "cancelOrder", side_effect=AssertionError("order API invoked")),
            patch("microcap_ibkr_probe._PACING_PAUSE_SECONDS", 0),
        )


class IBKRProbeTests(unittest.TestCase):
    def test_missing_quotes_blocks_even_with_bars(self) -> None:
        result = summarize_observations(
            bars=[{"t": "2024-05-15T14:30:00Z"}],
            quotes=[],
            articles=[],
            provider_codes=["BRFG"],
            errors=[],
        )

        self.assertEqual(result["bars"]["status"], "observed")
        self.assertEqual(result["quotes"]["status"], "unavailable")
        self.assertIn("QUOTE_COVERAGE_UNAVAILABLE", result["reasons"])
        self.assertEqual(result["news"]["status"], "unverified")
        self.assertIn("NEWS_COVERAGE_UNVERIFIED", result["reasons"])

    def test_rejects_malformed_or_timezone_naive_observation_timestamps(self) -> None:
        for timestamp in ("not-a-time", "2024-05-15T14:30:00"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(CoverageError, "TIMESTAMP_INVALID"):
                    summarize_observations(
                        bars=[{"t": timestamp}],
                        quotes=[],
                        articles=[],
                        provider_codes=[],
                        errors=[],
                    )

    def test_news_articles_retain_provider_id_and_publication_timestamp(self) -> None:
        result = summarize_observations(
            bars=[],
            quotes=[],
            articles=[{
                "provider_code": "BRFG",
                "article_id": "news-42",
                "t": "2024-05-15T14:29:00Z",
            }],
            provider_codes=["BRFG"],
            errors=[],
        )

        self.assertEqual(result["news"]["status"], "observed")
        self.assertEqual(result["news"]["articles"][0]["article_id"], "news-42")
        self.assertEqual(result["news"]["articles"][0]["provider_code"], "BRFG")
        self.assertEqual(
            result["news"]["articles"][0]["t"], "2024-05-15T14:29:00Z"
        )

    def test_unique_contract_fetches_bars_ticks_and_only_returned_news_providers(self) -> None:
        driver = OfflineIBKRDriver()
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC",
                "2024-05-15T14:00:00Z",
                "2024-05-15T15:00:00Z",
                host="127.0.0.1",
                port=4002,
                client_id=77,
                timeout_seconds=0.02,
            )

        self.assertEqual(result["contract"]["con_id"], 12345)
        self.assertIsInstance(driver.app, EWrapper)
        self.assertIsInstance(driver.app, EClient)
        self.assertEqual(result["bars"]["status"], "observed")
        self.assertEqual(result["bars"]["count"], 2)
        self.assertEqual(result["bars"]["reasons"], [])
        self.assertEqual(
            result["bars"]["first_utc"], "2024-05-15T14:00:00Z"
        )
        self.assertEqual(
            result["bars"]["last_utc"], "2024-05-15T14:30:00Z"
        )
        self.assertEqual(result["quotes"]["status"], "observed")
        self.assertEqual(result["quotes"]["observations"][0]["bid"], 1.09)
        self.assertEqual(result["quotes"]["observations"][0]["ask"], 1.11)
        self.assertEqual(
            result["quotes"]["observations"][0]["t"], "2024-05-15T14:30:01Z"
        )
        tick_calls = [call for call in driver.calls if call[0] == "reqHistoricalTicks"]
        self.assertEqual(tick_calls, [("reqHistoricalTicks", (
            1005, 12345, "20240515 14:00:00 UTC", "", 1000, "BID_ASK", 0, False, [],
        ))])
        self.assertFalse(result["quotes"]["truncated"])
        self.assertEqual(
            result["news"]["articles"][0],
            {
                "t": "2024-05-15T14:29:00Z",
                "provider_code": "BRFG",
                "article_id": "article-1",
                "headline": "Company announces a product update",
                "time_raw": str(utc_epoch("2024-05-15T14:29:00Z")),
                "time_basis": "EPOCH_SECONDS_UTC",
            },
        )
        news_calls = [call for call in driver.calls if call[0] == "reqHistoricalNews"]
        self.assertEqual(len(news_calls), 1)
        self.assertEqual(
            news_calls[0],
            (
                "reqHistoricalNews",
                (1006, 12345, "BRFG", "", "2024-05-15 15:00:00", 50, []),
            ),
        )
        self.assertEqual(result["news"]["status"], "unverified")
        self.assertIn("NEWS_WINDOW_COVERAGE_UNVERIFIED", result["news"]["reasons"])
        self.assertEqual(result["news"]["count"], 1)
        self.assertEqual(
            [call[1][1] for call in driver.calls if call[0] == "reqHistoricalData"],
            ["1 min", "1 hour", "1 day"],
        )
        self.assertEqual([call[0] for call in driver.calls].count("disconnect"), 1)
        json.dumps(result)

    def test_bar_observation_reads_installed_bardata_epoch_date_as_utc(self) -> None:
        for interval, raw, expected in (
            ("1 min", "1790861580", "2026-10-01T13:33:00Z"),
            ("1 hour", "1790859600", "2026-10-01T13:00:00Z"),
        ):
            with self.subTest(interval=interval):
                bar = BarData()
                bar.date = raw
                bar.open, bar.high, bar.low, bar.close = 1.0, 1.2, 0.9, 1.1
                bar.volume = Decimal(500)
                observation = _bar_observation(bar, interval)
                self.assertEqual(observation["t"], expected)
                self.assertEqual(observation["interval"], interval)
                self.assertEqual(observation["volume"], 500.0)
                json.dumps(observation)

    def test_intraday_bar_rejects_date_only_naive_or_missing_dates_explicitly(self) -> None:
        for raw in ("20261001", "20261001 09:33:00", "", "17908615800", "abc"):
            with self.subTest(raw=raw):
                bar = BarData()
                bar.date = raw
                with self.assertRaisesRegex(CoverageError, "TIMESTAMP_INVALID"):
                    _bar_observation(bar, "1 min")

    def test_daily_bar_keeps_session_date_without_fabricating_an_instant(self) -> None:
        bar = BarData()
        bar.date = "20261001"
        bar.open, bar.high, bar.low, bar.close = 1.0, 1.2, 0.9, 1.1
        bar.volume = Decimal(700)
        observation = _daily_bar_observation(bar)
        self.assertEqual(observation["session_date"], "2026-10-01")
        self.assertEqual(observation["date_raw"], "20261001")
        self.assertEqual(observation["timestamp_basis"], "SESSION_DATE_ONLY")
        self.assertIs(observation["decision_time_verified"], False)
        self.assertNotIn("t", observation)
        self.assertEqual(observation["volume"], 700.0)

    def test_daily_bar_rejects_malformed_or_non_date_values_explicitly(self) -> None:
        for raw in ("2026-10-01", "20261301", "1790861580", "20261001 16:00:00", "", None):
            with self.subTest(raw=raw):
                bar = BarData()
                bar.date = raw
                with self.assertRaisesRegex(CoverageError, "TIMESTAMP_INVALID: IBKR daily bar"):
                    _daily_bar_observation(bar)

    def test_daily_bars_are_reported_separately_as_date_only_unverified(self) -> None:
        driver = OfflineIBKRDriver()
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=78, timeout_seconds=0.02,
            )

        self.assertTrue(all(bar["interval"] != "1 day" for bar in result["bars"]["observations"]))
        daily = result["daily_bars"]
        self.assertEqual(daily["status"], "observed")
        self.assertEqual(daily["count"], 1)
        self.assertEqual(daily["first_session_date"], "2024-05-15")
        self.assertEqual(daily["last_session_date"], "2024-05-15")
        self.assertIn("DAILY_BAR_DECISION_TIME_UNVERIFIED", daily["reasons"])
        self.assertEqual(daily["observations"][0]["timestamp_basis"], "SESSION_DATE_ONLY")
        self.assertNotIn("t", daily["observations"][0])
        self.assertEqual(result["bar_samples"], {"1 min": 1, "1 hour": 1, "1 day": 1})
        self.assertNotIn("DAILY_BAR_DECISION_TIME_UNVERIFIED", result["bars"]["reasons"])
        self.assertEqual(
            [e for e in result["errors"] if e.get("severity") != "notice"
             and e.get("reason") != "NEWS_WINDOW_COVERAGE_UNVERIFIED"],
            [],
        )
        json.dumps(result)

    def test_malformed_daily_date_is_explicit_and_does_not_fail_intraday_bars(self) -> None:
        driver = OfflineIBKRDriver(scenario="malformed_daily")
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=79, timeout_seconds=0.02,
            )

        self.assertEqual(result["bars"]["status"], "observed")
        self.assertEqual(result["bars"]["count"], 2)
        self.assertEqual(result["daily_bars"]["status"], "unavailable")
        daily_errors = [e for e in result["errors"] if e.get("channel") == "daily_bars"]
        self.assertEqual(len(daily_errors), 1)
        self.assertIn("TIMESTAMP_INVALID: IBKR daily bar", daily_errors[0]["error_message"])
        self.assertEqual(daily_errors[0]["time_raw_examples"], ["2024-05-1x"])
        self.assertIn("IBKR_TIMESTAMP_AMBIGUOUS", result["daily_bars"]["reasons"])

    def test_repeated_bar_timestamp_errors_are_aggregated_with_bounded_examples(self) -> None:
        driver = OfflineIBKRDriver(scenario="flood_ambiguous")
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=80, timeout_seconds=0.02,
            )

        bar_errors = [e for e in result["errors"] if e.get("channel") == "bars"]
        self.assertEqual(len(bar_errors), 1)
        self.assertEqual(bar_errors[0]["reason"], "IBKR_TIMESTAMP_AMBIGUOUS")
        self.assertEqual(bar_errors[0]["count"], 300)
        self.assertEqual(bar_errors[0]["time_raw_examples"], ["20240515 10:30:00"])
        self.assertEqual(result["bars"]["status"], "partial")
        self.assertIn("IBKR_TIMESTAMP_AMBIGUOUS", result["bars"]["reasons"])

    def test_ambiguous_or_unknown_contract_skips_dependent_requests(self) -> None:
        for scenario in ("ambiguous", "unknown", "delisted"):
            with self.subTest(scenario=scenario):
                driver = OfflineIBKRDriver(scenario=scenario)
                with ExitStack() as stack:
                    for patcher in driver.patches():
                        stack.enter_context(patcher)
                    result = probe_ibkr(
                        "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                        host="127.0.0.1", port=4002, client_id=78, timeout_seconds=0.02,
                    )

                self.assertEqual(result["contract"]["status"], "unresolved")
                self.assertIn("CONTRACT_UNRESOLVED", result["reasons"])
                self.assertFalse(any(name.startswith("reqHistorical") for name, _ in driver.calls))
                self.assertNotIn("reqNewsProviders", [name for name, _ in driver.calls])

    def test_empty_news_and_permission_or_pacing_errors_are_not_success(self) -> None:
        for scenario, reason in (
            ("empty_news", "NEWS_COVERAGE_UNVERIFIED"),
            ("permission", "IBKR_PERMISSION_DENIED"),
            ("pacing", "IBKR_PACING_VIOLATION"),
        ):
            with self.subTest(scenario=scenario):
                driver = OfflineIBKRDriver(scenario=scenario)
                with ExitStack() as stack:
                    for patcher in driver.patches():
                        stack.enter_context(patcher)
                    result = probe_ibkr(
                        "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                        host="127.0.0.1", port=4002, client_id=79, timeout_seconds=0.02,
                    )

                self.assertIn(reason, result["reasons"])
                if scenario == "empty_news":
                    self.assertEqual(result["news"]["status"], "unverified")
                if scenario == "permission":
                    self.assertEqual(result["bars"]["status"], "unavailable")
                    self.assertTrue(any(
                        error["request_id"] == 1002 and error["error_code"] == 354
                        for error in result["errors"]
                    ))

    def run_probe(self, scenario: str, *, timeout_seconds: float = 0.02):
        driver = OfflineIBKRDriver(scenario=scenario)
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=90,
                timeout_seconds=timeout_seconds,
            )
        return driver, result

    def test_tick_limit_is_reported_as_truncated_not_complete(self) -> None:
        driver, result = self.run_probe("tick_limit")

        tick_call = next(call for call in driver.calls if call[0] == "reqHistoricalTicks")
        self.assertEqual(tick_call[1][2], "20240515 14:00:00 UTC")
        self.assertEqual(tick_call[1][3], "")
        self.assertEqual(tick_call[1][4], 1000)
        self.assertTrue(result["quotes"]["truncated"])
        self.assertEqual(result["quotes"]["status"], "partial")
        self.assertIn("QUOTE_RESULTS_TRUNCATED", result["quotes"]["reasons"])
        self.assertIn("QUOTE_RESULTS_TRUNCATED", result["reasons"])
        self.assertEqual(result["quotes"]["count"], 1000)

    def test_ticks_are_filtered_to_half_open_window(self) -> None:
        for scenario, count in (("ticks_past_end", 1), ("tick_limit_past_end", 999)):
            with self.subTest(scenario=scenario):
                _, result = self.run_probe(scenario)

                self.assertFalse(result["quotes"]["truncated"])
                self.assertEqual(result["quotes"]["status"], "observed")
                self.assertEqual(result["quotes"]["count"], count)
                self.assertEqual(result["quotes"]["last_utc"], "2024-05-15T14:30:01Z")
                self.assertNotIn("QUOTE_RESULTS_TRUNCATED", result["reasons"])

    def test_explicit_news_timestamp_formats_keep_exact_provenance(self) -> None:
        for scenario, raw, basis in (
            ("news_epoch", str(utc_epoch("2024-05-15T14:29:00Z")), "EPOCH_SECONDS_UTC"),
            (
                "news_epoch_millis",
                str(utc_epoch("2024-05-15T14:29:00Z") * 1000),
                "EPOCH_MILLIS_UTC",
            ),
            ("news_utc_suffix", "20240515 14:29:00 UTC", "EXPLICIT_UTC"),
            ("news_iso_zoned", "2024-05-15T10:29:00-04:00", "EXPLICIT_TIMEZONE"),
        ):
            with self.subTest(scenario=scenario):
                _, result = self.run_probe(scenario)

                self.assertEqual(result["news"]["status"], "unverified")
                article = result["news"]["articles"][0]
                self.assertEqual(article["t"], "2024-05-15T14:29:00Z")
                self.assertEqual(article["time_raw"], raw)
                self.assertEqual(article["time_basis"], basis)
                self.assertEqual(article["provider_code"], "BRFG")
                self.assertEqual(article["article_id"], "article-1")

    def test_naive_news_timestamp_is_preserved_but_not_counted_as_a_catalyst(self) -> None:
        for scenario, raw in (
            ("news_naive", "20240515 14:29:00"),
            ("news_dashed", "2024-05-15 14:29:00.0"),
        ):
            with self.subTest(scenario=scenario):
                _, result = self.run_probe(scenario)

                self.assertEqual(result["news"]["status"], "unverified")
                self.assertEqual(result["news"]["count"], 0)
                self.assertEqual(result["news"]["articles"], [])
                self.assertIn("NEWS_TIMESTAMP_AMBIGUOUS", result["news"]["reasons"])
                ambiguous = next(
                    error for error in result["errors"]
                    if error["reason"] == "NEWS_TIMESTAMP_AMBIGUOUS"
                )
                self.assertEqual(ambiguous["time_raw"], raw)

    def test_news_records_are_filtered_to_half_open_request_window(self) -> None:
        for scenario in ("news_at_end", "news_past_end"):
            with self.subTest(scenario=scenario):
                _, result = self.run_probe(scenario)

                self.assertEqual(result["news"]["count"], 0)
                self.assertEqual(result["news"]["articles"], [])
                self.assertIsNone(result["news"]["first_utc"])

    def test_multiple_news_providers_are_plus_joined(self) -> None:
        driver, result = self.run_probe("multi_provider")

        news_call = next(call for call in driver.calls if call[0] == "reqHistoricalNews")
        self.assertEqual(news_call[1][2], "BRFG+DJNL")
        self.assertEqual(result["provider_codes"], ["BRFG", "DJNL"])
        self.assertEqual(result["news"]["articles"][0]["provider_code"], "DJNL")

    def test_ambiguous_ibkr_timestamps_are_rejected(self) -> None:
        for scenario, channel in (
            ("ambiguous_time", "bars"),
            ("ambiguous_news", "news"),
            ("malformed_news", "news"),
        ):
            with self.subTest(scenario=scenario):
                driver = OfflineIBKRDriver(scenario=scenario)
                with ExitStack() as stack:
                    for patcher in driver.patches():
                        stack.enter_context(patcher)
                    result = probe_ibkr(
                        "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                        host="127.0.0.1", port=4002, client_id=82, timeout_seconds=0.02,
                    )

                self.assertIn("IBKR_TIMESTAMP_AMBIGUOUS", result["reasons"])
                self.assertNotEqual(result[channel]["status"], "observed")

    def test_account_wide_pacing_error_is_not_misattributed_as_a_request_id(self) -> None:
        driver = OfflineIBKRDriver(scenario="global_pacing")
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=83, timeout_seconds=0.001,
            )

        global_error = next(error for error in result["errors"] if error["error_code"] == 420)
        self.assertEqual(global_error["request_id"], -1)
        self.assertEqual(global_error["channel"], "global")
        self.assertIn("IBKR_PACING_VIOLATION", result["reasons"])

    def test_global_pacing_error_wakes_active_waiter_without_timeout(self) -> None:
        app = _ProbeApp()
        request_id, event = app.next_request("bars")
        app._active_channel = "bars"

        app.error(-1, 0, 420, "Historical data pacing violation", "")

        self.assertTrue(event.is_set())
        error = app._errors[-1]
        self.assertEqual(error["request_id"], -1)
        self.assertEqual(error["channel"], "global")
        self.assertEqual(error["reason"], "IBKR_PACING_VIOLATION")
        self.assertNotEqual(request_id, -1)

    def test_global_pacing_stops_requests_immediately(self) -> None:
        driver, result = self.run_probe("global_pacing", timeout_seconds=60)

        self.assertNotIn("REQUEST_TIMEOUT", result["reasons"])
        self.assertIn("IBKR_PACING_VIOLATION", result["reasons"])
        names = [name for name, _ in driver.calls]
        self.assertEqual(names.count("reqHistoricalData"), 1)
        self.assertNotIn("reqHistoricalTicks", names)
        self.assertNotIn("reqNewsProviders", names)

    def test_network_loop_failure_stops_further_requests(self) -> None:
        driver = OfflineIBKRDriver(scenario="network_error")
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=86, timeout_seconds=0.001,
            )

        self.assertIn("IBKR_NETWORK_LOOP_FAILED", result["reasons"])
        self.assertEqual(
            [name for name, _ in driver.calls].count("reqHistoricalData"), 1
        )

    def test_connection_failure_is_returned_as_unavailable(self) -> None:
        driver = OfflineIBKRDriver(scenario="connect_error")
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=84, timeout_seconds=0.02,
            )

        self.assertIn("IBKR_CONNECTION_FAILED", result["reasons"])
        self.assertEqual(result["bars"]["status"], "unavailable")
        self.assertEqual([name for name, _ in driver.calls].count("disconnect"), 1)

    def test_contract_lookup_timeout_is_exposed_and_skips_data_requests(self) -> None:
        driver = OfflineIBKRDriver(scenario="contract_timeout")
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=85, timeout_seconds=0.001,
            )

        self.assertIn("REQUEST_TIMEOUT", result["reasons"])
        self.assertEqual(result["contract"]["status"], "unresolved")
        self.assertFalse(any(name.startswith("reqHistorical") for name, _ in driver.calls))

    def test_disconnect_during_request_is_unavailable_and_disconnects_client(self) -> None:
        driver = OfflineIBKRDriver(scenario="disconnect")
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=80, timeout_seconds=0.02,
            )

        self.assertIn("IBKR_DISCONNECTED", result["reasons"])
        self.assertEqual(result["bars"]["status"], "unavailable")
        self.assertEqual([name for name, _ in driver.calls].count("disconnect"), 1)

    def test_timeout_is_reported_instead_of_empty_success(self) -> None:
        driver = OfflineIBKRDriver(scenario="timeout")
        with ExitStack() as stack:
            for patcher in driver.patches():
                stack.enter_context(patcher)
            result = probe_ibkr(
                "ABC", "2024-05-15T14:00:00Z", "2024-05-15T15:00:00Z",
                host="127.0.0.1", port=4002, client_id=81, timeout_seconds=0.001,
            )

        self.assertIn("REQUEST_TIMEOUT", result["reasons"])
        self.assertNotEqual(result["bars"]["status"], "observed")


if __name__ == "__main__":
    unittest.main()
