import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from microcap_history import CoverageError
from microcap_coverage_probe import _roster_status, coverage_report
from microcap_readiness import main, project_readiness, publish_readiness
from microcap_source_probe import extend_coverage
from test_microcap_batch_schema import batch_report as batch_report_fixture
from test_microcap_source_probe import base_report, news, roster


NOW = datetime(2026, 10, 5, 12, 19, 14, tzinfo=timezone.utc)


def _main_checkout() -> Path:
    here = Path(__file__).resolve().parent
    common_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=here, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return Path(common_dir).parent.resolve()


def source_report(issuer_id="issuer-1"):
    report = extend_coverage(base_report(), roster=None, alpaca_news=None,
                             cap_evidence=None)
    report["sample_manifest"]["issuer_ids"] = [issuer_id]
    for row in report["matrix"]:
        row["issuer_id"] = issuer_id
    return report


def pilot_source_report():
    roster_status, _ = _roster_status(
        {}, datetime(2025, 5, 28, tzinfo=timezone.utc), pilot=True,
    )
    base = coverage_report(
        roster_status,
        [{
            "issuer_id": "0000000001", "date": "2025-05-28",
            "session": "regular", "provider": "ibkr",
            "bars": {"status": "observed", "intervals": {
                "1 min": {"count": 3}, "1 hour": {"count": 2},
            }},
            "quotes": {"status": "observed"}, "news": {"status": "unavailable"},
        }],
        sample_manifest={"status": "predeclared", "issuer_ids": ["0000000001"],
                         "dates": ["2025-05-28"]},
    )
    base["sample_manifest"]["symbol"] = "SORA"
    base["request_window"] = {
        "start_utc": "2025-05-28T00:00:00Z",
        "end_utc": "2025-05-29T00:00:00Z",
    }
    return extend_coverage(base, roster=None, alpaca_news=None, cap_evidence=None)


def sec_report():
    return {
        "decision": "NO_TRADE",
        "order_approval": False,
        "model_calibrated": False,
        "target_probabilities": "unavailable",
        "market_cap_gate": {
            "status": "MARKET_CAP_UNVERIFIED",
            "source_verified": False,
            "coverage": "UNVERIFIED",
            "coverage_truncated": False,
            "cik": "0000000001",
            "decision_at": "2025-05-28T12:00:00Z",
            "observations": [{
                "accession": "0000000001-26-000001",
                "accepted_at": "2025-05-28T11:00:00Z",
                "report_date": "2025-05-27",
                "fetched_at": "2025-05-28T12:01:00Z",
                "filing_class": "quarterly",
                "shares_count": 123456,
            }],
            "blockers": ["CLASS_COVERAGE_UNVERIFIED"],
        },
    }


class ProjectReadinessTests(unittest.TestCase):
    def test_projects_actual_extended_source_report_as_sanitized_no_trade_snapshot(self):
        result = project_readiness(source_report(), now=NOW)

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["generated_at"], "2026-10-05T12:19:14Z")
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertIs(result["order_approval"], False)
        self.assertIs(result["model_calibrated"], False)
        self.assertEqual(result["target_probabilities"], "unavailable")
        self.assertEqual(result["sample"], {
            "issuer_id": "issuer-1", "symbol": "SORA", "date": "2025-05-28",
        })
        self.assertEqual(result["sample_window"], {
            "start_utc": "2025-05-28T00:00:00Z",
            "end_utc": "2025-05-29T00:00:00Z",
        })
        self.assertEqual(result["sources"]["roster"]["status"], "UNVERIFIED")
        self.assertEqual(result["sources"]["roster"]["evidence_status"], "MISSING")
        self.assertIsNone(result["sources"]["roster"]["active_count"])
        self.assertEqual(result["sources"]["news"]["status"], "MISSING")
        self.assertIsNone(result["sources"]["news"]["article_count"])
        self.assertEqual(result["sources"]["ibkr"]["minute_cells"], 1)
        self.assertEqual(result["sources"]["ibkr"]["quote_cells"], 1)
        self.assertEqual(result["sources"]["shares"]["status"], "MARKET_CAP_UNVERIFIED")
        self.assertEqual(result["sources"]["shares"]["sec_observation_count"], 0)
        self.assertEqual(result["blockers"], sorted(set(result["blockers"])))
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", result["blockers"])
        self.assertIn("MARKET_CAP_UNVERIFIED", result["blockers"])

    def test_self_declared_verified_base_roster_stays_unverified_with_blockers(self):
        report = source_report()
        self.assertEqual(report["roster_status"]["status"], "verified")

        result = project_readiness(report, now=NOW)

        self.assertEqual(result["sources"]["roster"]["status"], "UNVERIFIED")
        self.assertEqual(result["sources"]["roster"]["evidence_status"], "MISSING")
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", result["blockers"])

    def test_untrusted_article_price_url_and_credential_text_never_enter_snapshot(self):
        report = source_report()
        report["raw_article"] = {
            "body": "malicious headline and article body",
            "price": 0.01,
            "provider_url": "https://attacker.example/private",
            "credential": "do-not-copy-this-token",
        }
        report["matrix"][0]["alpaca_news"]["raw_articles"] = [
            report["raw_article"],
        ]

        serialized = json.dumps(project_readiness(report, now=NOW))

        for forbidden in (
            "malicious headline", "article body", "0.01", "attacker.example",
            "do-not-copy-this-token",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_projects_live_roster_and_observed_news_as_unverified_sources(self):
        report = extend_coverage(
            base_report(), roster={"active": roster(True), "inactive": roster(False)},
            alpaca_news=news(), cap_evidence=None, massive_fetched_live=True,
        )

        result = project_readiness(report, now=NOW)

        self.assertEqual(result["sources"]["roster"], {
            "status": "UNVERIFIED", "evidence_status": "DATED_ROSTER_OBSERVED",
            "active_count": 1, "inactive_count": 0,
        })
        self.assertEqual(result["sources"]["news"], {
            "status": "ARTICLES_OBSERVED", "article_count": 1,
        })
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", result["blockers"])
        self.assertIn("NEWS_COVERAGE_UNVERIFIED", result["blockers"])

    def test_accepts_known_partial_and_unverified_ibkr_channel_statuses(self):
        report = coverage_report(
            {"status": "unverified"},
            [{
                "issuer_id": "issuer-1", "date": "2025-05-28",
                "session": "unspecified", "provider": "ibkr",
                "bars": {"status": "partial", "intervals": {
                    "1 min": {"count": 0}, "1 hour": {"count": 0},
                }},
                "quotes": {"status": "unavailable"},
                "news": {"status": "unverified"},
            }],
            sample_manifest={
                "status": "predeclared", "issuer_ids": ["issuer-1"],
                "dates": ["2025-05-28"],
            },
        )
        report["sample_manifest"]["symbol"] = "SORA"
        report["request_window"] = {
            "start_utc": "2025-05-28T00:00:00Z",
            "end_utc": "2025-05-29T00:00:00Z",
        }

        readiness = project_readiness(
            extend_coverage(report, roster=None, alpaca_news=None, cap_evidence=None),
            now=NOW,
        )

        self.assertEqual(readiness["sources"]["ibkr"], {
            "minute_cells": 0, "quote_cells": 0,
        })

    def test_forged_approval_flags_are_rejected(self):
        report = source_report()
        report["order_approval"] = True
        with self.assertRaises(CoverageError):
            project_readiness(report, now=NOW)

    def test_unknown_source_status_or_reason_is_rejected(self):
        for field, value in (
            ("status", "ROSTER_VERIFIED"),
            ("status", []),
            ("blocking_reasons", ["TRADING_APPROVED"]),
        ):
            report = source_report()
            report["massive_roster"][field] = value if field == "status" else \
                report["massive_roster"][field] + value
            with self.subTest(field=field), self.assertRaises(CoverageError):
                project_readiness(report, now=NOW)

    def test_invalid_aggregate_counts_are_rejected(self):
        report = source_report()
        report["counts"]["observations"] += 1
        with self.assertRaises(CoverageError):
            project_readiness(report, now=NOW)

    def test_invalid_or_mismatched_matrix_rows_are_rejected(self):
        invalid = source_report()
        invalid["matrix"][0]["bars"]["intervals"]["1 min"]["count"] = -1
        mismatched = source_report()
        mismatched["matrix"][0]["date"] = "2025-05-27"
        mismatched_symbol = source_report()
        mismatched_symbol["matrix"][0]["symbol"] = "OTHER"
        for report in (invalid, mismatched, mismatched_symbol):
            with self.assertRaises(CoverageError):
                project_readiness(report, now=NOW)

    def test_duplicate_matrix_key_is_rejected_even_when_observation_count_matches(self):
        report = source_report()
        report["matrix"].append(copy.deepcopy(report["matrix"][0]))
        report["counts"]["observations"] = 2

        with self.assertRaises(CoverageError):
            project_readiness(report, now=NOW)

    def test_missing_fraction_must_match_channel_coverage_in_matrix(self):
        for channel in ("bars", "quotes", "news"):
            report = source_report()
            current = report["missing_fractions"][channel]
            report["missing_fractions"][channel] = (
                current - 0.25 if current >= 0.25 else current + 0.25
            )
            with self.subTest(channel=channel), self.assertRaises(CoverageError):
                project_readiness(report, now=NOW)

    def test_sec_pilot_adds_only_unverified_observation_summary_and_blockers(self):
        sec = sec_report()
        result = project_readiness(source_report("0000000001"), sec, now=NOW)
        self.assertEqual(result["sources"]["shares"]["status"], "MARKET_CAP_UNVERIFIED")
        self.assertEqual(result["sources"]["shares"]["sec_observation_count"], 1)
        self.assertEqual(result["sources"]["sec"], {
            "status": "OBSERVED", "observation_count": 1,
            "verification": "UNVERIFIED",
        })
        self.assertIn("CLASS_COVERAGE_UNVERIFIED", result["blockers"])
        self.assertNotIn("shares_count", json.dumps(result))

    def test_real_pilot_coverage_with_missing_sec_stays_unverified(self):
        report = pilot_source_report()
        self.assertEqual(report["roster_status"], {
            "status": "unverified", "reason": "ROSTER_UNVERIFIED",
            "sampling_bias": "UNVERIFIED_PILOT",
        })

        result = project_readiness(report, now=NOW)

        self.assertEqual(result["sources"]["roster"]["status"], "UNVERIFIED")
        self.assertEqual(result["sources"]["roster"]["evidence_status"], "MISSING")
        self.assertEqual(result["sources"]["sec"]["status"], "MISSING")
        self.assertIn("ROSTER_UNVERIFIED", result["blockers"])
        self.assertIn("ROSTER_COVERAGE_UNVERIFIED", result["blockers"])
        self.assertNotIn("UNVERIFIED_PILOT", result["blockers"])
        self.assertNotIn("UNVERIFIED_PILOT", json.dumps(result))

    def test_pilot_bias_is_only_allowed_in_unverified_roster_metadata(self):
        for edit in (
            lambda report: report["reasons"].append("UNVERIFIED_PILOT")
            or report["reasons"].sort(),
            lambda report: report["roster_status"].update(reason="UNVERIFIED_PILOT"),
            lambda report: report["roster_status"].update(sampling_bias="PROVIDER_ERROR"),
            lambda report: report["roster_status"].update(sampling_bias=[]),
            lambda report: report["roster_status"].update(status="verified"),
        ):
            report = pilot_source_report()
            edit(report)
            with self.subTest(roster_status=report["roster_status"]), \
                    self.assertRaises(CoverageError):
                project_readiness(report, now=NOW)

    def test_sec_observation_accession_may_carry_filing_agent_prefix(self):
        # EDGAR accession prefixes identify the submitter, not the issuer.
        sec = sec_report()
        sec["market_cap_gate"]["observations"][0]["accession"] = "0001213900-26-050019"

        result = project_readiness(source_report("0000000001"), sec, now=NOW)

        self.assertEqual(result["sources"]["sec"]["status"], "OBSERVED")

    def test_sec_status_distinguishes_missing_and_unavailable_evidence(self):
        report = source_report()
        missing = project_readiness(report, now=NOW)
        unavailable_sec = sec_report()
        unavailable_sec["market_cap_gate"]["observations"] = []
        unavailable = project_readiness(report, unavailable_sec, now=NOW)

        self.assertEqual(missing["sources"]["sec"]["status"], "MISSING")
        self.assertEqual(unavailable["sources"]["sec"]["status"], "UNAVAILABLE")

    def test_sec_observations_require_a_canonical_numeric_sample_cik(self):
        with self.assertRaises(CoverageError):
            project_readiness(source_report(), sec_report(), now=NOW)

    def test_sec_decision_time_and_cik_must_match_declared_sample(self):
        outside_window = sec_report()
        outside_window["market_cap_gate"]["decision_at"] = "2025-05-29T00:00:00Z"
        wrong_cik = sec_report()
        wrong_cik["market_cap_gate"]["cik"] = "0000000002"

        for sec in (outside_window, wrong_cik):
            with self.subTest(sec=sec), self.assertRaises(CoverageError):
                project_readiness(source_report("0000000001"), sec, now=NOW)

        with self.assertRaises(CoverageError):
            project_readiness(source_report("0000000002"), sec_report(), now=NOW)

    def test_sec_cannot_claim_verified_or_approve_trading(self):
        sec = {
            "decision": "NO_TRADE",
            "order_approval": False,
            "model_calibrated": False,
            "target_probabilities": "unavailable",
            "market_cap_gate": {
                "status": "MARKET_CAP_VERIFIED",
                "source_verified": True,
                "coverage": "VERIFIED",
                "coverage_truncated": False,
                "observations": [],
                "blockers": [],
            },
        }
        with self.assertRaises(CoverageError):
            project_readiness(source_report(), sec, now=NOW)


class PublishReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.report_path = self.directory / "source.json"
        self.status_path = self.directory / "status.json"
        self.report_path.write_text(json.dumps(source_report()), encoding="utf-8")

    def test_publish_uses_external_paths_and_atomic_json_output(self):
        result = publish_readiness(self.report_path, self.status_path)

        self.assertEqual(json.loads(self.status_path.read_text(encoding="utf-8")), result)
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         ["source.json", "status.json"])

    def test_publish_rejects_paths_in_main_checkout(self):
        main_checkout = _main_checkout()
        with tempfile.TemporaryDirectory(dir=main_checkout) as checkout_temp:
            inside = Path(checkout_temp)
            source = inside / "source.json"
            destination = inside / "status.json"
            source.write_text(json.dumps(source_report()), encoding="utf-8")

            with self.assertRaises(CoverageError):
                publish_readiness(source, self.status_path)
            with self.assertRaises(CoverageError):
                publish_readiness(self.report_path, destination)

    def test_publish_rejects_symlinks_into_main_checkout_and_worktree(self):
        roots = (_main_checkout(), Path(__file__).resolve().parent)
        for root in roots:
            with self.subTest(root=root), tempfile.TemporaryDirectory(dir=root) as inside_temp, \
                    tempfile.TemporaryDirectory() as outside_temp:
                inside = Path(inside_temp)
                outside = Path(outside_temp)
                source_target = inside / "source.json"
                output_target = inside / "output.json"
                source_target.write_text(json.dumps(source_report()), encoding="utf-8")
                output_target.write_text("previous", encoding="utf-8")
                source_link = outside / "source-link.json"
                output_link = outside / "output-link.json"
                source_link.symlink_to(source_target)
                output_link.symlink_to(output_target)

                with self.assertRaises(CoverageError):
                    publish_readiness(source_link, self.status_path)
                with self.assertRaises(CoverageError):
                    publish_readiness(self.report_path, output_link)

                self.assertEqual(output_target.read_text(encoding="utf-8"), "previous")

    def test_oversized_input_exits_two_without_echoing_input(self):
        self.report_path.write_text(" " * (2 * 1024 * 1024 + 1), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(["--source-report", str(self.report_path),
                              "--output", str(self.status_path)])
        self.assertEqual((exit_code, stdout.getvalue()), (2, ""))
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "INPUT_INVALID"})
        self.assertFalse(self.status_path.exists())

    def test_failed_atomic_replace_preserves_existing_snapshot_and_cleans_temp(self):
        self.status_path.write_text("previous snapshot", encoding="utf-8")
        with patch("microcap_readiness.os.replace", side_effect=OSError("private path")):
            with self.assertRaises(CoverageError):
                publish_readiness(self.report_path, self.status_path)

        self.assertEqual(self.status_path.read_text(encoding="utf-8"), "previous snapshot")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         ["source.json", "status.json"])

    def test_cli_invalid_input_exits_two_with_sanitized_error(self):
        self.report_path.write_text('{"decision":"APPROVED"}', encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(["--source-report", str(self.report_path),
                              "--output", str(self.status_path)])

        self.assertEqual((exit_code, stdout.getvalue()), (2, ""))
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "INPUT_INVALID"})
        self.assertFalse(self.status_path.exists())

    def test_cli_error_does_not_echo_configured_credential(self):
        self.report_path.write_text('{"decision":"APPROVED"}', encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"APCA_API_KEY_ID": "INPUT_INVALID"}):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(["--source-report", str(self.report_path),
                                  "--output", str(self.status_path)])

        self.assertEqual((exit_code, stdout.getvalue()), (2, ""))
        self.assertNotIn("INPUT_INVALID", stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue()), {"failure": "INVALID_INPUT"})
        self.assertFalse(self.status_path.exists())


class PublishBatchReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.batch_path = self.directory / "batch-report.json"
        self.status_path = self.directory / "status.json"
        self.batch_path.write_text(json.dumps(batch_report_fixture()), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_publishes_schema_2_snapshot(self):
        code, out, err = self.cli(["--batch-report", str(self.batch_path),
                                   "--output", str(self.status_path)])
        self.assertEqual((code, err), (0, ""))
        saved = json.loads(self.status_path.read_text(encoding="utf-8"))
        self.assertEqual(saved, json.loads(out))
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["coverage"]["ibkr_minute"], {"observed": 1, "total": 1})

    def test_source_and_batch_are_mutually_exclusive_and_sec_needs_source(self):
        for argv in (
            ["--batch-report", str(self.batch_path), "--source-report", str(self.batch_path),
             "--output", str(self.status_path)],
            ["--batch-report", str(self.batch_path), "--sec-report", str(self.batch_path),
             "--output", str(self.status_path)],
            ["--output", str(self.status_path)],
            ["--batch-report", "", "--output", str(self.status_path)],
            ["--batch-report", "", "--sec-report", str(self.batch_path),
             "--output", str(self.status_path)],
        ):
            with self.subTest(argv=argv):
                code, out, err = self.cli(argv)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(json.loads(err), {"error": "INPUT_INVALID"})
                self.assertFalse(self.status_path.exists())

    def test_inconsistent_batch_report_is_refused(self):
        value = batch_report_fixture()
        value["coverage"]["sec_shares"]["observed"] = 0
        self.batch_path.write_text(json.dumps(value), encoding="utf-8")
        code, _, _ = self.cli(["--batch-report", str(self.batch_path),
                               "--output", str(self.status_path)])
        self.assertEqual(code, 2)
        self.assertFalse(self.status_path.exists())


if __name__ == "__main__":
    unittest.main()
