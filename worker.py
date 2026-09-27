import math
import os
import re
import sqlite3
import threading
import time

from datetime import (
    datetime,
    timezone,
    timedelta
)

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel

from execution_guard import (
    check_pre_execution,
    ExecutionBlocked
)

from execution_freshness import (
    check_execution_freshness,
    SignalFreshnessError
)

from market_guard import (
    evaluate_liquid_hours,
    MarketSessionError
)

from position_policy import (
    evaluate_position_limits,
    PositionPolicyError
)

from trade_state import (
    init_trade_state,
    transition_signal,
    update_signal_metadata,
    record_event,
    claim_signal
)


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db"
)


IB_HOST = os.getenv(
    "IB_HOST",
    "127.0.0.1"
)

IB_PORT = int(
    os.getenv(
        "IB_PORT",
        "7496"
    )
)

IB_CLIENT_ID = int(
    os.getenv(
        "IB_CLIENT_ID",
        "10"
    )
)

IB_ACCOUNT = (
    os.getenv(
        "IB_ACCOUNT",
        ""
    ).strip()
)


LIVE_TRADING = (
    os.getenv(
        "LIVE_TRADING",
        "false"
    ).lower()
    == "true"
)


MAX_TRADES_PER_DAY = int(
    os.getenv(
        "MAX_TRADES_PER_DAY",
        "5"
    )
)

MAX_OPEN_POSITIONS = int(
    os.getenv(
        "MAX_OPEN_POSITIONS",
        "3"
    )
)

# Backward-compatible fallback.
# New safety policy distinguishes bot-managed positions
# from unrelated legacy holdings.
MAX_MANAGED_POSITIONS = int(
    os.getenv(
        "MAX_MANAGED_POSITIONS",
        str(
            MAX_OPEN_POSITIONS
        )
    )
)

MAX_TOTAL_BROKER_POSITIONS = int(
    os.getenv(
        "MAX_TOTAL_BROKER_POSITIONS",
        "10"
    )
)

# Hard gate for live-shaped end-to-end testing.
# When enabled, the worker completes every read-only
# safety check and stops BEFORE allocating order IDs
# or calling placeOrder().
PRELIVE_DRY_RUN = (
    os.getenv(
        "PRELIVE_DRY_RUN",
        "true"
    ).lower()
    == "true"
)


MAX_DAILY_LOSS_USD = float(
    os.getenv(
        "MAX_DAILY_LOSS_USD",
        "100"
    )
)

MIN_ACCOUNT_EQUITY_USD = float(
    os.getenv(
        "MIN_ACCOUNT_EQUITY_USD",
        "100"
    )
)


STATUS_MAX_AGE_SECONDS = int(
    os.getenv(
        "STATUS_MAX_AGE_SECONDS",
        "30"
    )
)

MAX_SIGNAL_AGE_SECONDS = int(
    os.getenv(
        "MAX_SIGNAL_AGE_SECONDS",
        "60"
    )
)

MAX_SIGNAL_FUTURE_SKEW_SECONDS = int(
    os.getenv(
        "MAX_SIGNAL_FUTURE_SKEW_SECONDS",
        "10"
    )
)


BLOCK_LIVE_ON_PENDING_CANCEL = (
    os.getenv(
        "BLOCK_LIVE_ON_PENDING_CANCEL",
        "true"
    ).lower()
    == "true"
)


ALLOW_OUTSIDE_RTH = (
    os.getenv(
        "ALLOW_OUTSIDE_RTH",
        "false"
    ).lower()
    == "true"
)


REQUIRE_LIQUID_SESSION = (
    os.getenv(
        "REQUIRE_LIQUID_SESSION",
        "true"
    ).lower()
    == "true"
)


ACK_TIMEOUT_SECONDS = int(
    os.getenv(
        "ACK_TIMEOUT_SECONDS",
        "15"
    )
)

CANCEL_TIMEOUT_SECONDS = int(
    os.getenv(
        "CANCEL_TIMEOUT_SECONDS",
        "15"
    )
)

PROCESSING_RECOVERY_MINUTES = int(
    os.getenv(
        "PROCESSING_RECOVERY_MINUTES",
        "5"
    )
)

RECONCILE_INTERVAL_SECONDS = int(
    os.getenv(
        "RECONCILE_INTERVAL_SECONDS",
        "15"
    )
)

STARTUP_RETRY_SECONDS = int(
    os.getenv(
        "STARTUP_RETRY_SECONDS",
        "5"
    )
)


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():
    init_trade_state(
        DB_FILE
    )


# ============================================================
# STATE
# ============================================================

def transition(
    signal_id,
    status,
    event_type,
    message=None,
    payload=None,
    fields=None,
    event_key=None,
    force=False
):
    return transition_signal(
        db_file=DB_FILE,
        signal_id=signal_id,
        new_status=status,
        event_type=event_type,
        source="worker",
        message=message,
        payload=payload,
        extra_fields=fields,
        event_key=event_key,
        allow_same_state=True,
        force=force
    )


# ============================================================
# ATOMIC QUEUE CLAIMS
# ============================================================

def get_next_signal():
    return claim_signal(
        db_file=DB_FILE,
        from_status="QUEUED",
        to_status="PROCESSING",
        event_type="WORKER_CLAIMED",
        source="worker",
        order_by="created_at",
        increment_attempts=True
    )


def get_next_cancel_request():
    return claim_signal(
        db_file=DB_FILE,
        from_status="CANCEL_REQUESTED",
        to_status="CANCELLING",
        event_type="CANCEL_WORKER_CLAIMED",
        source="worker",
        order_by="updated_at",
        increment_attempts=False
    )


def get_reconcile_candidates():
    conn = db_connect()

    try:
        rows = conn.execute(
            """
            SELECT *
            FROM signals

            WHERE
                test_mode = 0

                AND status IN (
                    'PROCESSING',
                    'SUBMITTED',
                    'ACCEPTED_WAITING_MARKET',
                    'FILLED',
                    'OPEN_POSITION',

                    'CANCEL_REQUESTED',
                    'CANCELLING',
                    'CANCEL_PENDING',
                    'CANCEL_UNKNOWN',

                    'UNKNOWN',
                    'ERROR'
                )

            ORDER BY created_at ASC
            """
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        conn.close()


# ============================================================
# RECOVERY
# ============================================================

def recover_stuck_work():
    cutoff = (
        datetime.now(
            timezone.utc
        )
        -
        timedelta(
            minutes=
                PROCESSING_RECOVERY_MINUTES
        )
    ).isoformat()


    conn = db_connect()

    try:
        rows = conn.execute(
            """
            SELECT
                signal_id,
                status

            FROM signals

            WHERE
                status IN (
                    'PROCESSING',
                    'CANCELLING'
                )

                AND updated_at < ?
            """,
            (
                cutoff,
            )
        ).fetchall()

    finally:
        conn.close()


    for row in rows:

        if (
            row[
                "status"
            ]
            == "PROCESSING"
        ):

            transition(
                row[
                    "signal_id"
                ],
                "UNKNOWN",
                "WORKER_RECOVERY",
                message=(
                    "Worker restarted while signal "
                    "was PROCESSING"
                )
            )

        else:

            transition(
                row[
                    "signal_id"
                ],
                "CANCEL_UNKNOWN",
                "CANCEL_RECOVERY",
                message=(
                    "Worker restarted while "
                    "cancellation was active"
                )
            )


# ============================================================
# SIGNAL FRESHNESS
# ============================================================

def enforce_execution_freshness(
    signal,
    phase
):
    try:
        result = (
            check_execution_freshness(
                signal_time=
                    signal.get(
                        "signal_time"
                    ),

                max_age_seconds=
                    MAX_SIGNAL_AGE_SECONDS,

                max_future_skew_seconds=
                    MAX_SIGNAL_FUTURE_SKEW_SECONDS
            )
        )


        record_event(
            db_file=DB_FILE,

            signal_id=
                signal[
                    "signal_id"
                ],

            event_type=
                "EXECUTION_FRESHNESS_APPROVED",

            source="worker",

            message=(
                f"Signal freshness passed "
                f"at {phase}"
            ),

            payload={
                **result,
                "phase":
                    phase
            },

            event_key=(
                f"freshness:"
                f"{signal['signal_id']}:"
                f"{phase}"
            )
        )


        return result


    except SignalFreshnessError as exc:

        transition(
            signal[
                "signal_id"
            ],

            "BLOCKED",

            "SIGNAL_STALE_AT_EXECUTION",

            message=str(
                exc
            ),

            payload={
                "phase":
                    phase,

                "signal_time":
                    signal.get(
                        "signal_time"
                    ),

                "max_age_seconds":
                    MAX_SIGNAL_AGE_SECONDS
            }
        )

        raise


# ============================================================
# ORDER REFERENCES
# ============================================================

def sanitize_ref_component(
    value
):
    value = str(
        value
    ).strip()

    return re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        value
    )[:80]


def build_order_refs(
    signal_id
):
    key = sanitize_ref_component(
        signal_id
    )

    return {
        "entry":
            f"TM:{key}:ENTRY",

        "target":
            f"TM:{key}:TP",

        "stop":
            f"TM:{key}:SL"
    }


def parse_order_ref(
    value
):
    if not value:
        return None


    value = str(
        value
    ).strip()


    parts = value.split(
        ":"
    )


    if (
        len(parts) != 3
        or
        parts[0] != "TM"
        or
        parts[2]
        not in {
            "ENTRY",
            "TP",
            "SL"
        }
    ):
        return None


    return {
        "signal_key":
            parts[1],

        "role":
            parts[2]
    }


def signal_key(
    signal_id
):
    return sanitize_ref_component(
        signal_id
    )


# ============================================================
# CONTRACT
# ============================================================

def stock_contract(
    symbol
):
    contract = Contract()

    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    return contract


# ============================================================
# IBKR CLIENT
# ============================================================

class IBApp(
    EWrapper,
    EClient
):

    def __init__(self):
        EClient.__init__(
            self,
            self
        )


        self.ready = (
            threading.Event()
        )

        self.positions_done = (
            threading.Event()
        )

        self.open_orders_done = (
            threading.Event()
        )

        self.completed_orders_done = (
            threading.Event()
        )

        self.pnl_done = (
            threading.Event()
        )

        self.contract_details_done = (
            threading.Event()
        )


        self.next_order_id = None

        self.accounts = []

        self.positions = {}

        self.open_orders = []

        self.completed_orders = []

        self.contract_details = []


        self.daily_pnl = None


        self.expected_order_ids = set()

        self.seen_order_ids = set()

        self.order_statuses = {}


        self.parent_order_id = None

        self.parent_status = None


        self.fatal_order_error = (
            threading.Event()
        )

        self.reject_messages = []


        self.cancel_target_ids = set()

        self.cancelled_ids = set()


    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def nextValidId(
        self,
        orderId
    ):
        self.next_order_id = (
            orderId
        )

        print(
            f"IBKR CONNECTED | "
            f"clientId={IB_CLIENT_ID} | "
            f"nextOrderId={orderId}",
            flush=True
        )

        self.ready.set()


    def managedAccounts(
        self,
        accountsList
    ):
        self.accounts = [
            account.strip()
            for account
            in accountsList.split(",")
            if account.strip()
        ]


    # --------------------------------------------------------
    # POSITIONS
    # --------------------------------------------------------

    def position(
        self,
        account,
        contract,
        position,
        avgCost
    ):
        if account != IB_ACCOUNT:
            return


        quantity = float(
            position
        )


        if quantity == 0:
            return


        self.positions[
            contract.symbol.upper()
        ] = {
            "quantity":
                quantity,

            "avg_cost":
                float(
                    avgCost
                )
        }


    def positionEnd(
        self
    ):
        self.positions_done.set()


    # --------------------------------------------------------
    # OPEN ORDERS
    # --------------------------------------------------------

    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState
    ):
        if (
            order.account
            != IB_ACCOUNT
        ):
            return


        item = {
            "order_id":
                orderId,

            "perm_id":
                getattr(
                    order,
                    "permId",
                    0
                ),

            "symbol":
                (
                    contract.symbol.upper()
                    if contract.symbol
                    else ""
                ),

            "status":
                orderState.status,

            "parent_id":
                order.parentId,

            "action":
                order.action,

            "order_type":
                order.orderType,

            "order_ref":
                getattr(
                    order,
                    "orderRef",
                    ""
                )
                or ""
        }


        self.open_orders.append(
            item
        )


        if (
            orderId
            in self.expected_order_ids
        ):

            self.seen_order_ids.add(
                orderId
            )

            self.order_statuses[
                orderId
            ] = orderState.status


            if (
                orderId
                == self.parent_order_id
            ):

                self.parent_status = (
                    orderState.status
                )


    def openOrderEnd(
        self
    ):
        self.open_orders_done.set()


    # --------------------------------------------------------
    # COMPLETED ORDERS
    # --------------------------------------------------------

    def completedOrder(
        self,
        contract,
        order,
        orderState
    ):
        if (
            order.account
            != IB_ACCOUNT
        ):
            return


        self.completed_orders.append({
            "order_id":
                getattr(
                    order,
                    "orderId",
                    None
                ),

            "perm_id":
                getattr(
                    order,
                    "permId",
                    0
                ),

            "symbol":
                (
                    contract.symbol.upper()
                    if contract.symbol
                    else ""
                ),

            "status":
                orderState.status,

            "parent_id":
                order.parentId,

            "action":
                order.action,

            "order_type":
                order.orderType,

            "order_ref":
                getattr(
                    order,
                    "orderRef",
                    ""
                )
                or ""
        })


    def completedOrdersEnd(
        self
    ):
        self.completed_orders_done.set()


    # --------------------------------------------------------
    # CONTRACT DETAILS
    # --------------------------------------------------------

    def contractDetails(
        self,
        reqId,
        contractDetails
    ):
        self.contract_details.append(
            contractDetails
        )


    def contractDetailsEnd(
        self,
        reqId
    ):
        self.contract_details_done.set()


    # --------------------------------------------------------
    # P/L
    # --------------------------------------------------------

    def pnl(
        self,
        reqId,
        dailyPnL,
        unrealizedPnL,
        realizedPnL
    ):
        try:
            value = float(
                dailyPnL
            )


            if (
                not math.isfinite(
                    value
                )
                or
                abs(
                    value
                ) > 1e100
            ):
                self.daily_pnl = None

            else:
                self.daily_pnl = (
                    value
                )

        except Exception:
            self.daily_pnl = None


        self.pnl_done.set()


    # --------------------------------------------------------
    # ORDER STATUS
    # --------------------------------------------------------

    def orderStatus(
        self,
        orderId,
        status,
        filled,
        remaining,
        avgFillPrice,
        permId,
        parentId,
        lastFillPrice,
        clientId,
        whyHeld,
        mktCapPrice
    ):
        print(
            f"ORDER STATUS | "
            f"id={orderId} | "
            f"status={status} | "
            f"filled={filled} | "
            f"remaining={remaining} | "
            f"permId={permId}",
            flush=True
        )


        if (
            orderId
            in self.expected_order_ids
        ):

            self.order_statuses[
                orderId
            ] = status


            if (
                orderId
                == self.parent_order_id
            ):

                self.parent_status = (
                    status
                )


            if status in {
                "Cancelled",
                "ApiCancelled",
                "Inactive"
            }:

                self.reject_messages.append(
                    f"order {orderId} "
                    f"status={status}"
                )

                self.fatal_order_error.set()


        if (
            orderId
            in self.cancel_target_ids
            and
            status in {
                "Cancelled",
                "ApiCancelled"
            }
        ):

            self.cancelled_ids.add(
                orderId
            )


    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

    @staticmethod
    def is_nonfatal_warning(
        errorCode,
        errorString
    ):
        message = (
            errorString
            or ""
        ).lower()


        if errorCode in {
            2104,
            2106,
            2158,
            2109
        }:
            return True


        if errorCode == 399:

            phrases = (
                "warning:",
                "will not be placed at the exchange until",
                "will be held until"
            )

            return any(
                phrase in message
                for phrase in phrases
            )


        return False


    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        message = (
            f"IBKR {errorCode}: "
            f"{errorString}"
        )


        if advancedOrderRejectJson:

            message += (
                " | advanced="
                f"{advancedOrderRejectJson}"
            )


        if self.is_nonfatal_warning(
            errorCode,
            errorString
        ):

            print(
                f"IB WARNING | "
                f"id={reqId} | "
                f"{message}",
                flush=True
            )

            return


        print(
            f"IB ERROR | "
            f"id={reqId} | "
            f"{message}",
            flush=True
        )


        if (
            reqId
            in self.expected_order_ids
        ):

            self.reject_messages.append(
                message
            )

            self.fatal_order_error.set()


# ============================================================
# CONNECTION
# ============================================================

def connect_ibkr():
    ib = IBApp()


    ib.connect(
        IB_HOST,
        IB_PORT,
        clientId=
            IB_CLIENT_ID
    )


    thread = threading.Thread(
        target=ib.run,
        daemon=True
    )

    thread.start()


    if not ib.ready.wait(
        timeout=5
    ):

        ib.disconnect()

        raise RuntimeError(
            "TWS connection timeout"
        )


    time.sleep(
        0.4
    )


    if (
        IB_ACCOUNT
        not in ib.accounts
    ):

        ib.disconnect()

        raise RuntimeError(
            "Configured IBKR account unavailable"
        )


    return ib


def load_order_state(
    ib
):
    ib.open_orders = []

    ib.completed_orders = []

    ib.open_orders_done.clear()

    ib.completed_orders_done.clear()


    #
    # Same execution clientId 10.
    # This path is for reconciliation / ownership recovery.
    #
    ib.reqOpenOrders()


    if not ib.open_orders_done.wait(
        timeout=5
    ):

        raise RuntimeError(
            "Open-order request timeout"
        )


    ib.reqCompletedOrders(
        True
    )


    if not ib.completed_orders_done.wait(
        timeout=5
    ):

        raise RuntimeError(
            "Completed-order request timeout"
        )


# ============================================================
# ORDER LOOKUP
# ============================================================

def valid_perm_id(
    value
):
    try:
        value = int(
            value
        )

        return (
            value
            if value > 0
            else None
        )

    except Exception:
        return None


def valid_order_id(
    value
):
    try:
        value = int(
            value
        )

        return (
            value
            if value >= 0
            else None
        )

    except Exception:
        return None


def find_by_ref(
    orders,
    order_ref
):
    if not order_ref:
        return None


    for order in orders:

        if (
            order.get(
                "order_ref"
            )
            == order_ref
        ):

            return order


    return None


def find_by_perm(
    orders,
    perm_id
):
    wanted = valid_perm_id(
        perm_id
    )


    if wanted is None:
        return None


    for order in orders:

        candidate = valid_perm_id(
            order.get(
                "perm_id"
            )
        )


        if candidate == wanted:
            return order


    return None


def find_by_order_id(
    orders,
    order_id
):
    wanted = valid_order_id(
        order_id
    )


    if wanted is None:
        return None


    for order in orders:

        candidate = valid_order_id(
            order.get(
                "order_id"
            )
        )


        if candidate == wanted:
            return order


    return None


def all_broker_orders(
    ib
):
    return (
        list(
            ib.open_orders
        )
        +
        list(
            ib.completed_orders
        )
    )


def find_entry_order(
    ib,
    signal
):
    orders = all_broker_orders(
        ib
    )


    #
    # 1. Actual stored broker orderRef
    #
    result = find_by_ref(
        orders,
        signal.get(
            "entry_order_ref"
        )
    )


    if result:
        return result


    #
    # 2. Deterministic orderRef.
    #
    # We use the deterministic value only for lookup.
    # It is not persisted unless IBKR actually returns it.
    #
    expected_ref = (
        build_order_refs(
            signal[
                "signal_id"
            ]
        )[
            "entry"
        ]
    )


    result = find_by_ref(
        orders,
        expected_ref
    )


    if result:
        return result


    #
    # 3. permId
    #
    result = find_by_perm(
        orders,
        signal.get(
            "parent_perm_id"
        )
    )


    if result:
        return result


    #
    # 4. stored broker orderId
    #
    return find_by_order_id(
        orders,
        signal.get(
            "parent_order_id"
        )
    )


def find_child_order(
    ib,
    signal,
    role,
    parent_order
):
    orders = all_broker_orders(
        ib
    )


    if role == "TP":

        stored_ref = signal.get(
            "target_order_ref"
        )

        stored_perm = signal.get(
            "target_perm_id"
        )

        stored_id = signal.get(
            "target_order_id"
        )

        expected_ref = (
            build_order_refs(
                signal[
                    "signal_id"
                ]
            )[
                "target"
            ]
        )

    else:

        stored_ref = signal.get(
            "stop_order_ref"
        )

        stored_perm = signal.get(
            "stop_perm_id"
        )

        stored_id = signal.get(
            "stop_order_id"
        )

        expected_ref = (
            build_order_refs(
                signal[
                    "signal_id"
                ]
            )[
                "stop"
            ]
        )


    result = find_by_ref(
        orders,
        stored_ref
    )


    if result:
        return result


    result = find_by_ref(
        orders,
        expected_ref
    )


    if result:
        return result


    result = find_by_perm(
        orders,
        stored_perm
    )


    if result:
        return result


    result = find_by_order_id(
        orders,
        stored_id
    )


    if result:
        return result


    #
    # Legacy fallback.
    # Use the broker's actual parentId relationship.
    # Never assume parent+1/parent+2 during recovery.
    #
    if parent_order:

        parent_id = valid_order_id(
            parent_order.get(
                "order_id"
            )
        )


        if parent_id is not None:

            children = [
                order
                for order
                in orders
                if valid_order_id(
                    order.get(
                        "parent_id"
                    )
                )
                == parent_id
            ]


            if role == "TP":

                for order in children:

                    if (
                        order.get(
                            "order_type"
                        )
                        == "LMT"
                    ):

                        return order


            if role == "SL":

                for order in children:

                    if (
                        order.get(
                            "order_type"
                        )
                        in {
                            "STP",
                            "STP LMT"
                        }
                    ):

                        return order


    return None


# ============================================================
# IDENTITY REPAIR
# ============================================================

def repair_order_identity(
    signal,
    entry,
    target,
    stop
):
    if not entry:
        return False


    fields = {}


    entry_id = valid_order_id(
        entry.get(
            "order_id"
        )
    )

    entry_perm = valid_perm_id(
        entry.get(
            "perm_id"
        )
    )

    entry_ref = (
        entry.get(
            "order_ref"
        )
        or ""
    ).strip()


    if entry_id is not None:

        fields[
            "parent_order_id"
        ] = entry_id

        fields[
            "entry_order_id"
        ] = entry_id


    if entry_perm is not None:

        fields[
            "parent_perm_id"
        ] = entry_perm


    #
    # Persist orderRef ONLY if it was actually returned
    # by IBKR.
    #
    if entry_ref:

        fields[
            "entry_order_ref"
        ] = entry_ref


    if target:

        target_id = valid_order_id(
            target.get(
                "order_id"
            )
        )

        target_perm = valid_perm_id(
            target.get(
                "perm_id"
            )
        )

        target_ref = (
            target.get(
                "order_ref"
            )
            or ""
        ).strip()


        if target_id is not None:

            fields[
                "target_order_id"
            ] = target_id


        if target_perm is not None:

            fields[
                "target_perm_id"
            ] = target_perm


        if target_ref:

            fields[
                "target_order_ref"
            ] = target_ref


    if stop:

        stop_id = valid_order_id(
            stop.get(
                "order_id"
            )
        )

        stop_perm = valid_perm_id(
            stop.get(
                "perm_id"
            )
        )

        stop_ref = (
            stop.get(
                "order_ref"
            )
            or ""
        ).strip()


        if stop_id is not None:

            fields[
                "stop_order_id"
            ] = stop_id


        if stop_perm is not None:

            fields[
                "stop_perm_id"
            ] = stop_perm


        if stop_ref:

            fields[
                "stop_order_ref"
            ] = stop_ref


    if not fields:
        return False


    identity = [
        str(
            fields.get(
                "parent_order_id",
                ""
            )
        ),

        str(
            fields.get(
                "parent_perm_id",
                ""
            )
        ),

        str(
            fields.get(
                "target_order_id",
                ""
            )
        ),

        str(
            fields.get(
                "stop_order_id",
                ""
            )
        )
    ]


    update_signal_metadata(
        db_file=DB_FILE,

        signal_id=
            signal[
                "signal_id"
            ],

        source="worker",

        event_type=
            "ORDER_IDENTITY_RECOVERED",

        fields=
            fields,

        message=(
            "Recovered order identity from "
            "actual IBKR broker data"
        ),

        payload={
            "entry":
                entry,

            "target":
                target,

            "stop":
                stop
        },

        event_key=(
            "worker-identity:"
            + signal[
                "signal_id"
            ]
            + ":"
            + ":".join(
                identity
            )
        )
    )


    return True


# ============================================================
# RECONCILIATION
# ============================================================

def classify_completed(
    order
):
    status = (
        order.get(
            "status",
            ""
        )
        or ""
    ).strip()


    if status in {
        "Cancelled",
        "ApiCancelled"
    }:

        return (
            "CANCELLED",
            "CANCEL_CONFIRMED"
        )


    if status == "Filled":

        return (
            "FILLED",
            "ORDER_FILLED"
        )


    if status == "Inactive":

        return (
            "ERROR",
            "ORDER_INACTIVE"
        )


    if status == "PendingCancel":

        return (
            "CANCEL_PENDING",
            "CANCEL_PENDING"
        )


    return (
        "UNKNOWN",
        "ORDER_COMPLETED_UNKNOWN"
    )


def reconcile_signal(
    ib,
    signal
):
    signal_id = signal[
        "signal_id"
    ]


    entry = find_entry_order(
        ib,
        signal
    )


    target = find_child_order(
        ib,
        signal,
        "TP",
        entry
    )


    stop = find_child_order(
        ib,
        signal,
        "SL",
        entry
    )


    if entry:

        repair_order_identity(
            signal,
            entry,
            target,
            stop
        )


    if not entry:

        if (
            signal[
                "status"
            ]
            in {
                "CANCEL_REQUESTED",
                "CANCELLING",
                "CANCEL_PENDING",
                "CANCEL_UNKNOWN"
            }
        ):

            target_status = (
                "CANCEL_UNKNOWN"
            )

        else:

            target_status = (
                "UNKNOWN"
            )


        transition(
            signal_id,
            target_status,
            "ORDER_NOT_FOUND",

            message=(
                "Entry order not found using "
                "orderRef, permId or orderId"
            ),

            event_key=(
                f"reconcile-not-found:"
                f"{signal_id}:"
                f"{target_status}"
            )
        )

        return


    broker_status = (
        entry.get(
            "status",
            ""
        )
        or ""
    ).strip()


    is_open = (
        entry
        in ib.open_orders
    )


    if is_open:

        if broker_status == "PendingCancel":

            transition(
                signal_id,
                "CANCEL_PENDING",
                "CANCEL_PENDING",

                message=(
                    "IBKR reports PendingCancel"
                ),

                payload=
                    entry,

                event_key=(
                    f"broker:"
                    f"{signal_id}:"
                    f"{entry.get('perm_id')}:"
                    f"PendingCancel"
                )
            )

            return


        if broker_status in {
            "PendingSubmit",
            "ApiPending",
            "PreSubmitted"
        }:

            if (
                signal[
                    "status"
                ]
                in {
                    "CANCEL_REQUESTED",
                    "CANCELLING",
                    "CANCEL_PENDING",
                    "CANCEL_UNKNOWN"
                }
            ):

                target_status = (
                    "CANCEL_PENDING"
                )

                event_type = (
                    "CANCEL_PENDING"
                )

            else:

                target_status = (
                    "ACCEPTED_WAITING_MARKET"
                )

                event_type = (
                    "ORDER_WAITING_MARKET"
                )


            transition(
                signal_id,
                target_status,
                event_type,

                message=(
                    f"IBKR status="
                    f"{broker_status}"
                ),

                payload=
                    entry,

                event_key=(
                    f"broker:"
                    f"{signal_id}:"
                    f"{entry.get('perm_id')}:"
                    f"{broker_status}:"
                    f"{target_status}"
                )
            )

            return


        if broker_status == "Submitted":

            if (
                signal[
                    "status"
                ]
                in {
                    "CANCEL_REQUESTED",
                    "CANCELLING",
                    "CANCEL_PENDING",
                    "CANCEL_UNKNOWN"
                }
            ):

                target_status = (
                    "CANCEL_PENDING"
                )

                event_type = (
                    "CANCEL_PENDING"
                )

            else:

                target_status = (
                    "SUBMITTED"
                )

                event_type = (
                    "ORDER_SUBMITTED"
                )


            transition(
                signal_id,
                target_status,
                event_type,

                message=(
                    "IBKR entry order Submitted"
                ),

                payload=
                    entry,

                event_key=(
                    f"broker:"
                    f"{signal_id}:"
                    f"{entry.get('perm_id')}:"
                    f"Submitted:"
                    f"{target_status}"
                )
            )

            return


        transition(
            signal_id,
            "UNKNOWN",
            "ORDER_STATE_UNKNOWN",

            message=(
                f"Unexpected IBKR status: "
                f"{broker_status}"
            ),

            payload=
                entry,

            event_key=(
                f"broker:"
                f"{signal_id}:"
                f"{entry.get('perm_id')}:"
                f"unknown:"
                f"{broker_status}"
            )
        )

        return


    new_status, event_type = (
        classify_completed(
            entry
        )
    )


    transition(
        signal_id,
        new_status,
        event_type,

        message=(
            f"IBKR completed entry status="
            f"{broker_status}"
        ),

        payload=
            entry,

        event_key=(
            f"completed:"
            f"{signal_id}:"
            f"{entry.get('perm_id')}:"
            f"{broker_status}"
        )
    )


def reconcile_orders():
    ib = None


    try:
        ib = connect_ibkr()

        load_order_state(
            ib
        )


        candidates = (
            get_reconcile_candidates()
        )


        print(
            f"RECONCILE | "
            f"broker_open="
            f"{len(ib.open_orders)} | "
            f"broker_completed="
            f"{len(ib.completed_orders)} | "
            f"db_candidates="
            f"{len(candidates)}",
            flush=True
        )


        for signal in candidates:

            try:

                reconcile_signal(
                    ib,
                    signal
                )

            except Exception as exc:

                print(
                    f"RECONCILE SIGNAL ERROR | "
                    f"{signal['signal_id']} | "
                    f"{exc}",
                    flush=True
                )


                record_event(
                    db_file=DB_FILE,

                    signal_id=
                        signal[
                            "signal_id"
                        ],

                    event_type=
                        "RECONCILE_EXCEPTION",

                    source="worker",

                    message=str(
                        exc
                    )
                )


        return True


    except Exception as exc:

        print(
            f"RECONCILE ERROR | "
            f"{exc}",
            flush=True
        )


        record_event(
            db_file=DB_FILE,

            signal_id=None,

            event_type=
                "BROKER_RECONCILE_FAILED",

            source="worker",

            message=str(
                exc
            )
        )


        return False


    finally:

        if ib is not None:

            try:
                ib.disconnect()

            except Exception:
                pass


# ============================================================
# CANCELLATION
# ============================================================

def open_order_ids_for_signal(
    ib,
    signal,
    entry
):
    result = set()


    if entry:

        entry_id = valid_order_id(
            entry.get(
                "order_id"
            )
        )


        if entry_id is not None:

            for order in ib.open_orders:

                order_id = valid_order_id(
                    order.get(
                        "order_id"
                    )
                )


                parent_id = valid_order_id(
                    order.get(
                        "parent_id"
                    )
                )


                if (
                    order_id
                    == entry_id
                    or
                    parent_id
                    == entry_id
                ):

                    result.add(
                        order_id
                    )


    #
    # Explicit stored IDs.
    #
    for field in (
        "entry_order_id",
        "parent_order_id",
        "target_order_id",
        "stop_order_id"
    ):

        stored_id = valid_order_id(
            signal.get(
                field
            )
        )


        if stored_id is None:
            continue


        if any(
            valid_order_id(
                order.get(
                    "order_id"
                )
            )
            == stored_id
            for order
            in ib.open_orders
        ):

            result.add(
                stored_id
            )


    #
    # Actual broker refs.
    #
    stored_refs = {
        value
        for value in (
            signal.get(
                "entry_order_ref"
            ),

            signal.get(
                "target_order_ref"
            ),

            signal.get(
                "stop_order_ref"
            )
        )
        if value
    }


    for order in ib.open_orders:

        if (
            order.get(
                "order_ref"
            )
            in stored_refs
        ):

            order_id = valid_order_id(
                order.get(
                    "order_id"
                )
            )


            if order_id is not None:

                result.add(
                    order_id
                )


    return result


def process_cancel(
    signal
):
    signal_id = signal[
        "signal_id"
    ]


    ib = None


    try:
        ib = connect_ibkr()

        load_order_state(
            ib
        )


        entry = find_entry_order(
            ib,
            signal
        )


        target = find_child_order(
            ib,
            signal,
            "TP",
            entry
        )


        stop = find_child_order(
            ib,
            signal,
            "SL",
            entry
        )


        if entry:

            repair_order_identity(
                signal,
                entry,
                target,
                stop
            )


        if not entry:

            transition(
                signal_id,
                "CANCEL_UNKNOWN",
                "CANCEL_ORDER_NOT_FOUND",

                message=(
                    "Cancellation requested but "
                    "entry order was not found"
                )
            )

            return


        broker_status = (
            entry.get(
                "status",
                ""
            )
            or ""
        ).strip()


        if (
            entry
            not in ib.open_orders
        ):

            completed_status, event_type = (
                classify_completed(
                    entry
                )
            )


            transition(
                signal_id,
                completed_status,
                event_type,

                message=(
                    "Cancellation reconciled "
                    "against completed broker order"
                ),

                payload=
                    entry
            )

            return


        if broker_status == "PendingCancel":

            transition(
                signal_id,
                "CANCEL_PENDING",
                "CANCEL_PENDING",

                message=(
                    "IBKR already reports "
                    "PendingCancel"
                ),

                payload=
                    entry,

                event_key=(
                    f"cancel-pending:"
                    f"{signal_id}:"
                    f"{entry.get('perm_id')}"
                )
            )

            return


        cancel_ids = (
            open_order_ids_for_signal(
                ib,
                signal,
                entry
            )
        )


        if not cancel_ids:

            transition(
                signal_id,
                "CANCEL_UNKNOWN",
                "CANCEL_TARGETS_NOT_FOUND",

                message=(
                    "No matching open broker orders "
                    "were found for cancellation"
                )
            )

            return


        entry_id = valid_order_id(
            entry.get(
                "order_id"
            )
        )


        cancel_sequence = []


        if (
            entry_id is not None
            and
            entry_id in cancel_ids
        ):

            cancel_sequence.append(
                entry_id
            )


        cancel_sequence.extend(
            sorted(
                order_id
                for order_id
                in cancel_ids
                if order_id
                != entry_id
            )
        )


        ib.cancel_target_ids = set(
            cancel_sequence
        )


        record_event(
            db_file=DB_FILE,

            signal_id=
                signal_id,

            event_type=
                "CANCEL_SENT",

            source="worker",

            message=(
                "Cancellation sent to "
                "identified bracket orders"
            ),

            payload={
                "order_ids":
                    cancel_sequence
            },

            event_key=(
                f"cancel-sent:"
                f"{signal_id}:"
                + ",".join(
                    str(item)
                    for item
                    in cancel_sequence
                )
            )
        )


        for order_id in cancel_sequence:

            ib.cancelOrder(
                order_id,
                OrderCancel()
            )

            time.sleep(
                0.25
            )


        deadline = (
            time.time()
            +
            CANCEL_TIMEOUT_SECONDS
        )


        while (
            time.time()
            < deadline
        ):

            if (
                ib.cancelled_ids
                == ib.cancel_target_ids
                and
                ib.cancel_target_ids
            ):

                break


            parent_status = (
                ib.order_statuses.get(
                    entry_id
                )
                if entry_id
                is not None
                else None
            )


            if (
                parent_status
                == "PendingCancel"
            ):

                break


            time.sleep(
                0.1
            )


        if (
            ib.cancelled_ids
            == ib.cancel_target_ids
            and
            ib.cancel_target_ids
        ):

            transition(
                signal_id,
                "CANCELLED",
                "CANCEL_CONFIRMED",

                payload={
                    "order_ids":
                        sorted(
                            ib.cancelled_ids
                        )
                },

                event_key=(
                    f"cancel-confirmed:"
                    f"{signal_id}:"
                    + ",".join(
                        str(item)
                        for item
                        in sorted(
                            ib.cancelled_ids
                        )
                    )
                )
            )

            return


        transition(
            signal_id,
            "CANCEL_PENDING",
            "CANCEL_PENDING",

            message=(
                "Cancellation sent; "
                "broker confirmation pending"
            ),

            payload={
                "order_ids":
                    cancel_sequence
            },

            event_key=(
                f"cancel-pending-after-send:"
                f"{signal_id}:"
                + ",".join(
                    str(item)
                    for item
                    in cancel_sequence
                )
            )
        )


    except Exception as exc:

        transition(
            signal_id,
            "CANCEL_UNKNOWN",
            "CANCEL_EXCEPTION",

            message=str(
                exc
            )
        )


    finally:

        if ib is not None:

            try:
                ib.disconnect()

            except Exception:
                pass


# ============================================================
# MARKET SESSION
# ============================================================

def check_market_session(
    ib,
    symbol
):
    ib.contract_details = []

    ib.contract_details_done.clear()


    ib.reqContractDetails(
        9201,
        stock_contract(
            symbol
        )
    )


    if not ib.contract_details_done.wait(
        timeout=5
    ):

        raise MarketSessionError(
            "IBKR contract details timeout"
        )


    if not ib.contract_details:

        raise MarketSessionError(
            (
                "No IBKR contract details "
                f"returned for {symbol}"
            )
        )


    candidates = []


    for details in (
        ib.contract_details
    ):

        contract = (
            details.contract
        )


        if (
            str(
                contract.symbol
            ).upper()
            == symbol.upper()
            and
            contract.secType
            == "STK"
            and
            contract.currency
            == "USD"
        ):

            candidates.append(
                details
            )


    if not candidates:

        raise MarketSessionError(
            (
                "No matching USD stock "
                f"contract for {symbol}"
            )
        )


    #
    # SMART stock queries can return multiple contracts.
    # Only accept a unique symbol/currency stock candidate.
    # Ambiguity fails closed rather than picking at random.
    #
    unique = {}


    for details in candidates:

        con_id = getattr(
            details.contract,
            "conId",
            None
        )

        key = (
            con_id
            if con_id
            else (
                getattr(
                    details.contract,
                    "symbol",
                    ""
                ),
                getattr(
                    details.contract,
                    "primaryExchange",
                    ""
                ),
                getattr(
                    details.contract,
                    "currency",
                    ""
                )
            )
        )

        unique[
            key
        ] = details


    if len(
        unique
    ) != 1:

        exchanges = sorted({
            str(
                getattr(
                    item.contract,
                    "primaryExchange",
                    ""
                )
            )
            for item
            in unique.values()
        })


        raise MarketSessionError(
            (
                "Ambiguous IBKR stock contract "
                f"for {symbol}; candidates="
                f"{len(unique)}; "
                f"primaryExchanges="
                f"{exchanges}"
            )
        )


    selected = next(
        iter(
            unique.values()
        )
    )


    result = evaluate_liquid_hours(
        liquid_hours=
            getattr(
                selected,
                "liquidHours",
                None
            ),

        timezone_name=
            getattr(
                selected,
                "timeZoneId",
                None
            )
    )


    result[
        "symbol"
    ] = symbol

    result[
        "con_id"
    ] = getattr(
        selected.contract,
        "conId",
        None
    )

    result[
        "primary_exchange"
    ] = getattr(
        selected.contract,
        "primaryExchange",
        None
    )


    return result


# ============================================================
# BRACKET
# ============================================================

def create_bracket(
    signal_id,
    parent_id,
    quantity,
    entry,
    target,
    stop
):
    refs = build_order_refs(
        signal_id
    )


    parent = Order()

    parent.orderId = parent_id
    parent.account = IB_ACCOUNT
    parent.action = "BUY"
    parent.orderType = "LMT"
    parent.totalQuantity = quantity
    parent.lmtPrice = entry
    parent.tif = "DAY"
    parent.outsideRth = (
        ALLOW_OUTSIDE_RTH
    )
    parent.transmit = False
    parent.orderRef = refs[
        "entry"
    ]


    take_profit = Order()

    take_profit.orderId = (
        parent_id + 1
    )

    take_profit.account = (
        IB_ACCOUNT
    )

    take_profit.action = "SELL"
    take_profit.orderType = "LMT"
    take_profit.totalQuantity = quantity
    take_profit.lmtPrice = target
    take_profit.parentId = parent_id
    take_profit.tif = "GTC"
    take_profit.outsideRth = False
    take_profit.transmit = False
    take_profit.orderRef = refs[
        "target"
    ]


    stop_loss = Order()

    stop_loss.orderId = (
        parent_id + 2
    )

    stop_loss.account = (
        IB_ACCOUNT
    )

    stop_loss.action = "SELL"
    stop_loss.orderType = "STP"
    stop_loss.totalQuantity = quantity
    stop_loss.auxPrice = stop
    stop_loss.parentId = parent_id
    stop_loss.tif = "GTC"
    stop_loss.outsideRth = False
    stop_loss.transmit = True
    stop_loss.orderRef = refs[
        "stop"
    ]


    return (
        [
            parent,
            take_profit,
            stop_loss
        ],
        refs
    )


def bracket_is_accepted(
    ib
):
    if (
        ib.fatal_order_error
        .is_set()
    ):

        return False


    if (
        ib.seen_order_ids
        != ib.expected_order_ids
    ):

        return False


    return (
        ib.parent_status
        in {
            "PreSubmitted",
            "Submitted",
            "Filled"
        }
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal(
    signal
):
    signal_id = signal[
        "signal_id"
    ]

    symbol = signal[
        "symbol"
    ].upper()


    print(
        f"PROCESSING | "
        f"{signal_id} | "
        f"{symbol}",
        flush=True
    )


    # --------------------------------------------------------
    # TEST SIGNAL
    # --------------------------------------------------------

    if (
        signal[
            "test_mode"
        ]
        == 1
    ):

        transition(
            signal_id,
            "TESTED",
            "TEST_SIGNAL_COMPLETED",

            message=(
                "Signal processed in TEST mode"
            )
        )

        return


    # --------------------------------------------------------
    # MASTER SWITCH
    # --------------------------------------------------------

    if not LIVE_TRADING:

        transition(
            signal_id,
            "BLOCKED",
            "LIVE_DISABLED",

            message=(
                "LIVE_TRADING=false"
            )
        )

        return


    # --------------------------------------------------------
    # FRESHNESS CHECK #1
    # --------------------------------------------------------

    try:

        enforce_execution_freshness(
            signal,
            "worker_start"
        )

    except SignalFreshnessError:

        return


    # --------------------------------------------------------
    # SAFETY CHECK #1
    # --------------------------------------------------------

    try:

        safety = check_pre_execution(
            db_file=
                DB_FILE,

            max_snapshot_age_seconds=
                STATUS_MAX_AGE_SECONDS,

            max_open_positions=
                MAX_MANAGED_POSITIONS,

            max_trades_per_day=
                MAX_TRADES_PER_DAY,

            max_daily_loss_usd=
                MAX_DAILY_LOSS_USD,

            minimum_account_equity=
                MIN_ACCOUNT_EQUITY_USD,

            block_on_pending_cancel=
                BLOCK_LIVE_ON_PENDING_CANCEL
        )


        record_event(
            db_file=
                DB_FILE,

            signal_id=
                signal_id,

            event_type=
                "SAFETY_APPROVED",

            source=
                "worker",

            payload=
                safety,

            event_key=(
                f"safety:"
                f"{signal_id}:"
                f"initial"
            )
        )


    except ExecutionBlocked as exc:

        transition(
            signal_id,
            "BLOCKED",
            "SAFETY_BLOCKED",

            message=str(
                exc
            )
        )

        return


    ib = None

    parent_id = None


    try:

        ib = connect_ibkr()


        # ----------------------------------------------------
        # MARKET SESSION
        # ----------------------------------------------------

        if REQUIRE_LIQUID_SESSION:

            try:

                session = (
                    check_market_session(
                        ib,
                        symbol
                    )
                )

            except Exception as exc:

                transition(
                    signal_id,
                    "BLOCKED",
                    "MARKET_SESSION_UNKNOWN",

                    message=str(
                        exc
                    )
                )

                return


            record_event(
                db_file=
                    DB_FILE,

                signal_id=
                    signal_id,

                event_type=(
                    "MARKET_SESSION_OPEN"
                    if session[
                        "is_open"
                    ]
                    else
                    "MARKET_SESSION_CLOSED"
                ),

                source=
                    "worker",

                message=
                    session[
                        "reason"
                    ],

                payload=
                    session,

                event_key=(
                    f"market-session:"
                    f"{signal_id}"
                )
            )


            if not session[
                "is_open"
            ]:

                transition(
                    signal_id,
                    "BLOCKED",
                    "MARKET_CLOSED_BLOCK",

                    message=
                        session[
                            "reason"
                        ],

                    payload=
                        session
                )

                return


        # ----------------------------------------------------
        # FRESH POSITIONS
        # ----------------------------------------------------

        ib.positions = {}

        ib.positions_done.clear()


        ib.reqPositions()


        if not (
            ib.positions_done.wait(
                timeout=3
            )
        ):

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_POSITIONS_TIMEOUT"
            )

            return


        fresh_broker_positions = [
            {
                "symbol":
                    broker_symbol,

                **position
            }

            for broker_symbol, position
            in ib.positions.items()
        ]


        try:

            fresh_position_state = (
                evaluate_position_limits(
                    db_file=
                        DB_FILE,

                    broker_positions=
                        fresh_broker_positions,

                    max_managed_positions=
                        MAX_MANAGED_POSITIONS,

                    max_total_broker_positions=
                        MAX_TOTAL_BROKER_POSITIONS
                )
            )


        except PositionPolicyError as exc:

            transition(
                signal_id,
                "BLOCKED",
                "FRESH_POSITION_POLICY_ERROR",

                message=str(
                    exc
                )
            )

            return


        except Exception as exc:

            transition(
                signal_id,
                "BLOCKED",
                "FRESH_POSITION_POLICY_ERROR",

                message=(
                    "Fresh IBKR position policy "
                    f"failed: {exc}"
                )
            )

            return


        if fresh_position_state[
            "blockers"
        ]:

            transition(
                signal_id,
                "BLOCKED",
                "POSITION_LIMIT_BLOCK",

                message="; ".join(
                    fresh_position_state[
                        "blockers"
                    ]
                ),

                payload=
                    fresh_position_state
            )

            return


        record_event(
            db_file=
                DB_FILE,

            signal_id=
                signal_id,

            event_type=
                "FRESH_POSITION_POLICY_APPROVED",

            source=
                "worker",

            payload=
                fresh_position_state,

            event_key=(
                f"fresh-position-policy:"
                f"{signal_id}"
            )
        )


        #
        # Same symbol is blocked even when the current
        # holding is a legacy position.
        #
        if symbol in ib.positions:

            transition(
                signal_id,
                "BLOCKED",
                "EXISTING_POSITION_BLOCK",

                message=(
                    f"Existing broker position "
                    f"in {symbol}"
                )
            )

            return


        # ----------------------------------------------------
        # FRESH ACCOUNT-WIDE OPEN ORDERS
        # ----------------------------------------------------

        ib.open_orders = []

        ib.open_orders_done.clear()


        #
        # reqAllOpenOrders() is deliberately used here.
        #
        # This is an account-wide READ of open orders and
        # can reveal PendingCancel / active orders submitted
        # through another API client.
        #
        # It does NOT transfer cancellation ownership.
        #
        ib.reqAllOpenOrders()


        if not (
            ib.open_orders_done.wait(
                timeout=3
            )
        ):

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_ORDERS_TIMEOUT"
            )

            return


        active_statuses = {
            "PendingSubmit",
            "ApiPending",
            "PreSubmitted",
            "Submitted",
            "PendingCancel"
        }


        for order in (
            ib.open_orders
        ):

            if (
                order[
                    "status"
                ]
                == "PendingCancel"
            ):

                transition(
                    signal_id,
                    "BLOCKED",
                    "FRESH_PENDING_CANCEL_BLOCK",

                    message=(
                        "Fresh account-wide IBKR "
                        "check found PendingCancel "
                        "order"
                    ),

                    payload=
                        order
                )

                return


            if (
                order[
                    "symbol"
                ]
                == symbol
                and
                order[
                    "status"
                ]
                in active_statuses
            ):

                transition(
                    signal_id,
                    "BLOCKED",
                    "EXISTING_ORDER_BLOCK",

                    message=(
                        f"Existing active "
                        f"order in {symbol}"
                    ),

                    payload=
                        order
                )

                return


        # ----------------------------------------------------
        # FRESH DAILY P/L
        # ----------------------------------------------------

        pnl_request_id = 9901


        ib.pnl_done.clear()

        ib.daily_pnl = None


        ib.reqPnL(
            pnl_request_id,
            IB_ACCOUNT,
            ""
        )


        if not (
            ib.pnl_done.wait(
                timeout=3
            )
        ):

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_PNL_TIMEOUT"
            )

            return


        try:

            ib.cancelPnL(
                pnl_request_id
            )

        except Exception:

            pass


        if ib.daily_pnl is None:

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_PNL_UNAVAILABLE"
            )

            return


        if (
            ib.daily_pnl
            <=
            -abs(
                MAX_DAILY_LOSS_USD
            )
        ):

            transition(
                signal_id,
                "BLOCKED",
                "DAILY_LOSS_BLOCK",

                message=(
                    f"Daily P/L="
                    f"{ib.daily_pnl}"
                )
            )

            return


        # ----------------------------------------------------
        # FINAL SAFETY
        # ----------------------------------------------------

        try:

            final_safety = (
                check_pre_execution(
                    db_file=
                        DB_FILE,

                    max_snapshot_age_seconds=
                        STATUS_MAX_AGE_SECONDS,

                    max_open_positions=
                        MAX_MANAGED_POSITIONS,

                    max_trades_per_day=
                        MAX_TRADES_PER_DAY,

                    max_daily_loss_usd=
                        MAX_DAILY_LOSS_USD,

                    minimum_account_equity=
                        MIN_ACCOUNT_EQUITY_USD,

                    block_on_pending_cancel=
                        BLOCK_LIVE_ON_PENDING_CANCEL
                )
            )


        except ExecutionBlocked as exc:

            transition(
                signal_id,
                "BLOCKED",
                "FINAL_SAFETY_BLOCK",

                message=str(
                    exc
                )
            )

            return


        record_event(
            db_file=
                DB_FILE,

            signal_id=
                signal_id,

            event_type=
                "FINAL_SAFETY_APPROVED",

            source=
                "worker",

            payload=
                final_safety,

            event_key=(
                f"safety:"
                f"{signal_id}:"
                f"final"
            )
        )


        # ----------------------------------------------------
        # FINAL SIGNAL FRESHNESS
        # ----------------------------------------------------

        try:

            freshness = (
                enforce_execution_freshness(
                    signal,
                    "before_place_order"
                )
            )


        except SignalFreshnessError:

            return


        print(
            f"FINAL FRESHNESS PASS | "
            f"{signal_id} | "
            f"age="
            f"{freshness['age_seconds']:.2f}s",
            flush=True
        )


        # ----------------------------------------------------
        # PRE-LIVE DRY-RUN HARD GATE
        # ----------------------------------------------------

        #
        # This block is intentionally positioned BEFORE:
        #
        #   * parent_id allocation
        #   * create_bracket()
        #   * ORDER_IDS_ALLOCATED
        #   * ORDER_SUBMISSION_INTENT
        #   * placeOrder()
        #
        # Therefore PRELIVE_DRY_RUN=true cannot submit a
        # broker order through process_signal().
        #
        if PRELIVE_DRY_RUN:

            dry_run_payload = {
                "symbol":
                    symbol,

                "managed_positions":
                    fresh_position_state[
                        "managed_position_count"
                    ],

                "legacy_positions":
                    fresh_position_state[
                        "legacy_position_count"
                    ],

                "total_broker_positions":
                    fresh_position_state[
                        "broker_position_count"
                    ],

                "max_managed_positions":
                    MAX_MANAGED_POSITIONS,

                "max_total_broker_positions":
                    MAX_TOTAL_BROKER_POSITIONS,

                "daily_pnl":
                    ib.daily_pnl,

                "signal_age_seconds":
                    freshness[
                        "age_seconds"
                    ]
            }


            record_event(
                db_file=
                    DB_FILE,

                signal_id=
                    signal_id,

                event_type=
                    "PRELIVE_DRY_RUN_PASSED",

                source=
                    "worker",

                message=(
                    "All live pre-execution checks "
                    "passed; broker submission "
                    "intentionally blocked by "
                    "PRELIVE_DRY_RUN=true"
                ),

                payload=
                    dry_run_payload,

                event_key=(
                    f"prelive-dry-run:"
                    f"{signal_id}"
                )
            )


            transition(
                signal_id,
                "BLOCKED",
                "PRELIVE_DRY_RUN_BLOCK",

                message=(
                    "PRELIVE_DRY_RUN=true: "
                    "stopped before order ID "
                    "allocation and placeOrder()"
                ),

                payload=
                    dry_run_payload,

                event_key=(
                    f"prelive-dry-run-block:"
                    f"{signal_id}"
                )
            )


            print(
                f"PRELIVE DRY RUN PASS | "
                f"{signal_id} | "
                "NO ORDER SUBMITTED",
                flush=True
            )

            return


        # ----------------------------------------------------
        # ORDER IDS
        # ----------------------------------------------------

        parent_id = (
            ib.next_order_id
        )


        if parent_id is None:

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_ORDER_ID_UNAVAILABLE"
            )

            return


        ib.parent_order_id = (
            parent_id
        )


        ib.expected_order_ids = {
            parent_id,
            parent_id + 1,
            parent_id + 2
        }


        contract = stock_contract(
            symbol
        )


        orders, refs = create_bracket(
            signal_id,
            parent_id,
            signal[
                "quantity"
            ],
            signal[
                "entry"
            ],
            signal[
                "target"
            ],
            signal[
                "stop"
            ]
        )


        # ----------------------------------------------------
        # PERSIST IDENTITY BEFORE BROKER CALL
        # ----------------------------------------------------

        update_signal_metadata(
            db_file=
                DB_FILE,

            signal_id=
                signal_id,

            source=
                "worker",

            event_type=
                "ORDER_IDS_ALLOCATED",

            fields={
                "parent_order_id":
                    parent_id,

                "entry_order_id":
                    parent_id,

                "target_order_id":
                    parent_id + 1,

                "stop_order_id":
                    parent_id + 2,

                "entry_order_ref":
                    refs[
                        "entry"
                    ],

                "target_order_ref":
                    refs[
                        "target"
                    ],

                "stop_order_ref":
                    refs[
                        "stop"
                    ]
            },

            payload={
                "parent_order_id":
                    parent_id,

                "refs":
                    refs,

                "freshness":
                    freshness
            },

            event_key=(
                f"order-ids:"
                f"{signal_id}:"
                f"{parent_id}"
            )
        )


        # ----------------------------------------------------
        # INTENT EVENT BEFORE BROKER SIDE EFFECT
        # ----------------------------------------------------

        record_event(
            db_file=
                DB_FILE,

            signal_id=
                signal_id,

            event_type=
                "ORDER_SUBMISSION_INTENT",

            source=
                "worker",

            message=(
                "Bracket identity persisted; "
                "broker submission beginning"
            ),

            payload={
                "parent_order_id":
                    parent_id,

                "order_ids":
                    sorted(
                        ib.expected_order_ids
                    ),

                "refs":
                    refs
            },

            event_key=(
                f"submission-intent:"
                f"{signal_id}:"
                f"{parent_id}"
            )
        )


        # ----------------------------------------------------
        # PLACE BRACKET
        # ----------------------------------------------------

        for order in orders:

            ib.placeOrder(
                order.orderId,
                contract,
                order
            )


            record_event(
                db_file=
                    DB_FILE,

                signal_id=
                    signal_id,

                event_type=
                    "IBKR_PLACE_ORDER_RETURNED",

                source=
                    "worker",

                payload={
                    "order_id":
                        order.orderId,

                    "order_ref":
                        order.orderRef
                },

                event_key=(
                    f"place-returned:"
                    f"{signal_id}:"
                    f"{order.orderId}"
                )
            )


            time.sleep(
                0.15
            )


        # ----------------------------------------------------
        # ACK
        # ----------------------------------------------------

        deadline = (
            time.time()
            +
            ACK_TIMEOUT_SECONDS
        )


        while (
            time.time()
            < deadline
        ):

            if (
                ib.fatal_order_error
                .is_set()
            ):

                break


            if bracket_is_accepted(
                ib
            ):

                break


            time.sleep(
                0.1
            )


        if (
            ib.fatal_order_error
            .is_set()
        ):

            transition(
                signal_id,
                "ERROR",
                "ORDER_REJECTED",

                message=(
                    "; ".join(
                        ib.reject_messages
                    )
                ),

                fields={
                    "parent_order_id":
                        parent_id
                }
            )

            return


        if bracket_is_accepted(
            ib
        ):

            transition(
                signal_id,
                "SUBMITTED",
                "ORDER_ACKNOWLEDGED",

                payload={
                    "statuses":
                        ib.order_statuses
                },

                fields={
                    "parent_order_id":
                        parent_id
                }
            )

            return


        transition(
            signal_id,
            "UNKNOWN",
            "ORDER_ACK_TIMEOUT",

            payload={
                "statuses":
                    ib.order_statuses,

                "expected_order_ids":
                    sorted(
                        ib.expected_order_ids
                    )
            },

            fields={
                "parent_order_id":
                    parent_id
            }
        )


    except Exception as exc:

        transition(
            signal_id,
            "UNKNOWN",
            "WORKER_EXCEPTION",

            message=str(
                exc
            ),

            fields=(
                {
                    "parent_order_id":
                        parent_id
                }
                if parent_id
                is not None
                else None
            )
        )


    finally:

        if ib is not None:

            try:
                ib.disconnect()

            except Exception:
                pass


# ============================================================
# STARTUP SYNCHRONIZATION
# ============================================================

def startup_sync():
    print(
        "STARTUP | waiting for successful "
        "IBKR reconciliation",
        flush=True
    )


    while True:

        success = (
            reconcile_orders()
        )


        if success:

            record_event(
                db_file=
                    DB_FILE,

                signal_id=None,

                event_type=
                    "WORKER_STARTUP_RECONCILED",

                source=
                    "worker",

                message=(
                    "Worker completed IBKR "
                    "startup synchronization"
                )
            )


            print(
                "STARTUP READY | "
                "IBKR reconciliation completed",
                flush=True
            )

            return


        print(
            f"STARTUP BLOCKED | "
            f"retrying in "
            f"{STARTUP_RETRY_SECONDS}s",
            flush=True
        )


        time.sleep(
            STARTUP_RETRY_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

def main():
    init_db()


    print(
        "TradingMax worker started",
        flush=True
    )

    print(
        f"IB_CLIENT_ID="
        f"{IB_CLIENT_ID}",
        flush=True
    )

    print(
        f"LIVE_TRADING="
        f"{LIVE_TRADING}",
        flush=True
    )

    print(
        f"REQUIRE_LIQUID_SESSION="
        f"{REQUIRE_LIQUID_SESSION}",
        flush=True
    )

    print(
        f"MAX_SIGNAL_AGE_SECONDS="
        f"{MAX_SIGNAL_AGE_SECONDS}",
        flush=True
    )

    print(
        f"MAX_MANAGED_POSITIONS="
        f"{MAX_MANAGED_POSITIONS}",
        flush=True
    )

    print(
        f"MAX_TOTAL_BROKER_POSITIONS="
        f"{MAX_TOTAL_BROKER_POSITIONS}",
        flush=True
    )

    print(
        f"PRELIVE_DRY_RUN="
        f"{PRELIVE_DRY_RUN}",
        flush=True
    )

    print(
        "QUEUE_CLAIM=ATOMIC",
        flush=True
    )

    print(
        "CANCEL_DISCOVERY="
        "stored IDs + broker refs + parentId",
        flush=True
    )


    recover_stuck_work()


    startup_sync()


    last_reconcile = (
        time.monotonic()
    )


    while True:

        try:

            # ------------------------------------------------
            # CANCEL HAS PRIORITY
            # ------------------------------------------------

            cancel_request = (
                get_next_cancel_request()
            )


            if cancel_request:

                process_cancel(
                    cancel_request
                )

                continue


            # ------------------------------------------------
            # NEW SIGNAL
            # ------------------------------------------------

            signal = (
                get_next_signal()
            )


            if signal:

                process_signal(
                    signal
                )

                continue


            # ------------------------------------------------
            # PERIODIC RECONCILIATION
            # ------------------------------------------------

            if (
                time.monotonic()
                -
                last_reconcile
                >=
                RECONCILE_INTERVAL_SECONDS
            ):

                success = (
                    reconcile_orders()
                )


                if not success:

                    print(
                        "BROKER SYNC LOST | "
                        "entering startup lock",
                        flush=True
                    )

                    startup_sync()


                last_reconcile = (
                    time.monotonic()
                )


            time.sleep(
                0.5
            )


        except Exception as exc:

            print(
                f"WORKER LOOP ERROR | "
                f"{exc}",
                flush=True
            )

            time.sleep(
                2
            )


if __name__ == "__main__":
    main()
