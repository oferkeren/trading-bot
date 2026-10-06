"""IBKR implementation of the rebound stop-manager broker, used inside the worker.

`wc` is the worker_core module (passed in to avoid a circular import).
"""

import threading
import time
import re
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
        if refresh:
            self.wc.load_order_state(self.ib)
        for item in self._child_order_candidates(signal, "SL", open_only=True):
            if item.get("status") in _ACTIVE_STATUSES:
                return item
        stop_id = signal.get("stop_order_id")
        raise LookupError(f"{signal['signal_id']}: stop order {stop_id} not active")

    def protection_state(self, signal):
        self.wc.load_order_state(self.ib)
        for role in ("SL", "TP"):
            for item in self._child_order_candidates(signal, role):
                if self._order_filled(item):
                    return "EXITED"
        for item in self._child_order_candidates(signal, "SL", open_only=True):
            if item.get("status") in _ACTIVE_STATUSES:
                quantity = self._broker_position_quantity(signal)
                if quantity is None:
                    return "ACTIVE"
                if quantity > 0:
                    return "ACTIVE"
                # Flat accounts must cancel active exits before closing; short accounts
                # are not safe to ratchet with another SELL stop and close() will fail safe.
                return "MISSING"
        if self._broker_position_flat(signal):
            if self._active_exit_orders(signal):
                return "MISSING"
            return "FLAT"
        return "MISSING"

    def _broker_position_quantity(self, signal):
        try:
            positions = self.wc.load_position_state(self.ib)
        except Exception:
            return None
        symbol = str(signal["symbol"]).strip().upper()
        position = positions.get(symbol) or {}
        try:
            return float(position.get("quantity") or 0)
        except (TypeError, ValueError):
            return None

    def _broker_position_flat(self, signal):
        quantity = self._broker_position_quantity(signal)
        if quantity is None:
            return False
        return quantity == 0

    def _active_exit_orders(self, signal):
        active = []
        seen = set()
        for role in ("TP", "SL"):
            for item in self._child_order_candidates(signal, role, open_only=True):
                if item.get("status") not in _ACTIVE_STATUSES:
                    continue
                marker = id(item)
                if marker not in seen:
                    seen.add(marker)
                    active.append(item)
        flatten = self._active_flatten_order(signal)
        if flatten is not None and id(flatten) not in seen:
            active.append(flatten)
        return active

    def _child_order_candidates(self, signal, role, open_only=False):
        orders = list(self.ib.open_orders)
        if not open_only:
            orders += list(self.ib.completed_orders)
        expected_ref = self._expected_child_ref(signal, role)
        if role == "TP":
            stored_ref = signal.get("target_order_ref")
            stored_perm = signal.get("target_perm_id")
            stored_id = signal.get("target_order_id")
        else:
            stored_ref = signal.get("stop_order_ref")
            stored_perm = signal.get("stop_perm_id")
            stored_id = signal.get("stop_order_id")
        candidates = []
        seen = set()
        for matches in (
            self._orders_by_ref(orders, stored_ref),
            self._orders_by_ref(orders, expected_ref),
            self._orders_by_perm(orders, stored_perm),
            self._orders_by_id(orders, stored_id),
            self._orders_by_parent_and_type(signal, orders, role),
        ):
            for order in matches:
                marker = id(order)
                if marker not in seen:
                    seen.add(marker)
                    candidates.append(order)
        return candidates

    def _expected_child_ref(self, signal, role):
        builder = getattr(self.wc, "build_order_refs", None)
        if not callable(builder):
            key = self._sanitize_ref_component(signal["signal_id"])
            return f"TM:{key}:{role}"
        refs = builder(signal["signal_id"])
        return refs["target" if role == "TP" else "stop"]

    @staticmethod
    def _sanitize_ref_component(value):
        value = str(value or "").strip()
        return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:80]

    def _orders_by_ref(self, orders, order_ref):
        if not order_ref:
            return []
        return [order for order in orders if order.get("order_ref") == order_ref]

    def _orders_by_perm(self, orders, perm_id):
        wanted = self._valid_perm_id(perm_id)
        if wanted is None:
            return []
        return [order for order in orders if self._valid_perm_id(order.get("perm_id")) == wanted]

    def _orders_by_id(self, orders, order_id):
        wanted = self._valid_order_id(order_id)
        if wanted is None:
            return []
        return [order for order in orders if self._as_int(order.get("order_id")) == wanted]

    def _orders_by_parent_and_type(self, signal, orders, role):
        parent_ids = {
            self._valid_order_id(signal.get("entry_order_id")),
            self._valid_order_id(signal.get("parent_order_id")),
        }
        parent_ids.discard(None)
        if not parent_ids:
            return []
        if role == "TP":
            types = {"LMT"}
        else:
            types = {"STP", "STP LMT"}
        return [
            order for order in orders
            if self._as_int(order.get("parent_id")) in parent_ids
            and order.get("order_type") in types
        ]

    @classmethod
    def _valid_order_id(cls, value):
        # IBKR reports order_id 0 for completed orders, so 0 can't identify an order.
        value = cls._as_int(value)
        return value if value is not None and value > 0 else None

    @staticmethod
    def _valid_perm_id(value):
        try:
            value = int(value)
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None

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
        quantity = self._broker_position_quantity(signal)
        if quantity is not None and quantity <= 0:
            raise RuntimeError(
                f"{signal['symbol']}: broker position is not long ({quantity}); refusing stop modify"
            )
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
        if state == "FLAT":
            return "FLAT"
        if self._broker_position_flat(signal):
            return self._flatten_without_stop(signal, None)
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
        positions = self.wc.load_position_state(self.ib)
        symbol = str(signal["symbol"]).strip().upper()
        position = positions.get(symbol) or {}
        quantity = float(position.get("quantity") or 0)
        if quantity == 0:
            if self._cancel_active_bracket_children(signal):
                return "CANCEL_REQUESTED"
            return "FLAT"
        if quantity < 0:
            raise RuntimeError(f"{symbol}: broker position is not long ({quantity})")
        active_flatten = self._active_flatten_order(signal)
        if active_flatten is not None:
            self._replace_flatten_order(active_flatten, last)
            return "FLATTEN_WORKING"
        self._cancel_active_bracket_children(signal)
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
        cancelled = 0
        seen = set()
        for item in self._active_exit_orders(signal):
            order_id = self._as_int(item.get("order_id"))
            if order_id is not None and order_id not in seen:
                self.ib.cancelOrder(order_id, OrderCancel())
                seen.add(order_id)
                cancelled += 1
        return cancelled

    @staticmethod
    def _as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _order_filled(self, item):
        order_id = self._as_int(item.get("order_id"))
        fill_state = getattr(self.ib, "order_fill_state", {}).get(order_id) or {}
        status = fill_state.get("status") or item.get("status")
        if status == _FILLED_STATUS:
            return True
        if order_id is None:
            return False
        try:
            filled = float(fill_state.get("filled") or item.get("filled") or 0)
            remaining = float(fill_state.get("remaining") or item.get("remaining") or 0)
            return filled > 0 and remaining == 0
        except (TypeError, ValueError):
            return False
