import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from microcap_coverage_probe import coverage_report
from microcap_history import AlpacaHistory, CoverageError
from microcap_source_probe import extend_coverage, main


START = "2025-05-28T00:00:00Z"
END = "2025-05-29T00:00:00Z"
MANIFEST = {"status": "predeclared", "issuer_ids": ["issuer-1"],
            "dates": ["2025-05-28"], "symbol": "SORA"}


class FakeAlpacaResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.payload


def base_report():
    observation = {
        "issuer_id": "issuer-1", "date": "2025-05-28", "session": "regular",
        "provider": "ibkr",
        "bars": {"status": "observed", "intervals": {
            "1 min": {"count": 3}, "1 hour": {"count": 2}}},
        "daily_bars": {"status": "observed", "count": 1},
        "quotes": {"status": "observed"}, "news": {"status": "unavailable"},
    }
    base = coverage_report({"status": "verified"}, [observation], sample_manifest=MANIFEST)
    base["sample_manifest"]["symbol"] = "SORA"
    base["request_window"] = {"start_utc": START, "end_utc": END}
    return base


def roster(active, records=None):
    return {"snapshot_date": "2025-05-28", "source": "massive",
            "fetched_at": "2025-05-29T00:01:00Z",
            "page_count": 1, "pagination_complete": True,
            "records": records if records is not None else [{
                "ticker": "SORA", "active": active, "snapshot_date": "2025-05-28",
                "cik": "123", "composite_figi": None, "identity_status": "verified",
                "source": "massive"}] if active else []}


def news(count=1):
    return {"symbol": "SORA", "request_start": START, "request_end": END,
            "completeness": "UNVERIFIED_BY_PROVIDER", "article_count": count,
            "articles": [{"id": "n1", "created_at": "2025-05-28T12:00:00Z",
                          "fetched_at": "2025-05-28T13:00:00Z", "symbols": ["SORA"],
                          "request_start": START, "request_end": END,
                          "data_source": "alpaca_news"}] if count else []}


class ExtensionTests(unittest.TestCase):
    def test_fixed_shape_and_ibkr_interval_matrix_retained(self):
        result = extend_coverage(base_report(), roster={"active": roster(True),
                                 "inactive": roster(False)}, alpaca_news=news(),
                                 cap_evidence={"status": "MICROCAP", "source_verified": True},
                                 massive_fetched_live=True)
        self.assertEqual(set(result), set(base_report()) |
                         {"massive_roster", "alpaca_news", "market_cap"})
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertIs(result["order_approval"], False)
        self.assertIs(result["model_calibrated"], False)
        self.assertEqual(result["target_probabilities"], "unavailable")
        self.assertEqual(result["bar_interval_coverage"], base_report()["bar_interval_coverage"])
        self.assertEqual(len(result["matrix"]), len(base_report()["matrix"]))
        row = result["matrix"][0]
        self.assertEqual(row["bars"]["intervals"]["1 min"]["count"], 3)
        self.assertEqual(row["bars"]["intervals"]["1 hour"]["count"], 2)
        self.assertEqual(row["daily_bars"]["count"], 1)
        self.assertEqual(row["massive_roster"]["pagination"], {
            "active": {"page_count": 1, "complete": True},
            "inactive": {"page_count": 1, "complete": True},
        })
        self.assertEqual(row["massive_roster"]["unresolved_identity_count"], 0)
        self.assertEqual(row["alpaca_news"]["article_count"], 1)
        self.assertIn("NEWS_COVERAGE_UNVERIFIED",
                      row["alpaca_news"]["blocking_reasons"])
        self.assertEqual(row["market_cap_gate"]["status"], "MARKET_CAP_UNVERIFIED")
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED",
                      row["massive_roster"]["blocking_reasons"])
        self.assertEqual(result["massive_roster"]["active_count"], 1)
        self.assertEqual(result["massive_roster"]["inactive_count"], 0)
        self.assertEqual(result["alpaca_news"]["article_count"], 1)
        self.assertIn("MARKET_CAP_UNVERIFIED", result["reasons"])

    def test_only_live_normalized_pages_can_report_pagination_completeness(self):
        evidence = {"active": {**roster(True), "page_count": 2},
                    "inactive": roster(False)}
        report = extend_coverage(base_report(), roster=evidence, alpaca_news=news(),
                                 cap_evidence=None)
        row_roster = report["matrix"][0]["massive_roster"]
        self.assertIsNone(row_roster["pagination"]["active"]["page_count"])
        self.assertIsNone(row_roster["pagination"]["active"]["complete"])
        self.assertIn("MASSIVE_PAGINATION_UNVERIFIED", row_roster["blocking_reasons"])
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", report["reasons"])

    def test_live_pagination_is_observed_but_never_proves_full_universe(self):
        evidence = {"active": {**roster(True), "page_count": 2},
                    "inactive": {**roster(False), "page_count": 2}}
        report = extend_coverage(base_report(), roster=evidence, alpaca_news=news(),
                                 cap_evidence=None, massive_fetched_live=True)
        row_roster = report["matrix"][0]["massive_roster"]
        self.assertEqual(row_roster["pagination"]["active"],
                         {"page_count": 2, "complete": True})
        self.assertEqual(row_roster["pagination"]["inactive"],
                         {"page_count": 2, "complete": True})
        self.assertEqual(row_roster["provider_completeness"], "UNVERIFIED_BY_PROVIDER")
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", row_roster["blocking_reasons"])
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", report["reasons"])

    def test_source_status_and_blocking_reasons_are_explicit_on_each_row(self):
        report = extend_coverage(base_report(), roster=None, alpaca_news=None,
                                 cap_evidence=None)
        row = report["matrix"][0]
        self.assertEqual(row["massive_roster"]["status"], "MISSING")
        self.assertEqual(row["alpaca_news"]["status"], "MISSING")
        self.assertEqual(row["market_cap_gate"]["status"], "MARKET_CAP_UNVERIFIED")
        self.assertIn("MASSIVE_SOURCE_MISSING", row["massive_roster"]["blocking_reasons"])
        self.assertIn("ALPACA_NEWS_MISSING", row["alpaca_news"]["blocking_reasons"])
        self.assertIn("MARKET_CAP_UNVERIFIED", row["market_cap_gate"]["blocking_reasons"])

    def test_unresolved_identities_are_counted_across_both_roster_filters(self):
        active = roster(True)
        inactive = roster(False, [{**roster(True)["records"][0], "active": False,
                                   "identity_status": "unverified", "cik": None}])
        report = extend_coverage(base_report(),
                                 roster={"active": active, "inactive": inactive},
                                 alpaca_news=None, cap_evidence=None,
                                 massive_fetched_live=True)
        self.assertEqual(report["matrix"][0]["massive_roster"]["unresolved_identity_count"], 1)
        self.assertIn("MASSIVE_IDENTITY_UNRESOLVED",
                      report["matrix"][0]["massive_roster"]["blocking_reasons"])

    def test_unreadable_roster_identity_count_is_unknown_not_zero(self):
        report = extend_coverage(base_report(),
                                 roster={"active": {**roster(True), "records": None},
                                         "inactive": roster(False)},
                                 alpaca_news=None, cap_evidence=None,
                                 massive_fetched_live=True)
        self.assertIsNone(report["matrix"][0]["massive_roster"]
                          ["unresolved_identity_count"])
        self.assertEqual(report["matrix"][0]["massive_roster"]["status"], "INVALID")

    def test_missing_sources_and_empty_news_are_unverified(self):
        result = extend_coverage(base_report(), roster=None, alpaca_news=news(0),
                                 cap_evidence=None)
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", result["reasons"])
        self.assertIn("NEWS_COVERAGE_UNVERIFIED", result["reasons"])
        self.assertIn("MARKET_CAP_UNVERIFIED", result["reasons"])
        self.assertEqual(result["massive_roster"]["status"], "MISSING")

    def test_incomplete_or_mismatched_roster_rejected_as_evidence(self):
        for evidence in ({"active": roster(True)}, {"active": roster(True),
                "inactive": roster(False, [{"ticker": "OTHER", "active": False,
                "snapshot_date": "2025-05-28", "identity_status": "unverified"}])},
                {"active": roster(True), "inactive": {**roster(False),
                 "next_url": "https://example.org/next"}}):
            with self.subTest(evidence=evidence):
                result = extend_coverage(base_report(), roster=evidence,
                                         alpaca_news=news(), cap_evidence=None)
                self.assertIn("ROSTER_COVERAGE_UNVERIFIED", result["reasons"])

    def test_roster_entitlement_and_observed_news_do_not_prove_completeness(self):
        result = extend_coverage(base_report(), roster={"active": roster(True),
                                 "inactive": roster(False)}, alpaca_news=news(),
                                 cap_evidence=None)
        self.assertEqual(result["massive_roster"]["entitlement"],
                         "BASIC_TWO_YEAR_HISTORY_LIMIT")
        self.assertEqual(result["alpaca_news"]["completeness"], "UNVERIFIED_BY_PROVIDER")
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", result["reasons"])

    def test_unidentified_or_missing_active_issuer_is_not_dated_roster_observed(self):
        unknown = roster(True, [{**roster(True)["records"][0],
                                 "identity_status": "unverified", "cik": None}])
        for active, expected_status in ((roster(True, []), "INVALID"),
                                        (unknown, "SAVED_EVIDENCE_UNVERIFIED")):
            with self.subTest(active=active):
                report = extend_coverage(
                    base_report(), roster={"active": active, "inactive": roster(False)},
                    alpaca_news=None, cap_evidence=None)
                self.assertEqual(report["massive_roster"]["status"],
                                 expected_status)

    def test_manifest_requires_uppercase_symbol_offline(self):
        for symbol in (None, "", "sora", "SORA.A", "SÓRA"):
            with self.subTest(symbol=symbol):
                base = base_report()
                if symbol is None:
                    del base["sample_manifest"]["symbol"]
                else:
                    base["sample_manifest"]["symbol"] = symbol
                with self.assertRaises(CoverageError):
                    extend_coverage(
                        base, roster={"active": roster(True), "inactive": roster(False)},
                        alpaca_news=None, cap_evidence=None)

    def test_unrelated_active_ticker_is_not_dated_roster_observed(self):
        unrelated = roster(True, [{**roster(True)["records"][0], "ticker": "OTHER"}])
        result = extend_coverage(
            base_report(), roster={"active": unrelated, "inactive": roster(False)},
            alpaca_news=None, cap_evidence=None)
        self.assertEqual(result["massive_roster"]["status"], "INVALID")

    def test_offline_article_without_provenance_not_counted(self):
        evidence = {**news(), "articles": [{"id": "n1"}]}
        result = extend_coverage(base_report(), roster=None, alpaca_news=evidence,
                                 cap_evidence=None)
        self.assertIsNone(result["alpaca_news"]["article_count"])
        self.assertIn("NEWS_COVERAGE_UNVERIFIED", result["reasons"])
        self.assertEqual(result["matrix"][0]["alpaca_news"]["status"], "INVALID")

    def test_forged_base_rejected(self):
        for changes in ({"decision": "TRADE"}, {"order_approval": 0},
                        {"model_calibrated": 0}, {"target_probabilities": {}},
                        {"schema_version": None}, {"request_window": {}},
                        {"sample_manifest": {**MANIFEST, "dates": ["2025-05-27"]}}):
            base = base_report()
            base.update(changes)
            with self.subTest(changes=changes), self.assertRaises(CoverageError):
                extend_coverage(base, roster=None, alpaca_news=None, cap_evidence=None)

    def test_base_self_declared_verified_roster_is_not_independent(self):
        result = extend_coverage(base_report(), roster=None, alpaca_news=None,
                                 cap_evidence=None)
        self.assertEqual(result["roster_status"]["status"], "verified")
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", result["reasons"])


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="source-probe-test-", dir=Path(__file__).resolve().parent.parent
        )
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.write("base.json", base_report())
        self.write("manifest.json", MANIFEST)

    def write(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def run_cli(self, extra=()):
        args = ["--base-report", str(self.directory / "base.json"),
                "--sample-manifest", str(self.directory / "manifest.json"),
                "--start", START, "--end", END, *extra]
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def test_offline_never_contacts_provider(self):
        with patch("microcap_source_probe.MassiveRoster.from_environment",
                   side_effect=AssertionError("HTTP forbidden")), patch(
                   "microcap_source_probe.AlpacaHistory.from_environment",
                   side_effect=AssertionError("HTTP forbidden")):
            status, out, err = self.run_cli()
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(json.loads(out)["decision"], "NO_TRADE")

    def test_fetch_bounded_both_rosters_and_news_only(self):
        calls = []
        class Massive:
            def fetch_snapshot(self, day, *, active, ticker, max_pages):
                calls.append((day, active, ticker, max_pages))
                return roster(active)
        class Alpaca:
            def fetch_news(self, symbols, start, end, *, max_pages):
                calls.append(("news", symbols, max_pages))
                return []
        with patch("microcap_source_probe.MassiveRoster.from_environment",
                   return_value=Massive()), patch(
                   "microcap_source_probe.AlpacaHistory.from_environment",
                   return_value=Alpaca()):
            status, out, err = self.run_cli(["--fetch", "--max-pages", "2"])
        self.assertEqual(status, 0, err)
        self.assertEqual(calls, [("2025-05-28", True, "SORA", 2),
                                 ("2025-05-28", False, "SORA", 2),
                                 ("news", ["SORA"], 2)])
        result = json.loads(out)
        self.assertEqual(result["alpaca_news"]["article_count"], 0)
        self.assertEqual(result["matrix"][0]["massive_roster"]["pagination"]["active"],
                         {"page_count": 1, "complete": True})

    def test_fetch_news_next_page_at_cap_fails_closed(self):
        class Massive:
            def fetch_snapshot(self, day, *, active, ticker, max_pages):
                return roster(active)
        responses = [{"news": [], "next_page_token": "page-2"}]
        with patch("microcap_source_probe.MassiveRoster.from_environment",
                   return_value=Massive()), patch(
                   "microcap_source_probe.AlpacaHistory.from_environment",
                   return_value=AlpacaHistory("test-key", "test-secret")), patch(
                   "microcap_history.urlopen",
                   side_effect=[FakeAlpacaResponse(page) for page in responses]) as opened:
            status, out, err = self.run_cli(["--fetch", "--max-pages", "1"])
        self.assertEqual((status, out), (3, ""))
        self.assertEqual(json.loads(err)["error"], "NEWS_PAGE_CAP_EXCEEDED")
        self.assertEqual(opened.call_count, 1)

    def test_fetch_alpaca_http_status_classification_is_sanitized(self):
        class Massive:
            def fetch_snapshot(self, day, *, active, ticker, max_pages):
                return roster(active)
        cases = {403: "NEWS_ACCESS_DENIED", 401: "NEWS_ACCESS_DENIED",
                 500: "PROVIDER_UNAVAILABLE"}
        for code, expected in cases.items():
            with self.subTest(code=code):
                denial = HTTPError("https://data.alpaca.markets/v1beta1/news?symbols=AAA",
                                   code, "Forbidden", {}, None)
                with patch.dict(os.environ, {"APCA_API_KEY_ID": "private-key-id",
                                             "APCA_API_SECRET_KEY": "private-secret"}), patch(
                           "microcap_source_probe.MassiveRoster.from_environment",
                           return_value=Massive()), patch(
                           "microcap_source_probe.AlpacaHistory.from_environment",
                           return_value=AlpacaHistory("private-key-id", "private-secret")), patch(
                           "microcap_history.urlopen", side_effect=denial):
                    status, out, err = self.run_cli(["--fetch", "--max-pages", "2"])
                self.assertEqual((status, out), (3, ""))
                self.assertEqual(json.loads(err), {"error": expected})
                self.assertNotIn("private", err)

    def test_mismatch_before_provider_initialization(self):
        self.write("manifest.json", {**MANIFEST, "issuer_ids": ["another"]})
        with patch("microcap_source_probe.MassiveRoster.from_environment",
                   side_effect=AssertionError("network initialized")):
            status, out, err = self.run_cli(["--fetch", "--max-pages", "2"])
        self.assertEqual((status, out), (2, ""))
        self.assertEqual(json.loads(err)["error"], "INPUT_INVALID")

    def test_denied_provider_is_sanitized_exit_three(self):
        class Massive:
            def fetch_snapshot(self, *args, **kwargs):
                raise CoverageError("ROSTER_COVERAGE_UNVERIFIED: provider returned HTTP 403")
        with patch("microcap_source_probe.MassiveRoster.from_environment",
                   return_value=Massive()):
            status, out, err = self.run_cli(["--fetch", "--max-pages", "2"])
        self.assertEqual((status, out), (3, ""))
        self.assertEqual(json.loads(err)["error"], "ROSTER_ACCESS_DENIED")

    def test_configured_credentials_never_leak_from_evidence(self):
        token = "very-private-token"
        self.write("news.json", {**news(), "articles": [{"id": token}],
                                  "article_count": 1})
        with patch.dict(os.environ, {"MASSIVE_API_KEY": token,
                                     "APCA_API_KEY_ID": token,
                                     "APCA_API_SECRET_KEY": token}):
            status, out, err = self.run_cli(["--news-evidence",
                                             str(self.directory / "news.json")])
        self.assertNotIn(token, out + err)
        self.assertEqual(status, 2)

    def test_fixed_error_text_never_leaks_colliding_token(self):
        with patch.dict(os.environ, {"APCA_API_KEY_ID": "INPUT_INVALID"}):
            status, out, err = self.run_cli(["--roster-evidence", "relative.json"])
        self.assertEqual(status, 2)
        self.assertNotIn("INPUT_INVALID", out + err)

    def test_relative_external_path_and_excessive_page_cap_invalid(self):
        for extra in (["--roster-evidence", "relative.json"],
                      ["--fetch", "--max-pages", "3"]):
            with self.subTest(extra=extra):
                status, out, err = self.run_cli(extra)
                self.assertEqual((status, out), (2, ""))

    def test_repo_contained_json_and_external_symlink_to_it_are_invalid(self):
        internal = Path(__file__).resolve().parent / ".source-probe-internal.json"
        internal.write_text(json.dumps(base_report()), encoding="utf-8")
        self.addCleanup(internal.unlink)
        alias = self.directory / "internal-link.json"
        alias.symlink_to(internal)
        internal_alias = Path(__file__).resolve().parent / ".source-probe-external-link.json"
        internal_alias.symlink_to(self.directory / "base.json")
        self.addCleanup(internal_alias.unlink)
        for path in (internal, alias, internal_alias):
            with self.subTest(path=path):
                status, out, err = self.run_cli(["--base-report", str(path)])
                self.assertEqual(status, 2)
                self.assertEqual(out, "")
                self.assertEqual(json.loads(err)["error"], "INPUT_INVALID")

    def test_repo_contained_optional_evidence_is_invalid(self):
        internal = Path(__file__).resolve().parent / ".source-probe-internal.json"
        internal.write_text(json.dumps({"active": roster(True),
                                        "inactive": roster(False)}), encoding="utf-8")
        self.addCleanup(internal.unlink)
        status, out, err = self.run_cli(["--roster-evidence", str(internal)])
        self.assertEqual((status, out), (2, ""))
        self.assertEqual(json.loads(err)["error"], "INPUT_INVALID")
