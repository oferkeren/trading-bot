import json
from pathlib import Path
from protection_position_resolver import resolve_recovery_position
from protection_guard import evaluate_live_protection
import math
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING
import os
import sys
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel

from execution_guard import check_pre_execution, ExecutionBlocked
from execution_freshness import (
    check_execution_freshness,
    SignalFreshnessError,
)
from market_guard import (
    evaluate_liquid_hours,
    MarketSessionError,
)
from position_policy import (
    evaluate_position_limits,
    PositionPolicyError,
)
from signal_contract import (
    normalize_action,
    trade_direction,
    validate_price_structure,
    SignalContractError,
)
from trade_state import (
    init_trade_state,
    transition_signal,
    update_signal_metadata,
    record_event,
    claim_signal,
)


load_dotenv()


BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db",
)


IB_HOST = os.getenv(
    "IB_HOST",
    "127.0.0.1",
)

IB_PORT = int(
    os.getenv(
        "IB_PORT",
        "7496",
    )
)

IB_CLIENT_ID = int(
    os.getenv(
        "IB_CLIENT_ID",
        "10",
    )
)

IB_ACCOUNT = os.getenv(
    "IB_ACCOUNT",
    "",
).strip()


LIVE_TRADING = (
    os.getenv(
        "LIVE_TRADING",
        "false",
    ).lower()
    == "true"
)


MAX_TRADES_PER_DAY = int(
    os.getenv(
        "MAX_TRADES_PER_DAY",
        "5",
    )
)

MAX_OPEN_POSITIONS = int(
    os.getenv(
        "MAX_OPEN_POSITIONS",
        "3",
    )
)

MAX_MANAGED_POSITIONS = int(
    os.getenv(
        "MAX_MANAGED_POSITIONS",
        str(
            MAX_OPEN_POSITIONS
        ),
    )
)

MAX_TOTAL_BROKER_POSITIONS = int(
    os.getenv(
        "MAX_TOTAL_BROKER_POSITIONS",
        "10",
    )
)


PRELIVE_DRY_RUN = (
    os.getenv(
        "PRELIVE_DRY_RUN",
        "true",
    ).lower()
    == "true"
)


MAX_DAILY_LOSS_USD = float(
    os.getenv(
        "MAX_DAILY_LOSS_USD",
        "100",
    )
)

MIN_ACCOUNT_EQUITY_USD = float(
    os.getenv(
        "MIN_ACCOUNT_EQUITY_USD",
        "100",
    )
)


STATUS_MAX_AGE_SECONDS = int(
    os.getenv(
        "STATUS_MAX_AGE_SECONDS",
        "30",
    )
)

MAX_SIGNAL_AGE_SECONDS = int(
    os.getenv(
        "MAX_SIGNAL_AGE_SECONDS",
        "60",
    )
)

MAX_SIGNAL_FUTURE_SKEW_SECONDS = int(
    os.getenv(
        "MAX_SIGNAL_FUTURE_SKEW_SECONDS",
        "10",
    )
)


BLOCK_LIVE_ON_PENDING_CANCEL = (
    os.getenv(
        "BLOCK_LIVE_ON_PENDING_CANCEL",
        "true",
    ).lower()
    == "true"
)


ALLOW_OUTSIDE_RTH = (
    os.getenv(
        "ALLOW_OUTSIDE_RTH",
        "false",
    ).lower()
    == "true"
)


REQUIRE_LIQUID_SESSION = (
    os.getenv(
        "REQUIRE_LIQUID_SESSION",
        "true",
    ).lower()
    == "true"
)


ACK_TIMEOUT_SECONDS = int(
    os.getenv(
        "ACK_TIMEOUT_SECONDS",
        "15",
    )
)

CANCEL_TIMEOUT_SECONDS = int(
    os.getenv(
        "CANCEL_TIMEOUT_SECONDS",
        "15",
    )
)

PROCESSING_RECOVERY_MINUTES = int(
    os.getenv(
        "PROCESSING_RECOVERY_MINUTES",
        "5",
    )
)

RECONCILE_INTERVAL_SECONDS = int(
    os.getenv(
        "RECONCILE_INTERVAL_SECONDS",
        "15",
    )
)

STARTUP_RETRY_SECONDS = int(
    os.getenv(
        "STARTUP_RETRY_SECONDS",
        "5",
    )
)


def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10,
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():
    init_trade_state(
        DB_FILE
    )

    conn = db_connect()

    try:
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(signals)"
            )
        }

        if "broker_account" not in columns:
            conn.execute(
                "ALTER TABLE signals "
                "ADD COLUMN broker_account TEXT"
            )

        if "broker_port" not in columns:
            conn.execute(
                "ALTER TABLE signals "
                "ADD COLUMN broker_port INTEGER"
            )

        conn.commit()

    finally:
        conn.close()


def transition(
    signal_id,
    status,
    event_type,
    message=None,
    payload=None,
    fields=None,
    event_key=None,
    force=False,
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
        force=force,
    )




def get_next_signal():
    return claim_signal(
        db_file=DB_FILE,
        from_status="QUEUED",
        to_status="PROCESSING",
        event_type="WORKER_CLAIMED",
        source="worker",
        order_by="created_at",
        increment_attempts=True,
        broker_account=IB_ACCOUNT,
        broker_port=IB_PORT
    )






def get_next_cancel_request():
    return claim_signal(
        db_file=DB_FILE,
        from_status="CANCEL_REQUESTED",
        to_status="CANCELLING",
        event_type="CANCEL_WORKER_CLAIMED",
        source="worker",
        order_by="updated_at",
        increment_attempts=False,
        broker_account=IB_ACCOUNT,
        broker_port=IB_PORT
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

                AND broker_account = ?
                AND broker_port = ?

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
            """,
            (
                IB_ACCOUNT,
                IB_PORT,
            ),
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        conn.close()


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
            ),
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
                ),
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
                ),
            )


def enforce_execution_freshness(
    signal,
    phase,
):
    try:
        result = check_execution_freshness(
            signal_time=
                signal.get(
                    "signal_time"
                ),

            max_age_seconds=
                MAX_SIGNAL_AGE_SECONDS,

            max_future_skew_seconds=
                MAX_SIGNAL_FUTURE_SKEW_SECONDS,
        )


        record_event(
            db_file=
                DB_FILE,

            signal_id=
                signal[
                    "signal_id"
                ],

            event_type=
                "EXECUTION_FRESHNESS_APPROVED",

            source=
                "worker",

            message=(
                f"Signal freshness passed "
                f"at {phase}"
            ),

            payload={
                **result,
                "phase":
                    phase,
            },

            event_key=(
                f"freshness:"
                f"{signal['signal_id']}:"
                f"{phase}"
            ),
        )


        return result


    except SignalFreshnessError as exc:

        transition(
            signal[
                "signal_id"
            ],

            "BLOCKED",

            "SIGNAL_STALE_AT_EXECUTION",

            message=
                str(
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
                    MAX_SIGNAL_AGE_SECONDS,
            },
        )

        raise


def sanitize_ref_component(
    value,
):
    value = str(
        value
    ).strip()

    return re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        value,
    )[:80]


def build_order_refs(
    signal_id,
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
            f"TM:{key}:SL",
    }


def parse_order_ref(
    value,
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
        len(
            parts
        )
        != 3
        or
        parts[
            0
        ]
        != "TM"
        or
        parts[
            2
        ]
        not in {
            "ENTRY",
            "TP",
            "SL",
        }
    ):
        return None


    return {
        "signal_key":
            parts[
                1
            ],

        "role":
            parts[
                2
            ],
    }


def signal_key(
    signal_id,
):
    return sanitize_ref_component(
        signal_id
    )


def stock_contract(
    symbol,
):
    contract = Contract()

    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    return contract



def safe_con_id(value):
    try:
        result = int(value)
        return result if result > 0 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


class IBApp(
    EWrapper,
    EClient,
):

    def __init__(
        self,
    ):
        EClient.__init__(
            self,
            self,
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
        self.positions_by_contract = {}
        self.position_identity_errors = []

        self.open_orders = []

        self.completed_orders = []

        self.contract_details = []

        self.market_rule_data = {}
        self.market_rule_events = {}

        self.daily_pnl = None

        self.expected_order_ids = set()

        self.seen_order_ids = set()

        self.order_statuses = {}

        # Latest broker-reported fill state per order.
        self.order_fill_state = {}

        # Order snapshot freshness boundaries.
        self.order_snapshot_started_at = None
        self.order_snapshot_completed_at = None

        # IBKR execution/commission correlation.
        # commissionReport identifies fills by execId, while our
        # database owns signals by IB order id.
        self.execution_order_ids = {}
        self.execution_details = {}
        self.commission_reports = {}

        self.parent_order_id = None

        self.parent_status = None

        self.fatal_order_error = (
            threading.Event()
        )

        self.reject_messages = []

        self.cancel_target_ids = set()

        self.cancelled_ids = set()

        self.historical_bars = {}
        self.historical_events = {}


    def nextValidId(
        self,
        orderId,
    ):
        self.next_order_id = (
            orderId
        )

        print(
            f"IBKR CONNECTED | "
            f"clientId={IB_CLIENT_ID} | "
            f"nextOrderId={orderId}",
            flush=True,
        )

        self.ready.set()


    def managedAccounts(
        self,
        accountsList,
    ):
        self.accounts = [
            account.strip()

            for account
            in accountsList.split(
                ","
            )

            if account.strip()
        ]


    def position(
        self,
        account,
        contract,
        position,
        avgCost,
    ):
        if account != IB_ACCOUNT:
            return


        quantity = float(
            position
        )


        if quantity == 0:
            return


        con_id = safe_con_id(
            getattr(contract, "conId", 0)
        )

        if con_id > 0:
            self.positions_by_contract[
                (account, con_id)
            ] = {
                "account": account,
                "con_id": con_id,
                "symbol": contract.symbol.upper(),
                "quantity": quantity,
                "avg_cost": float(avgCost),
            }
        else:
            self.position_identity_errors.append(
                {
                    "account": account,
                    "symbol": contract.symbol.upper(),
                    "reason": "MISSING_CON_ID",
                }
            )

        self.positions[
            contract.symbol.upper()
        ] = {
            "quantity":
                quantity,

            "avg_cost":
                float(
                    avgCost
                ),
        }


    def positionEnd(
        self,
    ):
        self.positions_done.set()


    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState,
    ):
        if (
            order.account
            != IB_ACCOUNT
        ):
            return


        item = {
            "account": order.account,
            "con_id": safe_con_id(
                getattr(contract, "conId", 0)
            ),
            "order_id":
                orderId,

            "perm_id":
                getattr(
                    order,
                    "permId",
                    0,
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

            "total_quantity":
                float(
                    getattr(
                        order,
                        "totalQuantity",
                        0,
                    )
                    or 0
                ),

            "order_ref":
                getattr(
                    order,
                    "orderRef",
                    "",
                )
                or "",
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


    def historicalData(self, reqId, bar):
        try:
            timestamp = int(str(bar.date))
        except (TypeError, ValueError):
            timestamp = None
        self.historical_bars.setdefault(reqId, []).append({
            "timestamp": timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume or 0),
        })

    def historicalDataEnd(self, reqId, start, end):
        event = self.historical_events.get(reqId)
        if event is not None:
            event.set()


    def openOrderEnd(
        self,
    ):
        self.open_orders_done.set()


    def completedOrder(
        self,
        contract,
        order,
        orderState,
    ):
        if (
            order.account
            != IB_ACCOUNT
        ):
            return


        self.completed_orders.append({
            "account": order.account,
            "con_id": safe_con_id(
                getattr(contract, "conId", 0)
            ),
            "order_id":
                getattr(
                    order,
                    "orderId",
                    None,
                ),

            "perm_id":
                getattr(
                    order,
                    "permId",
                    0,
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

            "total_quantity":
                float(
                    getattr(
                        order,
                        "totalQuantity",
                        0,
                    )
                    or 0
                ),

            "order_ref":
                getattr(
                    order,
                    "orderRef",
                    "",
                )
                or "",
        })


    def completedOrdersEnd(
        self,
    ):
        self.completed_orders_done.set()


    def contractDetails(
        self,
        reqId,
        contractDetails,
    ):
        self.contract_details.append(
            contractDetails
        )


    def contractDetailsEnd(
        self,
        reqId,
    ):
        self.contract_details_done.set()


    def marketRule(
        self,
        marketRuleId,
        priceIncrements,
    ):
        self.market_rule_data[
            int(marketRuleId)
        ] = [
            {
                "low_edge":
                    float(item.lowEdge),

                "increment":
                    float(item.increment),
            }

            for item
            in priceIncrements
        ]

        event = self.market_rule_events.get(
            int(marketRuleId)
        )

        if event is not None:
            event.set()


    def pnl(
        self,
        reqId,
        dailyPnL,
        unrealizedPnL,
        realizedPnL,
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
                )
                > 1e100
            ):
                self.daily_pnl = None

            else:
                self.daily_pnl = (
                    value
                )

        except Exception:
            self.daily_pnl = None


        self.pnl_done.set()


    def execDetails(
        self,
        reqId,
        contract,
        execution,
    ):
        exec_id = getattr(
            execution,
            "execId",
            "",
        )

        order_id = getattr(
            execution,
            "orderId",
            None,
        )

        if not exec_id:
            return

        self.execution_order_ids[
            exec_id
        ] = order_id

        self.execution_details[
            exec_id
        ] = {
            "req_id":
                reqId,

            "order_id":
                order_id,

            "symbol":
                getattr(
                    contract,
                    "symbol",
                    "",
                ),

            "shares":
                float(
                    getattr(
                        execution,
                        "shares",
                        0,
                    )
                    or 0
                ),

            "price":
                float(
                    getattr(
                        execution,
                        "price",
                        0,
                    )
                    or 0
                ),
        }

        print(
            "EXECUTION | "
            f"execId={exec_id} | "
            f"orderId={order_id} | "
            f"symbol={getattr(contract, 'symbol', '')} | "
            f"shares={getattr(execution, 'shares', None)} | "
            f"price={getattr(execution, 'price', None)}",
            flush=True,
        )


    def commissionReport(
        self,
        commissionReport,
    ):
        exec_id = getattr(
            commissionReport,
            "execId",
            "",
        )

        try:
            commission = float(
                getattr(
                    commissionReport,
                    "commission",
                    0,
                )
                or 0
            )

        except Exception:
            commission = 0.0

        order_id = (
            self.execution_order_ids
            .get(
                exec_id
            )
        )

        self.commission_reports[
            exec_id
        ] = {
            "order_id":
                order_id,

            "commission":
                commission,

            "currency":
                getattr(
                    commissionReport,
                    "currency",
                    None,
                ),

            "realized_pnl":
                getattr(
                    commissionReport,
                    "realizedPNL",
                    None,
                ),
        }

        print(
            "COMMISSION | "
            f"execId={exec_id} | "
            f"orderId={order_id} | "
            f"commission={commission} | "
            f"currency="
            f"{getattr(commissionReport, 'currency', None)}",
            flush=True,
        )

        if (
            order_id is not None
            and
            commission >= 0
        ):
            persist_commission(
                order_id,
                commission,
            )


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
        mktCapPrice,
    ):
        print(
            f"ORDER STATUS | "
            f"id={orderId} | "
            f"status={status} | "
            f"filled={filled} | "
            f"remaining={remaining} | "
            f"permId={permId}",
            flush=True,
        )


        self.order_fill_state[
            orderId
        ] = {
            "order_id":
                orderId,

            "received_at":
                time.monotonic(),

            "status":
                status,

            "filled":
                float(
                    filled
                    or 0
                ),

            "remaining":
                float(
                    remaining
                    or 0
                ),

            "avg_fill_price":
                float(
                    avgFillPrice
                    or 0
                ),

            "perm_id":
                permId,

            "parent_id":
                parentId,
        }


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
                "Inactive",
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
                "ApiCancelled",
            }
        ):
            self.cancelled_ids.add(
                orderId
            )


    @staticmethod
    def is_nonfatal_warning(
        errorCode,
        errorString,
    ):
        message = (
            errorString
            or ""
        ).lower()


        if errorCode in {
            2104,
            2106,
            2158,
            2109,
        }:
            return True


        if errorCode == 399:

            phrases = (
                "warning:",
                "will not be placed at the exchange until",
                "will be held until",
            )

            return any(
                phrase in message

                for phrase
                in phrases
            )


        return False


    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson="",
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
            errorString,
        ):
            print(
                f"IB WARNING | "
                f"id={reqId} | "
                f"{message}",
                flush=True,
            )

            return


        print(
            f"IB ERROR | "
            f"id={reqId} | "
            f"{message}",
            flush=True,
        )


        if (
            reqId
            in self.expected_order_ids
        ):
            self.reject_messages.append(
                message
            )

            self.fatal_order_error.set()


def find_signal_by_order_id(
    order_id,
):
    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT *
            FROM signals
            WHERE
                entry_order_id = ?
                OR target_order_id = ?
                OR stop_order_id = ?
                OR parent_order_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                order_id,
                order_id,
                order_id,
                order_id,
            ),
        ).fetchone()

        return (
            dict(row)
            if row is not None
            else None
        )

    finally:
        conn.close()


def classify_order_leg(
    signal,
    order_id,
):
    if not signal:
        return None

    if order_id in {
        signal.get("entry_order_id"),
        signal.get("parent_order_id"),
    }:
        return "ENTRY"

    if order_id == signal.get("target_order_id"):
        return "EXIT"

    if order_id == signal.get("stop_order_id"):
        return "EXIT"

    return None


def persist_commission(
    order_id,
    commission,
):
    signal = find_signal_by_order_id(
        order_id
    )

    if not signal:
        return

    leg = classify_order_leg(
        signal,
        order_id,
    )

    if leg not in {
        "ENTRY",
        "EXIT",
    }:
        return

    signal_id = signal[
        "signal_id"
    ]

    conn = db_connect()

    try:
        if leg == "ENTRY":
            conn.execute(
                """
                UPDATE signals
                SET
                    entry_commission =
                        COALESCE(
                            entry_commission,
                            0
                        )
                        + ?,

                    total_commission =
                        COALESCE(
                            total_commission,
                            0
                        )
                        + ?,

                    updated_at = ?
                WHERE signal_id = ?
                """,
                (
                    commission,
                    commission,
                    datetime.now(
                        timezone.utc
                    ).isoformat(),
                    signal_id,
                ),
            )

        else:
            conn.execute(
                """
                UPDATE signals
                SET
                    exit_commission =
                        COALESCE(
                            exit_commission,
                            0
                        )
                        + ?,

                    total_commission =
                        COALESCE(
                            total_commission,
                            0
                        )
                        + ?,

                    updated_at = ?
                WHERE signal_id = ?
                """,
                (
                    commission,
                    commission,
                    datetime.now(
                        timezone.utc
                    ).isoformat(),
                    signal_id,
                ),
            )

        conn.commit()

    finally:
        conn.close()

    record_event(
        db_file=
            DB_FILE,

        signal_id=
            signal_id,

        event_type=
            "IBKR_COMMISSION",

        source=
            "worker",

        message=(
            f"{leg} commission "
            f"${commission:.6f}"
        ),

        payload={
            "order_id":
                order_id,

            "leg":
                leg,

            "commission":
                commission,
        },
    )


def connect_ibkr():
    ib = IBApp()


    ib.connect(
        IB_HOST,
        IB_PORT,
        clientId=
            IB_CLIENT_ID,
    )


    thread = threading.Thread(
        target=
            ib.run,

        daemon=
            True,
    )

    thread.start()


    if not ib.ready.wait(
        timeout=5,
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


def load_position_state(
    ib,
):
    ib.positions = {}
    ib.positions_by_contract = {}
    ib.position_identity_errors = []

    ib.positions_done.clear()

    ib.reqPositions()

    if not ib.positions_done.wait(
        timeout=5,
    ):
        raise RuntimeError(
            "Position request timeout"
        )

    return {
        symbol: dict(position)
        for symbol, position
        in ib.positions.items()
    }


def load_order_state(
    ib,
):
    ib.open_orders = []

    ib.completed_orders = []

    # Never reuse fill/remaining data from an older snapshot.
    ib.order_fill_state = {}

    ib.order_snapshot_started_at = (
        time.monotonic()
    )

    ib.order_snapshot_completed_at = None

    ib.open_orders_done.clear()

    ib.completed_orders_done.clear()


    ib.reqOpenOrders()


    if not ib.open_orders_done.wait(
        timeout=5,
    ):
        raise RuntimeError(
            "Open-order request timeout"
        )


    ib.reqCompletedOrders(
        True
    )


    if not ib.completed_orders_done.wait(
        timeout=5,
    ):
        raise RuntimeError(
            "Completed-order request timeout"
        )

    ib.order_snapshot_completed_at = (
        time.monotonic()
    )


def valid_perm_id(
    value,
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
    value,
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
    order_ref,
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
    perm_id,
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
    order_id,
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
    ib,
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
    signal,
):
    orders = all_broker_orders(
        ib
    )


    result = find_by_ref(
        orders,
        signal.get(
            "entry_order_ref"
        ),
    )


    if result:
        return result


    expected_ref = build_order_refs(
        signal[
            "signal_id"
        ]
    )[
        "entry"
    ]


    result = find_by_ref(
        orders,
        expected_ref,
    )


    if result:
        return result


    result = find_by_perm(
        orders,
        signal.get(
            "parent_perm_id"
        ),
    )


    if result:
        return result


    return find_by_order_id(
        orders,
        signal.get(
            "parent_order_id"
        ),
    )


def find_child_order(
    ib,
    signal,
    role,
    parent_order,
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

        expected_ref = build_order_refs(
            signal[
                "signal_id"
            ]
        )[
            "target"
        ]

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

        expected_ref = build_order_refs(
            signal[
                "signal_id"
            ]
        )[
            "stop"
        ]


    result = find_by_ref(
        orders,
        stored_ref,
    )


    if result:
        return result


    result = find_by_ref(
        orders,
        expected_ref,
    )


    if result:
        return result


    result = find_by_perm(
        orders,
        stored_perm,
    )


    if result:
        return result


    result = find_by_order_id(
        orders,
        stored_id,
    )


    if result:
        return result


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

                if (
                    valid_order_id(
                        order.get(
                            "parent_id"
                        )
                    )
                    == parent_id
                )
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
                            "STP LMT",
                        }
                    ):
                        return order


    return None


def repair_order_identity(
    signal,
    entry,
    target,
    stop,
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
                "",
            )
        ),

        str(
            fields.get(
                "parent_perm_id",
                "",
            )
        ),

        str(
            fields.get(
                "target_order_id",
                "",
            )
        ),

        str(
            fields.get(
                "stop_order_id",
                "",
            )
        ),
    ]


    update_signal_metadata(
        db_file=
            DB_FILE,

        signal_id=
            signal[
                "signal_id"
            ],

        source=
            "worker",

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
                stop,
        },

        event_key=(
            "worker-identity:"
            +
            signal[
                "signal_id"
            ]
            +
            ":"
            +
            ":".join(
                identity
            )
        ),
    )


    return True


def classify_completed(
    order,
):
    status = (
        order.get(
            "status",
            "",
        )
        or ""
    ).strip()


    if status in {
        "Cancelled",
        "ApiCancelled",
    }:
        return (
            "CANCELLED",
            "CANCEL_CONFIRMED",
        )


    if status == "Filled":
        return (
            "FILLED",
            "ORDER_FILLED",
        )


    if status == "Inactive":
        return (
            "ERROR",
            "ORDER_INACTIVE",
        )


    if status == "PendingCancel":
        return (
            "CANCEL_PENDING",
            "CANCEL_PENDING",
        )


    return (
        "UNKNOWN",
        "ORDER_COMPLETED_UNKNOWN",
    )


def reconcile_signal(
    ib,
    signal,
):
    signal_id = signal[
        "signal_id"
    ]


    entry = find_entry_order(
        ib,
        signal,
    )


    target = find_child_order(
        ib,
        signal,
        "TP",
        entry,
    )


    stop = find_child_order(
        ib,
        signal,
        "SL",
        entry,
    )


    if entry:

        repair_order_identity(
            signal,
            entry,
            target,
            stop,
        )


    if not entry:

        # A filled entry can disappear from the broker's current
        # order history while the resulting position is still a
        # valid managed position. Do not downgrade OPEN_POSITION
        # merely because the historical parent order is absent.
        if (
            signal.get("status") == "OPEN_POSITION"
            and
            float(signal.get("filled_quantity") or 0) > 0
        ):
            record_event(
                db_file=DB_FILE,
                signal_id=signal_id,
                event_type="RECONCILE_OPEN_POSITION_ENTRY_ABSENT",
                source="worker",
                message=(
                    "OPEN_POSITION retained because entry was "
                    "previously filled; historical entry order "
                    "is not present in current broker snapshot"
                ),
                payload={
                    "filled_quantity":
                        signal.get("filled_quantity"),
                    "entry_fill_price":
                        signal.get("entry_fill_price"),
                    "parent_order_id":
                        signal.get("parent_order_id"),
                    "parent_perm_id":
                        signal.get("parent_perm_id"),
                },
                event_key=(
                    f"open-position-entry-absent:{signal_id}"
                ),
            )

            return

        if (
            signal.get(
                "status"
            )
            == "PROCESSING"
            and
            signal.get(
                "parent_order_id"
            )
            is None
        ):
            print(
                "RECONCILE SKIP | "
                f"{signal_id} | "
                "PROCESSING before order allocation",
                flush=True,
            )

            record_event(
                db_file=
                    DB_FILE,

                signal_id=
                    signal_id,

                event_type=
                    "RECONCILE_PRE_SUBMIT_SKIP",

                source=
                    "worker",

                message=(
                    "Skipped reconciliation while "
                    "signal is PROCESSING before "
                    "broker order allocation"
                ),

                payload={
                    "status":
                        signal.get(
                            "status"
                        ),

                    "parent_order_id":
                        signal.get(
                            "parent_order_id"
                        ),
                },

                event_key=(
                    f"reconcile-pre-submit:"
                    f"{signal_id}"
                ),
            )

            return

        if (
            signal[
                "status"
            ]
            in {
                "CANCEL_REQUESTED",
                "CANCELLING",
                "CANCEL_PENDING",
                "CANCEL_UNKNOWN",
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
            ),
        )

        return


    broker_status = (
        entry.get(
            "status",
            "",
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
                ),
            )

            return


        if broker_status in {
            "PendingSubmit",
            "ApiPending",
            "PreSubmitted",
        }:

            if (
                signal[
                    "status"
                ]
                in {
                    "CANCEL_REQUESTED",
                    "CANCELLING",
                    "CANCEL_PENDING",
                    "CANCEL_UNKNOWN",
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
                ),
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
                    "CANCEL_UNKNOWN",
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
                ),
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
            ),
        )

        return


    # A completed parent order with status Filled represents
    # the entry fill. If the trade is already OPEN_POSITION,
    # FILLED is not a valid lifecycle regression.
    if (
        broker_status == "Filled"
        and
        signal.get("status") == "OPEN_POSITION"
    ):
        record_event(
            db_file=DB_FILE,
            signal_id=signal_id,
            event_type="RECONCILE_ENTRY_ALREADY_FILLED",
            source="worker",
            message=(
                "Entry order is Filled and signal is already "
                "OPEN_POSITION; preserving OPEN_POSITION"
            ),
            payload=entry,
            event_key=(
                f"entry-already-filled:"
                f"{signal_id}:"
                f"{entry.get('perm_id')}"
            ),
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
        ),
    )


def reconcile_orders():
    ib = None
    had_errors = False


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
            flush=True,
        )


        for signal in candidates:

            try:
                reconcile_signal(
                    ib,
                    signal,
                )

            except Exception as exc:
                had_errors = True

                print(
                    f"RECONCILE SIGNAL ERROR | "
                    f"{signal['signal_id']} | "
                    f"{exc}",
                    flush=True,
                )


                record_event(
                    db_file=
                        DB_FILE,

                    signal_id=
                        signal[
                            "signal_id"
                        ],

                    event_type=
                        "RECONCILE_EXCEPTION",

                    source=
                        "worker",

                    message=
                        str(
                            exc
                        ),
                )


        return not had_errors


    except Exception as exc:

        print(
            f"RECONCILE ERROR | "
            f"{exc}",
            flush=True,
        )


        record_event(
            db_file=
                DB_FILE,

            signal_id=
                None,

            event_type=
                "BROKER_RECONCILE_FAILED",

            source=
                "worker",

            message=
                str(
                    exc
                ),
        )


        return False


    finally:

        if ib is not None:

            try:
                ib.disconnect()

            except Exception:
                pass


def open_order_ids_for_signal(
    ib,
    signal,
    entry,
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


    for field in (
        "entry_order_id",
        "parent_order_id",
        "target_order_id",
        "stop_order_id",
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


    stored_refs = {
        value

        for value
        in (
            signal.get(
                "entry_order_ref"
            ),

            signal.get(
                "target_order_ref"
            ),

            signal.get(
                "stop_order_ref"
            ),
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
    signal,
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
            signal,
        )

        target = find_child_order(
            ib,
            signal,
            "TP",
            entry,
        )

        stop = find_child_order(
            ib,
            signal,
            "SL",
            entry,
        )


        if entry:

            repair_order_identity(
                signal,
                entry,
                target,
                stop,
            )


        if not entry:

            transition(
                signal_id,
                "CANCEL_UNKNOWN",
                "CANCEL_ORDER_NOT_FOUND",

                message=(
                    "Cancellation requested but "
                    "entry order was not found"
                ),
            )

            return


        broker_status = (
            entry.get(
                "status",
                "",
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
                    entry,
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
                ),
            )

            return


        cancel_ids = (
            open_order_ids_for_signal(
                ib,
                signal,
                entry,
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
                ),
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
            db_file=
                DB_FILE,

            signal_id=
                signal_id,

            event_type=
                "CANCEL_SENT",

            source=
                "worker",

            message=(
                "Cancellation sent to "
                "identified bracket orders"
            ),

            payload={
                "order_ids":
                    cancel_sequence,
            },

            event_key=(
                f"cancel-sent:"
                f"{signal_id}:"
                +
                ",".join(
                    str(
                        item
                    )

                    for item
                    in cancel_sequence
                )
            ),
        )


        for order_id in cancel_sequence:

            ib.cancelOrder(
                order_id,
                OrderCancel(),
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
                        ),
                },

                event_key=(
                    f"cancel-confirmed:"
                    f"{signal_id}:"
                    +
                    ",".join(
                        str(
                            item
                        )

                        for item
                        in sorted(
                            ib.cancelled_ids
                        )
                    )
                ),
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
                    cancel_sequence,
            },

            event_key=(
                f"cancel-pending-after-send:"
                f"{signal_id}:"
                +
                ",".join(
                    str(
                        item
                    )

                    for item
                    in cancel_sequence
                )
            ),
        )


    except Exception as exc:

        transition(
            signal_id,
            "CANCEL_UNKNOWN",
            "CANCEL_EXCEPTION",

            message=
                str(
                    exc
                ),
        )


    finally:

        if ib is not None:

            try:
                ib.disconnect()

            except Exception:
                pass


def resolve_market_rule_id(
    details,
):
    exchanges = [
        item.strip().upper()

        for item
        in str(
            getattr(
                details,
                "validExchanges",
                "",
            )
            or ""
        ).split(",")
    ]

    rule_ids = [
        item.strip()

        for item
        in str(
            getattr(
                details,
                "marketRuleIds",
                "",
            )
            or ""
        ).split(",")
    ]

    if (
        len(exchanges)
        ==
        len(rule_ids)
    ):
        for exchange, rule_id in zip(
            exchanges,
            rule_ids,
        ):
            if (
                exchange == "SMART"
                and
                rule_id.isdigit()
                and
                int(rule_id) > 0
            ):
                return int(
                    rule_id
                )

    for rule_id in rule_ids:
        if (
            rule_id.isdigit()
            and
            int(rule_id) > 0
        ):
            return int(
                rule_id
            )

    return None


def market_increment_for_price(
    price,
    increments,
    fallback,
):
    price = float(
        price
    )

    selected = None

    for item in sorted(
        increments or [],
        key=lambda x: x[
            "low_edge"
        ],
    ):
        if (
            price
            >=
            float(
                item[
                    "low_edge"
                ]
            )
        ):
            selected = float(
                item[
                    "increment"
                ]
            )

    if (
        selected is None
        or
        selected <= 0
    ):
        selected = float(
            fallback
        )

    return selected


def check_market_session(
    ib,
    symbol,
):
    ib.contract_details = []

    ib.contract_details_done.clear()


    ib.reqContractDetails(
        9201,
        stock_contract(
            symbol
        ),
    )


    if not ib.contract_details_done.wait(
        timeout=5,
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


    for details in ib.contract_details:

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


    unique = {}


    for details in candidates:

        con_id = getattr(
            details.contract,
            "conId",
            None,
        )

        key = (
            con_id

            if con_id

            else (
                getattr(
                    details.contract,
                    "symbol",
                    "",
                ),

                getattr(
                    details.contract,
                    "primaryExchange",
                    "",
                ),

                getattr(
                    details.contract,
                    "currency",
                    "",
                ),
            )
        )

        unique[
            key
        ] = details


    if (
        len(
            unique
        )
        != 1
    ):
        exchanges = sorted({
            str(
                getattr(
                    item.contract,
                    "primaryExchange",
                    "",
                )
            )

            for item
            in unique.values()
        })


        raise MarketSessionError(
            (
                "Ambiguous IBKR stock contract "
                f"for {symbol}; "
                f"candidates={len(unique)}; "
                f"primaryExchanges={exchanges}"
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
                None,
            ),

        timezone_name=
            getattr(
                selected,
                "timeZoneId",
                None,
            ),
    )


    result[
        "symbol"
    ] = symbol

    result[
        "con_id"
    ] = getattr(
        selected.contract,
        "conId",
        None,
    )

    result[
        "primary_exchange"
    ] = getattr(
        selected.contract,
        "primaryExchange",
        None,
    )


    result[
        "min_tick"
    ] = float(
        getattr(
            selected,
            "minTick",
            0.01,
        )
        or 0.01
    )


    market_rule_id = (
        resolve_market_rule_id(
            selected
        )
    )

    market_rule = []

    if market_rule_id is not None:

        event = threading.Event()

        ib.market_rule_events[
            market_rule_id
        ] = event

        ib.reqMarketRule(
            market_rule_id
        )

        if event.wait(
            timeout=5
        ):
            market_rule = (
                ib.market_rule_data.get(
                    market_rule_id,
                    [],
                )
            )

    result[
        "market_rule_id"
    ] = market_rule_id

    result[
        "market_rule"
    ] = market_rule


    return result


def round_price_to_tick(
    price,
    min_tick,
):
    price_dec = Decimal(
        str(price)
    )

    tick_dec = Decimal(
        str(min_tick)
    )

    if tick_dec <= 0:
        raise ValueError(
            f"Invalid min_tick={min_tick}"
        )

    ticks = (
        price_dec
        /
        tick_dec
    ).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )

    return float(
        ticks
        *
        tick_dec
    )


def normalize_price_to_market_rule(
    price,
    market_rule,
    fallback_tick,
    rounding,
):
    price = float(price)

    increment = market_increment_for_price(
        price,
        market_rule,
        fallback_tick,
    )

    for _ in range(3):

        price_dec = Decimal(
            str(price)
        )

        increment_dec = Decimal(
            str(increment)
        )

        ticks = (
            price_dec
            /
            increment_dec
        ).quantize(
            Decimal("1"),
            rounding=rounding,
        )

        normalized = float(
            ticks
            *
            increment_dec
        )

        new_increment = (
            market_increment_for_price(
                normalized,
                market_rule,
                fallback_tick,
            )
        )

        if new_increment == increment:
            return (
                normalized,
                increment,
            )

        price = normalized
        increment = new_increment

    return (
        normalized,
        increment,
    )


def build_stop_limit_price(
    action,
    stop_trigger,
    market_rule,
    fallback_tick,
    offset_pct=0.0075,
    min_ticks=2,
):
    action = normalize_action(
        action
    )

    stop_trigger = float(
        stop_trigger
    )

    market_rule = (
        market_rule
        or []
    )

    trigger_tick = (
        market_increment_for_price(
            stop_trigger,
            market_rule,
            fallback_tick,
        )
    )

    offset = max(
        stop_trigger
        *
        float(offset_pct),

        trigger_tick
        *
        int(min_ticks),
    )

    if action == "BUY":
        # LONG position -> SELL stop-limit.
        # Limit must be below trigger.
        raw_limit = (
            stop_trigger
            -
            offset
        )

        rounding = (
            ROUND_FLOOR
        )

    else:
        # SHORT position -> BUY stop-limit.
        # Limit must be above trigger.
        raw_limit = (
            stop_trigger
            +
            offset
        )

        rounding = (
            ROUND_CEILING
        )

    if raw_limit <= 0:
        raise ValueError(
            f"Invalid stop-limit price "
            f"{raw_limit}"
        )

    stop_limit, stop_limit_tick = (
        normalize_price_to_market_rule(
            raw_limit,
            market_rule,
            fallback_tick,
            rounding,
        )
    )

    if action == "BUY":
        if stop_limit >= stop_trigger:
            stop_limit = (
                stop_trigger
                -
                trigger_tick
            )

            stop_limit, stop_limit_tick = (
                normalize_price_to_market_rule(
                    stop_limit,
                    market_rule,
                    fallback_tick,
                    ROUND_FLOOR,
                )
            )

    else:
        if stop_limit <= stop_trigger:
            stop_limit = (
                stop_trigger
                +
                trigger_tick
            )

            stop_limit, stop_limit_tick = (
                normalize_price_to_market_rule(
                    stop_limit,
                    market_rule,
                    fallback_tick,
                    ROUND_CEILING,
                )
            )

    return (
        stop_limit,
        stop_limit_tick,
    )


def normalize_bracket_prices(
    action,
    entry,
    target,
    stop,
    min_tick,
    market_rule=None,
):
    action = normalize_action(
        action
    )

    market_rule = (
        market_rule
        or []
    )

    fallback_tick = float(
        min_tick
    )

    if action == "BUY":

        entry, entry_tick = (
            normalize_price_to_market_rule(
                entry,
                market_rule,
                fallback_tick,
                ROUND_FLOOR,
            )
        )

        target, target_tick = (
            normalize_price_to_market_rule(
                target,
                market_rule,
                fallback_tick,
                ROUND_CEILING,
            )
        )

        stop, stop_tick = (
            normalize_price_to_market_rule(
                stop,
                market_rule,
                fallback_tick,
                ROUND_CEILING,
            )
        )

        if stop >= entry:
            stop = (
                entry
                -
                market_increment_for_price(
                    entry,
                    market_rule,
                    fallback_tick,
                )
            )

            stop, stop_tick = (
                normalize_price_to_market_rule(
                    stop,
                    market_rule,
                    fallback_tick,
                    ROUND_FLOOR,
                )
            )

        if target <= entry:
            target = (
                entry
                +
                market_increment_for_price(
                    entry,
                    market_rule,
                    fallback_tick,
                )
            )

            target, target_tick = (
                normalize_price_to_market_rule(
                    target,
                    market_rule,
                    fallback_tick,
                    ROUND_CEILING,
                )
            )

    else:

        entry, entry_tick = (
            normalize_price_to_market_rule(
                entry,
                market_rule,
                fallback_tick,
                ROUND_CEILING,
            )
        )

        target, target_tick = (
            normalize_price_to_market_rule(
                target,
                market_rule,
                fallback_tick,
                ROUND_FLOOR,
            )
        )

        stop, stop_tick = (
            normalize_price_to_market_rule(
                stop,
                market_rule,
                fallback_tick,
                ROUND_FLOOR,
            )
        )

        if stop <= entry:
            stop = (
                entry
                +
                market_increment_for_price(
                    entry,
                    market_rule,
                    fallback_tick,
                )
            )

            stop, stop_tick = (
                normalize_price_to_market_rule(
                    stop,
                    market_rule,
                    fallback_tick,
                    ROUND_CEILING,
                )
            )

        if target >= entry:
            target = (
                entry
                -
                market_increment_for_price(
                    entry,
                    market_rule,
                    fallback_tick,
                )
            )

            target, target_tick = (
                normalize_price_to_market_rule(
                    target,
                    market_rule,
                    fallback_tick,
                    ROUND_FLOOR,
                )
            )

    validate_price_structure(
        action=action,
        entry=entry,
        stop=stop,
        target=target,
    )

    return {
        "entry":
            entry,

        "target":
            target,

        "stop":
            stop,

        "entry_tick":
            entry_tick,

        "target_tick":
            target_tick,

        "stop_tick":
            stop_tick,

        "min_tick":
            fallback_tick,
    }


def create_bracket(
    signal_id,
    parent_id,
    quantity,
    action,
    entry,
    target,
    stop,
    stop_limit,
):
    try:
        price_structure = (
            validate_price_structure(
                action=
                    action,

                entry=
                    entry,

                stop=
                    stop,

                target=
                    target,
            )
        )

    except SignalContractError as exc:

        raise ValueError(
            str(
                exc
            )
        ) from exc


    entry_action = (
        price_structure[
            "action"
        ]
    )

    exit_action = (
        "SELL"

        if entry_action
        == "BUY"

        else "BUY"
    )


    refs = build_order_refs(
        signal_id
    )


    parent = Order()

    parent.orderId = (
        parent_id
    )

    parent.account = (
        IB_ACCOUNT
    )

    parent.action = (
        entry_action
    )

    parent.orderType = (
        "LMT"
    )

    parent.totalQuantity = (
        quantity
    )

    parent.lmtPrice = (
        entry
    )

    parent.tif = (
        "DAY"
    )

    parent.outsideRth = (
        ALLOW_OUTSIDE_RTH
    )

    parent.transmit = (
        False
    )

    parent.orderRef = refs[
        "entry"
    ]


    take_profit = Order()

    take_profit.orderId = (
        parent_id
        +
        1
    )

    take_profit.account = (
        IB_ACCOUNT
    )

    take_profit.action = (
        exit_action
    )

    take_profit.orderType = (
        "LMT"
    )

    take_profit.totalQuantity = (
        quantity
    )

    take_profit.lmtPrice = (
        target
    )

    take_profit.parentId = (
        parent_id
    )

    take_profit.tif = (
        "GTC"
    )

    take_profit.outsideRth = (
        ALLOW_OUTSIDE_RTH
    )

    take_profit.transmit = (
        False
    )

    take_profit.orderRef = refs[
        "target"
    ]


    stop_loss = Order()

    stop_loss.orderId = (
        parent_id
        +
        2
    )

    stop_loss.account = (
        IB_ACCOUNT
    )

    stop_loss.action = (
        exit_action
    )

    stop_loss.orderType = (
        "STP LMT"
        if ALLOW_OUTSIDE_RTH
        else "STP"
    )

    stop_loss.totalQuantity = (
        quantity
    )

    stop_loss.auxPrice = (
        stop
    )

    if ALLOW_OUTSIDE_RTH:
        stop_loss.lmtPrice = (
            stop_limit
        )

    stop_loss.parentId = (
        parent_id
    )

    stop_loss.tif = (
        "GTC"
    )

    stop_loss.outsideRth = (
        ALLOW_OUTSIDE_RTH
    )

    stop_loss.transmit = (
        True
    )

    stop_loss.orderRef = refs[
        "stop"
    ]


    return (
        [
            parent,
            take_profit,
            stop_loss,
        ],
        refs,
    )


def get_safe_parent_order_id(
    ib_next_order_id,
):
    if ib_next_order_id is None:
        return None

    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT MAX(order_id)
            FROM (
                SELECT parent_order_id AS order_id
                FROM signals

                UNION ALL

                SELECT entry_order_id
                FROM signals

                UNION ALL

                SELECT target_order_id
                FROM signals

                UNION ALL

                SELECT stop_order_id
                FROM signals
            )
            WHERE order_id IS NOT NULL
            """
        ).fetchone()

    finally:
        conn.close()

    db_max = (
        int(row[0])
        if row
        and row[0] is not None
        else 0
    )

    safe_id = max(
        int(ib_next_order_id),
        db_max + 1,
    )

    print(
        "ORDER ID ALLOCATION | "
        f"ib_next={ib_next_order_id} | "
        f"db_max={db_max} | "
        f"selected={safe_id}",
        flush=True,
    )

    return safe_id



# ============================================================
# IBKR SYMBOL QUARANTINE
# ============================================================

IBKR_SYMBOL_QUARANTINE_SECONDS = 24 * 60 * 60

IBKR_SYMBOL_QUARANTINE_FILE = (
    Path.home()
    / ".cache"
    / "tradingmax"
    / "ibkr_symbol_quarantine.json"
)


def quarantine_ibkr_symbol(symbol, reason):
    symbol = str(symbol or "").strip().upper()

    if not symbol:
        return

    now = time.time()

    data = {}

    try:
        if IBKR_SYMBOL_QUARANTINE_FILE.exists():
            loaded = json.loads(
                IBKR_SYMBOL_QUARANTINE_FILE.read_text(
                    encoding="utf-8"
                )
            )

            if isinstance(loaded, dict):
                data = loaded

    except Exception:
        data = {}

    data[symbol] = {
        "symbol": symbol,
        "created_at": now,
        "expires_at": now + IBKR_SYMBOL_QUARANTINE_SECONDS,
        "reason": str(reason or "")[:1000],
    }

    IBKR_SYMBOL_QUARANTINE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = IBKR_SYMBOL_QUARANTINE_FILE.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            data,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    tmp.replace(
        IBKR_SYMBOL_QUARANTINE_FILE
    )

    print(
        "IBKR SYMBOL QUARANTINE | "
        f"{symbol} | "
        f"hours=24 | "
        "reason=SMALL_CAP_COMPLIANCE",
        flush=True,
    )

def recover_rejected_bracket(
    ib,
    signal,
    parent_id,
):
    signal_id = signal[
        "signal_id"
    ]

    # Give IBKR callbacks a brief moment to settle before
    # querying authoritative broker state.
    time.sleep(
        0.5
    )

    try:
        load_order_state(
            ib
        )

    except Exception as exc:
        record_event(
            db_file=DB_FILE,
            signal_id=signal_id,
            event_type=
                "BRACKET_RECOVERY_STATE_FAILED",
            source="worker",
            message=str(exc),
            payload={
                "parent_order_id":
                    parent_id,
            },
        )

        transition(
            signal_id,
            "UNKNOWN",
            "BRACKET_RECOVERY_STATE_FAILED",
            message=str(exc),
            fields={
                "parent_order_id":
                    parent_id,
            },
        )

        return {
            "result":
                "UNKNOWN",

            "reason":
                "STATE_LOAD_FAILED",
        }


    try:
        positions = load_position_state(
            ib
        )

    except Exception as exc:
        record_event(
            db_file=DB_FILE,
            signal_id=signal_id,
            event_type=
                "BRACKET_RECOVERY_POSITION_STATE_FAILED",
            source="worker",
            message=str(exc),
            payload={
                "parent_order_id":
                    parent_id,
            },
        )

        transition(
            signal_id,
            "UNKNOWN",
            "BRACKET_RECOVERY_POSITION_STATE_FAILED",
            message=str(exc),
            fields={
                "parent_order_id":
                    parent_id,
            },
        )

        return {
            "result":
                "UNKNOWN",

            "reason":
                "POSITION_STATE_LOAD_FAILED",
        }


    entry = find_entry_order(
        ib,
        signal,
    )

    target = find_child_order(
        ib,
        signal,
        "TP",
        entry,
    )

    stop = find_child_order(
        ib,
        signal,
        "SL",
        entry,
    )


    active_statuses = {
        "PendingSubmit",
        "ApiPending",
        "PreSubmitted",
        "Submitted",
        "PendingCancel",
    }


    def is_active(
        order,
    ):
        if not order:
            return False

        status = (
            order.get(
                "status",
                "",
            )
            or ""
        ).strip()

        return (
            order in ib.open_orders
            and
            status in active_statuses
        )


    entry_status = (
        (
            entry.get(
                "status",
                "",
            )
            or ""
        ).strip()

        if entry

        else ""
    )

    entry_filled = (
        entry is not None
        and
        entry_status == "Filled"
    )

    target_active = (
        is_active(
            target
        )
    )

    stop_active = (
        is_active(
            stop
        )
    )


    symbol = str(
        signal.get(
            "symbol",
            ""
        )
        or ""
    ).strip().upper()

    # Resolve broker exposure by account and contract.
    # Never infer FLAT from a symbol-only lookup.

    recovery_entry_con_id = safe_con_id(
        entry.get("con_id", 0)
        if entry
        else 0
    )

    position_resolution = resolve_recovery_position(
        account=IB_ACCOUNT,
        symbol=symbol,
        entry_con_id=recovery_entry_con_id,
        legacy_positions=positions,
        positions_by_contract=ib.positions_by_contract,
        identity_errors=ib.position_identity_errors,
    )

    # Fail closed. Neither UNKNOWN nor FLAT authorizes
    # automatic cancellation during bracket recovery.
    if position_resolution["status"] != "VERIFIED":
        reason = position_resolution["reason"]

        record_event(
            db_file=DB_FILE,
            signal_id=signal_id,
            event_type="RECOVERY_POSITION_NOT_VERIFIED",
            source="worker",
            message=reason,
            payload={
                "parent_order_id": parent_id,
                "position_resolution": position_resolution,
            },
        )

        transition(
            signal_id,
            "UNKNOWN",
            "RECOVERY_POSITION_NOT_VERIFIED",
            message=reason,
            fields={
                "parent_order_id": parent_id,
            },
        )

        return {
            "result": "UNKNOWN",
            "reason": reason,
            "position_resolution": position_resolution,
        }

    position_quantity = float(
        position_resolution["quantity"]
    )

    exposure_quantity = abs(position_quantity)

    entry_action = normalize_action(
        signal.get(
            "action"
        )
    )

    expected_position_sign = (
        1
        if entry_action == "BUY"
        else -1
    )

    position_direction_matches = (
        exposure_quantity == 0
        or
        (
            position_quantity
            *
            expected_position_sign
        )
        > 0
    )

    stop_quantity = (
        float(
            stop.get(
                "total_quantity",
                0,
            )
            or 0
        )

        if stop

        else 0.0
    )

    stop_covers_exposure = (
        stop_active
        and
        exposure_quantity > 0
        and
        stop_quantity + 1e-9
        >=
        exposure_quantity
    )


    # --------------------------------------------------
    # AUTHORITATIVE PROTECTION VERIFICATION
    # --------------------------------------------------

    entry_con_id = safe_con_id(
        entry.get(
            "con_id",
            0,
        )
        if entry
        else 0
    )

    contract_position = (
        ib.positions_by_contract.get(
            (
                IB_ACCOUNT,
                entry_con_id,
            )
        )
        if entry_con_id > 0
        else None
    )

    stop_order_id = (
        valid_order_id(
            stop.get(
                "order_id"
            )
        )
        if stop
        else None
    )

    stop_fill_state = (
        ib.order_fill_state.get(
            stop_order_id
        )
        if stop_order_id is not None
        else None
    )

    protection = None

    if exposure_quantity > 0 and not stop_active:
        protection = {
            "protected": False,
            "stage": "BROKER_ORDER_STATE",
            "reason": "NO_ACTIVE_STOP",
        }

    elif exposure_quantity > 0:
        protection = evaluate_live_protection(
            position=
                contract_position,

            entry_action=
                entry_action,

            stop_order=
                stop,

            stop_fill_state=
                stop_fill_state,

            snapshot_started_at=
                ib.order_snapshot_started_at,

            snapshot_completed_at=
                ib.order_snapshot_completed_at,
        )


    payload = {
        "parent_order_id":
            parent_id,

        "entry_status":
            entry_status,

        "entry_filled":
            entry_filled,

        "target_active":
            target_active,

        "stop_active":
            stop_active,

        "symbol":
            symbol,

        "position_quantity":
            position_quantity,

        "exposure_quantity":
            exposure_quantity,

        "position_direction_matches":
            position_direction_matches,

        "stop_quantity":
            stop_quantity,

        "stop_covers_exposure":
            stop_covers_exposure,

        "entry_con_id":
            entry_con_id,

        "contract_position":
            contract_position,

        "stop_fill_state":
            stop_fill_state,

        "protection":
            protection,

        "reject_messages":
            list(
                ib.reject_messages
            ),

        "entry":
            entry,

        "target":
            target,

        "stop":
            stop,
    }


    print(
        "BRACKET RECOVERY | "
        f"{signal_id} | "
        f"entry_status={entry_status or 'NOT_FOUND'} | "
        f"entry_filled={entry_filled} | "
        f"position={position_quantity} | "
        f"target_active={target_active} | "
        f"stop_active={stop_active} | "
        f"stop_qty={stop_quantity} | "
        f"stop_covers={stop_covers_exposure}",
        flush=True,
    )


    # --------------------------------------------------
    # CASE 1:
    # Entry was NOT filled.
    #
    # Safe action:
    # cancel any surviving orders belonging to this
    # bracket. There is no position to protect.
    # --------------------------------------------------

    if exposure_quantity <= 0:

        cancel_ids = set()

        stored_ids = {
            valid_order_id(
                signal.get(
                    "parent_order_id"
                )
            ),

            valid_order_id(
                signal.get(
                    "entry_order_id"
                )
            ),

            valid_order_id(
                signal.get(
                    "target_order_id"
                )
            ),

            valid_order_id(
                signal.get(
                    "stop_order_id"
                )
            ),

            valid_order_id(
                parent_id
            ),

            valid_order_id(
                parent_id + 1
            ),

            valid_order_id(
                parent_id + 2
            ),
        }

        stored_ids.discard(
            None
        )


        stored_refs = {
            signal.get(
                "entry_order_ref"
            ),

            signal.get(
                "target_order_ref"
            ),

            signal.get(
                "stop_order_ref"
            ),
        }

        stored_refs.discard(
            None
        )

        stored_refs.discard(
            ""
        )


        for order in ib.open_orders:

            order_id = (
                valid_order_id(
                    order.get(
                        "order_id"
                    )
                )
            )

            order_ref = (
                order.get(
                    "order_ref"
                )
                or ""
            )

            if (
                order_id in stored_ids
                or
                order_ref in stored_refs
            ):
                cancel_ids.add(
                    order_id
                )


        cancel_ids.discard(
            None
        )

        if cancel_ids:

            ib.cancel_target_ids = set(
                cancel_ids
            )

            for order_id in sorted(
                cancel_ids
            ):
                print(
                    "BRACKET RECOVERY CANCEL | "
                    f"{signal_id} | "
                    f"orderId={order_id}",
                    flush=True,
                )

                ib.cancelOrder(
                    order_id,
                    OrderCancel(),
                )

                time.sleep(
                    0.15
                )



        # Cancellation requests are not cancellation
        # confirmations. Never release LIVE_ONE_SHOT
        # until authoritative reconciliation completes.

        record_event(
            db_file=DB_FILE,
            signal_id=signal_id,
            event_type="BRACKET_CANCEL_PENDING_VERIFICATION",
            source="worker",
            message=(
                "Bracket rejected. Cancellation requested "
                "where applicable. Broker confirmation "
                "and fresh position reconciliation required."
            ),
            payload={
                **payload,
                "cancel_ids": sorted(cancel_ids),
            },
        )

        transition(
            signal_id,
            "UNKNOWN",
            "BRACKET_CANCEL_PENDING_VERIFICATION",
            message=(
                "Broker cancellation and position "
                "state are not yet verified"
            ),
            payload={
                **payload,
                "cancel_ids": sorted(cancel_ids),
            },
            fields={
                "parent_order_id": parent_id,
            },
        )

        return {
            "result": "UNKNOWN",
            "reason": "CANCELLATION_UNVERIFIED",
            "cancel_ids": sorted(cancel_ids),
        }


    # --------------------------------------------------
    # CASE 2:
    # Entry filled and STOP is alive.
    #
    # This is what happened to QSI.
    # Do NOT cancel the stop.
    # --------------------------------------------------

    if (
        protection is not None
        and
        protection.get(
            "protected"
        )
    ):

        record_event(
            db_file=DB_FILE,
            signal_id=signal_id,
            event_type=
                "PARTIAL_BRACKET_PROTECTED",
            source="worker",
            message=(
                "Entry filled after bracket rejection; "
                "protective stop remains active"
            ),
            payload=payload,
        )


        transition(
            signal_id,
            "UNKNOWN",
            "PARTIAL_BRACKET_PROTECTED",
            message=(
                "Entry is filled and protective stop "
                "remains active; reconciliation required"
            ),
            payload=payload,
            fields={
                "parent_order_id":
                    parent_id,
            },
        )


        print(
            "BRACKET RECOVERY PROTECTED | "
            f"{signal_id} | "
            "ENTRY FILLED | STOP ACTIVE | "
            "STOP LEFT IN PLACE",
            flush=True,
        )

        return {
            "result":
                "PROTECTED",

            "reason":
                "STOP_ACTIVE",

            "target_active":
                target_active,
        }


    # --------------------------------------------------
    # CASE 3:
    # Live exposure exists but protection is not valid.
    # Classify the exact safety failure.
    # --------------------------------------------------

    if protection is None:
        unsafe_reason = (
            "PROTECTION_NOT_EVALUATED"
        )

        unsafe_message = (
            "CRITICAL: live broker exposure "
            "protection was not evaluated"
        )

    else:
        unsafe_reason = (
            protection.get(
                "reason"
            )
            or
            "PROTECTION_STATE_UNKNOWN"
        )

        unsafe_message = (
            "CRITICAL: live broker exposure "
            "failed authoritative protection "
            f"verification: {unsafe_reason}"
        )


    record_event(
        db_file=DB_FILE,
        signal_id=signal_id,
        event_type=
            "UNPROTECTED_LIVE_POSITION",
        source="worker",
        message=
            unsafe_message,
        payload={
            **payload,

            "unsafe_reason":
                unsafe_reason,
        },
    )


    transition(
        signal_id,
        "UNKNOWN",
        "UNPROTECTED_LIVE_POSITION",
        message=
            unsafe_message,
        payload={
            **payload,

            "unsafe_reason":
                unsafe_reason,
        },
        fields={
            "parent_order_id":
                parent_id,
        },
    )


    print(
        "CRITICAL BRACKET RECOVERY | "
        f"{signal_id} | "
        f"reason={unsafe_reason} | "
        f"position={position_quantity} | "
        f"stop_active={stop_active} | "
        f"stop_qty={stop_quantity}",
        flush=True,
    )


    return {
        "result":
            "UNPROTECTED",

        "reason":
            unsafe_reason,

        "target_active":
            target_active,

        "position_quantity":
            position_quantity,

        "stop_quantity":
            stop_quantity,
    }


def bracket_is_accepted(
    ib,
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


    accepted_statuses = {
        "PendingSubmit",
        "ApiPending",
        "PreSubmitted",
        "Submitted",
        "Filled",
    }


    for order_id in ib.expected_order_ids:

        status = (
            ib.order_statuses.get(
                order_id,
                ""
            )
            or ""
        ).strip()

        if status not in accepted_statuses:
            return False


    if (
        ib.parent_status
        not in {
            "PreSubmitted",
            "Submitted",
            "Filled",
        }
    ):
        return False


    return True


def process_signal(
    signal,
):
    signal_id = signal[
        "signal_id"
    ]

    symbol = signal[
        "symbol"
    ].upper()


    try:
        action = normalize_action(
            signal.get(
                "action"
            )
        )

        direction = trade_direction(
            action
        )

        validate_price_structure(
            action=
                action,

            entry=
                signal.get(
                    "entry"
                ),

            stop=
                signal.get(
                    "stop"
                ),

            target=
                signal.get(
                    "target"
                ),
        )


    except SignalContractError as exc:

        transition(
            signal_id,
            "BLOCKED",
            "SIGNAL_DIRECTION_INVALID",

            message=
                str(
                    exc
                ),
        )

        return


    update_signal_metadata(
        db_file=DB_FILE,
        signal_id=signal_id,
        source="worker",
        event_type="BROKER_OWNERSHIP_ASSIGNED",

        fields={
            "broker_account":
                IB_ACCOUNT,

            "broker_port":
                IB_PORT,
        },

        message=(
            f"Signal assigned to broker "
            f"account={IB_ACCOUNT} "
            f"port={IB_PORT}"
        ),

        event_key=(
            f"broker-ownership:"
            f"{signal_id}:"
            f"{IB_ACCOUNT}:"
            f"{IB_PORT}"
        ),
    )

    signal["broker_account"] = (
        IB_ACCOUNT
    )

    signal["broker_port"] = (
        IB_PORT
    )


    print(
        f"PROCESSING | "
        f"{signal_id} | "
        f"{symbol} | "
        f"{direction} "
        f"({action})",
        flush=True,
    )


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
            ),
        )

        return


    if not LIVE_TRADING:

        transition(
            signal_id,
            "BLOCKED",
            "LIVE_DISABLED",

            message=(
                "LIVE_TRADING=false"
            ),
        )

        return


    try:
        enforce_execution_freshness(
            signal,
            "worker_start",
        )

    except SignalFreshnessError:
        return


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
                BLOCK_LIVE_ON_PENDING_CANCEL,
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
            ),
        )


    except ExecutionBlocked as exc:

        transition(
            signal_id,
            "BLOCKED",
            "SAFETY_BLOCKED",

            message=
                str(
                    exc
                ),
        )

        return


    ib = None

    parent_id = None


    try:
        ib = connect_ibkr()



        try:
            session = (
                check_market_session(
                    ib,
                    symbol,
                )
            )

        except Exception as exc:

            transition(
                signal_id,
                "BLOCKED",
                "MARKET_SESSION_UNKNOWN",

                message=
                    str(
                        exc
                    ),
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
            ),
        )


        if (
            REQUIRE_LIQUID_SESSION
            and not session[
                "is_open"
            ]
        ):

            transition(
                signal_id,
                "BLOCKED",
                "MARKET_CLOSED_BLOCK",

                message=
                    session[
                        "reason"
                    ],

                payload=
                    session,
            )

            return


        ib.positions = {}

        ib.positions_done.clear()

        ib.reqPositions()


        if not ib.positions_done.wait(
            timeout=3,
        ):

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_POSITIONS_TIMEOUT",
            )

            return


        fresh_broker_positions = [
            {
                "symbol":
                    broker_symbol,

                **position,
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
                        MAX_TOTAL_BROKER_POSITIONS,
                )
            )


        except PositionPolicyError as exc:

            transition(
                signal_id,
                "BLOCKED",
                "FRESH_POSITION_POLICY_ERROR",

                message=
                    str(
                        exc
                    ),
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
                ),
            )

            return


        if fresh_position_state[
            "blockers"
        ]:

            transition(
                signal_id,
                "BLOCKED",
                "POSITION_LIMIT_BLOCK",

                message=
                    "; ".join(
                        fresh_position_state[
                            "blockers"
                        ]
                    ),

                payload=
                    fresh_position_state,
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
            ),
        )


        if symbol in ib.positions:

            transition(
                signal_id,
                "BLOCKED",
                "EXISTING_POSITION_BLOCK",

                message=(
                    f"Existing broker position "
                    f"in {symbol}"
                ),
            )

            return


        ib.open_orders = []

        ib.open_orders_done.clear()

        ib.reqAllOpenOrders()


        if not ib.open_orders_done.wait(
            timeout=3,
        ):

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_ORDERS_TIMEOUT",
            )

            return


        active_statuses = {
            "PendingSubmit",
            "ApiPending",
            "PreSubmitted",
            "Submitted",
            "PendingCancel",
        }


        for order in ib.open_orders:

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
                        order,
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
                        order,
                )

                return


        pnl_request_id = (
            9901
        )

        ib.pnl_done.clear()

        ib.daily_pnl = None


        ib.reqPnL(
            pnl_request_id,
            IB_ACCOUNT,
            "",
        )


        if not ib.pnl_done.wait(
            timeout=3,
        ):

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_PNL_TIMEOUT",
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
                "IBKR_PNL_UNAVAILABLE",
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
                ),
            )

            return


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
                        BLOCK_LIVE_ON_PENDING_CANCEL,
                )
            )


        except ExecutionBlocked as exc:

            transition(
                signal_id,
                "BLOCKED",
                "FINAL_SAFETY_BLOCK",

                message=
                    str(
                        exc
                    ),
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
            ),
        )


        try:
            freshness = (
                enforce_execution_freshness(
                    signal,
                    "before_place_order",
                )
            )

        except SignalFreshnessError:
            return


        print(
            f"FINAL FRESHNESS PASS | "
            f"{signal_id} | "
            f"age="
            f"{freshness['age_seconds']:.2f}s",
            flush=True,
        )


        if PRELIVE_DRY_RUN:

            dry_run_payload = {
                "symbol":
                    symbol,

                "action":
                    action,

                "direction":
                    direction,

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
                    ],
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
                ),
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
                ),
            )


            print(
                f"PRELIVE DRY RUN PASS | "
                f"{signal_id} | "
                f"{direction} | "
                "NO ORDER SUBMITTED",
                flush=True,
            )

            return


        parent_id = (
            get_safe_parent_order_id(
                ib.next_order_id
            )
        )


        if parent_id is None:

            transition(
                signal_id,
                "BLOCKED",
                "IBKR_ORDER_ID_UNAVAILABLE",
            )

            return


        ib.parent_order_id = (
            parent_id
        )


        ib.expected_order_ids = {
            parent_id,
            parent_id + 1,
            parent_id + 2,
        }


        contract = stock_contract(
            symbol
        )


        min_tick = (
            session.get(
                "min_tick",
                0.01,
            )
        )

        normalized_prices = (
            normalize_bracket_prices(
                action=
                    action,

                entry=
                    signal[
                        "entry"
                    ],

                target=
                    signal[
                        "target"
                    ],

                stop=
                    signal[
                        "stop"
                    ],

                min_tick=
                    min_tick,

                market_rule=
                    session.get(
                        "market_rule",
                        [],
                    ),
            )
        )

        (
            stop_limit_price,
            stop_limit_tick,
        ) = (
            build_stop_limit_price(
                action=
                    action,

                stop_trigger=
                    normalized_prices[
                        "stop"
                    ],

                market_rule=
                    session.get(
                        "market_rule",
                        [],
                    ),

                fallback_tick=
                    min_tick,
            )
        )


        print(
            "STOP LIMIT NORMALIZATION | "
            f"{symbol} | "
            f"action={action} | "
            f"trigger={normalized_prices['stop']} | "
            f"limit={stop_limit_price} | "
            f"tick={stop_limit_tick}",
            flush=True,
        )


        print(
            "PRICE NORMALIZATION | "
            f"{symbol} | "
            f"minTick={min_tick} | "
            f"marketRule={session.get('market_rule_id')} | "
            f"entry={signal['entry']}"
            f"->{normalized_prices['entry']}"
            f"(tick={normalized_prices['entry_tick']}) | "
            f"target={signal['target']}"
            f"->{normalized_prices['target']}"
            f"(tick={normalized_prices['target_tick']}) | "
            f"stop={signal['stop']}"
            f"->{normalized_prices['stop']}"
            f"(tick={normalized_prices['stop_tick']})",
            flush=True,
        )

        orders, refs = create_bracket(
            signal_id,
            parent_id,
            signal[
                "quantity"
            ],
            action,
            normalized_prices[
                "entry"
            ],
            normalized_prices[
                "target"
            ],
            normalized_prices[
                "stop"
            ],
            stop_limit_price,
        )


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
                    ],
            },

            payload={
                "parent_order_id":
                    parent_id,

                "action":
                    action,

                "direction":
                    direction,

                "refs":
                    refs,

                "freshness":
                    freshness,
            },

            event_key=(
                f"order-ids:"
                f"{signal_id}:"
                f"{parent_id}"
            ),
        )


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

                "action":
                    action,

                "direction":
                    direction,

                "order_ids":
                    sorted(
                        ib.expected_order_ids
                    ),

                "refs":
                    refs,
            },

            event_key=(
                f"submission-intent:"
                f"{signal_id}:"
                f"{parent_id}"
            ),
        )


        for order in orders:

            ib.placeOrder(
                order.orderId,
                contract,
                order,
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
                        order.orderRef,

                    "action":
                        order.action,

                    "order_type":
                        order.orderType,
                },

                event_key=(
                    f"place-returned:"
                    f"{signal_id}:"
                    f"{order.orderId}"
                ),
            )


            time.sleep(
                0.15
            )


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

            record_event(
                db_file=
                    DB_FILE,

                signal_id=
                    signal_id,

                event_type=
                    "BRACKET_REJECTION_DETECTED",

                source=
                    "worker",

                message=
                    "; ".join(
                        ib.reject_messages
                    ),

                payload={
                    "parent_order_id":
                        parent_id,

                    "statuses":
                        dict(
                            ib.order_statuses
                        ),
                },
            )

            reject_text = "; ".join(
                ib.reject_messages
            )

            if (
                "No Opening Trades: Small Cap"
                in reject_text
                and
                "Compliance Restriction"
                in reject_text
            ):
                quarantine_ibkr_symbol(
                    signal.get("symbol"),
                    reject_text,
                )

            recover_rejected_bracket(
                ib,
                signal,
                parent_id,
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
                        ib.order_statuses,
                },

                fields={
                    "parent_order_id":
                        parent_id,
                },
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
                    ),
            },

            fields={
                "parent_order_id":
                    parent_id,
            },
        )


    except Exception as exc:

        transition(
            signal_id,
            "UNKNOWN",
            "WORKER_EXCEPTION",

            message=
                str(
                    exc
                ),

            fields=(
                {
                    "parent_order_id":
                        parent_id,
                }

                if parent_id
                is not None

                else None
            ),
        )


    finally:

        if ib is not None:

            try:
                ib.disconnect()

            except Exception:
                pass


def startup_sync():
    print(
        "STARTUP | waiting for successful "
        "IBKR reconciliation",
        flush=True,
    )


    while True:

        success = (
            reconcile_orders()
        )


        if success:

            record_event(
                db_file=
                    DB_FILE,

                signal_id=
                    None,

                event_type=
                    "WORKER_STARTUP_RECONCILED",

                source=
                    "worker",

                message=(
                    "Worker completed IBKR "
                    "startup synchronization"
                ),
            )


            print(
                "STARTUP READY | "
                "IBKR reconciliation completed",
                flush=True,
            )

            return


        print(
            f"STARTUP BLOCKED | "
            f"retrying in "
            f"{STARTUP_RETRY_SECONDS}s",
            flush=True,
        )


        time.sleep(
            STARTUP_RETRY_SECONDS
        )


REBOUND_MANAGER_INTERVAL_SECONDS = float(
    os.getenv("REBOUND_MANAGER_INTERVAL_SECONDS", "10")
)


def run_rebound_manager():
    """Ratchet/close open microcap_rebound_v1 positions (paper account only)."""
    import rebound_stop_manager

    if not rebound_stop_manager.has_work(DB_FILE):
        return
    if not (str(IB_PORT) == "7497" and str(IB_ACCOUNT).upper().startswith("DU")):
        print("REBOUND MANAGER SKIP | not the paper account", flush=True)
        return
    import rebound_ib_broker

    ib = connect_ibkr()
    try:
        rebound_stop_manager.tick(
            DB_FILE, rebound_ib_broker.IBReboundBroker(ib, sys.modules[__name__])
        )
    finally:
        ib.disconnect()


def main():
    init_db()


    print(
        "TradingMax worker started",
        flush=True,
    )

    print(
        f"IB_CLIENT_ID="
        f"{IB_CLIENT_ID}",
        flush=True,
    )

    print(
        f"LIVE_TRADING="
        f"{LIVE_TRADING}",
        flush=True,
    )

    print(
        f"REQUIRE_LIQUID_SESSION="
        f"{REQUIRE_LIQUID_SESSION}",
        flush=True,
    )

    print(
        f"MAX_SIGNAL_AGE_SECONDS="
        f"{MAX_SIGNAL_AGE_SECONDS}",
        flush=True,
    )

    print(
        f"MAX_MANAGED_POSITIONS="
        f"{MAX_MANAGED_POSITIONS}",
        flush=True,
    )

    print(
        f"MAX_TOTAL_BROKER_POSITIONS="
        f"{MAX_TOTAL_BROKER_POSITIONS}",
        flush=True,
    )

    print(
        f"PRELIVE_DRY_RUN="
        f"{PRELIVE_DRY_RUN}",
        flush=True,
    )

    print(
        "DIRECTIONS=LONG,SHORT",
        flush=True,
    )

    print(
        "QUEUE_CLAIM=ATOMIC",
        flush=True,
    )

    print(
        "CANCEL_DISCOVERY="
        "stored IDs + broker refs + parentId",
        flush=True,
    )


    recover_stuck_work()

    startup_sync()


    last_reconcile = (
        time.monotonic()
    )
    last_rebound = 0.0


    while True:

        try:
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
                        flush=True,
                    )

                    startup_sync()


                last_reconcile = (
                    time.monotonic()
                )


            if (
                time.monotonic() - last_rebound
                >= REBOUND_MANAGER_INTERVAL_SECONDS
            ):
                last_rebound = time.monotonic()
                try:
                    run_rebound_manager()
                except Exception as exc:
                    print(
                        f"REBOUND MANAGER ERROR | {type(exc).__name__}: {exc}",
                        flush=True,
                    )


            cancel_request = (
                get_next_cancel_request()
            )


            if cancel_request:

                process_cancel(
                    cancel_request
                )

                continue


            signal = (
                get_next_signal()
            )


            if signal:

                process_signal(
                    signal
                )

                continue


            time.sleep(
                0.5
            )


        except Exception as exc:

            print(
                f"WORKER LOOP ERROR | "
                f"{exc}",
                flush=True,
            )

            time.sleep(
                2
            )


if __name__ == "__main__":
    main()
