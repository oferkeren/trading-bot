import copy
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from microcap_readiness import _repository_roots, project_readiness
from microcap_readiness_store import read_readiness
from test_microcap_readiness import source_report


NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def snapshot(at=NOW):
    return project_readiness(source_report(), now=at)


def configured_read(data, now=NOW):
    payload = data if isinstance(data, bytes) else json.dumps(data).encode()
    roots = _repository_roots()
    with patch("microcap_readiness._repository_roots", return_value=roots), \
            patch("pathlib.Path.open", return_value=io.BytesIO(payload)):
        return read_readiness("/etc/hosts", now=now)


class ReadReadinessTests(unittest.TestCase):
    def test_missing_configuration_or_file_fails_closed(self):
        for path in (None, "", "/missing/microcap-readiness.json"):
            with self.subTest(path=path):
                self.assertEqual(read_readiness(path, now=NOW), {
                    "status": "UNAVAILABLE", "decision": "NO_TRADE",
                    "order_approval": False, "model_calibrated": False,
                    "target_probabilities": "unavailable",
                    "blockers": ["READINESS_UNAVAILABLE"],
                })

    def test_repository_paths_and_relative_path_are_rejected(self):
        main = Path(__file__).resolve().parents[2]
        for path in (str(Path(__file__).resolve()), str(main / "signal_server.py"),
                     "readiness.json"):
            with self.subTest(path=path):
                self.assertEqual(read_readiness(path, now=NOW)["status"], "UNAVAILABLE")
        with Path(__file__).open("rb") as source:
            symlink = f"/proc/self/fd/{source.fileno()}"
            self.assertEqual(read_readiness(symlink, now=NOW)["status"], "UNAVAILABLE")

    def test_invalid_json_oversized_and_duplicate_keys_fail_closed(self):
        for data in (b"{", b'{"status":1,"status":2}', b"x" * (2 * 1024 * 1024 + 1)):
            with self.subTest(data=data[:30]):
                self.assertEqual(configured_read(data)["status"], "UNAVAILABLE")

    def test_rejects_unsupported_version_forged_approval_and_malformed_schema(self):
        for change in (
            lambda value: value.update(schema_version=2),
            lambda value: value.update(order_approval=True),
            lambda value: value.update(decision="TRADE"),
            lambda value: value["sources"]["shares"].update(status="VERIFIED"),
            lambda value: value["sources"]["news"].update(status="provider text"),
            lambda value: value["sources"]["news"].update(article_count=True),
            lambda value: value["sources"]["sec"].update(status="OBSERVED"),
            lambda value: value["blockers"].append("provider text"),
            lambda value: value["blockers"].remove("MARKET_CAP_UNVERIFIED"),
            lambda value: value.pop("sample_window"),
            lambda value: value["sample"].update(symbol="<script>alert(1)</script>"),
            lambda value: value.update(raw_provider_text="private credential"),
        ):
            value = copy.deepcopy(snapshot())
            change(value)
            with self.subTest(value=value):
                result = configured_read(value)
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertNotIn("private credential", json.dumps(result))

    def test_future_timestamp_fails_closed(self):
        result = configured_read(snapshot(NOW + timedelta(seconds=1)))
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["blockers"], ["READINESS_UNAVAILABLE"])

    def test_more_than_seven_days_old_is_stale_without_previous_claims(self):
        result = configured_read(snapshot(NOW - timedelta(days=7, seconds=1)))
        self.assertEqual(result, {
            "status": "STALE", "decision": "NO_TRADE",
            "order_approval": False, "model_calibrated": False,
            "target_probabilities": "unavailable",
            "blockers": ["READINESS_STALE"],
        })

    def test_valid_snapshot_contains_safe_fields_and_age(self):
        value = snapshot(NOW - timedelta(seconds=90))
        result = configured_read(value)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(result["age_seconds"], 90)
        self.assertEqual(result["generated_at"], value["generated_at"])
        self.assertEqual(result["sample"], value["sample"])
        self.assertEqual(result["sources"], value["sources"])
        self.assertEqual(result["decision"], "NO_TRADE")

    def test_seven_days_exactly_is_current(self):
        self.assertEqual(
            configured_read(snapshot(NOW - timedelta(days=7)))["status"], "CURRENT"
        )

    def test_bad_now_and_mutated_fallback_do_not_leak_into_later_requests(self):
        self.assertEqual(configured_read(snapshot(), now=datetime(2026, 10, 5))["status"],
                         "UNAVAILABLE")
        first = read_readiness(None, now=NOW)
        first["blockers"].append("MALICIOUS")
        self.assertEqual(read_readiness(None, now=NOW)["blockers"],
                         ["READINESS_UNAVAILABLE"])


if __name__ == "__main__":
    unittest.main()
