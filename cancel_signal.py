import os
import sys
import sqlite3
import threading
import time

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.order_cancel import OrderCancel


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

IB_CLIENT_ID = int(
    os.getenv(
        "IB_CLIENT_ID",
        "10"
    )
)

IB_ACCOUNT = os.getenv(
    "IB_ACCOUNT"
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


def get_signal(signal_id):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM signals
        WHERE signal_id = ?
        """,
        (signal_id,)
    )

    row = cur.fetchone()

    conn.close()

    if row is None:
        return None

    return dict(row)


def update_signal(
    signal_id,
    status,
    error_message=None
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE signals
        SET
            status = ?,
            updated_at = datetime('now'),
            error_message = ?
        WHERE signal_id = ?
        """,
        (
            status,
            error_message,
            signal_id
        )
    )

    conn.commit()
    conn.close()


# ============================================================
# IBKR
# ============================================================

class IBApp(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(
            self,
            self
        )

        self.ready = threading.Event()
        self.open_orders_done = threading.Event()

        self.accounts = []

        self.orders = {}

        self.target_order_ids = set()
        self.cancelled_order_ids = set()

        self.errors = []


    def nextValidId(
        self,
        orderId
    ):
        print(
            f"IBKR CONNECTED | "
            f"clientId={IB_CLIENT_ID} | "
            f"nextOrderId={orderId}"
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

        self.orders[
            orderId
        ] = {
            "symbol":
                contract.symbol,

            "status":
                orderState.status,

            "parent_id":
                order.parentId,

            "action":
                order.action,

            "order_type":
                order.orderType,

            "quantity":
                order.totalQuantity
        }


    def openOrderEnd(self):
        self.open_orders_done.set()


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
            f"clientId={clientId} | "
            f"parent={parentId}"
        )

        if (
            orderId
            not in self.target_order_ids
        ):
            return

        if status in {
            "Cancelled",
            "ApiCancelled"
        }:
            self.cancelled_order_ids.add(
                orderId
            )


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
            2158
        }:
            return

        message = (
            f"IBKR {errorCode}: "
            f"{errorString}"
        )

        print(
            f"IB | "
            f"id={reqId} | "
            f"{message}"
        )

        if (
            reqId
            in self.target_order_ids
        ):
            self.errors.append(
                f"order {reqId}: "
                f"{message}"
            )


# ============================================================
# MAIN
# ============================================================

def main():
    if len(sys.argv) != 2:
        print(
            "Usage:"
        )

        print(
            "  python cancel_signal.py "
            "<signal_id>"
        )

        sys.exit(1)


    signal_id = (
        sys.argv[1]
    )


    signal = get_signal(
        signal_id
    )


    if signal is None:
        print(
            f"Signal not found: "
            f"{signal_id}"
        )

        sys.exit(1)


    parent_id = signal[
        "parent_order_id"
    ]


    if parent_id is None:
        print(
            f"Signal {signal_id} "
            f"has no parent_order_id"
        )

        sys.exit(1)


    expected_ids = {
        parent_id,
        parent_id + 1,
        parent_id + 2
    }


    print(
        f"SIGNAL: {signal_id}"
    )

    print(
        f"SYMBOL: "
        f"{signal['symbol']}"
    )

    print(
        f"DB STATUS: "
        f"{signal['status']}"
    )

    print(
        f"CLIENT ID: "
        f"{IB_CLIENT_ID}"
    )

    print(
        f"EXPECTED ORDERS: "
        f"{sorted(expected_ids)}"
    )


    app = IBApp()


    try:
        app.connect(
            IB_HOST,
            IB_PORT,
            clientId=IB_CLIENT_ID
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


        time.sleep(0.5)


        if (
            IB_ACCOUNT
            not in app.accounts
        ):
            raise RuntimeError(
                "Configured account unavailable"
            )


        app.reqOpenOrders()


        if not app.open_orders_done.wait(
            timeout=5
        ):
            raise RuntimeError(
                "Open-order request timeout"
            )


        found_ids = (
            expected_ids.intersection(
                app.orders.keys()
            )
        )


        print(
            f"FOUND ORDERS: "
            f"{sorted(found_ids)}"
        )


        if not found_ids:
            print(
                "No matching open orders "
                "were found."
            )

            sys.exit(2)


        for order_id in sorted(
            found_ids
        ):
            order = (
                app.orders[
                    order_id
                ]
            )

            print(
                f"FOUND | "
                f"id={order_id} | "
                f"{order['symbol']} | "
                f"{order['action']} | "
                f"qty={order['quantity']} | "
                f"type={order['order_type']} | "
                f"status={order['status']} | "
                f"parent={order['parent_id']}"
            )


        if (
            found_ids
            != expected_ids
        ):
            print()
            print(
                "Partial bracket detected."
            )

            print(
                "Cancellation aborted."
            )

            update_signal(
                signal_id,
                "UNKNOWN",
                (
                    "Partial bracket detected "
                    "before cancellation"
                )
            )

            sys.exit(3)


        print()
        print(
            "Type CANCEL to confirm:"
        )

        confirmation = input(
            "> "
        ).strip()


        if confirmation != "CANCEL":
            print(
                "Cancellation aborted."
            )

            sys.exit(0)


        app.target_order_ids = (
            set(expected_ids)
        )


        order_cancel = (
            OrderCancel()
        )


        # Children first, parent last.
        cancel_sequence = [
            parent_id + 2,
            parent_id + 1,
            parent_id
        ]


        for order_id in (
            cancel_sequence
        ):
            print(
                f"CANCELLING | "
                f"order={order_id}"
            )

            app.cancelOrder(
                order_id,
                order_cancel
            )

            time.sleep(0.3)


        deadline = (
            time.time()
            + 10
        )


        while (
            time.time()
            < deadline
        ):
            if (
                app.cancelled_order_ids
                == app.target_order_ids
            ):
                break

            time.sleep(0.1)


        if (
            app.cancelled_order_ids
            == app.target_order_ids
        ):
            message = (
                "IBKR confirmed cancellation "
                f"of bracket orders "
                f"{sorted(expected_ids)}"
            )

            update_signal(
                signal_id,
                "CANCELLED",
                message
            )

            print()
            print(
                "CANCEL SUCCESS"
            )

            print(
                message
            )

            return


        message = (
            "Cancellation not fully "
            "confirmed. "
            f"expected="
            f"{sorted(expected_ids)}, "
            f"confirmed="
            f"{sorted(app.cancelled_order_ids)}"
        )


        if app.errors:
            message += (
                " | "
                + "; ".join(
                    app.errors
                )
            )


        update_signal(
            signal_id,
            "CANCEL_PENDING",
            message
        )


        print()
        print(
            "CANCEL NOT FULLY CONFIRMED"
        )

        print(
            message
        )


    finally:
        try:
            app.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
