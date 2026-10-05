import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from microcap_history import CoverageError
from microcap_coverage_probe import coverage_report, main


MANIFEST = {"status": "predeclared", "issuer_ids": ["a"], "dates": ["2024-05-15"]}
OBSERVATION = {
    "issuer_id": "a", "date": "2024-05-15", "session": "regular",
    "provider": "ibkr",
    "bars": {"status": "observed"}, "quotes": {"status": "unavailable"},
    "news": {"status": "observed"},
}

ROSTER = {
    "source_url": "https://example.org/roster",
    "retrieved_at": "2024-05-16T00:00:00Z",
    "coverage_claim": "historical-listed-and-delisted",
    "members": [{
        "issuer_id": "a", "symbol": "ABC", "company": "Example Corp",
        "valid_from": "2024-05-01T00:00:00Z", "valid_to": None,
        "listing_status": "listed", "source_record_id": "record-a",
    }],
}


class CoverageReportTests(unittest.TestCase):
    def test_missing_quotes_blocks_with_explicit_reason(self):
        result = coverage_report({"status": "verified"}, [OBSERVATION], sample_manifest=MANIFEST)
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertFalse(result["order_approval"])
        self.assertFalse(result["model_calibrated"])
        self.assertEqual(result["target_probabilities"], "unavailable")
        self.assertIn("QUOTE_COVERAGE_UNAVAILABLE", result["reasons"])

    def test_favorable_sample_is_observed_only(self):
        observation = {**OBSERVATION, "quotes": {"status": "observed"},
                       "bars": {"status": "observed", "count": 1, "intervals": {
                           "1 min": {"count": 1, "first_utc": "2024-05-15T14:30:00Z",
                                     "last_utc": "2024-05-15T14:30:00Z"}}}}
        result = coverage_report({"status": "verified"}, [observation], sample_manifest=MANIFEST)
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertEqual(result["coverage_status"], "PROBE_COVERAGE_OBSERVED")
        self.assertIn("RESEARCH_ONLY_NOT_CALIBRATED", result["reasons"])
        self.assertFalse(result["order_approval"])
        self.assertFalse(result["model_calibrated"])
        self.assertEqual(result["target_probabilities"], "unavailable")

    def test_combined_or_hourly_bar_count_never_passes_minute_gate(self):
        hourly = {"1 min": {"count": 0}, "1 hour": {
            "count": 7, "first_utc": "2024-05-15T13:00:00Z", "last_utc": "2024-05-15T19:00:00Z"}}
        daily = {"status": "observed", "count": 1, "first_session_date": "2024-05-15",
                 "last_session_date": "2024-05-15", "timestamp_basis": "SESSION_DATE_ONLY",
                 "decision_time_verified": True, "reasons": [], "observations": [{"close": 9.5}]}
        for bars in ({"status": "observed", "count": 7, "intervals": hourly},
                     {"status": "observed", "count": 7}):
            with self.subTest(bars=bars):
                observation = {**OBSERVATION, "quotes": {"status": "observed"},
                               "bars": bars, "daily_bars": daily}
                result = coverage_report({"status": "verified"}, [observation],
                                         sample_manifest=MANIFEST)
                self.assertEqual(result["decision"], "NO_TRADE")
                self.assertEqual(result["coverage_status"], "PROBE_COVERAGE_INCOMPLETE")
                self.assertIn("MINUTE_BAR_COVERAGE_UNAVAILABLE", result["reasons"])
                self.assertIn("IBKR_COVERAGE_UNAVAILABLE", result["reasons"])
                self.assertEqual(result["missing_fractions"]["bars"], 1.0)
                coverage = result["bar_interval_coverage"]
                self.assertEqual(coverage["1 min"]["observed_cells"], 0)
                self.assertEqual(coverage["1 day"]["observed_cells"], 1)
                self.assertIs(coverage["1 day"]["intraday_gate_eligible"], False)
                row_daily = result["matrix"][0]["daily_bars"]
                self.assertEqual(row_daily["timestamp_basis"], "SESSION_DATE_ONLY")
                self.assertIs(row_daily["decision_time_verified"], False)
                self.assertIn("DAILY_BAR_DECISION_TIME_UNVERIFIED", row_daily["reasons"])
                self.assertNotIn("observations", row_daily)
                self.assertNotIn("9.5", json.dumps(result))
        self.assertIn("MINUTE_BARS_MISSING_HOURLY_ONLY", coverage_report(
            {"status": "verified"}, [{**OBSERVATION, "bars": {"status": "observed",
                                                            "intervals": hourly}}],
            sample_manifest=MANIFEST)["reasons"])

    def test_marketaux_news_cannot_fill_a_missing_ibkr_cell(self):
        observation = {
            "issuer_id": "a", "date": "2024-05-15", "session": "regular",
            "provider": "marketaux",
            "bars": {"status": "not_applicable"},
            "quotes": {"status": "not_applicable"},
            "news": {"status": "observed"},
        }
        result = coverage_report({"status": "verified"}, [observation], sample_manifest=MANIFEST)
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertIn("IBKR_COVERAGE_UNAVAILABLE", result["reasons"])
        self.assertIn("BAR_COVERAGE_UNAVAILABLE", result["reasons"])
        self.assertIn("QUOTE_COVERAGE_UNAVAILABLE", result["reasons"])

    def test_bars_and_quotes_must_be_observed_in_the_same_ibkr_cell(self):
        observations = [
            {**OBSERVATION, "session": "morning",
             "quotes": {"status": "not_applicable"}},
            {**OBSERVATION, "session": "afternoon",
             "bars": {"status": "not_applicable"}, "quotes": {"status": "observed"}},
        ]
        result = coverage_report({"status": "verified"}, observations, sample_manifest=MANIFEST)
        self.assertEqual(result["coverage_status"], "PROBE_COVERAGE_INCOMPLETE")
        self.assertIn("IBKR_COVERAGE_UNAVAILABLE", result["reasons"])

    def test_unknown_roster_and_delisted_contract_block(self):
        observation = {**OBSERVATION, "quotes": {"status": "observed"},
                       "listing_status": "delisted", "contract": {"status": "unresolved"}}
        result = coverage_report({"status": "unverified"}, [observation], sample_manifest=MANIFEST)
        self.assertIn("ROSTER_UNVERIFIED", result["reasons"])
        self.assertIn("CONTRACT_UNRESOLVED", result["reasons"])

    def test_ambiguous_news_and_provider_error_retained(self):
        observation = {**OBSERVATION, "quotes": {"status": "observed"},
                       "news": {"status": "unverified", "reasons": ["NEWS_TIMESTAMP_AMBIGUOUS"],
                                "errors": [{"reason": "IBKR_PERMISSION_DENIED"}]},
                       "provider": "ibkr"}
        result = coverage_report({"status": "verified"}, [observation], sample_manifest=MANIFEST)
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertIn("NEWS_TIMESTAMP_AMBIGUOUS", result["reasons"])
        self.assertIn("IBKR_PERMISSION_DENIED", result["reasons"])
        self.assertEqual(result["matrix"][0]["provider"], "ibkr")
        self.assertEqual(result["matrix"][0]["news"]["errors"][0]["reason"], "IBKR_PERMISSION_DENIED")

    def test_empty_marketaux_and_unverified_ibkr_never_mean_no_catalyst(self):
        result = coverage_report(
            {"status": "verified"},
            [{**OBSERVATION, "quotes": {"status": "observed"},
              "news": {"status": "NEWS_COVERAGE_UNVERIFIED"}, "provider": "marketaux"}],
            sample_manifest=MANIFEST)
        self.assertIn("NEWS_COVERAGE_UNVERIFIED", result["reasons"])
        self.assertEqual(result["missing_fractions"]["news"], 1.0)

    def test_mixed_counts_and_missing_declared_sample(self):
        manifest = {**MANIFEST, "issuer_ids": ["a", "b"]}
        result = coverage_report({"status": "verified"}, [OBSERVATION], sample_manifest=manifest)
        self.assertEqual(result["counts"]["issuers"], 1)
        self.assertEqual(result["counts"]["declared_issuers"], 2)
        self.assertIn("SAMPLE_INCOMPLETE", result["reasons"])

    def test_manifest_extra_fields_and_source_secrets_are_not_repeated(self):
        manifest = {**MANIFEST, "api_token": "private-token"}
        observation = {**OBSERVATION, "bars": {
            "status": "observed", "source_records": [{"id": "safe",
                                                        "api_token": "private-token"}]}}
        result = coverage_report({"status": "verified", "api_token": "private-token"},
                                 [observation], sample_manifest=manifest)
        self.assertNotIn("private-token", json.dumps(result))

    def test_missing_timestamp_and_ambiguous_article_blocks(self):
        observation = {**OBSERVATION, "quotes": {"status": "observed"},
                       "news": {"status": "observed", "source_records": [
                           {"id": "article1", "time_basis": "AMBIGUOUS"}]}}
        result = coverage_report({"status": "verified"}, [observation], sample_manifest=MANIFEST)
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertIn("NEWS_TIMESTAMP_AMBIGUOUS", result["reasons"])

    def test_article_source_provenance_is_retained(self):
        observation = {**OBSERVATION, "news": {
            "status": "observed", "source_records": [
                {"id": "n1", "published_at": "2024-05-15T12:00:00Z",
                 "source": "ExampleNews", "api_token": "never-output"}]}}
        result = coverage_report({"status": "verified"}, [observation], sample_manifest=MANIFEST)
        record = result["matrix"][0]["news"]["source_records"][0]
        self.assertEqual(record["source"], "ExampleNews")
        self.assertNotIn("api_token", record)


class CLITests(unittest.TestCase):
    def run_cli(self, arguments):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_unverified_pilot_roster_fetch_invokes_probe_only_with_explicit_flags(self):
        roster = {**ROSTER, "coverage_claim": "pilot-unverified"}
        ibkr_result = {
            "contract": {"status": "resolved"},
            "bars": {"status": "observed", "count": 1, "observations": []},
            "quotes": {"status": "observed", "count": 1, "observations": []},
            "news": {"status": "observed", "count": 0, "observations": []},
            "errors": [],
        }
        base = ["--roster", "/external/roster.json",
                "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z",
                "--end", "2024-05-16T00:00:00Z"]
        for flags, expected_calls in (
            ([], 0), (["--pilot"], 0),
            (["--fetch", "--max-symbols", "1", "--max-days", "1"], 0),
            (["--pilot", "--fetch", "--max-symbols", "1", "--max-days", "1"], 1),
        ):
            with self.subTest(flags=flags), \
                 patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}, clear=True), \
                 patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, roster]), \
                 patch("microcap_coverage_probe.probe_ibkr", return_value=ibkr_result) as ibkr:
                code, out, err = self.run_cli(base + flags)
                self.assertEqual(ibkr.call_count, expected_calls)
                if "--pilot" not in flags:
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")
                    self.assertEqual(json.loads(err)["reason"], "ROSTER_UNVERIFIED")
                else:
                    self.assertEqual(code, 0, err)
                    report = json.loads(out)
                    self.assertEqual(report["decision"], "NO_TRADE")
                    self.assertIs(report["order_approval"], False)
                    self.assertIs(report["model_calibrated"], False)
                    self.assertEqual(report["roster_status"]["sampling_bias"], "UNVERIFIED_PILOT")
                    self.assertIn("ROSTER_UNVERIFIED", report["reasons"])
                    if expected_calls:
                        self.assertEqual(report["matrix"][0]["bars"]["status"], "observed")
                        ibkr.assert_called_once_with(
                            "ABC", "2024-05-15T00:00:00Z", "2024-05-16T00:00:00Z",
                            host="127.0.0.1", port=7497, client_id=9001)

    def test_fetch_keeps_hour_only_and_daily_bars_separate_without_prices(self):
        hour_bars = [{"t": f"2024-05-15T{hour}:00:00Z", "interval": "1 hour", "close": 9.5,
                      "volume": 1234} for hour in ("13", "19", "15")]
        ibkr_result = {
            "contract": {"status": "resolved"},
            "bars": {"status": "observed", "count": 3, "first_utc": "2024-05-15T13:00:00Z",
                     "last_utc": "2024-05-15T19:00:00Z", "reasons": [],
                     "observations": hour_bars},
            "daily_bars": {"status": "observed", "count": 1,
                           "first_session_date": "2024-05-15",
                           "last_session_date": "2024-05-15",
                           "timestamp_basis": "SESSION_DATE_ONLY",
                           "decision_time_verified": False,
                           "reasons": ["DAILY_BAR_DECISION_TIME_UNVERIFIED"],
                           "observations": [{"session_date": "2024-05-15", "close": 9.5}]},
            "quotes": {"status": "observed", "count": 1, "observations": []},
            "news": {"status": "observed", "count": 0, "observations": []},
            "bar_samples": {"1 min": 0, "1 hour": 3, "1 day": 1},
            "errors": [],
        }
        with patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}, clear=True), \
             patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, ROSTER]), \
             patch("microcap_coverage_probe.probe_ibkr", return_value=ibkr_result) as ibkr:
            code, out, err = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--fetch", "--pilot", "--max-symbols", "1", "--max-days", "1"])
        self.assertEqual(code, 0, err)
        ibkr.assert_called_once()
        report = json.loads(out)
        self.assertEqual(report["decision"], "NO_TRADE")
        self.assertIs(report["order_approval"], False)
        self.assertIn("MINUTE_BAR_COVERAGE_UNAVAILABLE", report["reasons"])
        self.assertIn("MINUTE_BARS_MISSING_HOURLY_ONLY", report["reasons"])
        bars = report["matrix"][0]["bars"]
        self.assertEqual(bars["intervals"], {
            "1 min": {"count": 0, "first_utc": None, "last_utc": None},
            "1 hour": {"count": 3, "first_utc": "2024-05-15T13:00:00Z",
                       "last_utc": "2024-05-15T19:00:00Z"}})
        self.assertEqual(bars["source_records"], [])
        daily = report["matrix"][0]["daily_bars"]
        self.assertEqual(daily["count"], 1)
        self.assertEqual(daily["first_session_date"], "2024-05-15")
        self.assertEqual(daily["timestamp_basis"], "SESSION_DATE_ONLY")
        self.assertIs(daily["decision_time_verified"], False)
        self.assertNotIn("first_utc", daily)
        self.assertEqual(report["bar_interval_coverage"]["1 hour"]["observed_cells"], 1)
        self.assertEqual(report["bar_interval_coverage"]["1 min"]["missing_fraction"], 1.0)
        self.assertEqual(report["bar_interval_coverage"]["1 day"]["observed_cells"], 1)
        self.assertNotIn("9.5", out)
        self.assertNotIn("1234", out)

    def test_ambiguous_issuer_symbols_fail_closed_before_broker_calls(self):
        base = ["--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--fetch", "--max-symbols", "2", "--max-days", "1"]
        for claim, flags in (("pilot-unverified", ["--pilot"]),
                             ("historical-listed-and-delisted", ["--pilot"]),
                             ("historical-listed-and-delisted", [])):
            for order in (("AAA", "BBB"), ("BBB", "AAA")):
                roster = {**ROSTER, "coverage_claim": claim, "members": [
                    {**ROSTER["members"][0], "symbol": symbol, "source_record_id": f"record-{symbol}"}
                    for symbol in order
                ]}
                with self.subTest(claim=claim, flags=flags, order=order), \
                     patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}, clear=True), \
                     patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, roster]), \
                     patch("microcap_coverage_probe.probe_ibkr") as ibkr, \
                     patch("microcap_coverage_probe.MarketauxProbe") as marketaux:
                    code, out, err = self.run_cli(base + flags)
                    ibkr.assert_not_called()
                    marketaux.assert_not_called()
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")
                    self.assertEqual(json.loads(err), {"error": "INPUT_OR_COVERAGE_INVALID",
                                                       "reason": "CONTRACT_UNRESOLVED"})

    def test_malformed_unverified_pilot_roster_fails_before_broker_calls(self):
        for mutation in (
            lambda data: data["members"][0].update(valid_from="bad"),
            lambda data: data["members"].append({**data["members"][0],
                                                  "source_record_id": "duplicate"}),
            lambda data: data["members"][0].update(issuer_id="other"),
        ):
            with self.subTest(mutation=mutation):
                roster = json.loads(json.dumps({**ROSTER, "coverage_claim": "pilot-unverified"}))
                mutation(roster)
                with patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}, clear=True), \
                     patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, roster]), \
                     patch("microcap_coverage_probe.probe_ibkr") as ibkr:
                    code, out, _ = self.run_cli([
                        "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                        "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                        "--pilot", "--fetch", "--max-symbols", "1", "--max-days", "1"])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                ibkr.assert_not_called()

    def test_offline_never_constructs_providers(self):
        with patch("microcap_coverage_probe.probe_ibkr") as ibkr, \
             patch("microcap_coverage_probe.MarketauxProbe") as marketaux, \
             patch("microcap_coverage_probe._load_json", side_effect=[
                 {"status": "predeclared", "issuer_ids": ["a"], "dates": ["2024-05-15"]},
                 {"source_url": "https://example.org/roster", "retrieved_at": "2024-05-16T00:00:00Z",
                  "coverage_claim": "historical-listed-and-delisted", "members": []},
                 [OBSERVATION],
             ]):
            code, out, err = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--observations", "/external/observations.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["decision"], "NO_TRADE")
        ibkr.assert_not_called()
        marketaux.assert_not_called()

    def test_offline_observation_strings_never_echo_configured_marketaux_token(self):
        secret = "synthetic-secret"
        manifest = {**MANIFEST, "issuer_ids": ["a", f"issuer-{secret}"]}
        observations = [
            {**OBSERVATION, "session": secret, "provider": secret,
             "quotes": {"status": secret, "reasons": [secret],
                        "errors": [{"reason": secret}]}},
            {**OBSERVATION, "issuer_id": f"issuer-{secret}", "session": f"pre-{secret}-x",
             "provider": f"ibkr-{secret}",
             "news": {"status": "observed", "first_utc": secret,
                      "source_records": [{"id": secret, "source": f"src-{secret}"}]}},
        ]
        snapshot = json.loads(json.dumps(observations))
        manifest_snapshot = json.loads(json.dumps(manifest))
        with patch.dict(os.environ, {"MARKETAUX_API_TOKEN": secret}), \
             patch("microcap_coverage_probe.probe_ibkr") as ibkr, \
             patch("microcap_coverage_probe.MarketauxProbe") as marketaux, \
             patch("microcap_coverage_probe._load_json",
                   side_effect=[manifest, ROSTER, observations]):
            code, out, err = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--observations", "/external/observations.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z"])
        self.assertEqual(code, 0, err)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)
        report = json.loads(out)
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(report["decision"], "NO_TRADE")
        self.assertEqual(report["coverage_status"], "PROBE_COVERAGE_INCOMPLETE")
        self.assertFalse(report["order_approval"])
        self.assertFalse(report["model_calibrated"])
        self.assertEqual(report["target_probabilities"], "unavailable")
        self.assertEqual(report["matrix"][0]["issuer_id"], "a")
        self.assertEqual(report["matrix"][0]["date"], "2024-05-15")
        self.assertTrue(report["matrix"][1]["session"].startswith("pre-"))
        self.assertTrue(report["matrix"][1]["provider"].startswith("ibkr-"))
        self.assertEqual(report["counts"]["observations"], 2)
        self.assertEqual(observations, snapshot)
        self.assertEqual(manifest, manifest_snapshot)
        ibkr.assert_not_called()
        marketaux.assert_not_called()

    def test_coverage_report_redacts_configured_token_without_mutating_inputs(self):
        secret = "SYNTHETIC_SECRET_CODE"
        observation = {**OBSERVATION, "session": secret,
                       "bars": {"status": "observed", "reasons": [secret]}}
        roster_status = {"status": secret, "reason": secret, "sampling_bias": secret}
        with patch.dict(os.environ, {"MARKETAUX_API_TOKEN": secret}):
            result = coverage_report(roster_status, [observation], sample_manifest=MANIFEST)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertEqual(observation["session"], secret)
        self.assertEqual(roster_status["status"], secret)

    def test_token_colliding_with_schema_key_fails_explicitly_without_renaming_fields(self):
        secret = "decision"
        observation = {**OBSERVATION, "session": secret, secret: "TRADE",
                       "news": {"status": "observed", secret: secret,
                                "source_records": [{secret: secret, "id": secret}]}}
        snapshot = json.loads(json.dumps(observation))
        with patch.dict(os.environ, {"MARKETAUX_API_TOKEN": secret}):
            with self.assertRaises(CoverageError) as caught:
                coverage_report({"status": "verified"}, [observation], sample_manifest=MANIFEST)
        self.assertEqual(str(caught.exception), "CREDENTIAL_CONFLICT")
        self.assertEqual(observation, snapshot)

    def offline_cli_with_token(self, secret, observation=None):
        observation = OBSERVATION if observation is None else observation
        with patch.dict(os.environ, {"MARKETAUX_API_TOKEN": secret}), \
             patch("microcap_coverage_probe.probe_ibkr") as ibkr, \
             patch("microcap_coverage_probe.MarketauxProbe") as marketaux, \
             patch("microcap_coverage_probe._load_json",
                   side_effect=[MANIFEST, ROSTER, [observation]]):
            result = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--observations", "/external/observations.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z"])
        ibkr.assert_not_called()
        marketaux.assert_not_called()
        return result

    def test_cli_token_schema_collision_exits_two_with_token_safe_json_diagnostic(self):
        for secret in ("decision", "error"):
            with self.subTest(secret=secret):
                code, out, err = self.offline_cli_with_token(
                    secret, {**OBSERVATION, secret: secret, "session": secret})
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(len(err.splitlines()), 1)
                diagnostic = json.loads(err)
                self.assertIsInstance(diagnostic, dict)
                self.assertTrue(diagnostic)
                self.assertNotIn(secret, err)
                self.assertNotIn("decision", diagnostic)
                self.assertNotIn("[REDACTED]", out + err)

    def test_cli_short_tokens_never_leak_and_always_emit_json_diagnostic_on_error(self):
        import string
        for secret in string.ascii_letters + string.digits + "_-.,":
            with self.subTest(secret=secret):
                code, out, err = self.offline_cli_with_token(secret)
                self.assertIn(code, (0, 2))
                self.assertNotIn(secret, out + err)
                if code == 0:
                    self.assertEqual(json.loads(out)["decision"], "NO_TRADE")
                else:
                    self.assertEqual(out, "")
                    self.assertIsInstance(json.loads(err), dict)

    def test_cli_token_of_json_punctuation_exits_two_without_leaking(self):
        # Every JSON object contains these, so no diagnostic can be emitted safely.
        for secret in ('{', '}', '"', ':'):
            with self.subTest(secret=secret):
                code, out, err = self.offline_cli_with_token(secret)
                self.assertEqual(code, 2)
                self.assertEqual(out + err, "")

    def test_cli_normal_long_token_keeps_valid_report(self):
        code, out, err = self.offline_cli_with_token("tok_9f8e7d6c5b4a3f2e1d0c")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        report = json.loads(out)
        self.assertEqual(report["decision"], "NO_TRADE")
        self.assertIs(report["order_approval"], False)

    def test_provider_failure_diagnostic_avoids_token_and_keeps_exit_three(self):
        for secret in ("PROVIDER_ERROR", "PROVIDER_UNAVAILABLE"):
            with self.subTest(secret=secret), \
                 patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001",
                                         "MARKETAUX_API_TOKEN": secret}), \
                 patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, {}, []]), \
                 patch("microcap_coverage_probe._fetch", return_value=([OBSERVATION], True)):
                code, out, err = self.run_cli([
                    "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                    "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                    "--fetch", "--pilot", "--max-symbols", "1", "--max-days", "1"])
                self.assertEqual(code, 3)
                self.assertEqual(json.loads(out)["decision"], "NO_TRADE")
                self.assertEqual(len(err.splitlines()), 1)
                self.assertIsInstance(json.loads(err), dict)
                self.assertNotIn(secret, out + err)

    def test_ordinary_token_in_user_controlled_keys_and_values_keeps_fixed_schema(self):
        secret = "tok_9f8e7d6c5b4a"
        observation = {**OBSERVATION, secret: secret, "session": f"s-{secret}",
                       "bars": {"status": "observed", "reasons": [secret.upper(), secret],
                                secret: [secret]},
                       "news": {"status": "observed", "first_utc": secret,
                                "source_records": [{secret: "x", "id": f"id-{secret}",
                                                    "source": "src"}]}}
        roster_status = {"status": secret, "reason": secret, secret: secret}
        snapshot = json.loads(json.dumps([observation, roster_status]))
        with patch.dict(os.environ, {"MARKETAUX_API_TOKEN": secret.upper()}):
            upper = coverage_report(roster_status, [observation], sample_manifest=MANIFEST)
        with patch.dict(os.environ, {"MARKETAUX_API_TOKEN": secret}):
            result = coverage_report(roster_status, [observation], sample_manifest=MANIFEST)
        for report in (result, upper):
            self.assertEqual(report["decision"], "NO_TRADE")
            self.assertIs(report["order_approval"], False)
            self.assertIs(report["model_calibrated"], False)
            self.assertEqual(set(report), {
                "schema_version", "decision", "coverage_status", "reasons", "order_approval",
                "model_calibrated", "target_probabilities", "sample_manifest", "roster_status",
                "matrix", "missing_fractions", "counts", "bar_interval_coverage"})
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret.upper(), json.dumps(upper))
        self.assertEqual(result["matrix"][0]["session"], "s-[REDACTED]")
        self.assertEqual(result["matrix"][0]["news"]["source_records"], [{"source": "src"}])
        self.assertEqual([observation, roster_status], snapshot)

    def test_pilot_cannot_claim_calibration_even_with_fake_verified_roster(self):
        roster = {"status": "verified", "members": [{"issuer_id": "a", "symbol": "ABC"}]}
        with patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, roster, []]), \
             patch("microcap_coverage_probe.probe_ibkr") as ibkr:
            code, out, _ = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--observations", "/external/obs.json", "--pilot",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIn("ROSTER_UNVERIFIED", report["reasons"])
        self.assertFalse(report["model_calibrated"])
        ibkr.assert_not_called()

    def test_invalid_input_is_redacted_single_line(self):
        code, out, err = self.run_cli(["--roster", "relative.json"])
        self.assertEqual(code, 2)
        self.assertFalse(out)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn("relative.json", err)

    def test_fetch_requires_dedicated_client_id_and_finite_bounds(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("microcap_coverage_probe.probe_ibkr") as ibkr:
            code, _, err = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--fetch", "--max-symbols", "1", "--max-days", "1"])
        self.assertEqual(code, 2)
        self.assertIn("IB_PROBE_CLIENT_ID", err)
        ibkr.assert_not_called()

    def test_reserved_service_client_ids_are_rejected(self):
        with patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "80"}), \
             patch("microcap_coverage_probe.probe_ibkr") as ibkr:
            code, _, _ = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--fetch", "--pilot", "--max-symbols", "1", "--max-days", "1"])
        self.assertEqual(code, 2)
        ibkr.assert_not_called()

    def test_provider_failure_exits_three_with_one_redacted_stderr_line(self):
        with patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}), \
             patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, {}, []]), \
             patch("microcap_coverage_probe._fetch",
                   return_value=([OBSERVATION], True)):
            code, out, err = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--fetch", "--pilot", "--max-symbols", "1", "--max-days", "1"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["decision"], "NO_TRADE")
        self.assertEqual(len(err.splitlines()), 1)
        self.assertEqual(json.loads(err)["reason"], "PROVIDER_ERROR")

    def test_exclusive_end_rejects_sample_date_at_midnight(self):
        manifest = {**MANIFEST, "dates": ["2024-05-16"]}
        with patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}), \
             patch("microcap_coverage_probe._load_json", side_effect=[manifest, ROSTER]), \
             patch("microcap_coverage_probe.probe_ibkr") as ibkr, \
             patch("microcap_coverage_probe.MarketauxProbe.from_environment") as marketaux:
            code, out, err = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--fetch", "--pilot", "--max-symbols", "1", "--max-days", "1"])
        self.assertEqual(code, 2)
        self.assertFalse(out)
        self.assertEqual(len(err.splitlines()), 1)
        ibkr.assert_not_called()
        marketaux.assert_not_called()

    def test_duplicate_manifest_values_fail_before_provider_calls(self):
        manifests = [
            {**MANIFEST, "issuer_ids": ["a", "a"]},
            {**MANIFEST, "dates": ["2024-05-15", "2024-05-15"]},
            {**MANIFEST, "issuer_ids": [1]},
            {**MANIFEST, "dates": [True]},
        ]
        for manifest in manifests:
            with self.subTest(manifest=manifest), \
                 patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}), \
                 patch("microcap_coverage_probe._load_json",
                       side_effect=[manifest, ROSTER]), \
                 patch("microcap_coverage_probe.probe_ibkr") as ibkr, \
                 patch("microcap_coverage_probe.MarketauxProbe.from_environment") as marketaux:
                code, out, _ = self.run_cli([
                    "--roster", "/external/roster.json",
                    "--sample-manifest", "/external/manifest.json",
                    "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                   "--fetch", "--pilot", "--max-symbols", "10", "--max-days", "7"])
                self.assertEqual(code, 2)
                self.assertFalse(out)
                ibkr.assert_not_called()
                marketaux.assert_not_called()

    def test_distinct_symbol_cap_accounts_for_ticker_changes_before_provider_calls(self):
        roster = {
            **ROSTER,
            "members": [
                {**ROSTER["members"][0], "valid_to": "2024-05-16T00:00:00Z",
                 "listing_status": "delisted"},
                {**ROSTER["members"][0], "symbol": "XYZ",
                 "valid_from": "2024-05-16T00:00:00Z", "source_record_id": "record-b"},
            ],
        }
        manifest = {**MANIFEST, "dates": ["2024-05-15", "2024-05-16"]}
        with patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}), \
             patch("microcap_coverage_probe._load_json", side_effect=[manifest, roster]), \
             patch("microcap_coverage_probe.probe_ibkr") as ibkr, \
             patch("microcap_coverage_probe.MarketauxProbe.from_environment") as marketaux:
            code, out, _ = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-17T00:00:00Z",
                "--fetch", "--pilot", "--max-symbols", "1", "--max-days", "2"])
        self.assertEqual(code, 2)
        self.assertFalse(out)
        ibkr.assert_not_called()
        marketaux.assert_not_called()

    def test_ibkr_only_fetch_does_not_require_marketaux_token(self):
        ibkr_result = {
            "contract": {"status": "resolved"},
            "bars": {"status": "observed", "count": 1, "observations": []},
            "quotes": {"status": "observed", "count": 1, "observations": []},
            "news": {"status": "observed", "count": 0, "observations": []},
            "errors": [],
        }
        with patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}, clear=True), \
             patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, ROSTER]), \
             patch("microcap_coverage_probe.probe_ibkr", return_value=ibkr_result) as ibkr, \
             patch("microcap_coverage_probe.MarketauxProbe.from_environment",
                   side_effect=CoverageError("MARKETAUX_TOKEN_MISSING")) as marketaux:
            code, out, err = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--fetch", "--pilot", "--max-symbols", "1", "--max-days", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["decision"], "NO_TRADE")
        ibkr.assert_called_once()
        marketaux.assert_not_called()

    def test_explicit_marketaux_fetch_without_token_exits_three(self):
        ibkr_result = {
            "contract": {"status": "resolved"},
            "bars": {"status": "observed", "count": 1, "observations": []},
            "quotes": {"status": "observed", "count": 1, "observations": []},
            "news": {"status": "observed", "count": 0, "observations": []},
            "errors": [],
        }
        with patch.dict(os.environ, {"IB_PROBE_CLIENT_ID": "9001"}, clear=True), \
             patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, ROSTER]), \
             patch("microcap_coverage_probe.probe_ibkr", return_value=ibkr_result), \
             patch("microcap_coverage_probe.MarketauxProbe.from_environment",
                   side_effect=CoverageError("MARKETAUX_TOKEN_MISSING")) as marketaux:
            code, out, err = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--fetch", "--marketaux", "--pilot", "--max-symbols", "1", "--max-days", "1"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["decision"], "NO_TRADE")
        self.assertEqual(json.loads(err)["reason"], "PROVIDER_ERROR")
        marketaux.assert_called_once()

    def test_marketaux_flag_requires_fetch(self):
        with patch("microcap_coverage_probe._load_json", side_effect=[MANIFEST, ROSTER, []]), \
             patch("microcap_coverage_probe.MarketauxProbe.from_environment") as marketaux:
            code, out, _ = self.run_cli([
                "--roster", "/external/roster.json", "--sample-manifest", "/external/manifest.json",
                "--observations", "/external/observations.json",
                "--start", "2024-05-15T00:00:00Z", "--end", "2024-05-16T00:00:00Z",
                "--marketaux"])
        self.assertEqual(code, 2)
        self.assertFalse(out)
        marketaux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
