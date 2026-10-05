import copy
import unittest
from datetime import date, datetime, timezone

from microcap_batch_schema import (
    BIAS, DEFAULT_RULE, coverage_from_rows, manifest_hash, project_batch_snapshot,
    session_window, validate_batch_report, validate_manifest, validate_row,
    validate_snapshot_v2,
)
from microcap_history import CoverageError


NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def sample(cik="0000000001", symbol="SORA", day="2025-05-28", move=52.3):
    start, end = session_window(date.fromisoformat(day))
    return {"issuer_id": cik, "symbol": symbol, "company": "Sora Example",
            "date": day, "move_pct": move, "start_utc": start, "end_utc": end}


def manifest(samples=None):
    body = {
        "schema_version": 1, "kind": "microcap_batch_manifest",
        "created_at": "2026-10-05T10:00:00Z", "as_of": "2025-05-28",
        "rule": dict(DEFAULT_RULE),
        "trading_days": [{"date": "2025-05-28", "request_id": "req-1"}],
        "candidates": [{"date": "2025-05-28", "ticker": "SORA", "move_pct": 52.3,
                        "outcome": "SELECTED"}],
        "samples": samples if samples is not None else [sample()],
        "bias": list(BIAS),
    }
    body["sha256"] = manifest_hash(body)
    return body


def row(**changes):
    value = {"date": "2025-05-28", "symbol": "SORA", "issuer_id": "0000000001",
             "move_pct": 52.3, "ibkr_minute": True, "ibkr_quotes": True,
             "roster": "DATED_ROSTER_OBSERVED", "news_count": 2,
             "sec_observations": 1, "stage_errors": ["ibkr:PROVIDER_ERROR"]}
    value.update(changes)
    return value


def batch_report(rows=None):
    rows = rows if rows is not None else [row()]
    return {
        "kind": "microcap_batch_report", "schema_version": 1,
        "generated_at": "2026-10-05T11:00:00Z", "decision": "NO_TRADE",
        "order_approval": False, "model_calibrated": False,
        "target_probabilities": "unavailable",
        "batch": {"as_of": "2025-05-28", "manifest_sha256": "a" * 64,
                  "sample_count": len(rows), "rule": dict(DEFAULT_RULE),
                  "bias": list(BIAS)},
        "coverage": coverage_from_rows(rows), "samples": rows,
        "blockers": ["MARKET_CAP_UNVERIFIED", "RESEARCH_ONLY_NOT_CALIBRATED",
                     "ROSTER_COVERAGE_UNVERIFIED"],
    }


class SessionWindowTests(unittest.TestCase):
    def test_regular_session_converts_daylight_and_standard_time(self):
        self.assertEqual(session_window(date(2025, 5, 28)),
                         ("2025-05-28T13:30:00Z", "2025-05-28T20:00:00Z"))
        self.assertEqual(session_window(date(2025, 12, 1)),
                         ("2025-12-01T14:30:00Z", "2025-12-01T21:00:00Z"))


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_passes(self):
        validate_manifest(manifest())

    def test_edited_manifest_is_rejected_by_hash(self):
        value = manifest()
        value["samples"][0]["symbol"] = "EDIT"
        with self.assertRaises(CoverageError):
            validate_manifest(value)

    def test_structural_violations_are_rejected(self):
        two = [sample(), sample(symbol="ABCD")]
        for value in (
            manifest(samples=[]),
            manifest(samples=two),  # duplicate issuer
            manifest(samples=[dict(sample(), start_utc="2025-05-28T13:00:00Z")]),
            manifest(samples=[dict(sample(), symbol="BRK.A")]),
            manifest(samples=[dict(sample(), issuer_id="123")]),
        ):
            with self.subTest(value=value["samples"]), self.assertRaises(CoverageError):
                validate_manifest(value)

    def test_bias_and_kind_are_fixed(self):
        for change in (lambda v: v.update(bias=["RUNNER_SCREEN_SELECTED"]),
                       lambda v: v.update(kind="other"),
                       lambda v: v.update(extra=True)):
            value = manifest()
            change(value)
            value["sha256"] = manifest_hash(value)
            with self.assertRaises(CoverageError):
                validate_manifest(value)


class RowAndReportTests(unittest.TestCase):
    def test_valid_row_and_coverage(self):
        validate_row(row())
        rows = [row(), row(issuer_id="0000000002", symbol="ABCD", ibkr_minute=False,
                           news_count=None, roster="MISSING", sec_observations=0,
                           stage_errors=["ibkr:TIMEOUT", "source:SKIPPED"])]
        self.assertEqual(coverage_from_rows(rows), {
            "ibkr_minute": {"observed": 1, "total": 2},
            "ibkr_quotes": {"observed": 2, "total": 2},
            "roster_dated": {"observed": 1, "total": 2},
            "news_found": {"observed": 1, "total": 2},
            "sec_shares": {"observed": 1, "total": 2},
        })

    def test_invalid_rows_are_rejected(self):
        for value in (row(stage_errors=["ibkr:OK"]), row(roster="provider text"),
                      row(news_count=True), row(sec_observations=3),
                      row(symbol="<b>"), row(move_pct=float("nan")),
                      row(stage_errors=["source:SKIPPED", "ibkr:TIMEOUT"]),
                      dict(row(), raw="x")):
            with self.subTest(value=value), self.assertRaises(CoverageError):
                validate_row(value)

    def test_report_coverage_must_match_rows(self):
        validate_batch_report(batch_report())
        value = batch_report()
        value["coverage"]["news_found"]["observed"] = 0
        with self.assertRaises(CoverageError):
            validate_batch_report(value)

    def test_report_rejects_approval_unknown_blocker_and_duplicates(self):
        for change in (lambda v: v.update(order_approval=True),
                       lambda v: v["blockers"].append("ZZZ_PROVIDER_TEXT"),
                       lambda v: v["blockers"].remove("MARKET_CAP_UNVERIFIED"),
                       lambda v: v.update(samples=[row(), row()])):
            value = batch_report()
            change(value)
            with self.assertRaises(CoverageError):
                validate_batch_report(value)


class SnapshotTests(unittest.TestCase):
    def test_projection_produces_valid_schema_2_snapshot(self):
        snapshot = project_batch_snapshot(batch_report(), now=NOW)
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["generated_at"], "2026-10-05T12:00:00Z")
        self.assertEqual(snapshot["decision"], "NO_TRADE")
        self.assertNotIn("kind", snapshot)
        self.assertEqual(validate_snapshot_v2(snapshot), NOW)

    def test_snapshot_validator_rejects_forged_fields(self):
        base = project_batch_snapshot(batch_report(), now=NOW)
        for change in (lambda v: v.update(schema_version=1),
                       lambda v: v.update(model_calibrated=True),
                       lambda v: v["batch"].update(sample_count=5),
                       lambda v: v.update(raw_provider_text="private")):
            value = copy.deepcopy(base)
            change(value)
            with self.assertRaises(CoverageError):
                validate_snapshot_v2(value)


if __name__ == "__main__":
    unittest.main()
