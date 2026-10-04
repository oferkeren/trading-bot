import json
import os
import unittest
from datetime import timezone
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import microcap_history
from microcap_history import AlpacaHistory, CoverageError, parse_utc


START = "2024-01-01T00:00:00Z"
END = "2024-01-02T00:00:00Z"


def bar(timestamp: str) -> dict[str, object]:
    return {
        "t": timestamp,
        "o": 178.26,
        "h": 178.26,
        "l": 178.21,
        "c": 178.21,
        "v": 1118,
        "vw": 178.235733,
        "n": 65,
    }


def article(article_id: int = 1) -> dict[str, object]:
    return {
        "id": article_id,
        "created_at": "2024-01-01T12:00:00Z",
        "headline": "Company announces results",
        "source": "Example News",
        "symbols": ["ABCD"],
        "summary": "A sample historical article.",
    }


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class AlpacaHistoryTests(unittest.TestCase):
    def client(self) -> AlpacaHistory:
        return AlpacaHistory("test-key", "test-secret")

    def test_accepts_official_stock_bars_response(self) -> None:
        official = {
            "bars": {
                "AAPL": [{
                    "t": "2022-01-03T09:00:00Z",
                    "o": 178.26,
                    "h": 178.26,
                    "l": 178.21,
                    "c": 178.21,
                    "v": 1118,
                    "vw": 178.235733,
                    "n": 65,
                }]
            },
            "next_page_token": None,
        }
        with patch.object(microcap_history, "urlopen", return_value=FakeResponse(official)):
            records = self.client().fetch_bars(
                ["AAPL"], "2022-01-03T00:00:00Z", "2022-01-04T00:00:00Z"
            )
        self.assertEqual(records[0]["symbol"], "AAPL")
        self.assertEqual(records[0]["t"], "2022-01-03T09:00:00Z")
        self.assertEqual(records[0]["v"], 1118)

    def test_follows_next_page_token_even_when_first_page_is_short(self) -> None:
        responses = iter(
            [
                FakeResponse({"bars": {"ABCD": [bar("2024-01-01T14:30:00Z")]}, "next_page_token": "next"}),
                FakeResponse({"bars": {"ABCD": [bar("2024-01-01T14:31:00Z")]}}),
            ]
        )
        with patch.object(microcap_history, "urlopen", side_effect=lambda *args, **kwargs: next(responses)) as opener:
            records = self.client().fetch_bars(["ABCD"], START, END)

        self.assertEqual(len(records), 2)
        self.assertEqual([record["symbol"] for record in records], ["ABCD", "ABCD"])
        self.assertEqual(opener.call_count, 2)
        first_query = parse_qs(urlparse(opener.call_args_list[0].args[0].full_url).query)
        self.assertEqual(first_query["feed"], ["iex"])
        self.assertEqual(first_query["adjustment"], ["split"])
        self.assertEqual(first_query["limit"], ["10000"])
        second_query = parse_qs(urlparse(opener.call_args_list[1].args[0].full_url).query)
        self.assertEqual(second_query["page_token"], ["next"])

    def test_bars_include_request_and_fetch_provenance(self) -> None:
        with patch.object(
            microcap_history,
            "urlopen",
            return_value=FakeResponse({"bars": {"ABCD": [bar("2024-01-01T14:30:00Z")]}}),
        ) as opener:
            records = self.client().fetch_bars(["ABCD"], START, END)

        record = records[0]
        self.assertEqual(record["request_feed"], "iex")
        self.assertEqual(record["request_adjustment"], "split")
        self.assertEqual(record["symbol"], "ABCD")
        self.assertEqual(parse_utc(record["fetched_at"]).tzinfo, timezone.utc)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Apca-api-key-id"), "test-key")
        self.assertEqual(request.get_header("Apca-api-secret-key"), "test-secret")

    def test_missing_credentials_fail_closed(self) -> None:
        for environment in (
            {},
            {"APCA_API_KEY_ID": "test-key"},
            {"APCA_API_SECRET_KEY": "test-secret"},
        ):
            with self.subTest(environment=tuple(environment)), patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaisesRegex(CoverageError, "CREDENTIALS"):
                    AlpacaHistory.from_environment()

    def test_multi_symbol_bars_are_flattened_across_pages(self) -> None:
        responses = iter([
            FakeResponse({"bars": {
                "AAPL": [bar("2024-01-01T14:30:00Z")],
                "MSFT": [bar("2024-01-01T14:31:00Z")],
            }, "next_page_token": "more"}),
            FakeResponse({"bars": {"MSFT": [bar("2024-01-01T14:32:00Z")]}}),
        ])
        with patch.object(microcap_history, "urlopen", side_effect=lambda *a, **kw: next(responses)):
            records = self.client().fetch_bars(["AAPL", "MSFT"], START, END)
        self.assertEqual([record["symbol"] for record in records], ["AAPL", "MSFT", "MSFT"])

    def test_bar_payload_rejects_unrequested_symbols_and_malformed_groups(self) -> None:
        for groups in ({"OTHER": [bar("2024-01-01T14:30:00Z")]},
                       {"ABCD": {"t": "2024-01-01T14:30:00Z"}},
                       {"ABCD": [None]}, {"": []}):
            with self.subTest(groups=groups), patch.object(
                microcap_history, "urlopen", return_value=FakeResponse({"bars": groups})
            ):
                with self.assertRaisesRegex(CoverageError, "RESPONSE_INVALID"):
                    self.client().fetch_bars(["ABCD"], START, END)

    def test_http_errors_report_status_endpoint_and_retry_after_without_credentials(self) -> None:
        for status, retry_after in ((401, None), (403, None), (429, "17")):
            headers = Message()
            if retry_after is not None:
                headers["Retry-After"] = retry_after
            error = HTTPError(
                "https://data.alpaca.markets/v2/stocks/bars",
                status,
                "request failed",
                headers,
                BytesIO(b""),
            )
            with self.subTest(status=status), patch.object(microcap_history, "urlopen", side_effect=error):
                with self.assertRaises(CoverageError) as raised:
                    self.client().fetch_bars(["ABCD"], START, END)

                message = str(raised.exception)
                self.assertIn(str(status), message)
                self.assertIn("/v2/stocks/bars", message)
                self.assertIn("HTTPError", message)
                self.assertNotIn("test-secret", message)
                if status == 429:
                    self.assertIn("Retry-After: 17", message)

    def test_rejects_malformed_or_naive_timestamps(self) -> None:
        for value in ("not-a-timestamp", "2024-01-01T12:00:00"):
            with self.subTest(value=value), self.assertRaisesRegex(CoverageError, "TIMESTAMP"):
                parse_utc(value)

    def test_rejects_unsupported_feed(self) -> None:
        with self.assertRaisesRegex(CoverageError, "FEED"):
            self.client().fetch_bars(["ABCD"], START, END, feed="unsupported")

    def test_news_requires_article_id_and_timestamp(self) -> None:
        for missing_key in ("id", "created_at"):
            payload_article = article()
            del payload_article[missing_key]
            payload = {"news": [payload_article]}
            with self.subTest(missing_key=missing_key), patch.object(
                microcap_history, "urlopen", return_value=FakeResponse(payload)
            ):
                with self.assertRaisesRegex(CoverageError, "NEWS"):
                    self.client().fetch_news(["ABCD"], START, END)

    def test_news_deduplicates_article_ids(self) -> None:
        duplicate = article()
        with patch.object(
            microcap_history,
            "urlopen",
            return_value=FakeResponse({"news": [article(), duplicate]}),
        ):
            records = self.client().fetch_news(["ABCD"], START, END)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], 1)
        self.assertEqual(records[0]["request_start"], START)
        self.assertEqual(records[0]["request_end"], END)
        self.assertEqual(parse_utc(records[0]["fetched_at"]).tzinfo, timezone.utc)

    def test_empty_news_does_not_establish_historical_news_coverage(self) -> None:
        with self.assertRaisesRegex(CoverageError, "NEWS_COVERAGE"):
            AlpacaHistory.require_coverage(
                bars=[{"t": "2024-01-01T15:00:00Z"}],
                news=[],
                start=START,
                end=END,
            )

    def test_sparse_bars_remain_observations_not_a_claim_of_session_coverage(self) -> None:
        report = AlpacaHistory.require_coverage(
            bars=[
                {"symbol": "ABCD", "t": "2024-01-01T14:30:00Z"},
                {"symbol": "ABCD", "t": "2024-01-01T20:59:00Z"},
            ],
            news=[article()],
            start=START,
            end=END,
        )

        self.assertEqual(len(report.bar_timestamps), 2)
        self.assertEqual(report.news_article_count, 1)

    def test_coverage_reports_internal_minute_gaps_and_unobserved_weekday_sessions(self) -> None:
        report = AlpacaHistory.require_coverage(
            bars=[
                {"symbol": "ABCD", "t": "2024-01-02T14:30:00Z"},
                {"symbol": "ABCD", "t": "2024-01-02T14:32:00Z"},
                {"symbol": "EFGH", "t": "2024-01-03T14:30:00Z"},
            ],
            news=[{**article(), "created_at": "2024-01-02T12:00:00Z"}],
            start="2024-01-02T00:00:00Z",
            end="2024-01-05T00:00:00Z",
            symbols=["ABCD", "EFGH"],
            timeframe="1Min",
        )
        self.assertEqual(
            [(gap.symbol, gap.day.isoformat(), gap.timeframe, gap.timestamps)
             for gap in report.missing_minutes],
            [("ABCD", "2024-01-02", "1Min", (parse_utc("2024-01-02T14:31:00Z"),))],
        )
        self.assertEqual(
            {(gap.symbol, gap.day.isoformat(), gap.timeframe) for gap in report.unobserved_sessions},
            {("EFGH", "2024-01-02", "1Min"), ("ABCD", "2024-01-03", "1Min"),
             ("ABCD", "2024-01-04", "1Min"), ("EFGH", "2024-01-04", "1Min")},
        )

    def test_five_minute_gaps_do_not_cross_days_or_count_weekends(self) -> None:
        report = AlpacaHistory.require_coverage(
            bars=[
                {"symbol": "ABCD", "t": "2024-01-05T14:30:00Z"},
                {"symbol": "ABCD", "t": "2024-01-05T14:40:00Z"},
                {"symbol": "ABCD", "t": "2024-01-08T14:30:00Z"},
            ],
            news=[{**article(), "created_at": "2024-01-05T12:00:00Z"}],
            start="2024-01-05T00:00:00Z",
            end="2024-01-09T00:00:00Z",
            timeframe="5Min",
        )
        self.assertEqual(report.missing_minutes[0].timestamps,
                         (parse_utc("2024-01-05T14:35:00Z"),))
        self.assertEqual(report.unobserved_sessions, ())

    def test_rejects_malformed_bar_records(self) -> None:
        malformed = {"t": "2024-01-01T14:30:00Z"}
        with patch.object(
            microcap_history,
            "urlopen",
            return_value=FakeResponse({"bars": {"ABCD": [malformed]}}),
        ):
            with self.assertRaisesRegex(CoverageError, "BAR"):
                self.client().fetch_bars(["ABCD"], START, END)

    def test_repeated_page_token_fails_closed(self) -> None:
        payload = {"bars": {"ABCD": [bar("2024-01-01T14:30:00Z")]}, "next_page_token": "again"}
        with patch.object(microcap_history, "urlopen", return_value=FakeResponse(payload)):
            with self.assertRaisesRegex(CoverageError, "PAGINATION"):
                self.client().fetch_bars(["ABCD"], START, END)



def quote(timestamp: str, bid: float = 2.00, ask: float = 2.02) -> dict[str, object]:
    return {
        "ap": ask, "as": 3, "ax": "V", "bp": bid, "bs": 2, "bx": "V",
        "c": ["R"], "t": timestamp, "z": "C",
    }


class AlpacaQuoteTests(unittest.TestCase):
    def client(self) -> AlpacaHistory:
        return AlpacaHistory("test-key", "test-secret")

    def test_fetches_official_quotes_response_from_data_api(self) -> None:
        official = {
            "quotes": {"ABCD": [quote("2024-01-01T14:30:00.028160898Z")]},
            "next_page_token": None,
        }
        with patch.object(microcap_history, "urlopen", return_value=FakeResponse(official)) as opener:
            records = self.client().fetch_quotes(["ABCD"], START, END)
        url = urlparse(opener.call_args.args[0].full_url)
        self.assertEqual(url.netloc, "data.alpaca.markets")
        self.assertEqual(url.path, "/v2/stocks/quotes")
        query = parse_qs(url.query)
        self.assertEqual(query["feed"], ["iex"])
        self.assertNotIn("adjustment", query)
        record = records[0]
        self.assertEqual(record["symbol"], "ABCD")
        self.assertEqual(record["bid"], 2.00)
        self.assertEqual(record["ask"], 2.02)
        self.assertEqual(record["request_feed"], "iex")
        self.assertEqual(record["price_basis"], "raw")
        self.assertEqual(parse_utc(record["fetched_at"]).tzinfo, timezone.utc)

    def test_nanosecond_quote_time_rounds_up_so_it_is_never_known_early(self) -> None:
        payload = {"quotes": {"ABCD": [
            quote("2024-01-01T14:30:00.000000001Z"),
            quote("2024-01-01T09:31:00.5-05:00"),
            quote("2024-01-01T14:32:00Z"),
        ]}, "next_page_token": None}
        with patch.object(microcap_history, "urlopen", return_value=FakeResponse(payload)):
            records = self.client().fetch_quotes(["ABCD"], START, END)
        self.assertEqual(
            [record["t"] for record in records],
            ["2024-01-01T14:30:00.000001Z", "2024-01-01T14:31:00.500000Z", "2024-01-01T14:32:00Z"],
        )

    def test_quotes_follow_pagination_and_reject_unrequested_symbols(self) -> None:
        responses = iter([
            FakeResponse({"quotes": {"ABCD": [quote("2024-01-01T14:30:00Z")]}, "next_page_token": "p2"}),
            FakeResponse({"quotes": {"ABCD": [quote("2024-01-01T14:30:01Z")]}, "next_page_token": None}),
        ])
        with patch.object(microcap_history, "urlopen", side_effect=lambda *a, **k: next(responses)) as opener:
            records = self.client().fetch_quotes(["ABCD"], START, END)
        self.assertEqual(len(records), 2)
        self.assertEqual(parse_qs(urlparse(opener.call_args.args[0].full_url).query)["page_token"], ["p2"])
        with patch.object(microcap_history, "urlopen",
                          return_value=FakeResponse({"quotes": {"OTHER": [quote("2024-01-01T14:30:00Z")]}})):
            with self.assertRaisesRegex(CoverageError, "RESPONSE_INVALID"):
                self.client().fetch_quotes(["ABCD"], START, END)

    def test_malformed_quotes_fail_closed(self) -> None:
        for bad in ({"t": "2024-01-01T14:30:00Z", "ap": 2.0},
                    quote("2024-01-01T14:30:00"),
                    quote("2024-01-01T14:30:00Z", bid=float("nan")),
                    quote("2024-01-01T14:30:00Z", ask=True),
                    quote("2024-01-01T14:30:00Z", bid=-1),
                    quote("2023-12-31T14:30:00Z")):
            payload = json.loads(json.dumps({"quotes": {"ABCD": [bad]}}, allow_nan=True))
            with self.subTest(bad=bad), patch.object(
                microcap_history, "urlopen", return_value=FakeResponse(payload)
            ):
                with self.assertRaisesRegex(CoverageError, "QUOTE|TIMESTAMP"):
                    self.client().fetch_quotes(["ABCD"], START, END)

    def test_quote_entitlement_errors_are_explicit_quote_coverage_failures(self) -> None:
        for status in (401, 403):
            error = HTTPError("https://data.alpaca.markets/v2/stocks/quotes", status, "x", Message(), BytesIO(b""))
            with self.subTest(status=status), patch.object(microcap_history, "urlopen", side_effect=error):
                with self.assertRaises(CoverageError) as raised:
                    self.client().fetch_quotes(["ABCD"], START, END)
            self.assertIn("QUOTE_COVERAGE_UNAVAILABLE", str(raised.exception))
            self.assertIn(str(status), str(raised.exception))
            self.assertNotIn("test-secret", str(raised.exception))
            self.assertNotIn("test-key", str(raised.exception))

    def test_quotes_reject_non_iex_feed(self) -> None:
        with self.assertRaisesRegex(CoverageError, "FEED"):
            self.client().fetch_quotes(["ABCD"], START, END, feed="sip")


class ExclusiveEndAndAdjustmentTests(unittest.TestCase):
    def client(self) -> AlpacaHistory:
        return AlpacaHistory("test-key", "test-secret")

    def test_requests_exclude_the_documented_inclusive_end_instant(self) -> None:
        cases = (
            ("fetch_bars", {"bars": {}}),
            ("fetch_news", {"news": []}),
            ("fetch_quotes", {"quotes": {}}),
        )
        for method, payload in cases:
            with self.subTest(method=method), patch.object(
                microcap_history, "urlopen", return_value=FakeResponse(payload)
            ) as opener:
                getattr(self.client(), method)(["ABCD"], START, END)
            query = parse_qs(urlparse(opener.call_args.args[0].full_url).query)
            self.assertEqual(query["end"], ["2024-01-01T23:59:59.999999Z"])

    def test_raw_adjustment_can_be_requested_for_price_basis_checks(self) -> None:
        with patch.object(microcap_history, "urlopen",
                          return_value=FakeResponse({"bars": {"ABCD": [bar("2024-01-01T14:30:00Z")]}})) as opener:
            records = self.client().fetch_bars(["ABCD"], START, END, adjustment="raw")
        self.assertEqual(parse_qs(urlparse(opener.call_args.args[0].full_url).query)["adjustment"], ["raw"])
        self.assertEqual(records[0]["request_adjustment"], "raw")
        with self.assertRaisesRegex(CoverageError, "ADJUSTMENT"):
            self.client().fetch_bars(["ABCD"], START, END, adjustment="all")


if __name__ == "__main__":
    unittest.main()
