import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request
from unittest.mock import patch

from microcap_history import CoverageError
from microcap_massive_roster import MassiveRoster, _NoRedirectHandler


API_KEY = "test-secret-token"
SNAPSHOT_DATE = "2025-10-01"


def next_url(*, cursor: str = "next", ticker: str | None = None, active: bool = True) -> str:
    query = [
        ("market", "stocks"),
        ("locale", "us"),
        ("date", SNAPSHOT_DATE),
        ("active", str(active).lower()),
        ("limit", "1000"),
    ]
    if ticker is not None:
        query.append(("ticker", ticker))
    query.append(("cursor", cursor))
    return f"https://api.massive.com/v3/reference/tickers?{urlencode(query)}"


def result(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "ticker": "SORA",
        "active": True,
        "name": "Sora Technologies",
        "market": "stocks",
        "locale": "us",
        "cik": "0001234567",
        "composite_figi": "BBG000000001",
        "primary_exchange": "XNAS",
        "type": "CS",
        "delisted_utc": None,
    }
    record.update(overrides)
    return record


def payload(*records: dict[str, object], **overrides: object) -> bytes:
    response: dict[str, object] = {"status": "OK", "results": list(records)}
    response.update(overrides)
    return json.dumps(response).encode()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class MassiveRosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = MassiveRoster(
            API_KEY,
            clock=lambda: datetime(2025, 10, 2, 3, 4, 5, tzinfo=timezone.utc),
        )

    def open_with(self, body: bytes):
        return patch(
            "microcap_massive_roster.urlopen",
            return_value=FakeResponse(body),
        )

    def test_active_snapshot_sends_documented_filters_and_keeps_key_private(self) -> None:
        with self.open_with(payload(result())) as opener:
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, ticker="SORA", max_pages=2)

        request = opener.call_args.args[0]
        timeout = opener.call_args.kwargs["timeout"]
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), (
            "https", "api.massive.com", "/v3/reference/tickers",
        ))
        self.assertEqual(query["market"], ["stocks"])
        self.assertEqual(query["locale"], ["us"])
        self.assertEqual(query["date"], [SNAPSHOT_DATE])
        self.assertEqual(query["active"], ["true"])
        self.assertEqual(query["ticker"], ["SORA"])
        self.assertLessEqual(int(query["limit"][0]), 1000)
        self.assertEqual(query["apiKey"], [API_KEY])
        self.assertGreater(timeout, 0)
        self.assertNotIn(API_KEY, repr(self.roster))
        self.assertNotIn(API_KEY, repr(snapshot))
        self.assertEqual(snapshot["snapshot_date"], SNAPSHOT_DATE)
        self.assertEqual(snapshot["source"], "massive")
        self.assertEqual(snapshot["page_count"], 1)
        self.assertIs(snapshot["pagination_complete"], True)
        self.assertEqual(snapshot["records"][0]["identity_status"], "verified")
        self.assertEqual(snapshot["records"][0]["snapshot_date"], SNAPSHOT_DATE)
        self.assertEqual(snapshot["records"][0]["fetched_at"], snapshot["fetched_at"])

    def test_inactive_snapshot_explicitly_requests_active_false(self) -> None:
        with self.open_with(payload(result(active=False))) as opener:
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=False)

        self.assertEqual(parse_qs(urlsplit(opener.call_args.args[0].full_url).query)["active"], ["false"])
        self.assertFalse(snapshot["records"][0]["active"])
        self.assertEqual(snapshot["snapshot_date"], SNAPSHOT_DATE)

    def test_follows_trusted_next_url_and_readds_key(self) -> None:
        first = payload(result(), next_url=next_url(cursor="abc/def") + "&apiKey=provider-key")
        with patch(
            "microcap_massive_roster.urlopen",
            side_effect=[FakeResponse(first), FakeResponse(payload(result(ticker="OTHER")))],
        ) as opener:
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, max_pages=2)

        self.assertEqual(opener.call_count, 2)
        self.assertEqual(snapshot["page_count"], 2)
        self.assertIs(snapshot["pagination_complete"], True)
        second_query = parse_qs(urlsplit(opener.call_args_list[1].args[0].full_url).query)
        self.assertEqual(second_query["cursor"], ["abc/def"])
        self.assertEqual(second_query["apiKey"], [API_KEY])
        for key, value in (
            ("market", "stocks"), ("locale", "us"), ("date", SNAPSHOT_DATE),
            ("active", "true"), ("limit", "1000"),
        ):
            self.assertEqual(second_query[key], [value])
        self.assertNotIn("provider-key", repr(snapshot))
        self.assertEqual([record["ticker"] for record in snapshot["records"]], ["SORA", "OTHER"])

    def test_reconstructs_original_filters_from_cursor_only_next_url(self) -> None:
        candidate = "https://api.massive.com/v3/reference/tickers?cursor=opaque-token"
        with patch(
            "microcap_massive_roster.urlopen",
            side_effect=[
                FakeResponse(payload(result(), next_url=candidate + "&apiKey=provider-key")),
                FakeResponse(payload(result(ticker="OTHER"))),
            ],
        ) as opener:
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, max_pages=2)

        second_query = parse_qs(urlsplit(opener.call_args_list[1].args[0].full_url).query)
        self.assertEqual(second_query["cursor"], ["opaque-token"])
        self.assertEqual(second_query["apiKey"], [API_KEY])
        self.assertNotIn("provider-key", opener.call_args_list[1].args[0].full_url)
        for key, value in (
            ("market", "stocks"), ("locale", "us"), ("date", SNAPSHOT_DATE),
            ("active", "true"), ("limit", "1000"),
        ):
            self.assertEqual(second_query[key], [value])
        self.assertEqual(snapshot["page_count"], 2)

    def test_rejects_filter_drift_without_leaking_provider_credentials(self) -> None:
        candidate = next_url().replace(SNAPSHOT_DATE, "2025-10-02") + "&apiKey=provider-secret"
        with self.open_with(payload(result(), next_url=candidate)) as opener:
            with self.assertRaises(CoverageError) as caught:
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)
        opener.assert_called_once()
        for token in (API_KEY, "provider-secret"):
            self.assertNotIn(token, str(caught.exception))
            self.assertNotIn(token, repr(caught.exception))

    def test_rejects_missing_changed_or_duplicate_pagination_filters_before_request(self) -> None:
        base = next_url(ticker="SORA")
        cases = (
            base.replace(SNAPSHOT_DATE, "2025-10-02"),
            base.replace("locale=us", "locale=ca"),
            base.replace("active=true", "active=false"),
            base.replace("limit=1000", "limit=999"),
            base.replace("ticker=SORA", "ticker=OTHER"),
            base + "&date=" + SNAPSHOT_DATE,
            base + "&DATE=2025-10-02",
            base + "&market=otc",
            base + "&ticker=SORA",
            base + "&limit=1000",
        )
        for candidate in cases:
            with self.subTest(candidate=candidate), self.open_with(
                payload(result(), next_url=candidate)
            ) as opener:
                with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED") as caught:
                    self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, ticker="SORA")
                opener.assert_called_once()
                self.assertNotIn(API_KEY, str(caught.exception))

    def test_rejects_unrequested_filter_and_extra_pagination_parameters(self) -> None:
        base = next_url(active=False)
        for candidate in (base + "&ticker=SORA", base + "&type=CS", base + "&cursor=other"):
            with self.subTest(candidate=candidate), self.open_with(
                payload(result(active=False), next_url=candidate)
            ) as opener:
                with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                    self.roster.fetch_snapshot(SNAPSHOT_DATE, active=False)
                opener.assert_called_once()

    def test_follows_matching_ticker_and_inactive_filters(self) -> None:
        with patch(
            "microcap_massive_roster.urlopen",
            side_effect=[
                FakeResponse(payload(result(active=False), next_url=next_url(ticker="SORA", active=False))),
                FakeResponse(payload(result(active=False))),
            ],
        ) as opener:
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=False, ticker="SORA")
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(snapshot["page_count"], 2)

    def test_rejects_foreign_host_next_url_without_requesting_it(self) -> None:
        body = payload(result(), next_url="https://evil.example/v3/reference/tickers?cursor=next")
        with self.open_with(body) as opener:
            with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)
        opener.assert_called_once()

    def test_rejects_untrusted_next_url_scheme_path_and_port(self) -> None:
        urls = (
            "http://api.massive.com/v3/reference/tickers?cursor=next",
            "https://api.massive.com/other/path?cursor=next",
            "https://api.massive.com:444/v3/reference/tickers?cursor=next",
        )
        for next_url in urls:
            with self.subTest(next_url=next_url), self.open_with(payload(result(), next_url=next_url)):
                with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                    self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)

    def test_redirect_handler_refuses_redirects(self) -> None:
        handler = _NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            Request("https://api.massive.com/v3/reference/tickers?apiKey=secret"),
            None,
            302,
            "Found",
            {},
            "https://evil.example/steal",
        ))

    def test_rejects_repeated_next_url(self) -> None:
        repeated_url = next_url(cursor="repeat")
        with patch(
            "microcap_massive_roster.urlopen",
            side_effect=[FakeResponse(payload(result(), next_url=repeated_url)),
                         FakeResponse(payload(result(), next_url=repeated_url + "&apiKey=other"))],
        ) as opener:
            with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, max_pages=3)
        self.assertEqual(opener.call_count, 2)

    def test_page_bound_fails_instead_of_returning_partial_snapshot(self) -> None:
        first = payload(result(), next_url=next_url(cursor="2"))
        second = payload(result(ticker="OTHER"), next_url=next_url(cursor="3"))
        with patch(
            "microcap_massive_roster.urlopen",
            side_effect=[FakeResponse(first), FakeResponse(second)],
        ) as opener:
            with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, max_pages=2)
        self.assertEqual(opener.call_count, 2)

    def test_rate_limit_has_specific_coverage_code(self) -> None:
        error = HTTPError("https://api.massive.com/v3/reference/tickers", 429, "limited", {}, None)
        with patch("microcap_massive_roster.urlopen", side_effect=error):
            with self.assertRaisesRegex(CoverageError, "ROSTER_RATE_LIMITED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)

    def test_expired_or_unavailable_ticker_snapshot_is_unverified(self) -> None:
        error = HTTPError("https://api.massive.com/v3/reference/tickers", 404, "expired", {}, None)
        with patch("microcap_massive_roster.urlopen", side_effect=error):
            with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)

    def test_authentication_and_authorization_fail_closed_without_leaking_key(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                error = HTTPError(
                    f"https://api.massive.com/v3/reference/tickers?apiKey={API_KEY}",
                    status,
                    API_KEY,
                    {},
                    None,
                )
                with patch("microcap_massive_roster.urlopen", side_effect=error):
                    with self.assertRaises(CoverageError) as caught:
                        self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)
                self.assertNotIn(API_KEY, str(caught.exception))

    def test_missing_api_key_uses_required_error_code(self) -> None:
        with patch.dict(os.environ, {"MASSIVE_API_KEY": ""}):
            with self.assertRaisesRegex(CoverageError, "^MASSIVE_KEY_MISSING$"):
                MassiveRoster.from_environment()

    def test_missing_identifiers_are_returned_as_unverified_without_invention(self) -> None:
        with self.open_with(payload(result(cik=None, composite_figi=None))):
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)
        record_out = snapshot["records"][0]
        self.assertEqual(record_out["identity_status"], "unverified")
        self.assertIsNone(record_out["cik"])
        self.assertIsNone(record_out["composite_figi"])

    def test_reused_ticker_with_multiple_issuer_identities_is_unverified(self) -> None:
        second_issuer = result(cik="0007654321", composite_figi="BBG000000002")
        with self.open_with(payload(result(), second_issuer)):
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, ticker="SORA")
        self.assertEqual(len(snapshot["records"]), 2)
        self.assertEqual(
            [record["identity_status"] for record in snapshot["records"]],
            ["unverified", "unverified"],
        )

    def test_reused_ticker_with_unrelated_identifier_types_is_ambiguous(self) -> None:
        second_issuer = result(cik=None, composite_figi="BBG000000002")
        first_issuer = result(cik="0001234567", composite_figi=None)
        with self.open_with(payload(first_issuer, second_issuer)):
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, ticker="SORA")
        self.assertEqual(
            [record["identity_status"] for record in snapshot["records"]],
            ["unverified", "unverified"],
        )

    def test_unrelated_market_cap_and_share_count_fields_are_not_returned(self) -> None:
        provider_record = result(market_cap=1_000_000, share_class_shares_outstanding=500_000)
        with self.open_with(payload(provider_record)):
            snapshot = self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)
        self.assertNotIn("market_cap", snapshot["records"][0])
        self.assertNotIn("share_class_shares_outstanding", snapshot["records"][0])

    def test_rejects_inactive_result_for_active_request(self) -> None:
        with self.open_with(payload(result(active=False))):
            with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)

    def test_rejects_ticker_mismatch_and_inconsistent_market_locale_or_date(self) -> None:
        cases = (
            {"ticker": "OTHER"},
            {"market": "otc"},
            {"locale": "ca"},
        )
        for change in cases:
            with self.subTest(change=change), self.open_with(payload(result(**change))):
                with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                    self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, ticker="SORA")

        with self.assertRaisesRegex(CoverageError, "ROSTER_PARAMS_INVALID"):
            self.roster.fetch_snapshot("2025-02-30", active=True)

    def test_rejects_malformed_or_incomplete_response_and_invalid_record_fields(self) -> None:
        bodies = (
            b"{",
            json.dumps({"results": [result()]}).encode(),
            payload({"ticker": "SORA"}),  # type: ignore[arg-type]
            payload(result(active="true")),  # type: ignore[arg-type]
            payload(result(delisted_utc="not-a-date")),
            payload(result(), count=2),
        )
        for body in bodies:
            with self.subTest(body=body), self.open_with(body):
                with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                    self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)

    def test_rejects_truncated_or_oversized_response_body(self) -> None:
        with patch("microcap_massive_roster._MAX_RESPONSE_BYTES", 8), self.open_with(
            payload(result())
        ):
            with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)

    def test_rejects_incomplete_http_body_as_unverified(self) -> None:
        response = FakeResponse(b"")
        response.read = unittest.mock.Mock(side_effect=IncompleteRead(b'{"status":', 12))
        with patch("microcap_massive_roster.urlopen", return_value=response):
            with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)

    def test_rejects_malformed_http_protocol_as_unverified(self) -> None:
        with patch("microcap_massive_roster.urlopen", side_effect=HTTPException("bad response")):
            with self.assertRaisesRegex(CoverageError, "ROSTER_COVERAGE_UNVERIFIED"):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)

    def test_rejects_explicit_http_error_statuses_without_echoing_response_text(self) -> None:
        for status in (401, 403, 429):
            error = HTTPError(
                f"https://api.massive.com/v3/reference/tickers?apiKey={API_KEY}",
                status,
                API_KEY,
                {},
                None,
            )
            with self.subTest(status=status), patch("microcap_massive_roster.urlopen", side_effect=error):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(CoverageError) as caught:
                        self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True)
                self.assertNotIn(API_KEY, str(caught.exception))
                self.assertNotIn(API_KEY, repr(caught.exception))
                self.assertNotIn(API_KEY, stdout.getvalue())
                self.assertNotIn(API_KEY, stderr.getvalue())
                expected = "ROSTER_RATE_LIMITED" if status == 429 else "ROSTER_COVERAGE_UNVERIFIED"
                self.assertIn(expected, str(caught.exception))

    def test_rejects_invalid_limits_and_boolean_like_inputs(self) -> None:
        for max_pages in (0, 11, True):
            with self.subTest(max_pages=max_pages), self.assertRaisesRegex(
                CoverageError, "ROSTER_PARAMS_INVALID"
            ):
                self.roster.fetch_snapshot(SNAPSHOT_DATE, active=True, max_pages=max_pages)
        with self.assertRaisesRegex(CoverageError, "ROSTER_PARAMS_INVALID"):
            self.roster.fetch_snapshot(SNAPSHOT_DATE, active=1)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
