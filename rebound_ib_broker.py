"""IBKR implementation of the rebound stop-manager broker, used inside the worker.

`wc` is the worker_core module (passed in to avoid a circular import).
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR

from ibapi.order import Order
from ibapi.order_cancel import OrderCancel

HISTORY_TIMEOUT_SECONDS = 10
REJECT_WAIT_SECONDS = 3
CLOSE_TRIGGER_UP = 1.01
CLOSE_LIMIT_DOWN = 0.97
_ACTIVE_STATUSES = {"PreSubmitted", "Submitted", "PendingSubmit", "ApiPending", "PendingCancel"}
_FILLED_STATUS = "Filled"


class IBReboundBroker:
    def __init__(self, ib, wc, req_id_base=91000):
        self.ib = ib
        self.wc = wc
        self._next_req = req_id_base

    def bars_since(self, symbol, since):
        self._next_req += 1
        req_id = self._next_req
        event = threading.Event()
        self.ib.historical_events[req_id] = event
        self.ib.historical_bars[req_id] = []
        self.ib.reqHistoricalData(req_id, self.wc.stock_contract(symbol), "", "1 D", "1 min",
                                  "TRADES", 0, 2, False, [])
        try:
            if not event.wait(timeout=HISTORY_TIMEOUT_SECONDS):
                try:
                    self.ib.cancelHistoricalData(req_id)
                except Exception:
                    pass
                raise TimeoutError(f"historical bars timeout for {symbol}")
            cutoff = since.timestamp() - 60
            return [bar for bar in self.ib.historical_bars.get(req_id, [])
                    if bar.get("timestamp") is not None and bar["timestamp"] >= cutoff]
        finally:
            self.ib.historical_events.pop(req_id, None)
            self.ib.historical_bars.pop(req_id, None)

    def _stop_order(self, signal, refresh=True):
        stop_id = signal.get("stop_order_id")
        if not stop_id:
            raise LookupError(f"{signal['signal_id']}: no stop_order_id")
        if refresh:
            self.wc.load_order_state(self.ib)
        for item in self.ib.open_orders:
            if int(item.get("order_id") or 0) == int(stop_id) \
                    and item.get("status") in _ACTIVE_STATUSES:
                return item
        raise LookupError(f"{signal['signal_id']}: stop order {stop_id} not active")

    def protection_state(self, signal):
        self.wc.load_order_state(self.ib)
        stop_id = self._as_int(signal.get("stop_order_id"))
        target_id = self._as_int(signal.get("target_order_id"))
        child_ids = {order_id for order_id in (stop_id, target_id) if order_id is not None}
        for item in list(self.ib.open_orders) + list(self.ib.completed_orders):
            order_id = self._as_int(item.get("order_id"))
            if order_id in child_ids and self._order_filled(item, order_id):
                return "EXITED"
        if stop_id is not None:
            for item in self.ib.open_orders:
                if self._as_int(item.get("order_id")) == stop_id \
                        and item.get("status") in _ACTIVE_STATUSES:
                    return "ACTIVE"
        return "MISSING"

    def _rules(self, symbol):
        session = self.wc.check_market_session(self.ib, symbol)
        return session.get("market_rule") or [], float(session.get("min_tick") or 0.01)

    def _place_stop(self, item, trigger, limit):
        order = Order()
        order.orderId = int(item["order_id"])
        order.account = self.wc.IB_ACCOUNT
        order.action = item["action"]
        order.orderType = item["order_type"]
        order.totalQuantity = item["total_quantity"]
        order.auxPrice = trigger
        if item["order_type"] == "STP LMT":
            order.lmtPrice = limit
        order.parentId = int(item.get("parent_id") or 0)
        order.tif = "GTC"
        order.outsideRth = self.wc.ALLOW_OUTSIDE_RTH
        order.transmit = True
        order.orderRef = item.get("order_ref") or ""
        self.ib.expected_order_ids = {order.orderId}
        self.ib.reject_messages = []
        self.ib.fatal_order_error.clear()
        self.ib.placeOrder(order.orderId, self.wc.stock_contract(item["symbol"]), order)
        if self.ib.fatal_order_error.wait(timeout=REJECT_WAIT_SECONDS):
            raise RuntimeError("; ".join(self.ib.reject_messages) or "stop modify rejected")

    def modify_stop(self, signal, trigger):
        item = self._stop_order(signal)
        rule, tick = self._rules(signal["symbol"])
        trigger, _ = self.wc.normalize_price_to_market_rule(trigger, rule, tick, ROUND_FLOOR)
        limit, _ = self.wc.build_stop_limit_price("BUY", trigger, rule, tick)
        self._place_stop(item, trigger, limit)

    def close(self, signal, reason):
        """Move the stop child to the market so the bracket closes as a stop exit."""
        state = self.protection_state(signal)
        if state == "EXITED":
            return "EXITED_AT_BROKER"
        bars = self.bars_since(signal["symbol"],
                               datetime.now(timezone.utc) - timedelta(days=1))
        if not bars:
            raise LookupError(f"{signal['symbol']}: no bars to price the close")
        last = float(bars[-1]["close"])
        if state == "ACTIVE":
            item = self._stop_order(signal, refresh=False)
        else:
            return self._flatten_without_stop(signal, last)
        rule, tick = self._rules(signal["symbol"])
        trigger, _ = self.wc.normalize_price_to_market_rule(last * CLOSE_TRIGGER_UP, rule, tick,
                                                            ROUND_CEILING)
        limit, _ = self.wc.normalize_price_to_market_rule(last * CLOSE_LIMIT_DOWN, rule, tick,
                                                          ROUND_FLOOR)
        self._place_stop(item, trigger, limit)

    def _flatten_without_stop(self, signal, last):
        self.wc.load_order_state(self.ib)
        active_flatten = self._active_flatten_order(signal)
        if active_flatten is not None:
            self._replace_flatten_order(active_flatten, last)
            return "FLATTEN_WORKING"
        self._cancel_active_bracket_children(signal)
        positions = self.wc.load_position_state(self.ib)
        symbol = str(signal["symbol"]).strip().upper()
        position = positions.get(symbol) or {}
        quantity = float(position.get("quantity") or 0)
        if quantity <= 0:
            return "FLAT"
        rule, tick = self._rules(symbol)
        limit, _ = self.wc.normalize_price_to_market_rule(last * CLOSE_LIMIT_DOWN, rule, tick,
                                                          ROUND_FLOOR)
        order_id = self.wc.get_safe_parent_order_id(self.ib.next_order_id)
        if order_id is None:
            raise RuntimeError("flatten order id unavailable")
        self.ib.next_order_id = int(order_id) + 1
        order = Order()
        order.orderId = int(order_id)
        order.account = self.wc.IB_ACCOUNT
        order.action = "SELL"
        order.orderType = "LMT"
        order.totalQuantity = quantity
        order.lmtPrice = limit
        order.tif = "DAY"
        order.outsideRth = self.wc.ALLOW_OUTSIDE_RTH
        order.transmit = True
        order.orderRef = f"rebound-flatten-{signal['signal_id']}"
        self.ib.expected_order_ids = {order.orderId}
        self.ib.reject_messages = []
        self.ib.fatal_order_error.clear()
        self.ib.placeOrder(order.orderId, self.wc.stock_contract(symbol), order)
        if self.ib.fatal_order_error.wait(timeout=REJECT_WAIT_SECONDS):
            raise RuntimeError("; ".join(self.ib.reject_messages) or "flatten order rejected")
        return "FLATTEN_SENT"

    def _active_flatten_order(self, signal):
        order_ref = f"rebound-flatten-{signal['signal_id']}"
        for item in self.ib.open_orders:
            if item.get("status") in _ACTIVE_STATUSES and item.get("order_ref") == order_ref:
                return item
        return None

    def _replace_flatten_order(self, item, last):
        symbol = str(item["symbol"]).strip().upper()
        rule, tick = self._rules(symbol)
        limit, _ = self.wc.normalize_price_to_market_rule(last * CLOSE_LIMIT_DOWN, rule, tick,
                                                          ROUND_FLOOR)
        order = Order()
        order.orderId = int(item["order_id"])
        order.account = item.get("account") or self.wc.IB_ACCOUNT
        order.action = "SELL"
        order.orderType = item.get("order_type") or "LMT"
        order.totalQuantity = float(item.get("total_quantity") or 0)
        order.lmtPrice = limit
        order.tif = "DAY"
        order.outsideRth = self.wc.ALLOW_OUTSIDE_RTH
        order.transmit = True
        order.orderRef = item.get("order_ref") or ""
        self.ib.expected_order_ids = {order.orderId}
        self.ib.reject_messages = []
        self.ib.fatal_order_error.clear()
        self.ib.placeOrder(order.orderId, self.wc.stock_contract(symbol), order)
        if self.ib.fatal_order_error.wait(timeout=REJECT_WAIT_SECONDS):
            raise RuntimeError("; ".join(self.ib.reject_messages) or "flatten modify rejected")

    def _cancel_active_bracket_children(self, signal):
        parent_ids = {
            self._as_int(signal.get("entry_order_id")),
            self._as_int(signal.get("parent_order_id")),
        }
        parent_ids.discard(None)
        child_ids = {
            self._as_int(signal.get("target_order_id")),
            self._as_int(signal.get("stop_order_id")),
        }
        child_ids.discard(None)
        for item in self.ib.open_orders:
            order_id = self._as_int(item.get("order_id"))
            parent_id = self._as_int(item.get("parent_id"))
            if item.get("status") not in _ACTIVE_STATUSES:
                continue
            if order_id in child_ids or (parent_id in parent_ids and order_id not in parent_ids):
                self.ib.cancelOrder(order_id, OrderCancel())

    @staticmethod
    def _as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _order_filled(self, item, order_id):
        if item.get("status") == _FILLED_STATUS:
            return True
        if order_id is None:
            return False
        fill_state = getattr(self.ib, "order_fill_state", {}).get(order_id) or {}
        try:
            return float(fill_state.get("filled") or item.get("filled") or 0) > 0
        except (TypeError, ValueError):
            return False
