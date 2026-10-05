import copy
import json
import os
import queue
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from microcap_readiness import project_readiness
from microcap_readiness_store import read_readiness
from test_microcap_readiness import source_report


NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def snapshot(at=NOW):
    return project_readiness(source_report(), now=at)


def external_tempdir():
    return tempfile.TemporaryDirectory(prefix="microcap-readiness-", dir=Path.home())


def configured_read(data, now=NOW):
    payload = data if isinstance(data, bytes) else json.dumps(data).encode()
    with external_tempdir() as directory:
        path = Path(directory) / "snapshot.json"
        path.write_bytes(payload)
        return read_readiness(str(path), now=now)


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

    def test_fifo_swapped_after_validation_fails_closed_without_blocking(self):
        with external_tempdir() as directory:
            fifo = Path(directory) / "snapshot.json"
            os.mkfifo(fifo)
            results = queue.Queue()

            def read_fifo():
                with patch("microcap_readiness_store._external", return_value=fifo):
                    results.put(read_readiness(str(fifo), now=NOW))

            thread = threading.Thread(target=read_fifo, daemon=True)
            thread.start()
            thread.join(0.25)
            if thread.is_alive():
                for _ in range(20):
                    try:
                        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                        break
                    except OSError:
                        fd = None
                if fd is not None:
                    try:
                        os.write(fd, b"{")
                    finally:
                        os.close(fd)
                thread.join(1)
                self.fail("read_readiness blocked on a FIFO snapshot path")
            self.assertEqual(results.get_nowait()["status"], "UNAVAILABLE")

    def test_symlink_final_component_is_rejected(self):
        with external_tempdir() as directory:
            target = Path(directory) / "target.json"
            link = Path(directory) / "snapshot.json"
            target.write_bytes(json.dumps(snapshot()).encode())
            link.symlink_to(target)
            with patch("microcap_readiness_store._external", return_value=link):
                self.assertEqual(read_readiness(str(link), now=NOW)["status"],
                                 "UNAVAILABLE")

    def test_inode_mismatch_after_validation_fails_closed(self):
        with external_tempdir() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_bytes(json.dumps(snapshot()).encode())
            current = os.stat(path)
            fields = list(current)
            fields[1] += 1
            mismatched = os.stat_result(fields)
            with patch("os.fstat", return_value=mismatched):
                self.assertEqual(read_readiness(str(path), now=NOW)["status"],
                                 "UNAVAILABLE")

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

    def test_schema_2_snapshot_is_current_and_forged_one_is_unavailable(self):
        from microcap_batch_schema import project_batch_snapshot
        from test_microcap_batch_schema import batch_report
        value = project_batch_snapshot(batch_report(), now=NOW - timedelta(seconds=30))
        result = configured_read(value)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["samples"], value["samples"])
        forged = copy.deepcopy(value)
        forged["order_approval"] = True
        self.assertEqual(configured_read(forged)["status"], "UNAVAILABLE")
        forged = copy.deepcopy(value)
        forged["schema_version"] = 3
        self.assertEqual(configured_read(forged)["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
