import os
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import rebound_journal as journal
import rebound_ib_broker as rib
import rebound_stop_manager as manager
import trade_state
from test_rebound_ib_broker import FakeIB, fake_wc

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=NY)


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE signals (signal_id TEXT PRIMARY KEY, symbol TEXT, quantity INTEGER,
        stop REAL, status TEXT, strategy TEXT, entry_fill_price REAL, exit_fill_price REAL,
        entry_time TEXT, exit_reason TEXT, realized_pnl REAL, net_realized_pnl REAL,
        stop_order_id INTEGER, target_order_id INTEGER, entry_order_id INTEGER, parent_order_id INTEGER,
        exit_time TEXT, created_at TEXT, signal_time TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE trade_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE, signal_id TEXT, event_type TEXT NOT NULL, source TEXT NOT NULL,
        old_status TEXT, new_status TEXT, message TEXT, payload_json TEXT, created_at TEXT NOT NULL)""")
    conn.commit()
    conn.close()


def add_signal(path, sid="s1", status="OPEN_POSITION", strategy=journal.STRATEGY,
               fill=2.00, stop=1.90, entry_time="20261006 10:00:00 US/Eastern",
               created_at="2026-10-06T14:00:00+00:00", signal_time=None, parent_order_id=5):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO signals (signal_id, symbol, quantity, stop, status, strategy, "
                     "entry_fill_price, entry_time, stop_order_id, target_order_id, entry_order_id, "
                     "parent_order_id, created_at, signal_time, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, "ABC", 500, stop, status, strategy, fill, entry_time, 7, 6, 5,
                      parent_order_id, created_at, signal_time, created_at))


def set_status(path, sid, status, exit_price=None, pnl=None):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE signals SET status=?, exit_fill_price=?, realized_pnl=? WHERE signal_id=?",
                     (status, exit_price, pnl, sid))


class FakeBroker:
    def __init__(self, highs=()):
        self.highs = list(highs)
        self.calls = []
        self.fail_bars = False
        self.protection = "ACTIVE"
        self.close_result = None

    def bars_since(self, symbol, since):
        if self.fail_bars:
            raise RuntimeError("no data")
        bars = []
        for item in self.highs:
            if isinstance(item, dict):
                bars.append(dict(item))
            elif isinstance(item, tuple):
                bars.append({"high": item[0], "close": item[1]})
            else:
                bars.append({"high": item})
        return bars

    def modify_stop(self, signal, trigger):
        self.calls.append(("modify_stop", signal["signal_id"], trigger))

    def protection_state(self, signal):
        self.calls.append(("protection_state", signal["signal_id"]))
        return self.protection

    def close(self, signal, reason):
        self.calls.append(("close", signal["signal_id"], reason))
        return self.close_result


def events(path):
    with sqlite3.connect(path) as conn:
        return [r[0] for r in conn.execute("SELECT event FROM rebound_journal ORDER BY id")]


def journal_rows(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT event, reason, detail FROM rebound_journal ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"]) if item["detail"] else None
            result.append(item)
        return result


def position(path, sid):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM rebound_positions WHERE signal_id=?", (sid,)).fetchone()
        return dict(row) if row else None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "trading.db")
        make_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()


class JournalTests(Base):
    def test_terminal_statuses_match_trade_state(self):
        self.assertEqual(journal.TERMINAL_STATUSES, frozenset(trade_state.TERMINAL_STATES))
        self.assertNotIn("ERROR", journal.TERMINAL_STATUSES)
        self.assertNotIn("UNKNOWN", journal.TERMINAL_STATUSES)

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

    def test_summary_stats_include_all_closed_trades_when_trade_rows_limited(self):
        for i in range(40):
            sid = f"w{i}"
            add_signal(self.db, sid)
            set_status(self.db, sid, "CLOSED_TP", 2.1, 2.0)
        for i in range(20):
            sid = f"l{i}"
            add_signal(self.db, sid)
            set_status(self.db, sid, "CLOSED_SL", 1.9, -1.0)

        result = journal.summary(self.db, limit=50)

        self.assertEqual(
            result["stats"],
            {"trades": 60, "wins": 40, "losses": 20, "total_pnl": 60.0},
        )
        self.assertEqual(len(result["trades"]), 50)


class ManagerTests(Base):
    def test_entry_then_ratchet(self):
        add_signal(self.db)
        broker = FakeBroker(highs=[2.05])
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [("protection_state", "s1")])
        broker.highs = [2.05, 2.25]
        manager.tick(self.db, broker, T0 + timedelta(minutes=2))
        self.assertEqual(broker.calls, [("protection_state", "s1"),
                                        ("protection_state", "s1"),
                                        ("modify_stop", "s1", 2.15)])
        broker.highs = [2.10]  # the stored high is kept
        manager.tick(self.db, broker, T0 + timedelta(minutes=3))
        self.assertEqual(broker.calls.count(("modify_stop", "s1", 2.15)), 1)
        self.assertEqual(events(self.db), ["ENTRY", "STOP_MOVE"])

    def test_trail_above_last_price_requests_close(self):
        add_signal(self.db)
        broker = FakeBroker(highs=[{"high": 2.25, "close": 2.12}])
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [("protection_state", "s1"), ("close", "s1", "TRAIL_HIT")])

    def test_error_status_with_fill_is_managed_and_not_recorded_as_exit(self):
        add_signal(self.db, status="ERROR")
        self.assertTrue(journal.is_busy(self.db))
        self.assertTrue(manager.has_work(self.db))
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=20))
        self.assertEqual(broker.calls, [("protection_state", "s1"), ("close", "s1", "MAX_HOLD")])
        self.assertNotIn("EXIT", events(self.db))

    def test_max_hold_close_and_retry(self):
        add_signal(self.db)
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=20))
        self.assertEqual(broker.calls, [("protection_state", "s1"), ("close", "s1", "MAX_HOLD")])
        manager.tick(self.db, broker, T0 + timedelta(minutes=20, seconds=30))
        self.assertEqual(len(broker.calls), 2)
        manager.tick(self.db, broker, T0 + timedelta(minutes=21, seconds=1))
        self.assertEqual(broker.calls[-1], ("close", "s1", "MAX_HOLD"))

    def test_missing_stop_requests_unprotected_close_before_other_actions(self):
        add_signal(self.db)
        broker = FakeBroker(highs=[2.25])
        broker.protection = "MISSING"
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [("protection_state", "s1"), ("close", "s1", "UNPROTECTED")])
        self.assertEqual(position(self.db, "s1")["state"], "CLOSING")
        self.assertEqual(position(self.db, "s1")["close_reason"], "UNPROTECTED")

    def test_broker_child_fill_defers_to_monitor_exit_record(self):
        add_signal(self.db)
        broker = FakeBroker(highs=[2.25])
        broker.protection = "EXITED"
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [("protection_state", "s1")])
        self.assertEqual(position(self.db, "s1")["state"], "OPEN")
        self.assertEqual(events(self.db), ["ENTRY"])

        set_status(self.db, "s1", "CLOSED_SL", 1.91, -45.0)
        manager.tick(self.db, broker, T0 + timedelta(minutes=2))

        self.assertEqual(position(self.db, "s1")["state"], "CLOSED")
        self.assertEqual([row for row in journal_rows(self.db) if row["event"] == "EXIT"],
                         [{"event": "EXIT", "reason": "CLOSED_SL",
                           "detail": {"status": "CLOSED_SL", "exit_price": 1.91,
                                      "pnl": -45.0, "exit_reason": None}}])

    def test_exited_at_broker_close_result_leaves_position_closing(self):
        add_signal(self.db)
        broker = FakeBroker()
        broker.close_result = "EXITED_AT_BROKER"
        manager.tick(self.db, broker, T0 + timedelta(minutes=20))
        self.assertEqual(broker.calls, [("protection_state", "s1"), ("close", "s1", "MAX_HOLD")])
        self.assertEqual(position(self.db, "s1")["state"], "CLOSING")
        self.assertEqual(position(self.db, "s1")["close_reason"], "MAX_HOLD")
        self.assertNotIn("EXIT", events(self.db))

    def test_flat_broker_close_marks_position_closed_and_journals_exit(self):
        add_signal(self.db)
        broker = FakeBroker()
        broker.close_result = "FLAT"
        manager.tick(self.db, broker, T0 + timedelta(minutes=20))
        self.assertEqual(position(self.db, "s1")["state"], "CLOSED")
        rows = journal_rows(self.db)
        self.assertIn({"event": "EXIT", "reason": "FLAT_AT_BROKER",
                       "detail": {"status": "OPEN_POSITION", "exit_price": None,
                                  "pnl": None, "exit_reason": "FLAT_AT_BROKER"}}, rows)

    def test_real_broker_flat_protection_marks_closed_without_close_order(self):
        add_signal(self.db)
        ib = FakeIB()
        ib.open_orders[1]["status"] = "Cancelled"
        ib.open_orders[2]["status"] = "Cancelled"
        ib.positions = {}
        broker = rib.IBReboundBroker(ib, fake_wc())

        manager.tick(self.db, broker, T0 + timedelta(minutes=1))

        self.assertEqual(position(self.db, "s1")["state"], "CLOSED")
        self.assertEqual(position(self.db, "s1")["close_reason"], "FLAT_AT_BROKER")
        self.assertEqual(ib.placed, [])
        self.assertEqual([row for row in journal_rows(self.db) if row["event"] == "EXIT"],
                         [{"event": "EXIT", "reason": "FLAT_AT_BROKER",
                           "detail": {"status": "OPEN_POSITION", "exit_price": None,
                                      "pnl": None, "exit_reason": "FLAT_AT_BROKER"}}])
        self.assertFalse(manager.has_work(self.db, T0 + timedelta(minutes=1)))
        self.assertFalse(journal.is_busy(self.db))

    def test_real_broker_flatten_fill_retry_marks_closed_and_clears_work(self):
        add_signal(self.db)
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        ib.open_orders[2]["status"] = "Cancelled"
        broker = rib.IBReboundBroker(ib, fake_wc())
        first = T0 + timedelta(minutes=20)

        manager.tick(self.db, broker, first)
        self.assertEqual(position(self.db, "s1")["state"], "CLOSING")
        self.assertEqual(ib.placed[-1][2].orderRef, "rebound-flatten-s1")

        ib.positions = {}
        ib.open_orders[1]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 0, "perm_id": 0, "symbol": "ABC",
             "status": "Filled", "parent_id": 0, "action": "SELL", "order_type": "LMT",
             "total_quantity": 500.0, "order_ref": "rebound-flatten-s1"}]
        manager.tick(self.db, broker, first + timedelta(seconds=61))

        self.assertEqual(position(self.db, "s1")["state"], "CLOSED")
        self.assertEqual(position(self.db, "s1")["close_reason"], "FLAT_AT_BROKER")
        self.assertEqual(len(ib.placed), 1)
        self.assertEqual([row for row in journal_rows(self.db) if row["event"] == "EXIT"],
                         [{"event": "EXIT", "reason": "FLAT_AT_BROKER",
                           "detail": {"status": "OPEN_POSITION", "exit_price": None,
                                      "pnl": None, "exit_reason": "FLAT_AT_BROKER"}}])
        self.assertFalse(manager.has_work(self.db, first + timedelta(seconds=61)))
        self.assertFalse(journal.is_busy(self.db))

    def test_real_broker_stop_fill_defers_to_monitor(self):
        add_signal(self.db)
        ib = FakeIB()
        ib.open_orders[1]["status"] = "Cancelled"
        ib.open_orders[2]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 0, "perm_id": 0, "symbol": "ABC",
             "status": "Filled", "parent_id": 0, "action": "SELL", "order_type": "STP LMT",
             "total_quantity": 500.0, "order_ref": "TM:s1:SL"}]
        ib.positions = {}
        broker = rib.IBReboundBroker(ib, fake_wc())

        manager.tick(self.db, broker, T0 + timedelta(minutes=1))

        self.assertEqual(position(self.db, "s1")["state"], "OPEN")
        self.assertEqual(events(self.db), ["ENTRY"])
        self.assertEqual(ib.placed, [])

    def test_stale_unfilled_entry_requests_cancel_once(self):
        add_signal(self.db, status="SUBMITTED", fill=None, created_at="2026-10-06T13:55:00+00:00")
        self.assertTrue(manager.has_work(self.db, now=T0))
        broker = FakeBroker()
        manager.tick(self.db, broker, T0)
        manager.tick(self.db, broker, T0 + timedelta(seconds=10))
        with sqlite3.connect(self.db) as conn:
            status = conn.execute("SELECT status FROM signals WHERE signal_id='s1'").fetchone()[0]
            trade_events = conn.execute("SELECT event_type, source, old_status, new_status FROM trade_events").fetchall()
        self.assertEqual(status, "CANCEL_REQUESTED")
        self.assertEqual(trade_events, [("CANCEL_REQUESTED", "rebound_stop_manager", "SUBMITTED", "CANCEL_REQUESTED")])
        self.assertEqual([row for row in journal_rows(self.db) if row["event"] == "ENTRY_EXPIRED"],
                         [{"event": "ENTRY_EXPIRED", "reason": "ENTRY_TIMEOUT",
                           "detail": {"age_seconds": 300}}])
        self.assertEqual(broker.calls, [])

    def test_stale_unfilled_entry_is_not_cancel_requested_after_expiry_record_exists(self):
        add_signal(self.db, status="SUBMITTED", fill=None, created_at="2026-10-06T13:55:00+00:00")
        broker = FakeBroker()
        manager.tick(self.db, broker, T0)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE signals SET status='SUBMITTED' WHERE signal_id='s1'")
        self.assertFalse(manager.has_work(self.db, now=T0 + timedelta(seconds=10)))
        manager.tick(self.db, broker, T0 + timedelta(seconds=10))
        with sqlite3.connect(self.db) as conn:
            status = conn.execute("SELECT status FROM signals WHERE signal_id='s1'").fetchone()[0]
            trade_events = conn.execute("SELECT event_type, source, old_status, new_status FROM trade_events").fetchall()
        self.assertEqual(status, "SUBMITTED")
        self.assertEqual(trade_events, [("CANCEL_REQUESTED", "rebound_stop_manager", "SUBMITTED", "CANCEL_REQUESTED")])
        self.assertEqual(len([row for row in journal_rows(self.db) if row["event"] == "ENTRY_EXPIRED"]), 1)

    def test_cancel_unknown_unfilled_entry_is_not_cancel_requested(self):
        add_signal(self.db, status="CANCEL_UNKNOWN", fill=None, created_at="2026-10-06T13:55:00+00:00")
        self.assertFalse(manager.has_work(self.db, now=T0))
        broker = FakeBroker()
        manager.tick(self.db, broker, T0)
        with sqlite3.connect(self.db) as conn:
            status = conn.execute("SELECT status FROM signals WHERE signal_id='s1'").fetchone()[0]
            trade_events = conn.execute("SELECT event_type FROM trade_events").fetchall()
        self.assertEqual(status, "CANCEL_UNKNOWN")
        self.assertEqual(trade_events, [])
        self.assertEqual(journal_rows(self.db), [])

    def test_unparseable_unfilled_entry_time_expires(self):
        add_signal(self.db, status="ACCEPTED_WAITING_MARKET", fill=None, created_at="not-a-date")
        manager.tick(self.db, FakeBroker(), T0)
        with sqlite3.connect(self.db) as conn:
            status = conn.execute("SELECT status FROM signals WHERE signal_id='s1'").fetchone()[0]
        self.assertEqual(status, "CANCEL_REQUESTED")

    def test_partial_fill_is_managed_not_cancelled_as_stale_entry(self):
        add_signal(self.db, status="SUBMITTED", fill=2.0, created_at="2026-10-06T13:55:00+00:00")
        broker = FakeBroker()
        manager.tick(self.db, broker, T0 + timedelta(minutes=20))
        self.assertEqual(broker.calls, [("protection_state", "s1"), ("close", "s1", "MAX_HOLD")])

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
        self.assertEqual(broker.calls, [("protection_state", "s1"), ("close", "s1", "MAX_HOLD")])
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
        self.assertEqual(broker.calls, [("protection_state", "a"),
                                        ("close", "a", "STOP_MODIFY_FAILED"),
                                        ("protection_state", "b"),
                                        ("modify_stop", "b", 2.15)])
        self.assertIn("ERROR", events(self.db))

    def test_stop_modify_failure_sets_position_closing(self):
        add_signal(self.db, "a")
        broker = FakeBroker(highs=[2.25])

        def fail_modify(signal, trigger):
            raise RuntimeError("ib down")
        broker.modify_stop = fail_modify
        manager.tick(self.db, broker, T0 + timedelta(minutes=1))
        self.assertEqual(broker.calls, [("protection_state", "a"),
                                        ("close", "a", "STOP_MODIFY_FAILED")])
        self.assertEqual(position(self.db, "a")["state"], "CLOSING")
        self.assertEqual(position(self.db, "a")["close_reason"], "STOP_MODIFY_FAILED")

    def test_stop_modify_failure_close_failure_waits_until_retry_window(self):
        add_signal(self.db, "a")
        broker = FakeBroker(highs=[2.25])

        def fail_modify(signal, trigger):
            broker.calls.append(("modify_stop", signal["signal_id"], trigger))
            raise RuntimeError("modify down")

        def fail_close(signal, reason):
            broker.calls.append(("close", signal["signal_id"], reason))
            raise RuntimeError("close down")

        broker.modify_stop = fail_modify
        broker.close = fail_close

        first = T0 + timedelta(minutes=1)
        for offset in (0, 5, 10):
            manager.tick(self.db, broker, first + timedelta(seconds=offset))

        self.assertEqual(broker.calls, [("protection_state", "a"),
                                        ("modify_stop", "a", 2.15),
                                        ("close", "a", "STOP_MODIFY_FAILED")])
        self.assertEqual(position(self.db, "a")["state"], "CLOSING")
        self.assertEqual(position(self.db, "a")["close_reason"], "STOP_MODIFY_FAILED")
        rows = journal_rows(self.db)
        self.assertIn({"event": "ERROR", "reason": "STOP_MODIFY_FAILED",
                       "detail": {"error": "RuntimeError('modify down')"}}, rows)
        self.assertIn({"event": "ERROR", "reason": "CLOSE_FAILED",
                       "detail": {"close_reason": "STOP_MODIFY_FAILED",
                                  "error": "RuntimeError('close down')"}}, rows)

        manager.tick(self.db, broker, first + timedelta(seconds=61))
        self.assertEqual(broker.calls, [("protection_state", "a"),
                                        ("modify_stop", "a", 2.15),
                                        ("close", "a", "STOP_MODIFY_FAILED"),
                                        ("close", "a", "STOP_MODIFY_FAILED")])

    def test_max_hold_close_failure_waits_until_retry_window(self):
        add_signal(self.db)
        broker = FakeBroker()

        def fail_close(signal, reason):
            broker.calls.append(("close", signal["signal_id"], reason))
            raise RuntimeError("close down")

        broker.close = fail_close

        first = T0 + timedelta(minutes=20)
        manager.tick(self.db, broker, first)

        self.assertEqual(broker.calls, [("protection_state", "s1"), ("close", "s1", "MAX_HOLD")])
        self.assertEqual(position(self.db, "s1")["state"], "CLOSING")
        rows = journal_rows(self.db)
        self.assertIn({"event": "ERROR", "reason": "CLOSE_FAILED",
                       "detail": {"close_reason": "MAX_HOLD",
                                  "error": "RuntimeError('close down')"}}, rows)

        manager.tick(self.db, broker, first + timedelta(seconds=30))
        self.assertEqual(len(broker.calls), 2)
        manager.tick(self.db, broker, first + timedelta(seconds=61))
        self.assertEqual(broker.calls, [("protection_state", "s1"),
                                        ("close", "s1", "MAX_HOLD"),
                                        ("close", "s1", "MAX_HOLD")])

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
        self.assertEqual(manager.parse_ib_time("20261006 17:00:00 Asia/Jerusalem"),
                         datetime(2026, 10, 6, 10, 0, tzinfo=NY))
        self.assertIsNone(manager.parse_ib_time("20261006 17:00:00 Bogus/Zone"))
        self.assertIsNone(manager.parse_ib_time("garbage"))


if __name__ == "__main__":
    unittest.main()
