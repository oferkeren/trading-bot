"""Tests for bounded, research-only SEC submissions and company facts access."""

import io
import json
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from microcap_history import CoverageError
from microcap_sec_reader import SecReader


SUBMISSIONS = "https://data.sec.gov/submissions/CIK0000000123.json"
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000123.json"


class Response(io.BytesIO):
    def __init__(self, url, payload=b"{}", *, status=200):
        super().__init__(payload)
        self.url = url
        self.status = status

    def geturl(self):
        return self.url


class SecReaderTests(unittest.TestCase):
    def reader(self):
        with patch.dict("os.environ", {"SEC_USER_AGENT": "Research contact@example.org"}):
            return SecReader.from_environment()

    def assert_code(self, code, operation):
        with self.assertRaises(CoverageError) as raised:
            operation()
        self.assertEqual(str(raised.exception), code)

    def test_environment_requires_nonempty_user_agent(self):
        for value in ("", " \t "):
            with self.subTest(value=value):
                with patch.dict("os.environ", {"SEC_USER_AGENT": value}):
                    self.assert_code("SEC_USER_AGENT_MISSING", SecReader.from_environment)

    def test_fetch_gets_exactly_two_documents_with_private_header(self):
        calls = []

        def open_request(request, timeout):
            calls.append((request, timeout))
            payload = {"kind": "submissions" if len(calls) == 1 else "facts"}
            return Response(request.full_url, json.dumps(payload).encode())

        with patch("microcap_sec_reader.urlopen", side_effect=open_request):
            result = self.reader().fetch("123")
        self.assertEqual(result, ({"kind": "submissions"}, {"kind": "facts"}))
        self.assertEqual([request.full_url for request, _ in calls], [SUBMISSIONS, FACTS])
        for request, timeout in calls:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("User-agent"), "Research contact@example.org")
            self.assertEqual(request.get_header("Accept-encoding"), "identity")
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30)
            self.assertNotIn("Research contact@example.org", request.full_url)

    def test_invalid_ciks_never_use_network(self):
        with patch("microcap_sec_reader.urlopen") as open_request:
            for cik in ("", "0" * 11, "１２３", "١٢٣", " 123", "+123", "12a", None, 123):
                with self.subTest(cik=cik):
                    self.assert_code("SEC_RESPONSE_INVALID", lambda: self.reader().fetch(cik))
            open_request.assert_not_called()

    def test_redirect_to_foreign_host_is_never_followed(self):
        redirect = HTTPError(
            SUBMISSIONS, 302, "Found", {"Location": "https://foreign.example/collect"}, None
        )
        with patch("microcap_sec_reader.urlopen", side_effect=redirect) as open_request:
            self.assert_code("SEC_ACCESS_UNAVAILABLE", lambda: self.reader().fetch("123"))
        self.assertEqual(open_request.call_count, 1)

    def test_final_url_must_be_the_expected_https_host_and_path(self):
        for url in (
            "https://foreign.example/submissions/CIK0000000123.json",
            "http://data.sec.gov/submissions/CIK0000000123.json",
            "https://data.sec.gov/submissions/CIK0000000999.json",
            SUBMISSIONS + "?token=oops",
            "https://data.sec.gov.evil.example/submissions/CIK0000000123.json",
        ):
            with self.subTest(url=url):
                with patch("microcap_sec_reader.urlopen", return_value=Response(url)) as open_request:
                    self.assert_code("SEC_ACCESS_UNAVAILABLE", lambda: self.reader().fetch("123"))
                self.assertEqual(open_request.call_count, 1)

    def test_timeouts_and_network_failures_have_fixed_code(self):
        for failure in (TimeoutError(), socket.timeout(), URLError("offline"), OSError("offline")):
            with self.subTest(failure=type(failure).__name__):
                with patch("microcap_sec_reader.urlopen", side_effect=failure):
                    self.assert_code("SEC_ACCESS_UNAVAILABLE", lambda: self.reader().fetch("123"))

    def test_http_403_429_and_5xx_have_fixed_codes_without_retry(self):
        for status, expected in (
            (403, "SEC_ACCESS_UNAVAILABLE"),
            (429, "SEC_RATE_LIMITED"),
            (503, "SEC_ACCESS_UNAVAILABLE"),
        ):
            with self.subTest(status=status):
                failure = HTTPError(
                    FACTS, status, "request failed",
                    {"Retry-After": "999999999"}, None,
                )
                with patch("microcap_sec_reader.urlopen", side_effect=[
                    Response(SUBMISSIONS), failure
                ]) as open_request:
                    self.assert_code(expected, lambda: self.reader().fetch("123"))
                self.assertEqual(open_request.call_count, 2)

    def test_invalid_json_and_non_object_documents_fail_closed(self):
        for body in (b"{", b"[]", b"null", b'"\xff"', b'{"shares":NaN}'):
            with self.subTest(body=body):
                with patch("microcap_sec_reader.urlopen", return_value=Response(SUBMISSIONS, body)) as open_request:
                    self.assert_code("SEC_RESPONSE_INVALID", lambda: self.reader().fetch("123"))
                self.assertEqual(open_request.call_count, 1)

    def test_oversized_body_is_rejected_with_bounded_read(self):
        class Oversized(Response):
            def read(self, size=-1):
                self.read_size = size
                return b"x" * size

        response = Oversized(SUBMISSIONS)
        with patch("microcap_sec_reader.urlopen", return_value=response):
            self.assert_code("SEC_RESPONSE_INVALID", lambda: self.reader().fetch("123"))
        self.assertGreater(response.read_size, 0)
        self.assertLessEqual(response.read_size, 2 * 1024 * 1024 + 1)

    def test_invalid_second_document_never_returns_partial_pair(self):
        with patch("microcap_sec_reader.urlopen", side_effect=[
            Response(SUBMISSIONS, b'{"accepted":true}'),
            Response(FACTS, b"broken"),
        ]) as open_request:
            self.assert_code("SEC_RESPONSE_INVALID", lambda: self.reader().fetch("123"))
        self.assertEqual(open_request.call_count, 2)

    def test_repr_and_errors_do_not_expose_user_agent(self):
        with patch.dict("os.environ", {"SEC_USER_AGENT": "Private contact@example.org"}):
            reader = SecReader.from_environment()
        self.assertNotIn("Private contact@example.org", repr(reader))
        self.assertNotIn("Private contact@example.org", str(reader))
        with patch("microcap_sec_reader.urlopen", side_effect=HTTPError(
            SUBMISSIONS, 403, "Private contact@example.org", {}, None
        )):
            with self.assertRaises(CoverageError) as raised:
                reader.fetch("123")
        self.assertNotIn("Private contact@example.org", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
