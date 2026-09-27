import os
import threading
import time

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


load_dotenv()

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7496"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "10"))
IB_ACCOUNT = os.getenv("IB_ACCOUNT")


class App(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.ready = threading.Event()
        self.open_done = threading.Event()
        self.all_done = threading.Event()

        self.mode = None
        self.orders = []


    def nextValidId(self, orderId):
        print(
            f"CONNECTED | "
            f"clientId={IB_CLIENT_ID} | "
            f"nextOrderId={orderId}"
        )

        self.ready.set()


    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState
    ):
        if order.account != IB_ACCOUNT:
            return

        self.orders.append({
            "mode": self.mode,
            "order_id": orderId,
            "perm_id": order.permId,
            "symbol": contract.symbol,
            "action": order.action,
            "type": order.orderType,
            "status": orderState.status,
            "parent_id": order.parentId,
            "transmit": order.transmit
        })


    def openOrderEnd(self):
        if self.mode == "OPEN":
            self.open_done.set()

        elif self.mode == "ALL":
            self.all_done.set()


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
            f"permId={permId} | "
            f"clientId={clientId} | "
            f"status={status} | "
            f"parent={parentId} | "
            f"filled={filled} | "
            f"remaining={remaining}"
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

        print(
            f"IB | "
            f"id={reqId} | "
            f"code={errorCode} | "
            f"{errorString}"
        )


app = App()

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

if not app.ready.wait(5):
    raise RuntimeError(
        "Could not connect to TWS"
    )

time.sleep(0.5)


print()
print("=== reqOpenOrders() ===")

app.mode = "OPEN"
app.orders.clear()

app.reqOpenOrders()
app.open_done.wait(5)

for order in app.orders:
    print(
        f"id={order['order_id']} "
        f"permId={order['perm_id']} "
        f"{order['symbol']} "
        f"{order['action']} "
        f"type={order['type']} "
        f"status={order['status']} "
        f"parent={order['parent_id']} "
        f"transmit={order['transmit']}"
    )


print()
print("=== reqAllOpenOrders() ===")

app.mode = "ALL"
app.orders.clear()

app.reqAllOpenOrders()
app.all_done.wait(5)

for order in app.orders:
    print(
        f"id={order['order_id']} "
        f"permId={order['perm_id']} "
        f"{order['symbol']} "
        f"{order['action']} "
        f"type={order['type']} "
        f"status={order['status']} "
        f"parent={order['parent_id']} "
        f"transmit={order['transmit']}"
    )


app.disconnect()
