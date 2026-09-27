import os
import threading
import time

from dotenv import load_dotenv
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order

load_dotenv()

HOST = os.getenv("IB_HOST", "127.0.0.1")
PORT = int(os.getenv("IB_PORT", "7496"))
CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "10"))
ACCOUNT = os.getenv("IB_ACCOUNT")

SYMBOL = "AAPL"

class App(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)
        self.order_id = None

    def nextValidId(self, orderId):
        self.order_id = orderId

        print(f"Connected. Order ID: {orderId}")
        print(f"Account: {ACCOUNT}")

        contract = Contract()
        contract.symbol = SYMBOL
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        order = Order()
        order.account = ACCOUNT
        order.action = "BUY"
        order.orderType = "LMT"
        order.totalQuantity = 1
        order.lmtPrice = 1.00
        order.tif = "DAY"

        # Required with newer TWS versions
        order.eTradeOnly = False
        order.firmQuoteOnly = False

        print(f"Submitting test order: BUY 1 {SYMBOL} @ $1.00")
        self.placeOrder(orderId, contract, order)

    def openOrder(self, orderId, contract, order, orderState):
        print(
            f"OPEN ORDER | "
            f"id={orderId} "
            f"{order.action} "
            f"{order.totalQuantity} "
            f"{contract.symbol} "
            f"@ {order.lmtPrice} "
            f"status={orderState.status}"
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
            f"ORDER STATUS | "
            f"id={orderId} "
            f"status={status} "
            f"filled={filled} "
            f"remaining={remaining}"
        )

        if status in ("Submitted", "PreSubmitted"):
            print("Order accepted by IBKR. Cancelling...")
            self.cancelOrder(orderId, "")

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        if errorCode in (2104, 2106, 2158):
            return

        print(
            f"IB MESSAGE | "
            f"id={reqId} "
            f"code={errorCode} "
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

time.sleep(15)

app.disconnect()
