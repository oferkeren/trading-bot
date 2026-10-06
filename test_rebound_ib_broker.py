import threading
import types
import unittest
from datetime import datetime, timezone

import rebound_ib_broker as rib


def fake_wc():
    wc = types.SimpleNamespace(IB_ACCOUNT="DU1", ALLOW_OUTSIDE_RTH=True)
    wc.stock_contract = lambda symbol: ("contract", symbol)
    wc.load_order_state = lambda ib: None
    wc.check_market_session = lambda ib, symbol: {"market_rule": [], "min_tick": 0.01}
    wc.normalize_price_to_market_rule = lambda p, rule, tick, rounding: round(p, 2)
    wc.build_stop_limit_price = lambda action, trig, rule, tick: round(trig * 0.99, 2)
    return wc


class FakeIB:
    def __init__(self, bars=(), reject=False):
        self.historical_events, self.historical_bars = {}, {}
        self.bars = list(bars)
        self.reject = reject
        self.placed = []
        self.fatal_order_error = threading.Event()
        self.expected_order_ids, self.reject_messages = set(), []
        self.open_orders = [{"order_id": 12, "status": "PreSubmitted", "action": "SELL",
                             "order_type": "STP LMT", "total_quantity": 500.0, "parent_id": 10,
                             "order_ref": "ref-sl", "symbol": "ABC"}]

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


SIGNAL = {"signal_id": "s1", "symbol": "ABC", "stop_order_id": 12}


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
        self.assertEqual((order.auxPrice, order.lmtPrice, order.totalQuantity), (2.16, 2.14, 500.0))
        self.assertEqual((order.tif, order.outsideRth, order.orderRef), ("GTC", True, "ref-sl"))

    def test_close_moves_stop_through_market(self):
        ib = FakeIB(bars=[{"timestamp": 4102444800, "close": 2.00, "high": 2.0}])
        rib.IBReboundBroker(ib, fake_wc()).close(SIGNAL, "MAX_HOLD")
        order = ib.placed[0][2]
        self.assertEqual((order.auxPrice, order.lmtPrice), (2.02, 1.94))

    def test_missing_stop_and_reject_raise(self):
        ib = FakeIB()
        ib.open_orders[0]["status"] = "Filled"
        with self.assertRaises(LookupError):
            rib.IBReboundBroker(ib, fake_wc()).modify_stop(SIGNAL, 2.1)
        with self.assertRaises(RuntimeError):
            rib.IBReboundBroker(FakeIB(reject=True), fake_wc()).modify_stop(SIGNAL, 2.1)


if __name__ == "__main__":
    unittest.main()
