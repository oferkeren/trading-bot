import os
import threading
import time

from dotenv import load_dotenv
from ibapi.client import EClient
from ibapi.wrapper import EWrapper

load_dotenv()

HOST = os.getenv("IB_HOST", "127.0.0.1")
PORT = int(os.getenv("IB_PORT", "7496"))
CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "10"))
ACCOUNT = os.getenv("IB_ACCOUNT")


class App(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)
        self.ready = threading.Event()

    def nextValidId(self, orderId):
        print(f"\nConnected. Next order ID: {orderId}\n")
        self.ready.set()

    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState
    ):
        if order.account != ACCOUNT:
            return

        print(
            f"OPEN | "
            f"id={orderId} | "
            f"symbol={contract.symbol} | "
            f"action={order.action} | "
            f"qty={order.totalQuantity} | "
            f"type={order.orderType} | "
            f"status={orderState.status} | "
            f"parentId={order.parentId} | "
            f"permId={order.permId} | "
            f"clientId={order.clientId}"
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
            f"parentId={parentId} | "
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
        if errorCode in (2104, 2106, 2158):
            return

        print(
            f"IB MESSAGE | "
            f"id={reqId} | "
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
    print("Connection timeout")
    raise SystemExit(1)

time.sleep(5)

app.disconnect()
