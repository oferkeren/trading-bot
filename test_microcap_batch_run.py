import json
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

from microcap_batch_schema import DEFAULT_RULE, BIAS, manifest_hash, session_window
from microcap_batch_run import load_sample_result, run_sample
from microcap_coverage_probe import _roster_status, coverage_report
from microcap_source_probe import extend_coverage
from test_microcap_readiness import sec_report


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
START, END = session_window(datetime(2025, 5, 28).date())
SCRATCH = Path(__file__).resolve().parent / ".test-scratch-microcap-batch-run"


def sample():
    return {"issuer_id": "0000000001", "symbol": "SORA", "company": "Sora Example",
            "date": "2025-05-28", "move_pct": 52.3, "start_utc": START, "end_utc": END}


def ibkr_report():
    roster_status, _ = _roster_status({}, datetime(2025, 5, 28, tzinfo=timezone.utc),
                                      pilot=True)
    report = coverage_report(
        roster_status,
        [{"issuer_id": "0000000001", "date": "2025-05-28", "session": "regular",
          "provider": "ibkr",
          "bars": {"status": "observed",
                   "intervals": {"1 min": {"count": 3}, "1 hour": {"count": 2}}},
          "quotes": {"status": "observed"}, "news": {"status": "unavailable"}}],
        sample_manifest={"status": "predeclared", "issuer_ids": ["0000000001"],
                         "dates": ["2025-05-28"]},
    )
    report["request_window"] = {"start_utc": START, "end_utc": END}
    return report


def session_sec_report():
    value = sec_report()
    value["market_cap_gate"]["decision_at"] = START
    return value


def arg(args, name):
    return args[args.index(name) + 1]


class FakeRunner:
    def __init__(self, ibkr=(0, None), source=0, sec=0, timeout=()):
        self.ibkr, self.source, self.sec, self.timeout = ibkr, source, sec, set(timeout)
        self.calls = []

    def __call__(self, script, args):
        self.calls.append(script)
        if script in self.timeout:
            raise subprocess.TimeoutExpired(script, 300)
        if script == "microcap_coverage_probe.py":
            code, body = self.ibkr
            payload = body if body is not None else json.dumps(ibkr_report()).encode()
            return code, payload
        if script == "microcap_source_probe.py":
            base = json.loads(Path(arg(args, "--base-report")).read_text())
            report = extend_coverage(base, roster=None, alpaca_news=None, cap_evidence=None)
            return self.source, json.dumps(report).encode()
        if script == "microcap_sec_probe.py":
            if self.sec == 0:
                Path(arg(args, "--output")).write_text(json.dumps(session_sec_report()))
            return self.sec, b""
        raise AssertionError(script)


class RunSampleTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(mode=0o700)
        self.dir = SCRATCH / "2025-05-28-0000000001"

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def run_with(self, runner):
        return run_sample(sample(), self.dir, runner=runner, manifest_sha256="a" * 64, now=NOW)

    def test_all_stages_ok(self):
        runner = FakeRunner()
        result = self.run_with(runner)
        self.assertEqual(runner.calls, ["microcap_coverage_probe.py",
                                        "microcap_source_probe.py", "microcap_sec_probe.py"])
        self.assertEqual(result["stages"], {"ibkr": "OK", "source": "OK", "sec": "OK"})
        row = result["row"]
        self.assertEqual((row["ibkr_minute"], row["ibkr_quotes"], row["sec_observations"],
                          row["stage_errors"]), (True, True, 1, []))
        self.assertEqual(row["roster"], "MISSING")
        self.assertIsNone(row["news_count"])
        self.assertEqual(load_sample_result(self.dir, sample(), "a" * 64), result)
        roster = json.loads((self.dir / "roster.json").read_text())
        self.assertEqual(roster["coverage_claim"], "pilot-unverified")
        self.assertEqual(roster["members"][0]["valid_from"], "2025-05-28T00:00:00Z")
        self.assertNotIn("symbol", json.loads((self.dir / "ibkr-manifest.json").read_text()))

    def test_ibkr_exit_3_keeps_report(self):
        result = self.run_with(FakeRunner(ibkr=(3, None)))
        self.assertEqual(result["stages"]["ibkr"], "PROVIDER_ERROR")
        self.assertEqual(result["stages"]["source"], "OK")
        self.assertTrue(result["row"]["ibkr_minute"])
        self.assertEqual(result["row"]["stage_errors"], ["ibkr:PROVIDER_ERROR"])
        self.assertIn("SAMPLE_INCOMPLETE", result["blockers"])

    def test_ibkr_failure_skips_source_but_sec_still_runs(self):
        for runner, status in ((FakeRunner(ibkr=(2, b"")), "PROVIDER_ERROR"),
                               (FakeRunner(ibkr=(0, b"not json")), "INVALID"),
                               (FakeRunner(timeout={"microcap_coverage_probe.py"}), "TIMEOUT")):
            with self.subTest(status=status):
                result = self.run_with(runner)
                self.assertNotIn("microcap_source_probe.py", runner.calls)
                self.assertEqual(result["stages"],
                                 {"ibkr": status, "source": "SKIPPED", "sec": "OK"})
                self.assertEqual(result["row"]["sec_observations"], 1)
                self.assertFalse(result["row"]["ibkr_minute"])

    def test_sec_failure_keeps_source_row(self):
        result = self.run_with(FakeRunner(sec=3))
        self.assertEqual(result["stages"]["sec"], "PROVIDER_ERROR")
        self.assertEqual(result["row"]["sec_observations"], 0)
        self.assertTrue(result["row"]["ibkr_quotes"])

    def test_identity_mismatch_marks_source_invalid(self):
        other = sample()
        other["issuer_id"] = "0000000002"
        result = run_sample(other, self.dir, runner=FakeRunner(),
                            manifest_sha256="a" * 64, now=NOW)
        self.assertEqual(result["stages"]["source"], "INVALID")
        self.assertFalse(result["row"]["ibkr_minute"])

    def test_tampered_or_foreign_result_is_not_resumed(self):
        self.run_with(FakeRunner())
        self.assertIsNone(load_sample_result(self.dir, sample(), "b" * 64))
        path = self.dir / "sample-result.json"
        value = json.loads(path.read_text())
        value["row"]["sec_observations"] = 9
        path.write_text(json.dumps(value))
        self.assertIsNone(load_sample_result(self.dir, sample(), "a" * 64))


if __name__ == "__main__":
    unittest.main()
