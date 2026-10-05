import unittest
from datetime import date, datetime, timezone

from microcap_batch_schema import DEFAULT_RULE, validate_manifest
from microcap_batch_select import (
    ProviderError, RateLimiter, SelectionError, parse_sec_tickers, screen, select_batch,
)


NOW = datetime(2025, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def bar(ticker, o, h, v=2_000_000):
    return {"T": ticker, "o": o, "h": h, "l": o, "c": h, "v": v}


class FakeProvider:
    def __init__(self, days, types=None, failing_types=()):
        self.days = days
        self.types = types or {}
        self.failing_types = set(failing_types)
        self.calls = []

    def grouped(self, day):
        self.calls.append(("grouped", day.isoformat()))
        return f"req-{day.isoformat()}", self.days.get(day.isoformat(), [])

    def ticker_type(self, ticker, day):
        self.calls.append(("type", ticker))
        if ticker in self.failing_types:
            raise ProviderError("PROVIDER_ERROR")
        return self.types.get(ticker, "CS")


SEC = {
    "AAA": ("0000000001", "Alpha Inc"), "BBB": ("0000000002", "Beta Inc"),
    "CCC": ("0000000003", "Gamma Inc"), "DUP": ("0000000001", "Alpha Inc"),
}


class ScreenTests(unittest.TestCase):
    def test_filters_and_ranks(self):
        rows = [
            bar("AAA", 2.0, 3.0), bar("BBB", 2.0, 4.0), bar("CCC", 2.0, 3.0),
            bar("LOW", 0.5, 1.0), bar("HIGH", 25.0, 40.0), bar("THIN", 2.0, 4.0, v=10),
            bar("FLAT", 2.0, 2.4), bar("BRK.B", 2.0, 4.0), bar("TOOLONG", 2.0, 4.0),
            {"T": "BAD", "o": None, "h": 3.0, "v": 2_000_000},
            bar("ZERO", 0.0, 3.0),
        ]
        self.assertEqual(screen(rows, DEFAULT_RULE),
                         [("BBB", 100.0), ("AAA", 50.0), ("CCC", 50.0)])


class RateLimiterTests(unittest.TestCase):
    def test_sixth_call_waits_until_window_frees(self):
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        limiter = RateLimiter(limit=5, window=60.0, clock=lambda: clock[0], sleep=sleep)
        for _ in range(5):
            limiter.wait()
            clock[0] += 1.0
        limiter.wait()
        self.assertEqual(sleeps, [55.0])


class SecTickerTests(unittest.TestCase):
    def test_parses_pads_and_drops_ambiguous(self):
        payload = {
            "0": {"cik_str": 1, "ticker": "AAA", "title": "Alpha Inc"},
            "1": {"cik_str": 2, "ticker": "XX", "title": "X One"},
            "2": {"cik_str": 3, "ticker": "XX", "title": "X Two"},
            "3": {"cik_str": 4, "ticker": "BRK-B", "title": "Class"},
            "4": {"cik_str": "5", "ticker": "STR", "title": "Bad cik"},
        }
        self.assertEqual(parse_sec_tickers(payload), {"AAA": ("0000000001", "Alpha Inc")})

    def test_rejects_non_object(self):
        with self.assertRaises(SelectionError):
            parse_sec_tickers([])


class SelectBatchTests(unittest.TestCase):
    def rule(self, **changes):
        value = dict(DEFAULT_RULE)
        value.update(changes)
        return value

    def test_selects_with_outcomes_and_valid_manifest(self):
        provider = FakeProvider(
            {"2025-05-30": [bar("AAA", 2.0, 4.0), bar("ZZZ", 2.0, 3.9),
                            bar("BBB", 2.0, 3.0), bar("CCC", 2.0, 2.9)],
             "2025-05-29": [bar("DUP", 2.0, 4.0), bar("CCC", 2.0, 3.0)]},
            types={"BBB": "WARRANT"},
        )
        manifest = select_batch(provider, SEC, as_of=date(2025, 5, 30),
                                rule=self.rule(days=2), now=NOW)
        validate_manifest(manifest)
        self.assertEqual([(s["date"], s["symbol"]) for s in manifest["samples"]],
                         [("2025-05-30", "AAA"), ("2025-05-30", "CCC")])
        outcomes = [(c["date"], c["ticker"], c["outcome"]) for c in manifest["candidates"]]
        self.assertEqual(outcomes, [
            ("2025-05-30", "AAA", "SELECTED"),
            ("2025-05-30", "ZZZ", "NO_SEC_CIK"),
            ("2025-05-30", "BBB", "NOT_COMMON_STOCK"),
            ("2025-05-30", "CCC", "SELECTED"),
            ("2025-05-29", "DUP", "DUPLICATE_ISSUER"),
            ("2025-05-29", "CCC", "DUPLICATE_ISSUER"),
        ])
        self.assertEqual(manifest["samples"][0]["start_utc"], "2025-05-30T13:30:00Z")
        self.assertEqual(manifest["samples"][0]["company"], "Alpha Inc")
        self.assertEqual(manifest["trading_days"],
                         [{"date": "2025-05-30", "request_id": "req-2025-05-30"},
                          {"date": "2025-05-29", "request_id": "req-2025-05-29"}])
        self.assertNotIn(("type", "ZZZ"), provider.calls)
        self.assertNotIn(("type", "DUP"), provider.calls)

    def test_weekends_skip_calls_and_holidays_do_not_count(self):
        provider = FakeProvider({"2025-05-30": [bar("AAA", 2.0, 4.0)]})
        manifest = select_batch(provider, SEC, as_of=date(2025, 6, 1),
                                rule=self.rule(days=1), now=NOW)
        grouped = [day for kind, day in provider.calls if kind == "grouped"]
        self.assertEqual(grouped, ["2025-05-30"])
        self.assertEqual(len(manifest["samples"]), 1)

    def test_stops_after_21_calendar_days(self):
        provider = FakeProvider({})
        with self.assertRaises(SelectionError) as raised:
            select_batch(provider, SEC, as_of=date(2025, 5, 30), rule=self.rule(), now=NOW)
        self.assertEqual(str(raised.exception), "NO_SAMPLES_SELECTED")
        self.assertEqual(len([c for c in provider.calls if c[0] == "grouped"]), 15)

    def test_type_failure_is_recorded_and_grouped_failure_aborts(self):
        provider = FakeProvider({"2025-05-30": [bar("AAA", 2.0, 4.0), bar("BBB", 2.0, 3.0)]},
                                failing_types={"AAA"})
        manifest = select_batch(provider, SEC, as_of=date(2025, 5, 30),
                                rule=self.rule(days=1), now=NOW)
        self.assertEqual([c["outcome"] for c in manifest["candidates"]],
                         ["PROVIDER_ERROR", "SELECTED"])

        class Broken(FakeProvider):
            def grouped(self, day):
                raise ProviderError("PROVIDER_ERROR")

        with self.assertRaises(SelectionError) as raised:
            select_batch(Broken({}), SEC, as_of=date(2025, 5, 30), rule=self.rule(), now=NOW)
        self.assertEqual(str(raised.exception), "PROVIDER_ERROR")

    def test_candidate_cap_per_day(self):
        rows = [bar(f"Q{chr(65 + i)}", 2.0, 4.0 - i * 0.01) for i in range(15)]
        provider = FakeProvider({"2025-05-30": rows})
        with self.assertRaises(SelectionError):
            select_batch(provider, SEC, as_of=date(2025, 5, 30),
                         rule=self.rule(days=1), now=NOW)
        self.assertEqual(len([c for c in provider.calls if c[0] == "type"]), 0)

    def test_as_of_must_be_before_today_in_new_york(self):
        with self.assertRaises(SelectionError) as raised:
            select_batch(FakeProvider({}), SEC, as_of=date(2025, 6, 2),
                         rule=self.rule(), now=NOW)
        self.assertEqual(str(raised.exception), "INPUT_INVALID")


import io
import json
import os
from pathlib import Path
from unittest import mock

import microcap_batch_select as selection


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def routed(routes):
    def fake_open(request, timeout):
        url = request.full_url
        for fragment, body in routes.items():
            if fragment in url:
                if isinstance(body, Exception):
                    raise body
                return FakeResponse(json.dumps(body).encode())
        raise OSError("unrouted")
    return fake_open


GROUPED_OK = {"status": "OK", "resultsCount": 2, "request_id": "abc123",
              "results": [bar("AAA", 2.0, 4.0), bar("BBB", 2.0, 2.1)]}
GROUPED_EMPTY = {"status": "OK", "resultsCount": 0, "request_id": "def456"}
TICKERS = {"0": {"cik_str": 1, "ticker": "AAA", "title": "Alpha Inc"}}


class HttpClientTests(unittest.TestCase):
    def test_massive_grouped_and_type(self):
        limiter = RateLimiter(clock=lambda: 0.0, sleep=lambda s: None)
        client = selection.MassiveDaily("k-test", limiter)
        with mock.patch.object(selection, "_open", routed({
            "/grouped/locale/us/market/stocks/2025-05-30": GROUPED_OK,
            "/grouped/locale/us/market/stocks/2025-05-31": GROUPED_EMPTY,
            "/v3/reference/tickers/AAA": {"status": "OK", "results": {"type": "CS"}},
        })):
            self.assertEqual(client.grouped(date(2025, 5, 30))[0], "abc123")
            self.assertEqual(client.grouped(date(2025, 5, 31)), ("def456", []))
            self.assertEqual(client.ticker_type("AAA", date(2025, 5, 30)), "CS")

    def test_malformed_and_failing_responses_raise_fixed_code(self):
        client = selection.MassiveDaily("k-test", RateLimiter(clock=lambda: 0.0,
                                                               sleep=lambda s: None))
        for body in ({"status": "ERROR"}, {"status": "OK", "resultsCount": "2"},
                     {"status": "OK", "resultsCount": 2}, OSError("k-test leaked")):
            with self.subTest(body=body), mock.patch.object(
                    selection, "_open", routed({"/grouped/": body})):
                with self.assertRaises(ProviderError) as raised:
                    client.grouped(date(2025, 5, 30))
                self.assertEqual(str(raised.exception), "PROVIDER_ERROR")

    def test_oversized_body_is_rejected(self):
        def fake_open(request, timeout):
            return FakeResponse(b" " * (selection._TYPE_LIMIT + 1))
        client = selection.MassiveDaily("k", RateLimiter(clock=lambda: 0.0,
                                                          sleep=lambda s: None))
        with mock.patch.object(selection, "_open", fake_open):
            with self.assertRaises(ProviderError):
                client.ticker_type("AAA", date(2025, 5, 30))

    def test_redirects_are_refused(self):
        self.assertIsNone(selection._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://elsewhere.example/"))


class CliTests(unittest.TestCase):
    def scratch_output(self, name):
        root = Path(os.environ.get(
            "MICROCAP_BATCH_TEST_DIR",
            str(Path.home() / ".copilot" / "microcap_batch_select_tests"),
        ))
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        path.unlink(missing_ok=True)
        return path

    def run_cli(self, argv, env, routes):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(selection, "_open", routed(routes)), \
                mock.patch.object(selection, "_now",
                                  lambda: datetime(2025, 6, 2, 12, tzinfo=timezone.utc)), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = selection.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_successful_run_writes_valid_manifest(self):
        output = self.scratch_output("batch-manifest.json")
        env = {"MASSIVE_API_KEY": "k-secret-value", "SEC_USER_AGENT": "Test test@example.com"}
        code, out, err = self.run_cli(
            ["--as-of", "2025-05-30", "--days", "1", "--output", str(output)], env,
            {"/grouped/": GROUPED_OK, "company_tickers.json": TICKERS,
             "/v3/reference/tickers/AAA": {"status": "OK", "results": {"type": "CS"}}})
        self.assertEqual((code, err), (0, ""))
        manifest = json.loads(output.read_text())
        validate_manifest(manifest)
        self.assertEqual(json.loads(out)["samples"], 1)
        self.assertNotIn("k-secret-value", out + output.read_text())
        output.unlink(missing_ok=True)

    def test_missing_environment_and_provider_failure(self):
        output = self.scratch_output("m.json")
        code, _, err = self.run_cli(["--output", str(output)], {}, {})
        self.assertEqual((code, json.loads(err)), (2, {"error": "ENVIRONMENT_INCOMPLETE"}))
        env = {"MASSIVE_API_KEY": "k-secret-value", "SEC_USER_AGENT": "Test test@example.com"}
        code, _, err = self.run_cli(["--as-of", "2025-05-30", "--output", str(output)], env,
                                    {"company_tickers.json": TICKERS,
                                     "/grouped/": OSError("k-secret-value")})
        self.assertEqual((code, json.loads(err)), (3, {"error": "PROVIDER_ERROR"}))
        self.assertFalse(output.exists())

    def test_relative_or_repository_output_is_invalid(self):
        env = {"MASSIVE_API_KEY": "k", "SEC_USER_AGENT": "T t@example.com"}
        code, _, err = self.run_cli(["--output", "relative.json"], env, {})
        self.assertEqual((code, json.loads(err)), (2, {"error": "INPUT_INVALID"}))


if __name__ == "__main__":
    unittest.main()
