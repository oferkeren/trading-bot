import inspect
import os
import sys
import threading
import types
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
resolver = types.ModuleType("protection_position_resolver")
resolver.resolve_recovery_position = lambda **kwargs: {"status": "UNKNOWN", "reason": "test stub"}
sys.modules.setdefault("protection_position_resolver", resolver)
guard = types.ModuleType("protection_guard")
guard.evaluate_live_protection = lambda *args, **kwargs: None
sys.modules.setdefault("protection_guard", guard)

import worker_core


class FakeBar:
    date = "1791216000"
    open, high, low, close = 1.0, 1.1, 0.9, 1.05
    volume = Decimal("1500")


class HistoricalCallbackTests(unittest.TestCase):
    def test_bars_are_collected_and_end_sets_event(self):
        ib = worker_core.IBApp()
        event = threading.Event()
        ib.historical_events[5] = event
        ib.historicalData(5, FakeBar())
        ib.historicalDataEnd(5, "", "")
        self.assertTrue(event.is_set())
        self.assertEqual(ib.historical_bars[5], [{"timestamp": 1791216000, "open": 1.0, "high": 1.1,
                                                  "low": 0.9, "close": 1.05, "volume": 1500.0}])

    def test_unknown_end_is_ignored(self):
        worker_core.IBApp().historicalDataEnd(99, "", "")


class RunReboundManagerTests(unittest.TestCase):
    def test_no_work_does_not_connect(self):
        with patch("rebound_stop_manager.has_work", return_value=False), \
                patch.object(worker_core, "connect_ibkr") as connect:
            worker_core.run_rebound_manager()
        connect.assert_not_called()

    def test_live_account_does_not_connect(self):
        with patch("rebound_stop_manager.has_work", return_value=True), \
                patch.object(worker_core, "IB_PORT", 7496), \
                patch.object(worker_core, "connect_ibkr") as connect:
            worker_core.run_rebound_manager()
        connect.assert_not_called()

    def test_paper_ticks_and_disconnects(self):
        fake_ib = MagicMock()
        with patch("rebound_stop_manager.has_work", return_value=True), \
                patch.object(worker_core, "IB_PORT", 7497), \
                patch.object(worker_core, "IB_ACCOUNT", "DU123"), \
                patch.object(worker_core, "connect_ibkr", return_value=fake_ib), \
                patch("rebound_stop_manager.tick", side_effect=RuntimeError("boom")) as tick:
            with self.assertRaises(RuntimeError):
                worker_core.run_rebound_manager()
        db_file, broker = tick.call_args.args
        self.assertEqual(db_file, worker_core.DB_FILE)
        self.assertIs(broker.ib, fake_ib)
        self.assertIs(broker.wc, worker_core)
        fake_ib.disconnect.assert_called_once()

    def test_main_loop_runs_manager(self):
        source = inspect.getsource(worker_core.main)
        self.assertIn("run_rebound_manager()", source)
        self.assertIn("REBOUND_MANAGER_INTERVAL_SECONDS", source)


if __name__ == "__main__":
    unittest.main()
