import os
import json
import sqlite3
import threading
import time
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


def safe_float(
    value
):
    try:
        value = float(
            value
        )

        #
        # IBKR may use enormous sentinel-like
        # values for unavailable P/L fields.
        #
        if abs(value) > 1e100:
            return None

        return value

    except Exception:
        return None


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


class StatusApp(
    EWrapper,
    EClient
):

    def __init__(self):
        EClient.__init__(
            self,
            self
        )

        self.ready = threading.Event()

        self.positions_done = (
            threading.Event()
        )

        self.orders_done = (
            threading.Event()
        )

        self.account_summary_done = (
            threading.Event()
        )

        self.pnl_done = (
            threading.Event()
        )

        self.accounts = []

        self.positions = []

        self.open_orders = []

        self.account_values = {}

        self.daily_pnl = None

        self.unrealized_pnl = None

        self.realized_pnl = None

        self.errors = []


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

            "quantity":
                qty,

            "avg_cost":
                float(
                    avgCost
                )
        })


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
                )
        })


    def openOrderEnd(
        self
    ):
        self.orders_done.set()


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

        self.errors.append(
            f"{errorCode}: "
            f"{errorString}"
        )


def collect_snapshot():
    app = StatusApp()

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
        # ORDERS
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


        # ----------------------------------------------------
        # P/L
        # ----------------------------------------------------

        pnl_req_id = 8101

        app.reqPnL(
            pnl_req_id,
            IB_ACCOUNT,
            ""
        )

        if not app.pnl_done.wait(
            timeout=5
        ):
            raise RuntimeError(
                "P/L timeout"
            )


        try:
            app.cancelPnL(
                pnl_req_id
            )
        except Exception:
            pass


        error_text = (
            "; ".join(
                app.errors
            )
            if app.errors
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
            f"{app.daily_pnl}"
        )


    except Exception as exc:

        save_snapshot(
            connected=False,
            positions=[],
            open_orders=[],
            account_values={},
            daily_pnl=None,
            unrealized_pnl=None,
            realized_pnl=None,
            error=str(exc)
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


    while True:
        collect_snapshot()

        time.sleep(
            STATUS_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
