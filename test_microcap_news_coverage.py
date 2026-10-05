import unittest
from traceback import format_exception
from urllib.error import URLError

from microcap_history import CoverageError, parse_utc
from microcap_news_coverage import observed_news


START = "2024-01-01T00:00:00Z"
END = "2024-01-02T00:00:00Z"


class BoundedHistory:
    def __init__(self, articles):
        self.articles = articles
        self.calls = []

    def fetch_news(self, symbols, start, end, *, max_pages):
        self.calls.append((symbols, start, end, max_pages))
        return self.articles


class FakeHistory:
    def __init__(self, articles):
        self.articles = articles
        self.calls = []

    def fetch_news(self, symbols, start, end):
        self.calls.append((symbols, start, end))
        return self.articles


def article(**overrides):
    record = {
        "id": 42,
        "created_at": "2024-01-01T12:00:00Z",
        "headline": "SORA reports results",
        "source": "Example News",
        "symbols": ["SORA"],
        "summary": "An announcement.",
        "fetched_at": "2024-01-03T12:00:00Z",
        "request_start": START,
        "request_end": END,
    }
    record.update(overrides)
    return record


class ObservedNewsTests(unittest.TestCase):
    def test_one_article_retains_evidence_without_claiming_provider_completeness(self):
        client = FakeHistory([article()])

        result = observed_news(client, "SORA", START, END)

        self.assertEqual(client.calls, [(["SORA"], START, END)])
        self.assertEqual(result["symbol"], "SORA")
        self.assertEqual(result["request_start"], START)
        self.assertEqual(result["request_end"], END)
        self.assertEqual(result["coverage_status"], "UNVERIFIED_BY_PROVIDER")
        self.assertEqual(result["completeness"], "UNVERIFIED_BY_PROVIDER")
        self.assertEqual(result["article_count"], 1)
        self.assertEqual(result["articles"], [{
            **article(),
            "data_source": "alpaca_news",
        }])
        self.assertNotIn("catalyst", result)
        self.assertNotIn("catalyst", result["articles"][0])

    def test_zero_articles_explicitly_leaves_news_coverage_unverified(self):
        result = observed_news(FakeHistory([]), "SORA", START, END)

        self.assertEqual(result["article_count"], 0)
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["coverage_status"], "UNVERIFIED_BY_PROVIDER")
        self.assertEqual(result["completeness"], "UNVERIFIED_BY_PROVIDER")
        self.assertEqual(result["coverage_reason"], "NEWS_COVERAGE_UNVERIFIED")

    def test_start_is_inclusive_and_end_is_exclusive(self):
        first = article(created_at=START)
        self.assertEqual(observed_news(FakeHistory([first]), "SORA", START, END)["article_count"], 1)
        with self.assertRaisesRegex(CoverageError, "NEWS_OUT_OF_RANGE"):
            observed_news(FakeHistory([article(created_at=END)]), "SORA", START, END)

    def test_article_after_decision_or_fetch_cannot_qualify(self):
        with self.assertRaisesRegex(CoverageError, "NEWS_OUT_OF_RANGE"):
            observed_news(FakeHistory([article(created_at=END)]), "SORA", START, END)
        with self.assertRaisesRegex(CoverageError, "NEWS_AFTER_FETCH"):
            observed_news(
                FakeHistory([article(fetched_at="2024-01-01T11:59:59Z")]),
                "SORA", START, END,
            )

    def test_wrong_company_headline_does_not_imply_catalyst(self):
        result = observed_news(
            FakeHistory([article(headline="Another company announces merger")]),
            "SORA", START, END,
        )
        self.assertEqual(result["articles"][0]["headline"], "Another company announces merger")
        self.assertNotIn("catalyst", result)
        self.assertNotIn("catalyst", result["articles"][0])

    def test_mismatched_symbol_tags_fail_explicitly(self):
        for tags in ([], ["OTHER"], "SORA", None):
            with self.subTest(tags=tags), self.assertRaisesRegex(CoverageError, "NEWS_SYMBOL_MISMATCH"):
                observed_news(FakeHistory([article(symbols=tags)]), "SORA", START, END)

    def test_identical_duplicate_ids_are_kept_once(self):
        result = observed_news(FakeHistory([article(), article()]), "SORA", START, END)
        self.assertEqual(result["article_count"], 1)
        self.assertEqual([item["id"] for item in result["articles"]], [42])

    def test_conflicting_duplicate_ids_fail_explicitly(self):
        with self.assertRaisesRegex(CoverageError, "NEWS_DUPLICATE_CONFLICT"):
            observed_news(
                FakeHistory([article(), article(headline="Different headline")]),
                "SORA", START, END,
            )

    def test_missing_or_unknown_publication_time_fails_explicitly(self):
        for value in (None, "", "unknown"):
            with self.subTest(value=value), self.assertRaises(CoverageError):
                observed_news(FakeHistory([article(created_at=value)]), "SORA", START, END)
        record = article()
        del record["created_at"]
        with self.assertRaisesRegex(CoverageError, "NEWS_PUBLICATION_UNKNOWN"):
            observed_news(FakeHistory([record]), "SORA", START, END)

    def test_timezone_must_be_explicit_on_all_timestamps(self):
        for field in ("created_at", "fetched_at"):
            with self.subTest(field=field), self.assertRaisesRegex(CoverageError, "TIMESTAMP_INVALID"):
                observed_news(
                    FakeHistory([article(**{field: "2024-01-01T12:00:00"})]),
                    "SORA", START, END,
                )
        with self.assertRaisesRegex(CoverageError, "TIMESTAMP_INVALID"):
            observed_news(FakeHistory([]), "SORA", "2024-01-01T00:00:00", END)

    def test_normalizes_offsets_to_utc_and_preserves_requested_window(self):
        result = observed_news(
            FakeHistory([article(
                created_at="2024-01-01T14:00:00+02:00",
                fetched_at="2024-01-03T14:00:00+02:00",
            )]), "SORA", START, END,
        )
        self.assertEqual(result["articles"][0]["created_at"], "2024-01-01T12:00:00Z")
        self.assertEqual(result["articles"][0]["fetched_at"], "2024-01-03T12:00:00Z")
        self.assertEqual(parse_utc(result["request_start"]), parse_utc(START))

    def test_provider_error_is_unavailable_not_zero(self):
        class BrokenHistory:
            def fetch_news(self, symbols, start, end):
                raise CoverageError("HTTP_ERROR: provider returned 503")

        with self.assertRaisesRegex(CoverageError, "NEWS_COVERAGE_UNAVAILABLE"):
            observed_news(BrokenHistory(), "SORA", START, END)

    def test_transport_failures_are_unavailable_without_exposing_exception_details(self):
        class BrokenHistory:
            def __init__(self, failure):
                self.failure = failure

            def fetch_news(self, symbols, start, end):
                raise self.failure

        secret = "private-api-token"
        for failure in (TimeoutError(secret), URLError(secret), ConnectionError(secret)):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(CoverageError) as raised:
                    observed_news(BrokenHistory(failure), "SORA", START, END)
                self.assertIn("NEWS_COVERAGE_UNAVAILABLE", str(raised.exception))
                self.assertIn(type(failure).__name__, str(raised.exception))
                self.assertNotIn(secret, "".join(format_exception(raised.exception)))

    def test_unexpected_client_error_is_not_misclassified_as_unavailable(self):
        class BrokenHistory:
            def fetch_news(self, symbols, start, end):
                raise ValueError("invalid client state")

        with self.assertRaisesRegex(ValueError, "invalid client state"):
            observed_news(BrokenHistory(), "SORA", START, END)

    def test_max_pages_is_forwarded_only_when_requested(self):
        client = BoundedHistory([article()])
        result = observed_news(client, "SORA", START, END, max_pages=1)
        self.assertEqual(client.calls, [(["SORA"], START, END, 1)])
        self.assertEqual(result["article_count"], 1)

    def test_truncated_pagination_is_unavailable_not_partial(self):
        class TruncatedHistory:
            def fetch_news(self, symbols, start, end, *, max_pages):
                raise CoverageError("PAGINATION_TRUNCATED: next page remains")

        with self.assertRaisesRegex(CoverageError,
                                    "NEWS_COVERAGE_UNAVAILABLE: PAGINATION_TRUNCATED"):
            observed_news(TruncatedHistory(), "SORA", START, END, max_pages=1)

    def test_mismatched_requested_window_fails(self):
        with self.assertRaisesRegex(CoverageError, "NEWS_REQUEST_MISMATCH"):
            observed_news(
                FakeHistory([article(request_end="2024-01-03T00:00:00Z")]),
                "SORA", START, END,
            )


if __name__ == "__main__":
    unittest.main()
