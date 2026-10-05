import http.client
import io
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from unittest.mock import patch

import microcap_marketaux_probe as mx
from microcap_history import CoverageError
from microcap_marketaux_probe import MarketauxProbe

TOKEN = "sekret-token-XYZ123"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 5, tzinfo=timezone.utc)


def article(uuid="a-1", published="2026-10-02T14:30:00.000000Z", symbol="ABCD", **over):
    item = {
        "uuid": uuid,
        "title": "ABCD wins contract",
        "url": "https://example.com/abcd",
        "published_at": published,
        "source": "example.com",
        "entities": [
            {
                "symbol": symbol,
                "name": "ABCD Corp",
                "exchange": "NASDAQ",
                "exchange_long": "NASDAQ Stock Exchange",
                "country": "us",
                "type": "equity",
                "industry": "Technology",
                "match_score": 20.5,
                "sentiment_score": 0.4,
                "highlights": [],
            }
        ],
        "similar": [],
    }
    item.update(over)
    return item


def page(data, *, found=None, limit=3, page_no=1, returned=None):
    return {
        "meta": {
            "found": len(data) if found is None else found,
            "returned": len(data) if returned is None else returned,
            "limit": limit,
            "page": page_no,
        },
        "data": data,
    }


class FakeResponse(io.BytesIO):
    def __init__(self, payload, *, status=200, url=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        super().__init__(body)
        self.status = status
        self._url = url or f"https://api.marketaux.com/v1/news/all?api_token={TOKEN}"

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


class DripResponse:
    """Delivers one byte per read and advances a fake monotonic clock (no real sleeps)."""

    def __init__(self, clock, *, step=1.0, total=10_000, use_read1=True):
        self.clock = clock
        self.step = step
        self.remaining = total
        self.requested = []
        self.socket_timeouts = []
        if use_read1:
            self.read1 = self._read
        sock = type("Sock", (), {"settimeout": lambda _s, value: self.socket_timeouts.append(value)})()
        raw = type("Raw", (), {"_sock": sock})()
        self.fp = type("Fp", (), {"raw": raw})()

    def _read(self, size=-1):
        self.requested.append(size)
        self.clock.now += self.step
        if self.remaining <= 0:
            return b""
        self.remaining -= 1
        return b" "

    def read(self, size=-1):
        return self._read(size)

    def geturl(self):
        return f"https://api.marketaux.com/v1/news/all?api_token={TOKEN}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=None):
    payload = json.dumps(body or {"error": {"code": "x", "message": f"bad {TOKEN}"}}).encode()
    return urllib.error.HTTPError(
        f"https://api.marketaux.com/v1/news/all?api_token={TOKEN}",
        code,
        f"status {code} {TOKEN}",
        {},
        io.BytesIO(payload),
    )


class MarketauxProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = MarketauxProbe(TOKEN, clock=lambda: NOW)

    def run_pages(self, responses, **kwargs):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append((request, timeout))
            item = responses[len(calls) - 1]
            if isinstance(item, BaseException):
                raise item
            return item

        with patch.object(mx, "urlopen", side_effect=fake_urlopen) as mocked:
            result = self.probe.fetch_articles("ABCD", START, END, **kwargs)
        return result, calls, mocked

    def assert_reason(self, ctx, reason):
        self.assertTrue(str(ctx.exception).startswith(reason), str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)

    def expect_failure(self, responses, reason, **kwargs):
        with patch.object(mx, "urlopen", side_effect=responses) as mocked:
            with self.assertRaises(CoverageError) as ctx:
                self.probe.fetch_articles("ABCD", START, END, **kwargs)
        self.assert_reason(ctx, reason)
        return mocked

    # --- credentials -------------------------------------------------------
    def test_missing_token_fails_before_http(self):
        for env in ({"MARKETAUX_API_TOKEN": ""}, {"MARKETAUX_API_TOKEN": "   "}):
            with patch.dict(os.environ, env), patch.object(mx, "urlopen") as mocked:
                with self.assertRaises(CoverageError) as ctx:
                    MarketauxProbe.from_environment()
                self.assertEqual(str(ctx.exception), "MARKETAUX_TOKEN_MISSING")
                mocked.assert_not_called()

    def test_absent_token_fails(self):
        env = {k: v for k, v in os.environ.items() if k != "MARKETAUX_API_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(CoverageError) as ctx:
                MarketauxProbe.from_environment()
        self.assertEqual(str(ctx.exception), "MARKETAUX_TOKEN_MISSING")

    def test_from_environment_reads_token_without_exposing_it(self):
        with patch.dict(os.environ, {"MARKETAUX_API_TOKEN": TOKEN}):
            probe = MarketauxProbe.from_environment()
        self.assertNotIn(TOKEN, repr(probe))
        self.assertNotIn(TOKEN, str(probe))

    # --- request contract --------------------------------------------------
    def test_request_targets_documented_endpoint_with_utc_window(self):
        result, calls, _ = self.run_pages([FakeResponse(page([article()]))])
        request, timeout = calls[0]
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "api.marketaux.com", "/v1/news/all"))
        query = urllib.parse.parse_qs(url.query)
        self.assertEqual(query["symbols"], ["ABCD"])
        self.assertEqual(query["filter_entities"], ["true"])
        self.assertEqual(query["must_have_entities"], ["true"])
        self.assertEqual(query["group_similar"], ["false"])
        self.assertEqual(query["published_after"], ["2026-10-01T00:00:00"])
        self.assertEqual(query["published_before"], ["2026-10-05T00:00:00"])
        self.assertEqual(query["page"], ["1"])
        self.assertEqual(query["api_token"], [TOKEN])
        self.assertNotIn("limit", query)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNotNone(timeout)
        self.assertLessEqual(timeout, 30)
        dumped = json.dumps(result)
        self.assertNotIn(TOKEN, dumped)
        self.assertNotIn("api_token", dumped)

    def test_valid_article_preserves_identity_time_source_and_id(self):
        result, _, _ = self.run_pages([FakeResponse(page([article()]))])
        self.assertEqual(result["status"], "NEWS_ARTICLES_OBSERVED")
        self.assertEqual(result["provider"], "marketaux")
        self.assertEqual(result["request_start"], "2026-10-01T00:00:00Z")
        self.assertEqual(result["request_end"], "2026-10-05T00:00:00Z")
        self.assertEqual(result["fetched_at"], "2026-10-05T12:00:00Z")
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["collected"], 1)
        self.assertEqual(result["pages"], [{"page": 1, "found": 1, "returned": 1, "limit": 3}])
        (record,) = result["articles"]
        self.assertEqual(record["id"], "a-1")
        self.assertEqual(record["published_at"], "2026-10-02T14:30:00Z")
        self.assertEqual(record["source"], "example.com")
        self.assertEqual(record["entity"]["symbol"], "ABCD")
        self.assertEqual(record["entity"]["name"], "ABCD Corp")
        self.assertEqual(record["entity"]["exchange"], "NASDAQ")
        self.assertEqual(record["entity"]["country"], "us")
        self.assertEqual(record["entity"]["type"], "equity")
        self.assertEqual(record["entity"]["match_score"], 20.5)
        self.assertEqual(record["request_start"], "2026-10-01T00:00:00Z")
        self.assertEqual(record["request_end"], "2026-10-05T00:00:00Z")

    def test_provider_credentials_are_redacted_from_article_metadata(self):
        item = article(
            uuid=f"article-{TOKEN}",
            title=f"headline {TOKEN}",
            url=f"https://example.com/?api_token={TOKEN}",
            source=f"source-{TOKEN}",
        )
        for field in ("name", "exchange", "exchange_long", "type", "industry"):
            item["entities"][0][field] = f"{field}-{TOKEN}"

        result, _, _ = self.run_pages([FakeResponse(page([item]))])
        record = result["articles"][0]

        self.assertEqual(record["id"], "article-<redacted>")
        self.assertEqual(record["source"], "source-<redacted>")
        self.assertEqual(record["published_at"], "2026-10-02T14:30:00Z")
        self.assertNotIn("url", record)
        self.assertNotIn(TOKEN, repr(result))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_valid_empty_page_is_unverified_not_no_catalyst(self):
        result, calls, _ = self.run_pages([FakeResponse(page([], found=0))])
        self.assertEqual(result["status"], "NEWS_COVERAGE_UNVERIFIED")
        self.assertNotEqual(result["status"], "NO_CATALYST")
        self.assertEqual(result["articles"], [])
        self.assertEqual(len(calls), 1)

    def test_paginates_until_found_and_records_each_page(self):
        first = page([article("a-1"), article("a-2")], found=3, limit=2)
        second = page([article("a-3")], found=3, limit=2, page_no=2)
        result, calls, _ = self.run_pages([FakeResponse(first), FakeResponse(second)])
        self.assertEqual([a["id"] for a in result["articles"]], ["a-1", "a-2", "a-3"])
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1][0].full_url).query)["page"], ["2"])

    # --- HTTP failures -----------------------------------------------------
    def test_http_failures_map_to_explicit_reasons(self):
        cases = {
            400: "NEWS_PROVIDER_UNAVAILABLE",
            401: "NEWS_PROVIDER_UNAVAILABLE",
            402: "NEWS_PROVIDER_UNAVAILABLE",
            403: "NEWS_PROVIDER_UNAVAILABLE",
            404: "NEWS_PROVIDER_UNAVAILABLE",
            429: "NEWS_RATE_LIMITED",
            500: "NEWS_PROVIDER_UNAVAILABLE",
            503: "NEWS_PROVIDER_UNAVAILABLE",
        }
        for code, reason in cases.items():
            with self.subTest(code=code):
                self.expect_failure([http_error(code)], reason)

    def test_documented_error_code_is_reported_without_message_text(self):
        err = http_error(402, {"error": {"code": "usage_limit_reached", "message": f"token {TOKEN}"}})
        with patch.object(mx, "urlopen", side_effect=[err]):
            with self.assertRaises(CoverageError) as ctx:
                self.probe.fetch_articles("ABCD", START, END)
        self.assertIn("usage_limit_reached", str(ctx.exception))
        self.assert_reason(ctx, "NEWS_PROVIDER_UNAVAILABLE")

    def test_200_error_payload_with_non_string_code_is_unavailable(self):
        for code in ([], {}):
            with self.subTest(code=code):
                self.expect_failure(
                    [FakeResponse({"error": {"code": code}})],
                    "NEWS_PROVIDER_UNAVAILABLE",
                )

    def test_200_error_payload_with_rate_limit_code_is_rate_limited(self):
        self.expect_failure(
            [FakeResponse({"error": {"code": "rate_limit_reached"}})],
            "NEWS_RATE_LIMITED",
        )

    def test_network_errors_are_unavailable_and_redacted(self):
        for exc in (
            urllib.error.URLError(f"connection refused {TOKEN}"),
            TimeoutError(f"timed out {TOKEN}"),
            OSError(f"socket {TOKEN}"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.expect_failure([exc], "NEWS_PROVIDER_UNAVAILABLE")

    def test_redirect_is_refused(self):
        self.expect_failure([http_error(302)], "NEWS_PROVIDER_UNAVAILABLE")
        handler = mx._NoRedirectHandler()
        req = urllib.request.Request(f"https://api.marketaux.com/v1/news/all?api_token={TOKEN}")
        self.assertIsNone(
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/")
        )

    def test_response_from_untrusted_host_is_rejected(self):
        resp = FakeResponse(page([article()]), url=f"https://evil.example/?api_token={TOKEN}")
        self.expect_failure([resp], "NEWS_PROVIDER_UNAVAILABLE")

    def test_rejects_non_https_or_foreign_base_url(self):
        for base in ("http://api.marketaux.com", "https://evil.example", "https://api.marketaux.com.evil.example"):
            with self.subTest(base=base):
                with self.assertRaises(CoverageError):
                    MarketauxProbe(TOKEN, base_url=base)

    # --- malformed payloads ------------------------------------------------
    def test_malformed_payloads_are_unavailable(self):
        bad_payloads = [
            b"not json",
            b"[]",
            json.dumps({"data": []}).encode(),
            json.dumps({"meta": {"found": 0, "returned": 0, "limit": 3, "page": 1}}).encode(),
            json.dumps({"error": {"code": "server_error", "message": "x"}}).encode(),
            json.dumps(page([], found=0, page_no=2)).encode(),
            json.dumps(page([article()], returned=2)).encode(),
            json.dumps(page([article()], limit=0)).encode(),
            json.dumps({"meta": {"found": "1", "returned": 1, "limit": 3, "page": 1}, "data": [article()]}).encode(),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload[:60]):
                self.expect_failure([FakeResponse(payload)], "NEWS_PROVIDER_UNAVAILABLE")

    def test_oversized_body_is_unavailable(self):
        big = b"{" + b" " * (mx._MAX_RESPONSE_BYTES + 10) + b"}"
        self.expect_failure([FakeResponse(big)], "NEWS_PROVIDER_UNAVAILABLE")

    def test_malformed_records_fail_instead_of_being_dropped(self):
        bad_records = [
            article(uuid=""),
            article(uuid=None),
            article(published="2026-10-02 14:30:00"),
            article(published="2026-10-02T14:30:00"),
            article(published="garbage"),
            article(source=""),
            article(source=None),
            article(entities=[]),
            article(entities="ABCD"),
            "not-an-object",
        ]
        for record in bad_records:
            with self.subTest(record=str(record)[:80]):
                self.expect_failure([FakeResponse(page([record]))], "NEWS_PROVIDER_UNAVAILABLE")

    def test_mismatched_entity_fails(self):
        for record in (article(symbol="WXYZ"), article(symbol="ABCDE")):
            with self.subTest(symbol=record["entities"][0]["symbol"]):
                self.expect_failure([FakeResponse(page([record]))], "NEWS_PROVIDER_UNAVAILABLE: entity mismatch")
        foreign = article()
        foreign["entities"][0]["country"] = "ca"
        self.expect_failure([FakeResponse(page([foreign]))], "NEWS_PROVIDER_UNAVAILABLE: entity mismatch")

    def test_duplicate_ids_fail(self):
        same_page = page([article("dup"), article("dup")])
        self.expect_failure([FakeResponse(same_page)], "NEWS_PROVIDER_UNAVAILABLE: duplicate article id")
        first = page([article("dup"), article("a-2")], found=3, limit=2)
        second = page([article("dup")], found=3, limit=2, page_no=2)
        self.expect_failure(
            [FakeResponse(first), FakeResponse(second)], "NEWS_PROVIDER_UNAVAILABLE: duplicate article id"
        )

    def test_future_timestamp_fails(self):
        future = article(published="2026-10-05T12:00:01.000000Z")
        probe_end = NOW
        with patch.object(mx, "urlopen", side_effect=[FakeResponse(page([future]))]):
            with self.assertRaises(CoverageError) as ctx:
                self.probe.fetch_articles("ABCD", START, probe_end)
        self.assert_reason(ctx, "NEWS_PROVIDER_UNAVAILABLE: future timestamp")

    def test_timestamp_outside_request_window_fails(self):
        for ts in ("2026-09-30T23:59:59.000000Z", "2026-10-05T00:00:01.000000Z"):
            with self.subTest(ts=ts):
                self.expect_failure(
                    [FakeResponse(page([article(published=ts)]))],
                    "NEWS_PROVIDER_UNAVAILABLE: timestamp outside request window",
                )

    def test_request_window_end_is_exclusive(self):
        just_before_end = "2026-10-04T23:59:59.999999Z"
        result, _, _ = self.run_pages(
            [FakeResponse(page([article(published=just_before_end)]))]
        )
        self.assertEqual(result["articles"][0]["published_at"], just_before_end)

        self.expect_failure(
            [FakeResponse(page([article(published="2026-10-05T00:00:00.000000Z")]))],
            "NEWS_PROVIDER_UNAVAILABLE: timestamp outside request window",
        )

    # --- truncation and bounds --------------------------------------------
    def test_truncated_by_page_bound_fails(self):
        first = page([article("a-1"), article("a-2")], found=5, limit=2)
        mocked = self.expect_failure([FakeResponse(first)], "NEWS_PROVIDER_UNAVAILABLE: truncated", max_pages=2)
        self.assertEqual(mocked.call_count, 1)

    def test_short_page_with_more_found_is_truncated(self):
        first = page([article("a-1")], found=3, limit=2)
        self.expect_failure([FakeResponse(first)], "NEWS_PROVIDER_UNAVAILABLE: truncated")

    def test_empty_later_page_is_truncated(self):
        first = page([article("a-1"), article("a-2")], found=3, limit=2)
        second = page([], found=3, limit=2, page_no=2)
        self.expect_failure(
            [FakeResponse(first), FakeResponse(second)], "NEWS_PROVIDER_UNAVAILABLE: truncated"
        )

    def test_found_inconsistent_with_empty_first_page_fails(self):
        self.expect_failure([FakeResponse(page([], found=4))], "NEWS_PROVIDER_UNAVAILABLE: truncated")

    def test_invalid_arguments_fail_before_http(self):
        bad = [
            ("abcd", START, END, {}),
            ("", START, END, {}),
            ("ABCD;DROP", START, END, {}),
            ("ABCD,EFGH", START, END, {}),
            ("ABCD", END, START, {}),
            ("ABCD", START.replace(tzinfo=None), END, {}),
            ("ABCD", START, datetime(2026, 10, 6, tzinfo=timezone.utc), {}),
            ("ABCD", START, END, {"max_pages": 0}),
            ("ABCD", START, END, {"max_pages": 11}),
            ("ABCD", datetime(2025, 1, 1, tzinfo=timezone.utc), END, {}),
        ]
        for symbol, start, end, kwargs in bad:
            with self.subTest(symbol=symbol, start=start, end=end, kwargs=kwargs):
                with patch.object(mx, "urlopen") as mocked:
                    with self.assertRaises(CoverageError) as ctx:
                        self.probe.fetch_articles(symbol, start, end, **kwargs)
                mocked.assert_not_called()
                self.assertTrue(str(ctx.exception).startswith("PARAMS_INVALID"), str(ctx.exception))

    def test_invalid_timeout_rejected(self):
        for timeout in (0, -1, 61, float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(CoverageError):
                    MarketauxProbe(TOKEN, timeout=timeout)

    # --- quality regressions ----------------------------------------------
    def test_http_client_exceptions_are_unavailable_and_redacted(self):
        reflected = f"GET /v1/news/all?api_token={TOKEN} HTTP/1.1"
        for exc in (
            http.client.BadStatusLine(reflected),
            http.client.RemoteDisconnected(reflected),
            http.client.IncompleteRead(reflected.encode()),
            http.client.HTTPException(reflected),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.expect_failure([exc], "NEWS_PROVIDER_UNAVAILABLE")

    def test_http_client_exception_during_body_read_is_unavailable(self):
        class Broken(FakeResponse):
            def read1(self, size=-1):
                raise http.client.BadStatusLine(f"api_token={TOKEN}")

            read = read1

        self.expect_failure([Broken(page([article()]))], "NEWS_PROVIDER_UNAVAILABLE")

    def test_fractional_second_bounds_rejected_before_http(self):
        cases = [
            (START.replace(microsecond=500_000), END),
            (START, END.replace(microsecond=1)),
            (START.replace(microsecond=999_999), END.replace(microsecond=999_999)),
        ]
        for start, end in cases:
            with self.subTest(start=start, end=end):
                with patch.object(mx, "urlopen") as mocked:
                    with self.assertRaises(CoverageError) as ctx:
                        self.probe.fetch_articles("ABCD", start, end)
                mocked.assert_not_called()
                self.assertTrue(str(ctx.exception).startswith("TIMESTAMP_INVALID"), str(ctx.exception))

    def test_timeout_documentation_disclaims_bounded_total_duration(self):
        documentation = mx.__doc__ or ""
        for phrase in (
            "best-effort deadline",
            "between blocking operations",
            "socket idle timeout",
            "slowly trickles bytes",
            "not a total-duration guarantee",
            "deadline overrun are not bounded",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, documentation)

    def test_slow_drip_body_checks_deadline_between_reads(self):
        clock = FakeClock()
        drip = DripResponse(clock)
        with patch.object(mx, "_monotonic", clock):
            self.expect_failure([drip], "NEWS_PROVIDER_UNAVAILABLE: best-effort deadline")
        # Each fake read advances one second, so the between-read checks stop this drip.
        self.assertLessEqual(len(drip.requested), 16)
        self.assertTrue(all(0 < size <= mx._MAX_RESPONSE_BYTES + 1 for size in drip.requested))
        self.assertTrue(drip.socket_timeouts)
        self.assertTrue(all(0 < t <= 15.0 for t in drip.socket_timeouts))
        self.assertEqual(drip.socket_timeouts, sorted(drip.socket_timeouts, reverse=True))

    def test_slow_drip_without_read1_checks_deadline_between_reads(self):
        clock = FakeClock()
        drip = DripResponse(clock, use_read1=False)
        with patch.object(mx, "_monotonic", clock):
            self.expect_failure([drip], "NEWS_PROVIDER_UNAVAILABLE: best-effort deadline")
        self.assertLessEqual(len(drip.requested), 16)

    def test_body_read_requests_at_most_one_bounded_body(self):
        clock = FakeClock()
        drip = DripResponse(clock, step=0.0, total=mx._MAX_RESPONSE_BYTES + 50)
        drip.read1 = lambda size=-1: (drip.requested.append(size), b"x" * size)[1]
        with patch.object(mx, "_monotonic", clock):
            self.expect_failure([drip], "NEWS_PROVIDER_UNAVAILABLE: response exceeds size bound")
        self.assertLessEqual(sum(drip.requested), mx._MAX_RESPONSE_BYTES + 1)

    def test_best_effort_deadline_is_shared_across_pages(self):
        clock = FakeClock()
        first = page([article("a-1"), article("a-2")], found=3, limit=2)
        second = page([article("a-3")], found=3, limit=2, page_no=2)
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                clock.now += 10.0
            return FakeResponse(first if len(calls) == 1 else second)

        with patch.object(mx, "_monotonic", clock), patch.object(mx, "urlopen", side_effect=fake_urlopen):
            result = self.probe.fetch_articles("ABCD", START, END)
        self.assertEqual(result["collected"], 3)
        self.assertEqual(calls[0], 15.0)
        self.assertAlmostEqual(calls[1], 5.0)

        clock.now = 0.0
        calls.clear()

        def slow_urlopen(request, timeout=None):
            calls.append(timeout)
            clock.now += 16.0
            return FakeResponse(first)

        with patch.object(mx, "_monotonic", clock), patch.object(mx, "urlopen", side_effect=slow_urlopen):
            with self.assertRaises(CoverageError) as ctx:
                self.probe.fetch_articles("ABCD", START, END)
        self.assert_reason(ctx, "NEWS_PROVIDER_UNAVAILABLE: best-effort deadline")
        self.assertEqual(len(calls), 1)

    def test_no_trading_or_order_calls_in_module(self):
        with open(mx.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("placeOrder", "submit_order", "ibapi", "alpaca"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
