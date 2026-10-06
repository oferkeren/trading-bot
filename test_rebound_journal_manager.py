import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import rebound_journal as journal
import rebound_stop_manager as manager

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=NY)


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE signals (signal_id TEXT PRIMARY KEY, symbol TEXT, quantity INTEGER,
        stop REAL, status TEXT, strategy TEXT, entry_fill_price REAL, exit_fill_price REAL,
        entry_time TEXT, exit_reason TEXT, realized_pnl REAL, net_realized_pnl REAL,
        stop_order_id INTEGER, exit_time TEXT)""")
    conn.commit()
    conn.close()


def add_signal(path, sid="s1", status="OPEN_POSITION", strategy=journal.STRATEGY,
               fill=2.00, stop=1.90, entry_time="20261006 10:00:00 US/Eastern"):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO signals (signal_id, symbol, quantity, stop, status, strategy, "
                     "entry_fill_price, entry_time, stop_order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                     (sid, "ABC", 500, stop, status, strategy, fill, entry_time, 7))


def set_status(path, sid, status, exit_price=None, pnl=None):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE signals SET status=?, exit_fill_price=?, realized_pnl=? WHERE signal_id=?",
                     (status, exit_price, pnl, sid))


class FakeBroker:
    def __init__(self, highs=()):
        self.highs = list(highs)
        self.calls = []
        self.fail_bars = False

    def bars_since(self, symbol, since):
        if self.fail_bars:
            raise RuntimeError("no data")
        return [{"high": h} for h in self.highs]

    def modify_stop(self, signal, trigger):
        self.calls.append(("modify_stop", signal["signal_id"], trigger))

    def close(self, signal, reason):
        self.calls.append(("close", signal["signal_id"], reason))


def events(path):
    with sqlite3.connect(path) as conn:
        return [r[0] for r in conn.execute("SELECT event FROM rebound_journal ORDER BY id")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "trading.db")
        make_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()


class JournalTests(Base):
    def test_is_busy(self):
        self.assertFalse(journal.is_busy(self.db))
        add_signal(self.db, "old", strategy="scalp_pingpong_v1")
        self.assertFalse(journal.is_busy(self.db))
        add_signal(self.db, "a", status="SUBMITTED")
        self.assertTrue(journal.is_busy(self.db))
        set_status(self.db, "a", "CLOSED_TP")
        self.assertFalse(journal.is_busy(self.db))
        add_signal(self.db, "b", status=None)
        self.assertTrue(journal.is_busy(self.db))

    def test_missing_db_or_table_is_busy(self):
        self.assertTrue(journal.is_busy(os.path.join(self.tmp.name, "missing.db")))
        empty = os.path.join(self.tmp.name, "empty.db")
        sqlite3.connect(empty).close()
        self.assertTrue(journal.is_busy(empty))

    def test_record_skip_dedupes_for_ten_minutes(self):
        self.assertTrue(journal.record_skip(self.db, "ABC", "SKIP_SPREAD", now=T0))
        self.assertFalse(journal.record_skip(self.db, "ABC", "SKIP_SPREAD", now=T0 + timedelta(minutes=9)))
        self.assertTrue(journal.record_skip(self.db, "ABC", "SKIP_NEWS", now=T0 + timedelta(minutes=9)))
        self.assertTrue(journal.record_skip(self.db, "ABC", "SKIP_SPREAD", now=T0 + timedelta(minutes=11)))

    def test_summary(self):
        add_signal(self.db, "w")
        set_status(self.db, "w", "CLOSED_TP", 2.3, 150.0)
        add_signal(self.db, "l")
        set_status(self.db, "l", "CLOSED_SL", 1.9, -50.0)
        journal.record(self.db, "SKIP", symbol="X", reason="R", detail={"a": 1}, now=T0)
        result = journal.summary(self.db)
        self.assertEqual(result["stats"], {"trades": 2, "wins": 1, "losses": 1, "total_pnl": 100.0})
        self.assertEqual(result["events"][0]["detail"], {"a": 1})


class ManagerTests(Base):
    def test_entry_then_ratchet(self):
        add_signal(self.db)
        broker = FakeBroker(highs=[2.05])
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [])
        broker.highs = [2.05, 2.25]
        manager.tick(self.db, broker, T0 + timedelta(minutes=2))
        self.assertEqual(broker.calls, [("modify_stop", "s1", 2.15)])
        broker.highs = [2.10]  # the stored high is kept
        manager.tick(self.db, broker, T0 + timedelta(minutes=3))
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(events(self.db), ["ENTRY", "STOP_MOVE"])

    def test_max_hold_close_and_retry(self):
        add_signal(self.db)
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=20))
        self.assertEqual(broker.calls, [("close", "s1", "MAX_HOLD")])
        manager.tick(self.db, broker, T0 + timedelta(minutes=20, seconds=30))
        self.assertEqual(len(broker.calls), 1)
        manager.tick(self.db, broker, T0 + timedelta(minutes=21, seconds=1))
        self.assertEqual(broker.calls[-1], ("close", "s1", "MAX_HOLD"))

    def test_exit_recorded_once(self):
        add_signal(self.db)
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        set_status(self.db, "s1", "CLOSED_SL", 1.9, -50.0)
        manager.tick(self.db, broker, T0 + timedelta(minutes=2))
        manager.tick(self.db, broker, T0 + timedelta(minutes=3))
        self.assertEqual(events(self.db), ["ENTRY", "EXIT"])

    def test_bar_failure_still_enforces_time_exit(self):
        add_signal(self.db)
        broker = FakeBroker()
        broker.fail_bars = True
        manager.tick(self.db, broker, T0 + timedelta(minutes=25))
        self.assertEqual(broker.calls, [("close", "s1", "MAX_HOLD")])
        self.assertIn("ERROR", events(self.db))

    def test_broker_error_is_journaled_and_other_positions_continue(self):
        add_signal(self.db, "a")
        add_signal(self.db, "b")
        broker = FakeBroker(highs=[2.25])
        original = broker.modify_stop

        def flaky(signal, trigger):
            if signal["signal_id"] == "a":
                raise RuntimeError("ib down")
            original(signal, trigger)
        broker.modify_stop = flaky
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [("modify_stop", "b", 2.15)])
        self.assertIn("ERROR", events(self.db))

    def test_ignores_other_strategies(self):
        add_signal(self.db, strategy="scalp_pingpong_v1")
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=30))
        self.assertEqual(broker.calls, [])

    def test_has_work(self):
        self.assertFalse(manager.has_work(os.path.join(self.tmp.name, "missing.db")))
        self.assertFalse(manager.has_work(self.db))
        add_signal(self.db, strategy="scalp_pingpong_v1")
        self.assertFalse(manager.has_work(self.db))
        add_signal(self.db, "r1")
        self.assertTrue(manager.has_work(self.db))
        manager.tick(self.db, FakeBroker(), T0 + timedelta(minutes=1))
        set_status(self.db, "r1", "CLOSED_SL")
        self.assertTrue(manager.has_work(self.db))  # position row still needs its EXIT record
        manager.tick(self.db, FakeBroker(), T0 + timedelta(minutes=2))
        self.assertFalse(manager.has_work(self.db))

    def test_parse_ib_time(self):
        self.assertEqual(manager.parse_ib_time("20261006 10:00:00 US/Eastern"), T0)
        self.assertIsNone(manager.parse_ib_time("garbage"))


if __name__ == "__main__":
    unittest.main()
