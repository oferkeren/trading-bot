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
        "o": 1,
        "h": 2,
        "l": 0.5,
        "c": 1.5,
        "v": 100,
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

    def test_follows_next_page_token_even_when_first_page_is_short(self) -> None:
        responses = iter(
            [
                FakeResponse({"bars": [bar("2024-01-01T14:30:00Z")], "next_page_token": "next"}),
                FakeResponse({"bars": [bar("2024-01-01T14:31:00Z")]}),
            ]
        )
        with patch.object(microcap_history, "urlopen", side_effect=lambda *args, **kwargs: next(responses)) as opener:
            records = self.client().fetch_bars(["ABCD"], START, END)

        self.assertEqual(len(records), 2)
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
            return_value=FakeResponse({"bars": [bar("2024-01-01T14:30:00Z")]}),
        ) as opener:
            records = self.client().fetch_bars(["ABCD"], START, END)

        record = records[0]
        self.assertEqual(record["request_feed"], "iex")
        self.assertEqual(record["request_adjustment"], "split")
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
                {"t": "2024-01-01T14:30:00Z"},
                {"t": "2024-01-01T20:59:00Z"},
            ],
            news=[article()],
            start=START,
            end=END,
        )

        self.assertEqual(len(report.bar_timestamps), 2)
        self.assertEqual(report.news_article_count, 1)

    def test_rejects_malformed_bar_records(self) -> None:
        malformed = {"t": "2024-01-01T14:30:00Z"}
        with patch.object(
            microcap_history,
            "urlopen",
            return_value=FakeResponse({"bars": [malformed]}),
        ):
            with self.assertRaisesRegex(CoverageError, "BAR"):
                self.client().fetch_bars(["ABCD"], START, END)

    def test_repeated_page_token_fails_closed(self) -> None:
        payload = {"bars": [bar("2024-01-01T14:30:00Z")], "next_page_token": "again"}
        with patch.object(microcap_history, "urlopen", return_value=FakeResponse(payload)):
            with self.assertRaisesRegex(CoverageError, "PAGINATION"):
                self.client().fetch_bars(["ABCD"], START, END)


if __name__ == "__main__":
    unittest.main()
