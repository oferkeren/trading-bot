import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.execution import ExecutionFilter

from protection_guard import evaluate_live_protection

from signal_contract import (
    normalize_action,
    trade_direction,
    SignalContractError,
)

from trade_state import (
    init_trade_state,
    transition_signal,
    update_signal_metadata,
    record_event,
)


load_dotenv()


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

IB_ACCOUNT = os.getenv(
    "IB_ACCOUNT"
)

IB_MONITOR_CLIENT_ID = int(
    os.getenv(
        "IB_MONITOR_CLIENT_ID",
        "40"
    )
)

TRADE_MONITOR_INTERVAL_SECONDS = int(
    os.getenv(
        "TRADE_MONITOR_INTERVAL_SECONDS",
        "10"
    )
)


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    return conn


def column_exists(
    conn,
    table,
    column
):
    cur = conn.cursor()

    cur.execute(
        f"PRAGMA table_info({table})"
    )

    return column in {
        row[1]
        for row in cur.fetchall()
    }


def ensure_column(
    conn,
    table,
    column,
    definition
):
    if not column_exists(
        conn,
        table,
        column
    ):
        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )


def init_db():
    conn = db_connect()

    columns = {
        "entry_order_id":
            "INTEGER",

        "target_order_id":
            "INTEGER",

        "stop_order_id":
            "INTEGER",

        "parent_perm_id":
            "INTEGER",

        "target_perm_id":
            "INTEGER",

        "stop_perm_id":
            "INTEGER",

        "entry_order_ref":
            "TEXT",

        "target_order_ref":
            "TEXT",

        "stop_order_ref":
            "TEXT",

        "filled_quantity":
            "REAL",

        "entry_fill_price":
            "REAL",

        "exit_fill_price":
            "REAL",

        "entry_time":
            "TEXT",

        "exit_time":
            "TEXT",

        "exit_reason":
            "TEXT",

        "realized_pnl":
            "REAL",

        "monitor_message":
            "TEXT",

        "broker_account":
            "TEXT",

        "broker_port":
            "INTEGER",
    }


    for (
        column,
        definition
    ) in columns.items():

        ensure_column(
            conn,
            "signals",
            column,
            definition
        )


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_executions (
            exec_id TEXT PRIMARY KEY,
            signal_id TEXT,
            order_id INTEGER,
            perm_id INTEGER,
            symbol TEXT,
            side TEXT,
            shares REAL,
            price REAL,
            exec_time TEXT,
            received_at TEXT NOT NULL
        )
        """
    )


    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_trade_executions_signal
        ON trade_executions(signal_id)
        """
    )


    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_trade_executions_perm
        ON trade_executions(perm_id)
        """
    )


    conn.commit()
    conn.close()


    init_trade_state(
        DB_FILE
    )


def get_active_signals():
    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
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
    )


    rows = [
        dict(
            row
        )
        for row
        in cur.fetchall()
    ]


    conn.close()

    return rows


def transition(
    signal_id,
    status,
    event_type,
    message=None,
    payload=None,
    fields=None,
    event_key=None
):
    result = transition_signal(
        db_file=
            DB_FILE,

        signal_id=
            signal_id,

        new_status=
            status,

        event_type=
            event_type,

        source=
            "monitor",

        message=
            message,

        payload=
            payload,

        extra_fields=
            fields,

        event_key=
            event_key,

        allow_same_state=
            True,

        force=
            True
    )


    if result[
        "changed"
    ]:

        print(
            f"MONITOR STATE | "
            f"{signal_id} | "
            f"{result['old_status']} "
            f"-> "
            f"{result['new_status']} | "
            f"{event_type}"
        )


    return result


def sanitize_ref_component(
    value
):
    import re

    value = str(
        value
    ).strip()

    value = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        value
    )

    return value[:80]


def build_order_refs(
    signal_id
):
    key = sanitize_ref_component(
        signal_id
    )

    return {
        "ENTRY":
            f"TM:{key}:ENTRY",

        "TP":
            f"TM:{key}:TP",

        "SL":
            f"TM:{key}:SL",
    }


class MonitorApp(
    EWrapper,
    EClient
):

    def __init__(
        self
    ):
        EClient.__init__(
            self,
            self
        )

        self.ready = threading.Event()
        self.positions_done = threading.Event()
        self.open_orders_done = threading.Event()
        self.completed_orders_done = threading.Event()
        self.executions_done = threading.Event()

        self.accounts = []
        self.positions = {}

        self.open_orders = []
        self.completed_orders = []

        self.executions = []
        self.errors = []
        self.stop_status_updates = {}
        self.snapshot_started_at = None
        self.snapshot_completed_at = None


    def nextValidId(
        self,
        orderId
    ):
        print(
            f"MONITOR CONNECTED | "
            f"clientId="
            f"{IB_MONITOR_CLIENT_ID} | "
            f"nextOrderId="
            f"{orderId}"
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
            "account": account,
            "con_id": int(contract.conId),

            "avg_cost":
                float(
                    avgCost
                ),
        }


    def positionEnd(
        self
    ):
        self.positions_done.set()


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


        self.open_orders.append({
            "order_id":
                orderId,
            "account": order.account,
            "con_id": int(contract.conId),

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

            "action":
                order.action,

            "order_type":
                order.orderType,

            "status":
                orderState.status,

            "parent_id":
                order.parentId,

            "order_ref":
                getattr(
                    order,
                    "orderRef",
                    ""
                )
                or "",
        })


    def orderStatus(
        self, orderId, status, filled, remaining,
        avgFillPrice, permId, parentId, lastFillPrice,
        clientId, whyHeld, mktCapPrice=0.0,
    ):
        self.stop_status_updates[
            (int(orderId), int(permId))
        ] = {
            "order_id": int(orderId),
            "perm_id": int(permId),
            "status": status,
            "remaining": remaining,
            "received_at": time.monotonic(),
        }

    def openOrderEnd(
        self
    ):
        self.open_orders_done.set()


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


        order_id = getattr(
            order,
            "orderId",
            None
        )


        self.completed_orders.append({
            "order_id":
                order_id,

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

            "action":
                order.action,

            "order_type":
                order.orderType,

            "status":
                orderState.status,

            "parent_id":
                order.parentId,

            "order_ref":
                getattr(
                    order,
                    "orderRef",
                    ""
                )
                or "",
        })


    def completedOrdersEnd(
        self
    ):
        self.completed_orders_done.set()


    def execDetails(
        self,
        reqId,
        contract,
        execution
    ):
        account = getattr(
            execution,
            "acctNumber",
            ""
        )


        if (
            account
            and
            account != IB_ACCOUNT
        ):
            return


        self.executions.append({
            "symbol":
                (
                    contract.symbol.upper()
                    if contract.symbol
                    else ""
                ),

            "execution":
                execution,
        })


    def execDetailsEnd(
        self,
        reqId
    ):
        self.executions_done.set()


    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        if errorCode in {
            2104,
            2106,
            2158,
            2109,
        }:
            return


        message = (
            f"{errorCode}: "
            f"{errorString}"
        )


        self.errors.append(
            message
        )


        print(
            f"MONITOR IB ERROR | "
            f"id={reqId} | "
            f"{message}"
        )


def collect_ibkr_state():
    app = MonitorApp()


    try:
        app.connect(
            IB_HOST,
            IB_PORT,
            clientId=
                IB_MONITOR_CLIENT_ID
        )


        thread = threading.Thread(
            target=app.run,
            daemon=True
        )

        thread.start()


        if not app.ready.wait(
            timeout=5
        ):
            raise RuntimeError(
                "TWS connection timeout"
            )


        time.sleep(
            0.4
        )


        if (
            IB_ACCOUNT
            not in app.accounts
        ):
            raise RuntimeError(
                "Configured account unavailable"
            )


        app.snapshot_started_at = time.monotonic()
        app.reqPositions()


        if not app.positions_done.wait(
            timeout=5
        ):
            raise RuntimeError(
                "Positions timeout"
            )


        app.reqAllOpenOrders()


        if not app.open_orders_done.wait(
            timeout=5
        ):
            raise RuntimeError(
                "Open orders timeout"
            )


        app.reqCompletedOrders(
            True
        )


        if not app.completed_orders_done.wait(
            timeout=5
        ):
            raise RuntimeError(
                "Completed orders timeout"
            )


        execution_filter = (
            ExecutionFilter()
        )

        execution_filter.acctCode = (
            IB_ACCOUNT
        )


        app.reqExecutions(
            7001,
            execution_filter
        )


        if not app.executions_done.wait(
            timeout=5
        ):
            raise RuntimeError(
                "Executions timeout"
            )


        time.sleep(0.25)
        app.snapshot_completed_at = time.monotonic()

        return app


    except Exception:

        try:
            app.disconnect()

        except Exception:
            pass

        raise


def combined_orders(
    app
):
    return (
        list(
            app.open_orders
        )
        +
        list(
            app.completed_orders
        )
    )


def valid_order_id(
    value
):
    try:
        value = int(
            value
        )

    except Exception:
        return None


    #
    # IBKR may return completed-order records with orderId=0.
    #
    # 0 is not a trustworthy identity for order lookup or
    # parent-child relationships. orderRef or permId may
    # still identify that broker record.
    #
    if value <= 0:
        return None


    return value


def valid_perm_id(
    value
):
    try:
        value = int(
            value
        )

    except Exception:
        return None


    if value <= 0:
        return None


    return value


def find_order_by_ref(
    app,
    order_ref
):
    if not order_ref:
        return None


    wanted = str(
        order_ref
    ).strip()


    if not wanted:
        return None


    for order in combined_orders(
        app
    ):

        if (
            str(
                order.get(
                    "order_ref"
                )
                or ""
            ).strip()
            == wanted
        ):
            return order


    return None


def find_order_by_perm_id(
    app,
    perm_id
):
    wanted = valid_perm_id(
        perm_id
    )


    if wanted is None:
        return None


    for order in combined_orders(
        app
    ):

        candidate = valid_perm_id(
            order.get(
                "perm_id"
            )
        )


        if candidate == wanted:
            return order


    return None


def find_order_by_id(
    app,
    order_id
):
    wanted = valid_order_id(
        order_id
    )


    if wanted is None:
        return None


    matches = [
        order

        for order
        in combined_orders(
            app
        )

        if (
            valid_order_id(
                order.get(
                    "order_id"
                )
            )
            == wanted
        )
    ]


    if not matches:
        return None


    if len(
        matches
    ) != 1:
        return None


    return matches[
        0
    ]


def find_children_by_parent(
    app,
    parent_order,
    signal
):
    if not parent_order:
        return []


    parent_id = valid_order_id(
        parent_order.get(
            "order_id"
        )
    )


    if parent_id is None:
        return []


    try:
        entry_action = normalize_action(
            signal.get(
                "action"
            )
            or "BUY"
        )

    except SignalContractError:
        return []


    exit_action = (
        "SELL"
        if entry_action == "BUY"
        else "BUY"
    )


    return [
        order

        for order
        in combined_orders(
            app
        )

        if (
            valid_order_id(
                order.get(
                    "parent_id"
                )
            )
            == parent_id

            and

            str(
                order.get(
                    "action"
                )
                or ""
            ).upper()
            == exit_action
        )
    ]


def resolve_leg(
    app,
    signal,
    role
):
    refs = build_order_refs(
        signal[
            "signal_id"
        ]
    )


    config = {
        "ENTRY": {
            "ref_column":
                "entry_order_ref",

            "id_column":
                "entry_order_id",

            "perm_column":
                "parent_perm_id",

            "expected_ref":
                refs[
                    "ENTRY"
                ],
        },

        "TP": {
            "ref_column":
                "target_order_ref",

            "id_column":
                "target_order_id",

            "perm_column":
                "target_perm_id",

            "expected_ref":
                refs[
                    "TP"
                ],
        },

        "SL": {
            "ref_column":
                "stop_order_ref",

            "id_column":
                "stop_order_id",

            "perm_column":
                "stop_perm_id",

            "expected_ref":
                refs[
                    "SL"
                ],
        },
    }


    cfg = config[
        role
    ]


    stored_ref = signal.get(
        cfg[
            "ref_column"
        ]
    )


    if stored_ref:

        result = find_order_by_ref(
            app,
            stored_ref
        )


        if result:
            return result


    result = find_order_by_ref(
        app,
        cfg[
            "expected_ref"
        ]
    )


    if result:
        return result


    result = find_order_by_perm_id(
        app,
        signal.get(
            cfg[
                "perm_column"
            ]
        )
    )


    if result:
        return result


    result = find_order_by_id(
        app,
        signal.get(
            cfg[
                "id_column"
            ]
        )
    )


    if result:
        return result


    if role == "ENTRY":

        return find_order_by_id(
            app,
            signal.get(
                "parent_order_id"
            )
        )


    parent_order = resolve_leg(
        app,
        signal,
        "ENTRY"
    )


    if not parent_order:
        return None


    children = find_children_by_parent(
        app,
        parent_order,
        signal
    )


    if role == "TP":

        candidates = [
            order

            for order
            in children

            if (
                str(
                    order.get(
                        "order_type"
                    )
                    or ""
                ).upper()
                == "LMT"
            )
        ]

    else:

        candidates = [
            order

            for order
            in children

            if (
                str(
                    order.get(
                        "order_type"
                    )
                    or ""
                ).upper()
                in {
                    "STP",
                    "STP LMT",
                }
            )
        ]


    if len(
        candidates
    ) == 1:

        return candidates[
            0
        ]


    return None


def execution_matches_order(
    execution,
    order
):
    if order is None:
        return False


    exec_perm_id = valid_perm_id(
        getattr(
            execution,
            "permId",
            None
        )
    )

    order_perm_id = valid_perm_id(
        order.get(
            "perm_id"
        )
    )


    if (
        exec_perm_id is not None
        and
        order_perm_id is not None
        and
        exec_perm_id == order_perm_id
    ):
        return True


    exec_order_id = valid_order_id(
        getattr(
            execution,
            "orderId",
            None
        )
    )

    order_id = valid_order_id(
        order.get(
            "order_id"
        )
    )


    if (
        exec_order_id is not None
        and
        order_id is not None
        and
        exec_order_id == order_id
    ):
        return True


    return False


def executions_for_order(
    app,
    order
):
    if order is None:
        return []


    result = []


    for item in app.executions:

        execution = item[
            "execution"
        ]


        if execution_matches_order(
            execution,
            order
        ):

            result.append(
                item
            )


    return result


def save_execution(
    signal_id,
    role,
    symbol,
    execution
):
    exec_id = getattr(
        execution,
        "execId",
        None
    )


    if not exec_id:
        return False


    order_id = getattr(
        execution,
        "orderId",
        None
    )

    perm_id = getattr(
        execution,
        "permId",
        None
    )

    side = getattr(
        execution,
        "side",
        None
    )

    shares = float(
        getattr(
            execution,
            "shares",
            0
        )
    )

    price = float(
        getattr(
            execution,
            "price",
            0
        )
    )

    exec_time = getattr(
        execution,
        "time",
        None
    )


    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
        """
        INSERT OR IGNORE INTO trade_executions (
            exec_id,
            signal_id,
            order_id,
            perm_id,
            symbol,
            side,
            shares,
            price,
            exec_time,
            received_at
        )
        VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?
        )
        """,
        (
            exec_id,
            signal_id,
            order_id,
            perm_id,
            symbol,
            side,
            shares,
            price,
            exec_time,
            now_iso(),
        )
    )


    inserted = (
        cur.rowcount
        == 1
    )


    conn.commit()
    conn.close()


    if inserted:

        event_type = {
            "ENTRY":
                "ENTRY_EXECUTION",

            "TP":
                "TP_EXECUTION",

            "SL":
                "SL_EXECUTION",
        }[
            role
        ]


        record_event(
            db_file=
                DB_FILE,

            signal_id=
                signal_id,

            event_type=
                event_type,

            source=
                "monitor",

            message=(
                f"{role} execution: "
                f"{shares} @ {price}"
            ),

            payload={
                "exec_id":
                    exec_id,

                "order_id":
                    order_id,

                "perm_id":
                    perm_id,

                "symbol":
                    symbol,

                "side":
                    side,

                "shares":
                    shares,

                "price":
                    price,

                "exec_time":
                    exec_time,
            },

            event_key=(
                f"execution:"
                f"{exec_id}"
            )
        )


        print(
            f"EXECUTION | "
            f"{signal_id} | "
            f"{role} | "
            f"{shares} @ {price} | "
            f"execId={exec_id}"
        )


    return inserted


def aggregate_executions(
    items
):
    if not items:
        return None


    total_shares = 0.0
    total_value = 0.0
    latest_time = None


    for item in items:

        execution = item[
            "execution"
        ]


        shares = float(
            execution.shares
        )

        price = float(
            execution.price
        )


        total_shares += (
            shares
        )

        total_value += (
            shares
            *
            price
        )


        exec_time = getattr(
            execution,
            "time",
            None
        )


        if exec_time:

            latest_time = (
                exec_time
            )


    if total_shares <= 0:
        return None


    return {
        "shares":
            total_shares,

        "price":
            (
                total_value
                /
                total_shares
            ),

        "time":
            latest_time,
    }


def normalized_status(
    order
):
    if not order:
        return ""


    return (
        order.get(
            "status"
        )
        or ""
    ).strip()


def is_cancelled_status(
    status
):
    return status in {
        "Cancelled",
        "ApiCancelled",
    }


def is_pending_cancel_status(
    status
):
    return (
        status
        == "PendingCancel"
    )


def is_order_open(
    app,
    order
):
    if order is None:
        return False


    if order in app.open_orders:
        return True


    order_ref = str(
        order.get(
            "order_ref"
        )
        or ""
    ).strip()


    perm_id = valid_perm_id(
        order.get(
            "perm_id"
        )
    )


    order_id = valid_order_id(
        order.get(
            "order_id"
        )
    )


    for candidate in app.open_orders:

        if (
            order_ref
            and
            str(
                candidate.get(
                    "order_ref"
                )
                or ""
            ).strip()
            == order_ref
        ):
            return True


        if (
            perm_id is not None
            and
            valid_perm_id(
                candidate.get(
                    "perm_id"
                )
            )
            == perm_id
        ):
            return True


        if (
            order_id is not None
            and
            valid_order_id(
                candidate.get(
                    "order_id"
                )
            )
            == order_id
        ):
            return True


    return False


def repair_identity(
    signal,
    entry_order,
    target_order,
    stop_order
):
    fields = {}
    payload = {}


    if entry_order:

        entry_id = valid_order_id(
            entry_order.get(
                "order_id"
            )
        )

        entry_perm = valid_perm_id(
            entry_order.get(
                "perm_id"
            )
        )

        entry_ref = str(
            entry_order.get(
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


        payload[
            "entry"
        ] = entry_order


    if target_order:

        target_id = valid_order_id(
            target_order.get(
                "order_id"
            )
        )

        target_perm = valid_perm_id(
            target_order.get(
                "perm_id"
            )
        )

        target_ref = str(
            target_order.get(
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


        payload[
            "target"
        ] = target_order


    if stop_order:

        stop_id = valid_order_id(
            stop_order.get(
                "order_id"
            )
        )

        stop_perm = valid_perm_id(
            stop_order.get(
                "perm_id"
            )
        )

        stop_ref = str(
            stop_order.get(
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


        payload[
            "stop"
        ] = stop_order


    if not fields:
        return


    identity_parts = []


    if entry_order:

        identity_parts.append(
            f"E:"
            f"{valid_order_id(entry_order.get('order_id'))}:"
            f"{valid_perm_id(entry_order.get('perm_id'))}"
        )


    if target_order:

        identity_parts.append(
            f"T:"
            f"{valid_order_id(target_order.get('order_id'))}:"
            f"{valid_perm_id(target_order.get('perm_id'))}"
        )


    if stop_order:

        identity_parts.append(
            f"S:"
            f"{valid_order_id(stop_order.get('order_id'))}:"
            f"{valid_perm_id(stop_order.get('perm_id'))}"
        )


    event_key = (
        "monitor-identity:"
        +
        signal[
            "signal_id"
        ]
        +
        ":"
        +
        "|".join(
            identity_parts
        )
    )


    update_signal_metadata(
        db_file=
            DB_FILE,

        signal_id=
            signal[
                "signal_id"
            ],

        source=
            "monitor",

        event_type=
            "ORDER_IDENTITY_OBSERVED",

        fields=
            fields,

        message=(
            "Monitor resolved IBKR "
            "order identity"
        ),

        payload=
            payload,

        event_key=
            event_key
    )


def calculate_realized_pnl(
    action,
    entry_fill,
    exit_fill
):
    entry_action = normalize_action(
        action
        or "BUY"
    )


    quantity = min(
        entry_fill[
            "shares"
        ],
        exit_fill[
            "shares"
        ]
    )


    if entry_action == "BUY":

        pnl_per_share = (
            exit_fill[
                "price"
            ]
            -
            entry_fill[
                "price"
            ]
        )

    else:

        pnl_per_share = (
            entry_fill[
                "price"
            ]
            -
            exit_fill[
                "price"
            ]
        )


    pnl = (
        pnl_per_share
        *
        quantity
    )


    return (
        quantity,
        pnl
    )


def reconcile_signal(
    app,
    signal
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
            or "BUY"
        )

        direction = trade_direction(
            action
        )


    except SignalContractError as exc:

        transition(
            signal_id,
            "ERROR",
            "MONITOR_DIRECTION_INVALID",

            message=
                str(
                    exc
                ),

            fields={
                "monitor_message":
                    "Invalid signal direction",
            },

            event_key=(
                f"monitor-direction-invalid:"
                f"{signal_id}"
            )
        )

        return


    entry_order = resolve_leg(
        app,
        signal,
        "ENTRY"
    )

    target_order = resolve_leg(
        app,
        signal,
        "TP"
    )

    stop_order = resolve_leg(
        app,
        signal,
        "SL"
    )


    repair_identity(
        signal,
        entry_order,
        target_order,
        stop_order
    )


    entry_execs = executions_for_order(
        app,
        entry_order
    )

    target_execs = executions_for_order(
        app,
        target_order
    )

    stop_execs = executions_for_order(
        app,
        stop_order
    )


    for item in entry_execs:

        save_execution(
            signal_id,
            "ENTRY",
            symbol,
            item[
                "execution"
            ]
        )


    for item in target_execs:

        save_execution(
            signal_id,
            "TP",
            symbol,
            item[
                "execution"
            ]
        )


    for item in stop_execs:

        save_execution(
            signal_id,
            "SL",
            symbol,
            item[
                "execution"
            ]
        )


    entry_fill = aggregate_executions(
        entry_execs
    )

    target_fill = aggregate_executions(
        target_execs
    )

    stop_fill = aggregate_executions(
        stop_execs
    )


    if (
        target_fill
        and
        stop_fill
    ):

        transition(
            signal_id,
            "ERROR",
            "DOUBLE_EXIT_EXECUTION",

            message=(
                "CRITICAL: both TP and SL "
                "have executions"
            ),

            payload={
                "action":
                    action,

                "direction":
                    direction,

                "entry":
                    entry_fill,

                "tp":
                    target_fill,

                "sl":
                    stop_fill,
            },

            fields={
                "monitor_message":
                    (
                        "CRITICAL: TP and SL "
                        "both executed"
                    ),
            },

            event_key=(
                f"double-exit:"
                f"{signal_id}"
            )
        )

        return


    live_position = app.positions.get(symbol)

    if live_position and (
        entry_fill
        or signal.get("status") in {
            "OPEN_POSITION",
            "FILLED",
        }
    ):
        open_stop = next(
            (
                order
                for order in app.open_orders
                if stop_order is not None
                and order.get("order_id")
                    == stop_order.get("order_id")
                and order.get("perm_id")
                    == stop_order.get("perm_id")
                and order.get("order_ref")
                    == stop_order.get("order_ref")
            ),
            None,
        )

        fill_state = None

        if open_stop:
            fill_state = app.stop_status_updates.get(
                (
                    int(open_stop["order_id"]),
                    int(open_stop["perm_id"]),
                )
            )

        verdict = evaluate_live_protection(
            position=live_position,
            entry_action=action,
            stop_order=open_stop,
            stop_fill_state=fill_state,
            snapshot_started_at=app.snapshot_started_at,
            snapshot_completed_at=app.snapshot_completed_at,
        )

        if not verdict["protected"]:
            reason = verdict["reason"]

            print(
                "CRITICAL MONITOR STOP UNVERIFIED | "
                f"{signal_id} | {reason}",
                flush=True,
            )

            transition(
                signal_id,
                "UNKNOWN",
                "MONITOR_STOP_UNVERIFIED",
                message=(
                    "Broker exposure without verified "
                    f"protection: {reason}"
                ),
                payload={
                    "protection": verdict,
                },
                fields={
                    "monitor_message":
                        f"STOP UNVERIFIED: {reason}",
                },
                event_key=(
                    f"monitor-stop-unverified:"
                    f"{signal_id}:{reason}:"
                    f"{int(time.time() // 60)}"
                ),
            )

            return

    if (
        entry_fill
        and
        target_fill
    ):

        exit_quantity, pnl = (
            calculate_realized_pnl(
                action,
                entry_fill,
                target_fill
            )
        )


        fully_closed = (
            target_fill[
                "shares"
            ]
            >=
            entry_fill[
                "shares"
            ]
        )


        payload = {
            "action":
                action,

            "direction":
                direction,

            "entry_fill":
                entry_fill,

            "target_fill":
                target_fill,

            "exit_quantity":
                exit_quantity,

            "realized_pnl":
                pnl,
        }


        if fully_closed:

            transition(
                signal_id,
                "CLOSED_TP",
                "TP_FILLED",

                message=(
                    f"{direction} take profit filled: "
                    f"{target_fill['shares']} "
                    f"@ {target_fill['price']}"
                ),

                payload=
                    payload,

                fields={
                    "filled_quantity":
                        entry_fill[
                            "shares"
                        ],

                    "entry_fill_price":
                        entry_fill[
                            "price"
                        ],

                    "entry_time":
                        entry_fill[
                            "time"
                        ],

                    "exit_fill_price":
                        target_fill[
                            "price"
                        ],

                    "exit_time":
                        target_fill[
                            "time"
                        ],

                    "exit_reason":
                        "TAKE_PROFIT",

                    "realized_pnl":
                        pnl,

                    "monitor_message":
                        (
                            f"{direction} "
                            "take-profit exit "
                            "fully executed"
                        ),
                },

                event_key=(
                    f"closed-tp:"
                    f"{signal_id}:"
                    f"{target_fill['shares']}:"
                    f"{target_fill['price']}"
                )
            )


        else:

            transition(
                signal_id,
                "OPEN_POSITION",
                "TP_PARTIAL_FILL",

                message=(
                    f"{direction} partial TP fill "
                    f"{target_fill['shares']}/"
                    f"{entry_fill['shares']}"
                ),

                payload=
                    payload,

                fields={
                    "filled_quantity":
                        entry_fill[
                            "shares"
                        ],

                    "entry_fill_price":
                        entry_fill[
                            "price"
                        ],

                    "entry_time":
                        entry_fill[
                            "time"
                        ],

                    "exit_fill_price":
                        target_fill[
                            "price"
                        ],

                    "realized_pnl":
                        pnl,

                    "monitor_message":
                        (
                            f"{direction} partial "
                            "take-profit execution"
                        ),
                },

                event_key=(
                    f"partial-tp:"
                    f"{signal_id}:"
                    f"{target_fill['shares']}:"
                    f"{target_fill['price']}"
                )
            )


        return


    if (
        entry_fill
        and
        stop_fill
    ):

        exit_quantity, pnl = (
            calculate_realized_pnl(
                action,
                entry_fill,
                stop_fill
            )
        )


        fully_closed = (
            stop_fill[
                "shares"
            ]
            >=
            entry_fill[
                "shares"
            ]
        )


        payload = {
            "action":
                action,

            "direction":
                direction,

            "entry_fill":
                entry_fill,

            "stop_fill":
                stop_fill,

            "exit_quantity":
                exit_quantity,

            "realized_pnl":
                pnl,
        }


        if fully_closed:

            transition(
                signal_id,
                "CLOSED_SL",
                "SL_FILLED",

                message=(
                    f"{direction} stop loss filled: "
                    f"{stop_fill['shares']} "
                    f"@ {stop_fill['price']}"
                ),

                payload=
                    payload,

                fields={
                    "filled_quantity":
                        entry_fill[
                            "shares"
                        ],

                    "entry_fill_price":
                        entry_fill[
                            "price"
                        ],

                    "entry_time":
                        entry_fill[
                            "time"
                        ],

                    "exit_fill_price":
                        stop_fill[
                            "price"
                        ],

                    "exit_time":
                        stop_fill[
                            "time"
                        ],

                    "exit_reason":
                        "STOP_LOSS",

                    "realized_pnl":
                        pnl,

                    "monitor_message":
                        (
                            f"{direction} "
                            "stop-loss exit "
                            "fully executed"
                        ),
                },

                event_key=(
                    f"closed-sl:"
                    f"{signal_id}:"
                    f"{stop_fill['shares']}:"
                    f"{stop_fill['price']}"
                )
            )


        else:

            transition(
                signal_id,
                "OPEN_POSITION",
                "SL_PARTIAL_FILL",

                message=(
                    f"{direction} partial stop fill "
                    f"{stop_fill['shares']}/"
                    f"{entry_fill['shares']}"
                ),

                payload=
                    payload,

                fields={
                    "filled_quantity":
                        entry_fill[
                            "shares"
                        ],

                    "entry_fill_price":
                        entry_fill[
                            "price"
                        ],

                    "entry_time":
                        entry_fill[
                            "time"
                        ],

                    "exit_fill_price":
                        stop_fill[
                            "price"
                        ],

                    "realized_pnl":
                        pnl,

                    "monitor_message":
                        (
                            f"{direction} partial "
                            "stop-loss execution"
                        ),
                },

                event_key=(
                    f"partial-sl:"
                    f"{signal_id}:"
                    f"{stop_fill['shares']}:"
                    f"{stop_fill['price']}"
                )
            )


        return


    if entry_fill:

        position = app.positions.get(
            symbol
        )


        fields = {
            "filled_quantity":
                entry_fill[
                    "shares"
                ],

            "entry_fill_price":
                entry_fill[
                    "price"
                ],

            "entry_time":
                entry_fill[
                    "time"
                ],
        }


        if position:

            #
            # Long broker positions are positive.
            # Short broker positions are negative.
            #
            broker_quantity = float(
                position.get(
                    "quantity",
                    0
                )
            )


            expected_sign = (
                1
                if action == "BUY"
                else -1
            )


            direction_matches = (
                (
                    broker_quantity > 0
                    and
                    expected_sign > 0
                )
                or
                (
                    broker_quantity < 0
                    and
                    expected_sign < 0
                )
            )


            if not direction_matches:

                fields[
                    "monitor_message"
                ] = (
                    "Entry execution found but "
                    "broker position direction "
                    "does not match signal"
                )


                transition(
                    signal_id,
                    "ERROR",
                    "POSITION_DIRECTION_MISMATCH",

                    message=(
                        f"Signal direction={direction}; "
                        f"broker quantity="
                        f"{broker_quantity}"
                    ),

                    payload={
                        "action":
                            action,

                        "direction":
                            direction,

                        "entry_fill":
                            entry_fill,

                        "position":
                            position,
                    },

                    fields=
                        fields,

                    event_key=(
                        f"position-direction-mismatch:"
                        f"{signal_id}:"
                        f"{broker_quantity}"
                    )
                )

                return


            fields[
                "monitor_message"
            ] = (
                f"{direction} entry execution "
                f"{entry_fill['shares']} "
                f"@ {entry_fill['price']}"
            )


            transition(
                signal_id,
                "OPEN_POSITION",
                "ENTRY_FILLED",

                message=(
                    f"{direction} entry filled: "
                    f"{entry_fill['shares']} "
                    f"@ {entry_fill['price']}"
                ),

                payload={
                    "action":
                        action,

                    "direction":
                        direction,

                    "entry_fill":
                        entry_fill,

                    "position":
                        position,
                },

                fields=
                    fields,

                event_key=(
                    f"entry-filled:"
                    f"{signal_id}:"
                    f"{entry_fill['shares']}:"
                    f"{entry_fill['price']}"
                )
            )


        else:

            fields[
                "monitor_message"
            ] = (
                "Entry execution exists, "
                "but position is absent "
                "and no exit execution "
                "was found"
            )


            transition(
                signal_id,
                "UNKNOWN",
                "ENTRY_POSITION_MISMATCH",

                message=(
                    "Entry execution found "
                    "without corresponding "
                    "position or exit"
                ),

                payload={
                    "action":
                        action,

                    "direction":
                        direction,

                    "entry_fill":
                        entry_fill,
                },

                fields=
                    fields,

                event_key=(
                    f"entry-mismatch:"
                    f"{signal_id}:"
                    f"{entry_fill['shares']}:"
                    f"{entry_fill['price']}"
                )
            )


        return


    if (
        entry_order
        and
        is_order_open(
            app,
            entry_order
        )
    ):

        broker_status = (
            normalized_status(
                entry_order
            )
        )


        payload = {
            "action":
                action,

            "direction":
                direction,

            "order_id":
                entry_order.get(
                    "order_id"
                ),

            "perm_id":
                entry_order.get(
                    "perm_id"
                ),

            "order_ref":
                entry_order.get(
                    "order_ref"
                ),

            "broker_status":
                broker_status,
        }


        if is_pending_cancel_status(
            broker_status
        ):

            transition(
                signal_id,
                "CANCEL_PENDING",
                "CANCEL_PENDING",

                message=(
                    f"Entry order "
                    f"{entry_order.get('order_id')} "
                    "is PendingCancel"
                ),

                payload=
                    payload,

                fields={
                    "monitor_message":
                        (
                            "IBKR parent order "
                            "is PendingCancel"
                        ),
                },

                event_key=(
                    f"broker-status:"
                    f"{signal_id}:"
                    f"{entry_order.get('perm_id')}:"
                    f"PendingCancel"
                )
            )


        elif broker_status in {
            "PendingSubmit",
            "ApiPending",
            "PreSubmitted",
        }:

            if signal[
                "status"
            ] in {
                "CANCEL_REQUESTED",
                "CANCELLING",
                "CANCEL_PENDING",
                "CANCEL_UNKNOWN",
            }:

                transition(
                    signal_id,
                    "CANCEL_PENDING",
                    "CANCEL_PENDING",

                    message=(
                        "Cancellation requested; "
                        f"IBKR status="
                        f"{broker_status}"
                    ),

                    payload=
                        payload,

                    fields={
                        "monitor_message":
                            (
                                "Cancellation requested; "
                                f"entry remains "
                                f"{broker_status}"
                            ),
                    },

                    event_key=(
                        f"broker-status:"
                        f"{signal_id}:"
                        f"{entry_order.get('perm_id')}:"
                        f"cancel-{broker_status}"
                    )
                )


            else:

                transition(
                    signal_id,
                    "ACCEPTED_WAITING_MARKET",
                    "ORDER_WAITING_MARKET",

                    message=(
                        f"IBKR entry status="
                        f"{broker_status}"
                    ),

                    payload=
                        payload,

                    fields={
                        "monitor_message":
                            (
                                f"{direction} entry order "
                                f"is {broker_status}"
                            ),
                    },

                    event_key=(
                        f"broker-status:"
                        f"{signal_id}:"
                        f"{entry_order.get('perm_id')}:"
                        f"{broker_status}"
                    )
                )


        elif broker_status == "Submitted":

            if signal[
                "status"
            ] in {
                "CANCEL_REQUESTED",
                "CANCELLING",
                "CANCEL_PENDING",
                "CANCEL_UNKNOWN",
            }:

                transition(
                    signal_id,
                    "CANCEL_PENDING",
                    "CANCEL_PENDING",

                    message=(
                        "Cancellation requested; "
                        "entry remains Submitted"
                    ),

                    payload=
                        payload,

                    fields={
                        "monitor_message":
                            (
                                "Cancellation requested; "
                                "entry remains Submitted"
                            ),
                    },

                    event_key=(
                        f"broker-status:"
                        f"{signal_id}:"
                        f"{entry_order.get('perm_id')}:"
                        "cancel-Submitted"
                    )
                )


            else:

                transition(
                    signal_id,
                    "SUBMITTED",
                    "ORDER_SUBMITTED",

                    message=(
                        f"{direction} IBKR entry "
                        "order is Submitted"
                    ),

                    payload=
                        payload,

                    fields={
                        "monitor_message":
                            (
                                f"{direction} entry order "
                                "is Submitted"
                            ),
                    },

                    event_key=(
                        f"broker-status:"
                        f"{signal_id}:"
                        f"{entry_order.get('perm_id')}:"
                        "Submitted"
                    )
                )


        else:

            transition(
                signal_id,
                "UNKNOWN",
                "ORDER_STATE_UNKNOWN",

                message=(
                    "Unexpected open-order "
                    f"status: {broker_status}"
                ),

                payload=
                    payload,

                fields={
                    "monitor_message":
                        (
                            "Unexpected entry "
                            f"status: "
                            f"{broker_status}"
                        ),
                },

                event_key=(
                    f"broker-status:"
                    f"{signal_id}:"
                    f"{entry_order.get('perm_id')}:"
                    f"unknown-{broker_status}"
                )
            )


        return


    if entry_order:

        broker_status = (
            normalized_status(
                entry_order
            )
        )


        identity = (
            entry_order.get(
                "perm_id"
            )
            or
            entry_order.get(
                "order_ref"
            )
            or
            valid_order_id(
                entry_order.get(
                    "order_id"
                )
            )
        )


        payload = {
            "action":
                action,

            "direction":
                direction,

            "order_id":
                entry_order.get(
                    "order_id"
                ),

            "perm_id":
                entry_order.get(
                    "perm_id"
                ),

            "order_ref":
                entry_order.get(
                    "order_ref"
                ),

            "broker_status":
                broker_status,
        }


        if is_cancelled_status(
            broker_status
        ):

            transition(
                signal_id,
                "CANCELLED",
                "CANCEL_CONFIRMED",

                message=(
                    "IBKR confirmed "
                    f"{broker_status}"
                ),

                payload=
                    payload,

                fields={
                    "monitor_message":
                        (
                            "Broker confirmed "
                            f"{broker_status}"
                        ),
                },

                event_key=(
                    f"cancel-confirmed:"
                    f"{signal_id}:"
                    f"{identity}:"
                    f"{broker_status}"
                )
            )


        elif broker_status == "Filled":

            transition(
                signal_id,
                "UNKNOWN",
                "FILLED_WITHOUT_EXECUTION",

                message=(
                    "Entry completed as Filled "
                    "but no matching execution "
                    "was returned by IBKR"
                ),

                payload=
                    payload,

                fields={
                    "monitor_message":
                        (
                            "Entry is Filled, "
                            "but execution details "
                            "are unavailable"
                        ),
                },

                event_key=(
                    f"filled-no-exec:"
                    f"{signal_id}:"
                    f"{identity}"
                )
            )


        elif broker_status == "Inactive":

            transition(
                signal_id,
                "ERROR",
                "ORDER_INACTIVE",

                message=(
                    "IBKR entry order "
                    "completed as Inactive"
                ),

                payload=
                    payload,

                fields={
                    "monitor_message":
                        (
                            "IBKR entry order "
                            "is Inactive"
                        ),
                },

                event_key=(
                    f"inactive:"
                    f"{signal_id}:"
                    f"{identity}"
                )
            )


        elif is_pending_cancel_status(
            broker_status
        ):

            transition(
                signal_id,
                "CANCEL_PENDING",
                "CANCEL_PENDING",

                message=(
                    "IBKR still reports "
                    "PendingCancel"
                ),

                payload=
                    payload,

                fields={
                    "monitor_message":
                        (
                            "IBKR still reports "
                            "PendingCancel"
                        ),
                },

                event_key=(
                    f"pending-cancel:"
                    f"{signal_id}:"
                    f"{identity}"
                )
            )


        else:

            transition(
                signal_id,
                "UNKNOWN",
                "COMPLETED_STATE_UNKNOWN",

                message=(
                    "Unexpected completed "
                    f"entry status: "
                    f"{broker_status}"
                ),

                payload=
                    payload,

                fields={
                    "monitor_message":
                        (
                            "Unexpected completed "
                            f"entry status: "
                            f"{broker_status}"
                        ),
                },

                event_key=(
                    f"completed-unknown:"
                    f"{signal_id}:"
                    f"{identity}:"
                    f"{broker_status}"
                )
            )


        return


    if (
        signal.get("status")
        == "OPEN_POSITION"

        and

        float(
            signal.get(
                "filled_quantity"
            )
            or 0
        )
        > 0
    ):
        record_event(
            db_file=DB_FILE,
            signal_id=signal_id,

            event_type=
                "MONITOR_OPEN_POSITION_ENTRY_ABSENT",

            source="monitor",

            message=(
                "OPEN_POSITION retained because "
                "a prior entry fill is recorded "
                "while the historical entry order "
                "is absent from this broker snapshot"
            ),

            payload={
                "broker_account":
                    signal.get(
                        "broker_account"
                    ),

                "broker_port":
                    signal.get(
                        "broker_port"
                    ),

                "filled_quantity":
                    signal.get(
                        "filled_quantity"
                    ),

                "entry_fill_price":
                    signal.get(
                        "entry_fill_price"
                    ),
            },

            event_key=(
                f"monitor-open-position-entry-absent:"
                f"{signal_id}"
            ),
        )

        return


    expected_refs = build_order_refs(
        signal_id
    )


    if signal[
        "status"
    ] in {
        "CANCEL_REQUESTED",
        "CANCELLING",
        "CANCEL_PENDING",
        "CANCEL_UNKNOWN",
    }:

        new_status = (
            "CANCEL_UNKNOWN"
        )

    else:

        new_status = (
            "UNKNOWN"
        )


    transition(
        signal_id,
        new_status,
        "ORDER_NOT_FOUND",

        message=(
            "Entry order not found "
            "by orderRef, permId or "
            "trusted positive orderId"
        ),

        payload={
            "action":
                action,

            "direction":
                direction,

            "expected_entry_ref":
                expected_refs[
                    "ENTRY"
                ],

            "stored_parent_order_id":
                signal.get(
                    "parent_order_id"
                ),

            "stored_perm_id":
                signal.get(
                    "parent_perm_id"
                ),
        },

        fields={
            "monitor_message":
                (
                    "Entry order not found "
                    "using any trusted "
                    "identity method"
                ),
        },

        event_key=(
            f"order-not-found:"
            f"{signal_id}"
        )
    )


def monitor_once():
    signals = (
        get_active_signals()
    )


    if not signals:

        print(
            "MONITOR | "
            "no active signals"
        )

        return


    app = collect_ibkr_state()


    try:

        print(
            f"MONITOR | "
            f"signals="
            f"{len(signals)} | "
            f"positions="
            f"{len(app.positions)} | "
            f"open_orders="
            f"{len(app.open_orders)} | "
            f"completed="
            f"{len(app.completed_orders)} | "
            f"executions="
            f"{len(app.executions)}"
        )


        for signal in signals:

            try:

                reconcile_signal(
                    app,
                    signal
                )


            except Exception as exc:

                print(
                    f"MONITOR SIGNAL ERROR | "
                    f"{signal['signal_id']} | "
                    f"{exc}"
                )


                record_event(
                    db_file=
                        DB_FILE,

                    signal_id=
                        signal[
                            "signal_id"
                        ],

                    event_type=
                        "MONITOR_EXCEPTION",

                    source=
                        "monitor",

                    message=
                        str(
                            exc
                        )
                )


    finally:

        try:
            app.disconnect()

        except Exception:
            pass


def main():
    init_db()


    print(
        "TradingMax trade monitor started"
    )

    print(
        f"clientId="
        f"{IB_MONITOR_CLIENT_ID}"
    )

    print(
        f"interval="
        f"{TRADE_MONITOR_INTERVAL_SECONDS}s"
    )

    print(
        "event_store=enabled"
    )

    print(
        "identity="
        "orderRef -> permId -> "
        "positive-orderId -> broker parentId"
    )

    print(
        "directions=LONG,SHORT"
    )

    print(
        "completed_order_storage=list"
    )


    while True:

        try:

            monitor_once()


        except Exception as exc:

            print(
                f"MONITOR ERROR | "
                f"{exc}"
            )


        time.sleep(
            TRADE_MONITOR_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
