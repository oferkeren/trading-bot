import os
import json
import sqlite3
import threading
import time
import math
from datetime import datetime, timezone

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


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

STATUS_CLIENT_ID = int(
    os.getenv(
        "IB_STATUS_CLIENT_ID",
        "30"
    )
)

STATUS_INTERVAL_SECONDS = int(
    os.getenv(
        "STATUS_INTERVAL_SECONDS",
        "10"
    )
)

POSITION_PNL_TIMEOUT_SECONDS = float(
    os.getenv(
        "POSITION_PNL_TIMEOUT_SECONDS",
        "3"
    )
)

ACCOUNT_PNL_TIMEOUT_SECONDS = float(
    os.getenv(
        "ACCOUNT_PNL_TIMEOUT_SECONDS",
        "3"
    )
)


# ============================================================
# TIME
# ============================================================

def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10
    )

    conn.row_factory = (
        sqlite3.Row
    )

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
        for row
        in cur.fetchall()
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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_status (
            id INTEGER PRIMARY KEY CHECK(id=1),
            updated_at TEXT NOT NULL,
            tws_connected INTEGER NOT NULL,
            account TEXT,
            positions_json TEXT NOT NULL,
            open_orders_json TEXT NOT NULL,
            last_error TEXT
        )
        """
    )

    columns = {
        "account_values_json":
            "TEXT",

        "net_liquidation":
            "REAL",

        "available_funds":
            "REAL",

        "buying_power":
            "REAL",

        "total_cash_value":
            "REAL",

        "excess_liquidity":
            "REAL",

        "daily_pnl":
            "REAL",

        "unrealized_pnl":
            "REAL",

        "realized_pnl":
            "REAL"
    }

    for column, definition in (
        columns.items()
    ):
        ensure_column(
            conn,
            "runtime_status",
            column,
            definition
        )

    conn.commit()
    conn.close()


# ============================================================
# VALUES
# ============================================================

def safe_float(
    value
):
    try:
        value = float(
            value
        )

        if not math.isfinite(
            value
        ):
            return None

        #
        # Ignore IBKR sentinel-like values.
        #
        if abs(
            value
        ) > 1e100:
            return None

        return value

    except Exception:
        return None


def safe_order_price(
    value
):
    value = safe_float(
        value
    )

    if value is None:
        return None

    if value <= 0:
        return None

    return value


def normalize_account_update_pnl_key(
    key
):
    text = (
        str(
            key
            or ""
        )
        .strip()
    )

    if text.startswith(
        "$LEDGER-"
    ):
        text = text[
            len(
                "$LEDGER-"
            ):
        ]

    if text in {
        "RealizedPnL",
        "UnrealizedPnL",
    }:
        return text

    return None


def select_account_update_pnl_value(
    app,
    key
):
    for currency in (
        "BASE",
        "USD",
    ):
        value = (
            app.account_update_pnl_values.get(
                (
                    key,
                    currency
                )
            )
        )

        if value is not None:
            return value

    return None


def apply_account_update_pnl_fallback(
    app,
    soft_warnings
):
    if app.daily_pnl is not None:
        return False

    if app.positions:
        return False

    realized = select_account_update_pnl_value(
        app,
        "RealizedPnL"
    )

    unrealized = select_account_update_pnl_value(
        app,
        "UnrealizedPnL"
    )

    if (
        realized is None
        or
        unrealized is None
    ):
        return False

    app.daily_pnl = (
        realized
        +
        unrealized
    )

    if app.realized_pnl is None:
        app.realized_pnl = realized

    if app.unrealized_pnl is None:
        app.unrealized_pnl = unrealized

    soft_warnings.append(
        "Account daily P/L derived from account updates"
    )

    return True


# ============================================================
# SNAPSHOT STORAGE
# ============================================================

def save_snapshot(
    *,
    connected,
    positions,
    open_orders,
    account_values,
    daily_pnl,
    unrealized_pnl,
    realized_pnl,
    error
):
    conn = db_connect()

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO runtime_status (
            id,
            updated_at,
            tws_connected,
            account,

            positions_json,
            open_orders_json,
            account_values_json,

            net_liquidation,
            available_funds,
            buying_power,
            total_cash_value,
            excess_liquidity,

            daily_pnl,
            unrealized_pnl,
            realized_pnl,

            last_error
        )

        VALUES (
            1,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?
        )

        ON CONFLICT(id)
        DO UPDATE SET

            updated_at =
                excluded.updated_at,

            tws_connected =
                excluded.tws_connected,

            account =
                excluded.account,

            positions_json =
                excluded.positions_json,

            open_orders_json =
                excluded.open_orders_json,

            account_values_json =
                excluded.account_values_json,

            net_liquidation =
                excluded.net_liquidation,

            available_funds =
                excluded.available_funds,

            buying_power =
                excluded.buying_power,

            total_cash_value =
                excluded.total_cash_value,

            excess_liquidity =
                excluded.excess_liquidity,

            daily_pnl =
                excluded.daily_pnl,

            unrealized_pnl =
                excluded.unrealized_pnl,

            realized_pnl =
                excluded.realized_pnl,

            last_error =
                excluded.last_error
        """,
        (
            now_iso(),

            1
            if connected
            else 0,

            IB_ACCOUNT,

            json.dumps(
                positions,
                ensure_ascii=False
            ),

            json.dumps(
                open_orders,
                ensure_ascii=False
            ),

            json.dumps(
                account_values,
                ensure_ascii=False
            ),

            safe_float(
                account_values.get(
                    "NetLiquidation"
                )
            ),

            safe_float(
                account_values.get(
                    "AvailableFunds"
                )
            ),

            safe_float(
                account_values.get(
                    "BuyingPower"
                )
            ),

            safe_float(
                account_values.get(
                    "TotalCashValue"
                )
            ),

            safe_float(
                account_values.get(
                    "ExcessLiquidity"
                )
            ),

            safe_float(
                daily_pnl
            ),

            safe_float(
                unrealized_pnl
            ),

            safe_float(
                realized_pnl
            ),

            error
        )
    )

    conn.commit()
    conn.close()


def mark_snapshot_error(
    error
):
    """
    Hard collector failures must NOT replace a previously valid
    broker snapshot with fake empty data.

    We update only last_error.

    The existing snapshot keeps its original updated_at timestamp,
    therefore STATUS_MAX_AGE_SECONDS will naturally make it stale
    and fail closed if the collector cannot recover.
    """

    conn = db_connect()

    try:
        row = (
            conn.execute(
                """
                SELECT id
                FROM runtime_status
                WHERE id=1
                """
            )
            .fetchone()
        )

        if row is None:
            #
            # No valid snapshot has ever existed.
            #
            conn.execute(
                """
                INSERT INTO runtime_status (
                    id,
                    updated_at,
                    tws_connected,
                    account,
                    positions_json,
                    open_orders_json,
                    account_values_json,
                    last_error
                )

                VALUES (
                    1,
                    ?,
                    0,
                    ?,
                    '[]',
                    '[]',
                    '{}',
                    ?
                )
                """,
                (
                    now_iso(),
                    IB_ACCOUNT,
                    str(
                        error
                    ),
                )
            )

        else:
            conn.execute(
                """
                UPDATE runtime_status

                SET last_error = ?

                WHERE id=1
                """,
                (
                    str(
                        error
                    ),
                )
            )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# IBKR CLIENT
# ============================================================

class StatusApp(
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

        self.orders_done = (
            threading.Event()
        )

        self.account_summary_done = (
            threading.Event()
        )

        self.account_updates_done = (
            threading.Event()
        )

        self.pnl_done = (
            threading.Event()
        )

        self.position_pnl_done = (
            threading.Event()
        )

        self.position_pnl_req_to_index = {}

        self.position_pnl_pending = set()

        self.position_pnl_errors = []

        self.accounts = []

        self.positions = []

        self.open_orders = []

        self.account_values = {}

        self.account_update_pnl_values = {}

        self.daily_pnl = None

        self.unrealized_pnl = None

        self.realized_pnl = None

        self.errors = []


    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def nextValidId(
        self,
        orderId
    ):
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

        qty = float(
            position
        )

        if qty == 0:
            return

        self.positions.append({
            "symbol":
                contract.symbol.upper(),

            "con_id":
                int(
                    contract.conId
                ),

            "currency":
                getattr(
                    contract,
                    "currency",
                    None
                ),

            "quantity":
                qty,

            "avg_cost":
                float(
                    avgCost
                ),

            "market_price":
                None,

            "market_value":
                None,

            "daily_pnl":
                None,

            "unrealized_pnl":
                None,

            "realized_pnl":
                None,

            "position_pnl_available":
                False
        })


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

        order_type = (
            str(
                order.orderType
                or
                ""
            )
            .upper()
            .strip()
        )

        limit_price = (
            safe_order_price(
                getattr(
                    order,
                    "lmtPrice",
                    None
                )
            )
        )

        stop_price = (
            safe_order_price(
                getattr(
                    order,
                    "auxPrice",
                    None
                )
            )
        )

        display_price = None

        if (
            order_type
            in {
                "LMT",
                "LOC",
            }
        ):
            display_price = (
                limit_price
            )

        elif (
            order_type
            in {
                "STP",
                "STOP",
                "STP LMT",
            }
        ):
            display_price = (
                stop_price
            )

        self.open_orders.append({
            "order_id":
                orderId,

            "perm_id":
                getattr(
                    order,
                    "permId",
                    0
                ),

            "symbol":
                contract.symbol.upper(),

            "action":
                order.action,

            "order_type":
                order.orderType,

            "quantity":
                float(
                    order.totalQuantity
                ),

            "status":
                orderState.status,

            "parent_id":
                order.parentId,

            "order_ref":
                getattr(
                    order,
                    "orderRef",
                    ""
                ),

            "limit_price":
                limit_price,

            "stop_price":
                stop_price,

            "price":
                display_price,

            "tif":
                getattr(
                    order,
                    "tif",
                    None
                ),

            "outside_rth":
                bool(
                    getattr(
                        order,
                        "outsideRth",
                        False
                    )
                ),

            "transmit":
                bool(
                    getattr(
                        order,
                        "transmit",
                        False
                    )
                )
        })


    def openOrderEnd(
        self
    ):
        self.orders_done.set()


    # --------------------------------------------------------
    # ACCOUNT SUMMARY
    # --------------------------------------------------------

    def accountSummary(
        self,
        reqId,
        account,
        tag,
        value,
        currency
    ):
        if account != IB_ACCOUNT:
            return

        self.account_values[
            tag
        ] = value


    def accountSummaryEnd(
        self,
        reqId
    ):
        self.account_summary_done.set()


    # --------------------------------------------------------
    # ACCOUNT UPDATES
    # --------------------------------------------------------

    def updateAccountValue(
        self,
        key,
        val,
        currency,
        accountName
    ):
        if accountName != IB_ACCOUNT:
            return

        normalized_key = normalize_account_update_pnl_key(
            key
        )

        if normalized_key is None:
            return

        normalized_currency = (
            str(
                currency
                or ""
            )
            .strip()
            .upper()
        )

        self.account_update_pnl_values[
            (
                normalized_key,
                normalized_currency
            )
        ] = safe_float(
            val
        )


    def accountDownloadEnd(
        self,
        accountName
    ):
        if accountName != IB_ACCOUNT:
            return

        self.account_updates_done.set()


    # --------------------------------------------------------
    # ACCOUNT P/L
    # --------------------------------------------------------

    def pnl(
        self,
        reqId,
        dailyPnL,
        unrealizedPnL,
        realizedPnL
    ):
        self.daily_pnl = (
            safe_float(
                dailyPnL
            )
        )

        self.unrealized_pnl = (
            safe_float(
                unrealizedPnL
            )
        )

        self.realized_pnl = (
            safe_float(
                realizedPnL
            )
        )

        self.pnl_done.set()


    # --------------------------------------------------------
    # POSITION P/L
    # --------------------------------------------------------

    def pnlSingle(
        self,
        reqId,
        pos,
        dailyPnL,
        unrealizedPnL,
        realizedPnL,
        value
    ):
        index = (
            self.position_pnl_req_to_index.get(
                reqId
            )
        )

        if index is None:
            return

        if (
            index < 0
            or
            index >= len(
                self.positions
            )
        ):
            return

        item = (
            self.positions[
                index
            ]
        )

        market_value = (
            safe_float(
                value
            )
        )

        quantity = (
            safe_float(
                pos
            )
        )

        if quantity is None:
            quantity = (
                safe_float(
                    item.get(
                        "quantity"
                    )
                )
            )

        market_price = None

        if (
            market_value is not None
            and
            quantity is not None
            and
            quantity != 0
        ):
            market_price = abs(
                market_value
                /
                quantity
            )

        item[
            "market_price"
        ] = market_price

        item[
            "market_value"
        ] = market_value

        item[
            "daily_pnl"
        ] = (
            safe_float(
                dailyPnL
            )
        )

        item[
            "unrealized_pnl"
        ] = (
            safe_float(
                unrealizedPnL
            )
        )

        item[
            "realized_pnl"
        ] = (
            safe_float(
                realizedPnL
            )
        )

        item[
            "position_pnl_available"
        ] = any([
            market_price
            is not None,

            market_value
            is not None,

            item[
                "unrealized_pnl"
            ]
            is not None
        ])

        self.position_pnl_pending.discard(
            reqId
        )

        if not self.position_pnl_pending:
            self.position_pnl_done.set()


    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

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
            2109
        }:
            return

        if (
            reqId
            in self.position_pnl_req_to_index
        ):
            self.position_pnl_errors.append(
                f"reqId={reqId} "
                f"{errorCode}: "
                f"{errorString}"
            )

            self.position_pnl_pending.discard(
                reqId
            )

            if not self.position_pnl_pending:
                self.position_pnl_done.set()

            return

        self.errors.append(
            f"{errorCode}: "
            f"{errorString}"
        )


# ============================================================
# SNAPSHOT
# ============================================================

def collect_snapshot():
    app = StatusApp()

    soft_warnings = []

    try:
        app.connect(
            IB_HOST,
            IB_PORT,
            clientId=
                STATUS_CLIENT_ID
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


        # ----------------------------------------------------
        # POSITIONS
        # ----------------------------------------------------

        app.reqPositions()

        if not app.positions_done.wait(
            timeout=5
        ):
            raise RuntimeError(
                "Positions timeout"
            )


        # ----------------------------------------------------
        # PER-POSITION P/L
        #
        # Best effort only.
        # It is useful for the dashboard but it is NOT allowed
        # to destroy a valid broker/account snapshot.
        # ----------------------------------------------------

        if app.positions:
            base_req_id = 8200

            for index, item in enumerate(
                app.positions
            ):
                req_id = (
                    base_req_id
                    +
                    index
                )

                app.position_pnl_req_to_index[
                    req_id
                ] = index

                app.position_pnl_pending.add(
                    req_id
                )

                try:
                    app.reqPnLSingle(
                        req_id,
                        IB_ACCOUNT,
                        "",
                        int(
                            item[
                                "con_id"
                            ]
                        )
                    )

                except Exception as exc:
                    soft_warnings.append(
                        (
                            "Position P/L request "
                            f"failed reqId={req_id}: "
                            f"{exc}"
                        )
                    )

                    app.position_pnl_pending.discard(
                        req_id
                    )

            if app.position_pnl_pending:
                completed = (
                    app.position_pnl_done.wait(
                        timeout=
                            POSITION_PNL_TIMEOUT_SECONDS
                    )
                )

                if not completed:
                    soft_warnings.append(
                        (
                            "Position P/L timeout "
                            f"pending="
                            f"{len(app.position_pnl_pending)}"
                        )
                    )

            for req_id in list(
                app.position_pnl_req_to_index
                .keys()
            ):
                try:
                    app.cancelPnLSingle(
                        req_id
                    )

                except Exception:
                    pass


        # ----------------------------------------------------
        # OPEN ORDERS
        # ----------------------------------------------------

        app.reqAllOpenOrders()

        if not app.orders_done.wait(
            timeout=5
        ):
            raise RuntimeError(
                "Open orders timeout"
            )


        # ----------------------------------------------------
        # ACCOUNT SUMMARY
        #
        # This is critical. NetLiquidation and AvailableFunds
        # are part of the LIVE safety state.
        # ----------------------------------------------------

        account_req_id = 8001

        tags = (
            "NetLiquidation,"
            "AvailableFunds,"
            "BuyingPower,"
            "TotalCashValue,"
            "ExcessLiquidity"
        )

        app.reqAccountSummary(
            account_req_id,
            "All",
            tags
        )

        if not (
            app.account_summary_done.wait(
                timeout=5
            )
        ):
            raise RuntimeError(
                "Account summary timeout"
            )

        try:
            app.cancelAccountSummary(
                account_req_id
            )

        except Exception:
            pass


        net_liquidation = (
            safe_float(
                app.account_values.get(
                    "NetLiquidation"
                )
            )
        )

        available_funds = (
            safe_float(
                app.account_values.get(
                    "AvailableFunds"
                )
            )
        )

        if net_liquidation is None:
            raise RuntimeError(
                "NetLiquidation unavailable"
            )

        if available_funds is None:
            raise RuntimeError(
                "AvailableFunds unavailable"
            )


        # ----------------------------------------------------
        # ACCOUNT P/L
        #
        # IMPORTANT:
        #
        # reqPnL is allowed to time out here.
        #
        # The worker performs its own fresh reqPnL immediately
        # before any broker-side execution, so this collector
        # must not claim that TWS is offline merely because the
        # account P/L subscription was delayed.
        # ----------------------------------------------------

        pnl_req_id = 8101

        try:
            app.reqPnL(
                pnl_req_id,
                IB_ACCOUNT,
                ""
            )

            pnl_received = (
                app.pnl_done.wait(
                    timeout=
                        ACCOUNT_PNL_TIMEOUT_SECONDS
                )
            )

            if not pnl_received:
                soft_warnings.append(
                    "Account P/L timeout"
                )

        except Exception as exc:
            soft_warnings.append(
                (
                    "Account P/L request "
                    f"failed: {exc}"
                )
            )

        finally:
            try:
                app.cancelPnL(
                    pnl_req_id
                )

            except Exception:
                pass


        # ----------------------------------------------------
        # P/L FALLBACK
        #
        # If every position returned pnlSingle and account-level
        # reqPnL did not answer, use the sum only as snapshot
        # telemetry. The worker still does a fresh authoritative
        # account reqPnL before execution.
        # ----------------------------------------------------

        position_pnl_rows = [
            item

            for item
            in app.positions

            if item.get(
                "position_pnl_available"
            )
        ]

        if (
            app.daily_pnl is None
            and
            app.positions
            and
            len(
                position_pnl_rows
            )
            ==
            len(
                app.positions
            )
        ):
            daily_values = [
                item.get(
                    "daily_pnl"
                )

                for item
                in position_pnl_rows
            ]

            if all(
                value is not None
                for value
                in daily_values
            ):
                app.daily_pnl = sum(
                    daily_values
                )

                soft_warnings.append(
                    (
                        "Account daily P/L "
                        "derived from pnlSingle"
                    )
                )


        if (
            app.unrealized_pnl is None
            and
            position_pnl_rows
        ):
            values = [
                item.get(
                    "unrealized_pnl"
                )

                for item
                in position_pnl_rows

                if item.get(
                    "unrealized_pnl"
                )
                is not None
            ]

            if (
                len(values)
                ==
                len(
                    position_pnl_rows
                )
            ):
                app.unrealized_pnl = sum(
                    values
                )


        if (
            app.realized_pnl is None
            and
            position_pnl_rows
        ):
            values = [
                item.get(
                    "realized_pnl"
                )

                for item
                in position_pnl_rows

                if item.get(
                    "realized_pnl"
                )
                is not None
            ]

            if (
                len(values)
                ==
                len(
                    position_pnl_rows
                )
            ):
                app.realized_pnl = sum(
                    values
                )


        if (
            app.daily_pnl is None
            and
            not app.positions
        ):
            app.account_updates_done.clear()
            app.account_update_pnl_values = {}

            try:
                app.reqAccountUpdates(
                    True,
                    IB_ACCOUNT
                )

                app.account_updates_done.wait(
                    timeout=5
                )

                apply_account_update_pnl_fallback(
                    app,
                    soft_warnings
                )

            except Exception as exc:
                soft_warnings.append(
                    (
                        "Account update P/L fallback "
                        f"failed: {exc}"
                    )
                )

            finally:
                try:
                    app.reqAccountUpdates(
                        False,
                        IB_ACCOUNT
                    )

                except Exception:
                    pass


        # ----------------------------------------------------
        # SAVE VALID BROKER SNAPSHOT
        # ----------------------------------------------------

        warning_parts = []

        if app.errors:
            warning_parts.extend(
                app.errors
            )

        if app.position_pnl_errors:
            warning_parts.extend(
                app.position_pnl_errors
            )

        warning_parts.extend(
            soft_warnings
        )

        error_text = (
            "; ".join(
                warning_parts
            )
            if warning_parts
            else None
        )

        save_snapshot(
            connected=True,

            positions=
                app.positions,

            open_orders=
                app.open_orders,

            account_values=
                app.account_values,

            daily_pnl=
                app.daily_pnl,

            unrealized_pnl=
                app.unrealized_pnl,

            realized_pnl=
                app.realized_pnl,

            error=
                error_text
        )

        print(
            f"STATUS | "
            f"positions="
            f"{len(app.positions)} | "
            f"orders="
            f"{len(app.open_orders)} | "
            f"net_liq="
            f"{app.account_values.get('NetLiquidation')} | "
            f"available="
            f"{app.account_values.get('AvailableFunds')} | "
            f"daily_pnl="
            f"{app.daily_pnl} | "
            f"position_pnl="
            f"{sum(1 for item in app.positions if item.get('position_pnl_available'))}"
            f"/{len(app.positions)} | "
            f"warnings="
            f"{len(warning_parts)}"
        )

        if warning_parts:
            print(
                "STATUS WARN | "
                +
                "; ".join(
                    warning_parts
                )
            )


    except Exception as exc:
        #
        # Do NOT fabricate a disconnected empty account.
        #
        # Preserve the last real snapshot and let its age become
        # stale. That is precisely what the safety controller is
        # designed to detect.
        #
        mark_snapshot_error(
            str(
                exc
            )
        )

        print(
            f"STATUS ERROR | "
            f"{exc}"
        )


    finally:
        try:
            app.disconnect()

        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main():
    init_db()

    print(
        "TradingMax status collector started"
    )

    print(
        f"clientId="
        f"{STATUS_CLIENT_ID}"
    )

    print(
        f"interval="
        f"{STATUS_INTERVAL_SECONDS}s"
    )

    print(
        f"positionPnLTimeout="
        f"{POSITION_PNL_TIMEOUT_SECONDS}s"
    )

    print(
        f"accountPnLTimeout="
        f"{ACCOUNT_PNL_TIMEOUT_SECONDS}s"
    )

    while True:
        collect_snapshot()

        time.sleep(
            STATUS_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
