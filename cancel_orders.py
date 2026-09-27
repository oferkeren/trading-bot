import os
import threading
import time

from dotenv import load_dotenv
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.order_cancel import OrderCancel


load_dotenv()

HOST = os.getenv("IB_HOST", "127.0.0.1")
PORT = int(os.getenv("IB_PORT", "7496"))
CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "10"))
ACCOUNT = os.getenv("IB_ACCOUNT")


class App(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.ready = threading.Event()
        self.open_orders = {}

    def nextValidId(self, orderId):
        print()
        print("Connected to TWS")
        print(f"Next valid order ID: {orderId}")
        print()

        self.ready.set()

    def managedAccounts(self, accountsList):
        print(f"Accounts: {accountsList}")

    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState
    ):
        if order.account != ACCOUNT:
            return

        self.open_orders[orderId] = {
            "symbol": contract.symbol,
            "action": order.action,
            "qty": order.totalQuantity,
            "status": orderState.status,
            "account": order.account
        }

        print(
            f"OPEN ORDER | "
            f"id={orderId} | "
            f"{order.action} "
            f"{order.totalQuantity} "
            f"{contract.symbol} | "
            f"status={orderState.status} | "
            f"account={order.account}"
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
        mktCapPrice
    ):
        print(
            f"STATUS | "
            f"id={orderId} | "
            f"status={status} | "
            f"filled={filled} | "
            f"remaining={remaining} | "
            f"permId={permId} | "
            f"clientId={clientId}"
        )

    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        if errorCode in (
            2104,
            2106,
            2158
        ):
            return

        print(
            f"IB MESSAGE | "
            f"id={reqId} | "
            f"time={errorTime} | "
            f"code={errorCode} | "
            f"{errorString}"
        )


app = App()

app.connect(
    HOST,
    PORT,
    clientId=CLIENT_ID
)

threading.Thread(
    target=app.run,
    daemon=True
).start()


if not app.ready.wait(timeout=10):
    print("TWS connection timeout.")
    app.disconnect()
    raise SystemExit(1)


#
# Give TWS a moment to send existing orders
#
time.sleep(3)


if not app.open_orders:
    print("No open orders found.")
    app.disconnect()
    raise SystemExit(0)


print()
print("Orders detected:")
print()

for order_id, info in app.open_orders.items():

    print(
        f"ID {order_id}: "
        f"{info['symbol']} "
        f"{info['action']} "
        f"{info['qty']} "
        f"status={info['status']}"
    )


print()

confirmation = input(
    'Type "CANCEL" to cancel these orders: '
)

if confirmation != "CANCEL":
    print("Nothing cancelled.")
    app.disconnect()
    raise SystemExit(0)


print()

for order_id, info in list(app.open_orders.items()):

    if info["status"] not in (
        "Cancelled",
        "Filled",
        "Inactive"
    ):

        print(
            f"Sending cancel for order {order_id}..."
        )

        cancel = OrderCancel()

        app.cancelOrder(
            order_id,
            cancel
        )

        time.sleep(0.5)


print()
print("Waiting for cancellation confirmation...")
print()

time.sleep(10)

app.disconnect()
