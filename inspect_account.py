import os
import threading
import time

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


load_dotenv()

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7496"))
IB_ACCOUNT = os.getenv("IB_ACCOUNT")


class App(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.ready = threading.Event()
        self.positions_done = threading.Event()
        self.orders_done = threading.Event()

        self.positions = []
        self.orders = []

    def nextValidId(self, orderId):
        self.ready.set()

    def position(
        self,
        account,
        contract,
        position,
        avgCost
    ):
        if account != IB_ACCOUNT:
            return

        if float(position) == 0:
            return

        self.positions.append({
            "symbol": contract.symbol,
            "quantity": position,
            "avg_cost": avgCost
        })

    def positionEnd(self):
        self.positions_done.set()

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
            "order_id": orderId,
            "symbol": contract.symbol,
            "action": order.action,
            "quantity": order.totalQuantity,
            "type": order.orderType,
            "status": orderState.status,
            "parent_id": order.parentId
        })

    def openOrderEnd(self):
        self.orders_done.set()

    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        if errorCode in (2104, 2106, 2158):
            return

        print(
            f"IB ERROR | reqId={reqId} | "
            f"code={errorCode} | {errorString}"
        )


app = App()

app.connect(
    IB_HOST,
    IB_PORT,
    clientId=77
)

thread = threading.Thread(
    target=app.run,
    daemon=True
)

thread.start()

if not app.ready.wait(5):
    raise RuntimeError("Could not connect to TWS")

time.sleep(0.5)

app.reqPositions()
app.positions_done.wait(5)

app.reqOpenOrders()
app.orders_done.wait(5)

print()
print(f"ACCOUNT: {IB_ACCOUNT}")

print()
print("=== POSITIONS ===")

if not app.positions:
    print("No open positions")
else:
    for p in app.positions:
        print(
            f"{p['symbol']:8} "
            f"qty={p['quantity']} "
            f"avg={p['avg_cost']}"
        )

print()
print("=== OPEN ORDERS ===")

if not app.orders:
    print("No open orders")
else:
    for o in app.orders:
        print(
            f"id={o['order_id']} "
            f"{o['symbol']} "
            f"{o['action']} "
            f"qty={o['quantity']} "
            f"type={o['type']} "
            f"status={o['status']} "
            f"parent={o['parent_id']}"
        )

app.disconnect()
