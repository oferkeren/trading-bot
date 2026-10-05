"""Contract tests for the bounded, offline-first SEC feasibility CLI."""

import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from microcap_sec_evidence import observe_shares
from microcap_sec_probe import main
from test_microcap_sec_evidence import documents
from test_microcap_sec_reader import Response


WORKTREE = Path(__file__).resolve().parent
CIK = "0000123456"
DECISION = "2025-05-28T15:00:00Z"


class SecProbeTests(unittest.TestCase):
    def setUp(self):
        self.external = tempfile.TemporaryDirectory(dir=WORKTREE.parent)
        self.addCleanup(self.external.cleanup)
        self.directory = Path(self.external.name)
        self.submissions = self.directory / "submissions.json"
        self.facts = self.directory / "companyfacts.json"
        submissions, facts = documents(cik=CIK, accession="0000123456-25-000001")
        self.submissions.write_text(json.dumps(submissions))
        self.facts.write_text(json.dumps(facts))

    def run_cli(self, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(["--cik", CIK, "--decision-at", DECISION, *options])
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_offline_uses_saved_documents_without_constructing_reader(self):
        with patch("microcap_sec_probe.SecReader", side_effect=AssertionError("network")):
            code, stdout, stderr = self.run_cli(
                "--submissions", str(self.submissions), "--companyfacts", str(self.facts))
        self.assertEqual((code, stderr), (0, ""))
        report = json.loads(stdout)
        self.assertEqual(report["decision"], "NO_TRADE")
        self.assertIs(report["order_approval"], False)
        self.assertIs(report["model_calibrated"], False)
        self.assertEqual(report["target_probabilities"], "unavailable")
        self.assertEqual(report["market_cap_gate"]["status"], "MARKET_CAP_UNVERIFIED")
        self.assertEqual(report["market_cap_gate"]["cik"], CIK)
        self.assertIn("CLASS_COVERAGE_UNVERIFIED", report["market_cap_gate"]["blockers"])
        self.assertEqual(report["market_cap_gate"]["observations"][0]["shares_count"], 100_000_000)
        self.assertNotIn("ISSUER INC", stdout)

    def test_fetch_invokes_reader_once_and_does_not_read_saved_files(self):
        submissions, facts = documents(cik=CIK, accession="0000123456-25-000001")
        with patch.dict(os.environ, {"SEC_USER_AGENT": "Private contact@example.org"}):
            with patch("microcap_sec_probe.SecReader.from_environment") as reader:
                reader.return_value.fetch.return_value = submissions, facts
                code, stdout, stderr = self.run_cli("--fetch", "--output",
                                                     str(self.directory / "report.json"))
        self.assertEqual((code, stderr), (0, ""))
        reader.assert_called_once_with()
        reader.return_value.fetch.assert_called_once_with(CIK)
        self.assertEqual(stdout, (self.directory / "report.json").read_text())
        self.assertNotIn("Private contact@example.org", stdout)
        self.assertNotIn("ISSUER INC", stdout)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         ["companyfacts.json", "report.json", "submissions.json"])

    def test_invalid_inputs_stop_before_fetch(self):
        invalid = [
            ["--cik", "１２３", "--fetch", "--output", str(self.directory / "out.json")],
            ["--cik", "12345678901", "--fetch", "--output", str(self.directory / "out.json")],
            ["--decision-at", "2025-05-28", "--fetch", "--output", str(self.directory / "out.json")],
            ["--fetch"],
            ["--fetch", "--submissions", str(self.submissions), "--output",
             str(self.directory / "out.json")],
            ["--fetch", "--output", str(WORKTREE / "inside.json")],
            ["--fetch", "--output", "relative.json"],
            ["--submissions", "relative.json", "--companyfacts", str(self.facts)],
            ["--submissions", str(self.submissions), "--companyfacts", str(self.facts),
             "--output", str(self.submissions)],
            ["--submissions", str(self.submissions), "--companyfacts", str(self.facts),
             "--output", str(WORKTREE / "inside.json")],
            ["--submissions", str(self.submissions)],
        ]
        with patch("microcap_sec_probe.SecReader.from_environment") as reader:
            for options in invalid:
                with self.subTest(options=options):
                    code, stdout, stderr = self.run_cli(*options)
                    self.assertEqual((code, stdout), (2, ""))
                    self.assertEqual(json.loads(stderr)["error"], "INPUT_INVALID")
            reader.assert_not_called()

    def test_missing_user_agent_fails_without_fetch(self):
        with patch.dict(os.environ, {"SEC_USER_AGENT": ""}):
            with patch("microcap_sec_probe.SecReader.fetch") as fetch:
                code, stdout, stderr = self.run_cli(
                    "--fetch", "--output", str(self.directory / "report.json"))
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr)["error"], "SEC_USER_AGENT_MISSING")
        fetch.assert_not_called()

    def test_provider_failures_use_sanitized_stderr_and_never_save(self):
        from microcap_history import CoverageError
        for error_code in (
            "SEC_ACCESS_UNAVAILABLE", "SEC_RATE_LIMITED", "SEC_RESPONSE_TOO_LARGE",
        ):
            with self.subTest(error_code=error_code):
                with patch.dict(os.environ, {"SEC_USER_AGENT": "Private contact@example.org"}):
                    with patch("microcap_sec_probe.SecReader.fetch",
                               side_effect=CoverageError(error_code)):
                        code, stdout, stderr = self.run_cli(
                            "--fetch", "--output", str(self.directory / "report.json"))
                self.assertEqual((code, stdout), (3, ""))
                self.assertEqual(json.loads(stderr)["error"], error_code)
                self.assertNotIn("Private contact@example.org", stderr)
                self.assertFalse((self.directory / "report.json").exists())

    def test_offline_companyfacts_just_above_two_mib_is_accepted(self):
        large_facts = {"padding": "a" * (2 * 1024 * 1024)}
        self.facts.write_text(json.dumps(large_facts), encoding="utf-8")

        with patch("microcap_sec_probe.SecReader", side_effect=AssertionError("network")):
            code, stdout, stderr = self.run_cli(
                "--submissions", str(self.submissions), "--companyfacts", str(self.facts))

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["market_cap_gate"]["status"],
                         "MARKET_CAP_UNVERIFIED")

    def test_offline_companyfacts_above_sixteen_mib_is_input_error(self):
        self.facts.write_bytes(b'{"padding":"' + (b"a" * (16 * 1024 * 1024)) + b'"}')

        code, stdout, stderr = self.run_cli(
            "--submissions", str(self.submissions), "--companyfacts", str(self.facts))

        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr), {"error": "INPUT_INVALID"})

    def test_offline_submissions_above_two_mib_is_input_error(self):
        self.submissions.write_bytes(b'{"padding":"' + (b"a" * (2 * 1024 * 1024)) + b'"}')

        code, stdout, stderr = self.run_cli(
            "--submissions", str(self.submissions), "--companyfacts", str(self.facts))

        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr), {"error": "INPUT_INVALID"})

    def test_fetch_uses_two_fixed_endpoints_once_and_no_retry(self):
        urls = []
        submissions, facts = documents(cik=CIK, accession="0000123456-25-000001")

        def respond(request, timeout):
            urls.append(request.full_url)
            return Response(request.full_url, json.dumps(
                submissions if len(urls) == 1 else facts).encode())

        with patch.dict(os.environ, {"SEC_USER_AGENT": "Research contact@example.org"}):
            with patch("microcap_sec_reader.urlopen", side_effect=respond):
                code, stdout, stderr = self.run_cli(
                    "--fetch", "--output", str(self.directory / "out.json"))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(urls, [
            "https://data.sec.gov/submissions/CIK0000123456.json",
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000123456.json",
        ])
        self.assertEqual(json.loads(stdout)["market_cap_gate"]["status"],
                         "MARKET_CAP_UNVERIFIED")

    def test_http_denials_and_timeout_exit_three_without_success_output(self):
        for failure, expected in (
            (HTTPError("https://data.sec.gov/submissions/CIK0000123456.json",
                       403, "Private contact@example.org", {}, None), "SEC_ACCESS_UNAVAILABLE"),
            (HTTPError("https://data.sec.gov/submissions/CIK0000123456.json",
                       429, "Private contact@example.org", {}, None), "SEC_RATE_LIMITED"),
            (socket.timeout("Private contact@example.org"), "SEC_ACCESS_UNAVAILABLE"),
        ):
            with self.subTest(expected=expected, failure=type(failure).__name__):
                with patch.dict(os.environ, {"SEC_USER_AGENT": "Private contact@example.org"}):
                    with patch("microcap_sec_reader.urlopen", side_effect=failure) as request:
                        code, stdout, stderr = self.run_cli(
                            "--fetch", "--output", str(self.directory / "out.json"))
                self.assertEqual((code, stdout), (3, ""))
                self.assertEqual(json.loads(stderr)["error"], expected)
                self.assertNotIn("Private contact@example.org", stderr)
                self.assertEqual(request.call_count, 1)
                self.assertFalse((self.directory / "out.json").exists())

    def test_rejects_symlinks_into_worktree_before_network(self):
        alias = self.directory / "alias.json"
        alias.symlink_to(WORKTREE / "OPERATIONS.md")
        with patch("microcap_sec_probe.SecReader.from_environment") as reader:
            code, stdout, stderr = self.run_cli("--fetch", "--output", str(alias))
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr)["error"], "INPUT_INVALID")
        reader.assert_not_called()

    def test_self_referential_symlinks_are_input_errors_without_path_leak(self):
        loop = self.directory / "loop.json"
        loop.symlink_to(loop)
        cases = [
            ["--fetch", "--output", str(loop)],
            ["--submissions", str(loop), "--companyfacts", str(self.facts)],
            ["--submissions", str(self.submissions), "--companyfacts", str(loop)],
        ]
        for options in cases:
            with self.subTest(options=options):
                with patch("microcap_sec_probe.SecReader.from_environment") as reader:
                    code, stdout, stderr = self.run_cli(*options)
                self.assertEqual((code, stdout), (2, ""))
                self.assertEqual(json.loads(stderr), {"error": "INPUT_INVALID"})
                self.assertNotIn(str(self.directory), stderr)
                reader.assert_not_called()

    def test_offline_malformed_json_is_input_error(self):
        self.facts.write_text('{"cik":NaN}')
        code, stdout, stderr = self.run_cli(
            "--submissions", str(self.submissions), "--companyfacts", str(self.facts))
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr)["error"], "INPUT_INVALID")

    def test_offline_deeply_nested_json_is_input_error_without_traceback(self):
        self.facts.write_text("[" * 100_000 + "]" * 100_000)
        code, stdout, stderr = self.run_cli(
            "--submissions", str(self.submissions), "--companyfacts", str(self.facts))
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr), {"error": "INPUT_INVALID"})

    def test_fetch_deeply_nested_response_is_provider_error_without_traceback(self):
        body = b"[" * 100_000 + b"]" * 100_000
        with patch.dict(os.environ, {"SEC_USER_AGENT": "Private contact@example.org"}):
            with patch("microcap_sec_reader.urlopen", side_effect=lambda request, timeout:
                       Response(request.full_url, body)) as request:
                code, stdout, stderr = self.run_cli(
                    "--fetch", "--output", str(self.directory / "out.json"))
        self.assertEqual((code, stdout), (3, ""))
        self.assertEqual(json.loads(stderr), {"error": "SEC_RESPONSE_INVALID"})
        self.assertEqual(request.call_count, 1)
        self.assertFalse((self.directory / "out.json").exists())

    def test_output_replace_is_atomic_and_cleans_temp_on_error(self):
        output = self.directory / "report.json"
        output.write_text("previous")
        with patch("microcap_sec_probe.os.replace", side_effect=OSError("secret failure")):
            code, stdout, stderr = self.run_cli(
                "--submissions", str(self.submissions), "--companyfacts", str(self.facts),
                "--output", str(output))
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr)["error"], "INPUT_INVALID")
        self.assertEqual(output.read_text(), "previous")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         ["companyfacts.json", "report.json", "submissions.json"])

    def test_observation_is_exactly_existing_evidence_contract(self):
        submissions, facts = documents(cik=CIK, accession="0000123456-25-000001")
        with patch("microcap_sec_probe.SecReader.from_environment") as reader:
            reader.return_value.fetch.return_value = submissions, facts
            with patch.dict(os.environ, {"SEC_USER_AGENT": "Research contact@example.org"}):
                code, stdout, stderr = self.run_cli(
                    "--fetch", "--output", str(self.directory / "out.json"))
        self.assertEqual((code, stderr), (0, ""))
        result = json.loads(stdout)["market_cap_gate"]
        self.assertEqual(result, observe_shares(submissions, facts, cik=CIK,
                         decision_at=DECISION, fetched_at=result["observations"][0]["fetched_at"]))


if __name__ == "__main__":
    unittest.main()
