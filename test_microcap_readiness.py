import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from microcap_history import CoverageError
from microcap_coverage_probe import coverage_report
from microcap_readiness import main, project_readiness, publish_readiness
from microcap_source_probe import extend_coverage
from test_microcap_source_probe import base_report, news, roster


NOW = datetime(2026, 10, 5, 12, 19, 14, tzinfo=timezone.utc)


def source_report():
    return extend_coverage(base_report(), roster=None, alpaca_news=None,
                           cap_evidence=None)


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
        self.assertEqual(result["sources"]["roster"]["status"], "MISSING")
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
            "status": "DATED_ROSTER_OBSERVED", "active_count": 1, "inactive_count": 0,
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

    def test_sec_pilot_adds_only_unverified_observation_summary_and_blockers(self):
        sec = {
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
                "decision_at": "2026-10-05T12:00:00Z",
                "observations": [{
                    "accession": "0000000001-26-000001",
                    "accepted_at": "2026-10-01T15:00:00Z",
                    "report_date": "2026-09-30",
                    "fetched_at": "2026-10-05T12:01:00Z",
                    "filing_class": "quarterly",
                    "shares_count": 123456,
                }],
                "blockers": ["CLASS_COVERAGE_UNVERIFIED"],
            },
        }
        result = project_readiness(source_report(), sec, now=NOW)
        self.assertEqual(result["sources"]["shares"]["status"], "MARKET_CAP_UNVERIFIED")
        self.assertEqual(result["sources"]["shares"]["sec_observation_count"], 1)
        self.assertIn("CLASS_COVERAGE_UNVERIFIED", result["blockers"])
        self.assertNotIn("shares_count", json.dumps(result))

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


if __name__ == "__main__":
    unittest.main()
