import threading
import types
import unittest
from datetime import datetime, timedelta, timezone

import rebound_ib_broker as rib
import worker_core


def fake_wc():
    wc = types.SimpleNamespace(IB_ACCOUNT="DU1", ALLOW_OUTSIDE_RTH=True)
    wc.stock_contract = lambda symbol: ("contract", symbol)
    wc.load_order_state = lambda ib: None
    wc.load_position_state = lambda ib: {symbol: dict(pos) for symbol, pos in ib.positions.items()}
    wc.get_safe_parent_order_id = lambda ib_next_order_id: ib_next_order_id
    wc.check_market_session = lambda ib, symbol: {"market_rule": [], "min_tick": 0.01}
    wc.normalize_price_to_market_rule = worker_core.normalize_price_to_market_rule
    wc.build_stop_limit_price = worker_core.build_stop_limit_price
    return wc


class FakeIB:
    def __init__(self, bars=(), reject=False):
        self.historical_events, self.historical_bars = {}, {}
        self.bars = list(bars)
        self.reject = reject
        self.placed = []
        self.fatal_order_error = threading.Event()
        self.expected_order_ids, self.reject_messages = set(), []
        self.next_order_id = 99
        self.cancelled = []
        self.order_fill_state = {}
        self.positions = {"ABC": {"quantity": 500.0, "avg_cost": 2.0}}
        self.completed_orders = []
        self.open_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 10, "perm_id": 0, "symbol": "ABC",
             "status": "Filled", "parent_id": 0, "action": "BUY", "order_type": "LMT",
             "total_quantity": 500.0, "order_ref": "ref-entry"},
            {"account": "DU1", "con_id": 0, "order_id": 11, "perm_id": 0, "symbol": "ABC",
             "status": "Submitted", "parent_id": 10, "action": "SELL", "order_type": "LMT",
             "total_quantity": 500.0, "order_ref": "ref-tp"},
            {"account": "DU1", "con_id": 0, "order_id": 12, "perm_id": 0, "symbol": "ABC",
             "status": "PreSubmitted", "parent_id": 10, "action": "SELL",
             "order_type": "STP LMT", "total_quantity": 500.0, "order_ref": "ref-sl"}]

    def reqHistoricalData(self, req_id, *args):
        self.historical_bars[req_id] = list(self.bars)
        self.historical_events[req_id].set()

    def cancelHistoricalData(self, req_id):
        pass

    def placeOrder(self, order_id, contract, order):
        self.placed.append((order_id, contract, order))
        if self.reject:
            self.reject_messages.append("IBKR 201: rejected")
            self.fatal_order_error.set()

    def cancelOrder(self, order_id, cancel):
        self.cancelled.append(order_id)


SIGNAL = {"signal_id": "s1", "symbol": "ABC", "entry_order_id": 10,
          "parent_order_id": 10, "target_order_id": 11, "stop_order_id": 12,
          "target_order_ref": "ref-tp", "stop_order_ref": "ref-sl",
          "target_perm_id": 1100, "stop_perm_id": 1200}


class BrokerTests(unittest.TestCase):
    def test_bars_since_filters_and_cleans_up(self):
        since = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
        t = int(since.timestamp())
        ib = FakeIB(bars=[{"timestamp": t - 120, "high": 9}, {"timestamp": t - 60, "high": 2},
                          {"timestamp": t + 60, "high": 3}, {"timestamp": None, "high": 99}])
        bars = rib.IBReboundBroker(ib, fake_wc()).bars_since("ABC", since)
        self.assertEqual([b["high"] for b in bars], [2, 3])
        self.assertEqual((ib.historical_events, ib.historical_bars), ({}, {}))

    def test_modify_stop_reuses_child_identity(self):
        ib = FakeIB()
        rib.IBReboundBroker(ib, fake_wc()).modify_stop(SIGNAL, 2.157)
        order_id, contract, order = ib.placed[0]
        self.assertEqual((order_id, order.parentId, order.action, order.orderType), (12, 10, "SELL", "STP LMT"))
        self.assertIs(type(order.auxPrice), float)
        self.assertIs(type(order.lmtPrice), float)
        self.assertEqual((order.auxPrice, order.lmtPrice, order.totalQuantity), (2.15, 2.13, 500.0))
        self.assertEqual((order.tif, order.outsideRth, order.orderRef), ("GTC", True, "ref-sl"))

    def test_close_moves_stop_through_market(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")
        order = ib.placed[0][2]
        self.assertIs(type(order.auxPrice), float)
        self.assertIs(type(order.lmtPrice), float)
        self.assertEqual((order.auxPrice, order.lmtPrice), (2.02, 1.94))

    def test_close_uses_two_hour_old_bar_when_no_recent_trade(self):
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        ib = FakeIB(bars=[{"timestamp": stale.timestamp(), "close": 3.00, "high": 3.0}])
        rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")
        order = ib.placed[0][2]
        self.assertIs(type(order.auxPrice), float)
        self.assertIs(type(order.lmtPrice), float)
        self.assertEqual((order.auxPrice, order.lmtPrice), (3.04, 2.91))

    def test_protection_state_reports_active_exit_and_missing(self):
        ib = FakeIB()
        broker = rib.IBReboundBroker(ib, fake_wc())
        self.assertEqual(broker.protection_state(SIGNAL), "ACTIVE")
        ib.open_orders[2]["status"] = "Filled"
        self.assertEqual(broker.protection_state(SIGNAL), "EXITED")
        ib.open_orders[1]["status"] = "Cancelled"
        ib.open_orders[2]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 11, "perm_id": 0, "symbol": "ABC",
             "status": "Filled", "parent_id": 10, "action": "SELL", "order_type": "LMT",
             "total_quantity": 500.0, "order_ref": "ref-tp"}]
        self.assertEqual(broker.protection_state(SIGNAL), "EXITED")
        ib.completed_orders = []
        ib.open_orders[1]["status"] = "Submitted"
        self.assertEqual(broker.protection_state(SIGNAL), "MISSING")

    def test_close_missing_stop_flattens_long_broker_position(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        ib.open_orders[2]["status"] = "Cancelled"
        result = rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")
        self.assertEqual(result, "FLATTEN_SENT")
        self.assertEqual(ib.cancelled, [11])
        order_id, contract, order = ib.placed[-1]
        self.assertEqual((order_id, contract), (99, ("contract", "ABC")))
        self.assertEqual(ib.next_order_id, 100)
        self.assertEqual((order.action, order.orderType, order.totalQuantity, order.lmtPrice),
                         ("SELL", "LMT", 500.0, 1.94))
        self.assertEqual((order.tif, order.outsideRth, order.account, order.orderRef),
                         ("DAY", True, "DU1", "rebound-flatten-s1"))

    def test_close_replaces_active_flatten_without_new_order_id_or_quantity(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0},
                          {"timestamp": 4102444800, "close": 2.20, "high": 2.2}])
        ib.open_orders[2]["status"] = "Cancelled"
        broker = rib.IBReboundBroker(ib, fake_wc())
        self.assertEqual(broker.close(SIGNAL, "MAX_HOLD"), "FLATTEN_SENT")
        first_order_id = ib.placed[-1][0]
        ib.open_orders.append(
            {"account": "DU1", "con_id": 0, "order_id": first_order_id, "perm_id": 0,
             "symbol": "ABC", "status": "Submitted", "parent_id": 0, "action": "SELL",
             "order_type": "LMT", "total_quantity": 500.0, "order_ref": "rebound-flatten-s1"}
        )

        self.assertEqual(broker.close(SIGNAL, "MAX_HOLD"), "FLATTEN_WORKING")

        placed_ids = [order_id for order_id, _, order in ib.placed if order.action == "SELL"]
        latest_quantity_by_id = {
            order_id: order.totalQuantity
            for order_id, _, order in ib.placed
            if order.action == "SELL"
        }
        self.assertEqual(placed_ids, [first_order_id, first_order_id])
        self.assertEqual(sum(latest_quantity_by_id.values()), 500.0)
        self.assertEqual(ib.next_order_id, first_order_id + 1)
        self.assertEqual(ib.placed[-1][2].lmtPrice, 2.13)

    def test_close_returns_exited_at_broker_when_child_already_filled(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        ib.open_orders[2]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 12, "perm_id": 0, "symbol": "ABC",
             "status": "Filled", "parent_id": 10, "action": "SELL", "order_type": "STP LMT",
             "total_quantity": 500.0, "order_ref": "ref-sl"}]
        result = rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")
        self.assertEqual(result, "EXITED_AT_BROKER")
        self.assertEqual(ib.placed, [])
        self.assertEqual(ib.cancelled, [])

    def test_completed_stop_with_ibapi_zero_order_id_matches_order_ref(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        ib.open_orders[2]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 0, "perm_id": 1200, "symbol": "ABC",
             "status": "Filled", "parent_id": 10, "action": "SELL", "order_type": "STP LMT",
             "total_quantity": 500.0, "order_ref": "ref-sl"}]

        result = rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")

        self.assertEqual(result, "EXITED_AT_BROKER")
        self.assertEqual(ib.placed, [])
        self.assertEqual(ib.cancelled, [])

    def test_completed_target_with_ibapi_zero_order_id_matches_default_ref(self):
        signal = dict(SIGNAL)
        signal.pop("target_order_ref")
        signal["signal_id"] = "abc-123"
        ib = FakeIB()
        ib.open_orders[1]["status"] = "Cancelled"
        ib.open_orders[2]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 0, "perm_id": 0, "symbol": "ABC",
             "status": "Filled", "parent_id": 10, "action": "SELL", "order_type": "LMT",
             "total_quantity": 500.0, "order_ref": "TM:abc-123:TP"}]

        self.assertEqual(rib.IBReboundBroker(ib, fake_wc()).protection_state(signal), "EXITED")

    def test_completed_target_with_ibapi_zero_order_id_matches_perm_id(self):
        signal = dict(SIGNAL)
        signal.pop("target_order_ref")
        ib = FakeIB()
        ib.open_orders[1]["status"] = "Cancelled"
        ib.open_orders[2]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 0, "perm_id": 1100, "symbol": "ABC",
             "status": "Filled", "parent_id": 10, "action": "SELL", "order_type": "LMT",
             "total_quantity": 500.0, "order_ref": ""}]

        self.assertEqual(rib.IBReboundBroker(ib, fake_wc()).protection_state(signal), "EXITED")

    def test_partial_target_fill_with_active_stop_remains_active_for_close(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        ib.order_fill_state[11] = {
            "order_id": 11, "status": "Submitted", "filled": 100.0,
            "remaining": 400.0, "perm_id": 1100,
        }

        result = rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")

        self.assertIsNone(result)
        self.assertEqual(len(ib.placed), 1)
        order = ib.placed[0][2]
        self.assertEqual((order.orderId, order.action, order.orderType), (12, "SELL", "STP LMT"))

    def test_missing_stop_with_flat_broker_position_is_exited_like(self):
        ib = FakeIB()
        ib.open_orders[2]["status"] = "Cancelled"
        ib.positions = {}
        result = rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")
        self.assertEqual(result, "FLAT")
        self.assertEqual(ib.placed, [])

    def test_protection_state_reports_flat_only_when_no_child_filled(self):
        ib = FakeIB()
        ib.open_orders[1]["status"] = "Cancelled"
        ib.open_orders[2]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 0, "perm_id": 0, "symbol": "ABC",
             "status": "Filled", "parent_id": 0, "action": "SELL", "order_type": "LMT",
             "total_quantity": 500.0, "order_ref": "rebound-flatten-s1"}]
        ib.positions = {}

        self.assertEqual(rib.IBReboundBroker(ib, fake_wc()).protection_state(SIGNAL), "FLAT")

    def test_negative_broker_position_is_not_flat_and_close_does_not_sell_more(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        ib.open_orders[1]["status"] = "Cancelled"
        ib.open_orders[2]["status"] = "Cancelled"
        ib.positions = {"ABC": {"quantity": -100.0, "avg_cost": 2.0}}
        broker = rib.IBReboundBroker(ib, fake_wc())

        self.assertEqual(broker.protection_state(SIGNAL), "MISSING")
        with self.assertRaises(RuntimeError):
            broker.close(SIGNAL, "MAX_HOLD")
        self.assertEqual(ib.placed, [])
        self.assertEqual(ib.cancelled, [])

    def test_stop_child_fill_with_flat_position_remains_exited(self):
        ib = FakeIB()
        ib.open_orders[1]["status"] = "Cancelled"
        ib.open_orders[2]["status"] = "Cancelled"
        ib.completed_orders = [
            {"account": "DU1", "con_id": 0, "order_id": 0, "perm_id": 0, "symbol": "ABC",
             "status": "Filled", "parent_id": 0, "action": "SELL", "order_type": "STP LMT",
             "total_quantity": 500.0, "order_ref": "ref-sl"}]
        ib.positions = {}

        broker = rib.IBReboundBroker(ib, fake_wc())

        self.assertEqual(broker.protection_state(SIGNAL), "EXITED")
        self.assertEqual(broker.close(SIGNAL, "MAX_HOLD"), "EXITED_AT_BROKER")
        self.assertEqual(ib.placed, [])
        self.assertEqual(ib.cancelled, [])

    def test_missing_stop_and_reject_raise(self):
        ib = FakeIB()
        ib.open_orders[2]["status"] = "Cancelled"
        with self.assertRaises(LookupError):
            rib.IBReboundBroker(ib, fake_wc()).modify_stop(SIGNAL, 2.1)
        with self.assertRaises(RuntimeError):
            rib.IBReboundBroker(FakeIB(reject=True), fake_wc()).modify_stop(SIGNAL, 2.1)


if __name__ == "__main__":
    unittest.main()
