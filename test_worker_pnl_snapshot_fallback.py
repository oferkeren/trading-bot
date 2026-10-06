import threading
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import worker_core


class FakeWait:
    def __init__(self, result):
        self.result = result
        self.cleared = False

    def clear(self):
        self.cleared = True

    def wait(self, timeout=None):
        return self.result


class FakeIB:
    def __init__(self, pnl_wait=False, daily_pnl=None):
        self.pnl_done = FakeWait(pnl_wait)
        self.daily_pnl = daily_pnl
        self.req_pnl_calls = []
        self.cancel_pnl_calls = []

    def reqPnL(self, req_id, account, model_code):
        self.req_pnl_calls.append((req_id, account, model_code))

    def cancelPnL(self, req_id):
        self.cancel_pnl_calls.append(req_id)


def snapshot_row(*, account="DUQ569670", daily_pnl=12.5, age_seconds=0):
    updated_at = (
        datetime.now(timezone.utc)
        - timedelta(seconds=age_seconds)
    ).isoformat()

    return {
        "updated_at": updated_at,
        "account": account,
        "daily_pnl": daily_pnl,
    }


class ExecutionPnlSnapshotFallbackTests(unittest.TestCase):
    def test_uses_fresh_status_snapshot_daily_pnl_when_reqpnl_times_out(self):
        ib = FakeIB(pnl_wait=False)

        with patch.object(worker_core, "IB_ACCOUNT", "DUQ569670"), \
                patch.object(worker_core, "STATUS_MAX_AGE_SECONDS", 30), \
                patch.object(
                    worker_core,
                    "get_runtime_status",
                    return_value=snapshot_row(daily_pnl=-12.5),
                ):
            result = worker_core.resolve_execution_daily_pnl(ib)

        self.assertEqual(result["daily_pnl"], -12.5)
        self.assertEqual(result["source"], "status_snapshot")
        self.assertIsNone(result["failure_code"])
        self.assertEqual(ib.req_pnl_calls, [(9901, "DUQ569670", "")])
        self.assertEqual(ib.cancel_pnl_calls, [9901])

    def test_keeps_timeout_when_snapshot_is_stale(self):
        ib = FakeIB(pnl_wait=False)

        with patch.object(worker_core, "IB_ACCOUNT", "DUQ569670"), \
                patch.object(worker_core, "STATUS_MAX_AGE_SECONDS", 30), \
                patch.object(
                    worker_core,
                    "get_runtime_status",
                    return_value=snapshot_row(age_seconds=31),
                ):
            result = worker_core.resolve_execution_daily_pnl(ib)

        self.assertIsNone(result["daily_pnl"])
        self.assertEqual(result["failure_code"], "IBKR_PNL_TIMEOUT")

    def test_keeps_timeout_when_snapshot_is_missing_or_account_mismatch(self):
        for row in (
            None,
            snapshot_row(account="OTHER"),
        ):
            ib = FakeIB(pnl_wait=False)

            with self.subTest(row=row), \
                    patch.object(worker_core, "IB_ACCOUNT", "DUQ569670"), \
                    patch.object(worker_core, "STATUS_MAX_AGE_SECONDS", 30), \
                    patch.object(
                        worker_core,
                        "get_runtime_status",
                        return_value=row,
                    ):
                result = worker_core.resolve_execution_daily_pnl(ib)

            self.assertIsNone(result["daily_pnl"])
            self.assertEqual(result["failure_code"], "IBKR_PNL_TIMEOUT")

    def test_process_signal_applies_daily_loss_block_to_snapshot_pnl(self):
        class ProcessIB(FakeIB):
            def __init__(self):
                super().__init__(pnl_wait=False)
                self.positions_done = FakeWait(True)
                self.open_orders_done = FakeWait(True)
                self.positions = {}
                self.open_orders = []

            def reqPositions(self):
                pass

            def reqAllOpenOrders(self):
                pass

            def disconnect(self):
                pass

        ib = ProcessIB()
        transitions = []
        signal = {
            "signal_id": "sig-pnl-loss",
            "symbol": "AAPL",
            "action": "BUY",
            "entry": 10.0,
            "stop": 9.0,
            "target": 12.0,
            "test_mode": 0,
        }

        def capture_transition(signal_id, status, event_type, **kwargs):
            transitions.append((signal_id, status, event_type, kwargs))

        with patch.object(worker_core, "IB_ACCOUNT", "DUQ569670"), \
                patch.object(worker_core, "LIVE_TRADING", True), \
                patch.object(worker_core, "MAX_DAILY_LOSS_USD", 100.0), \
                patch.object(worker_core, "STATUS_MAX_AGE_SECONDS", 30), \
                patch.object(worker_core, "update_signal_metadata"), \
                patch.object(worker_core, "record_event"), \
                patch.object(worker_core, "enforce_execution_freshness", return_value={"age_seconds": 0.0}), \
                patch.object(worker_core, "check_pre_execution", return_value={}), \
                patch.object(worker_core, "connect_ibkr", return_value=ib), \
                patch.object(worker_core, "check_market_session", return_value={"is_open": True, "reason": "open"}), \
                patch.object(worker_core, "evaluate_position_limits", return_value={"blockers": [], "managed_position_count": 0, "legacy_position_count": 0, "broker_position_count": 0}), \
                patch.object(worker_core, "get_runtime_status", return_value=snapshot_row(daily_pnl=-150.0)), \
                patch.object(worker_core, "transition", side_effect=capture_transition):
            worker_core.process_signal(signal)

        self.assertEqual(
            [item[2] for item in transitions],
            ["DAILY_LOSS_BLOCK"],
        )
        self.assertIn(
            "source=status_snapshot",
            transitions[0][3]["message"],
        )


if __name__ == "__main__":
    unittest.main()
