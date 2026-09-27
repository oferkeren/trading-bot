from ibapi.client import EClient
from ibapi.wrapper import EWrapper

import threading
import time


class IBApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

    def nextValidId(self, orderId):
        print(f"Connected. Next order ID: {orderId}")

        self.reqAccountSummary(
            9001,
            "All",
            "AccountType,NetLiquidation,TotalCashValue,"
            "AvailableFunds,BuyingPower,Currency"
        )

    def accountSummary(
        self,
        reqId,
        account,
        tag,
        value,
        currency
    ):
        print(
            f"{account:12} "
            f"{tag:20} "
            f"{value:15} "
            f"{currency}"
        )

    def accountSummaryEnd(self, reqId):
        print("\nAccount summary complete.")
        self.cancelAccountSummary(reqId)
        self.disconnect()

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        # Ignore normal IBKR connection-status messages
        if errorCode in (2104, 2106, 2158):
            return

        print(
            f"ERROR "
            f"reqId={reqId} "
            f"code={errorCode}: "
            f"{errorString}"
        )


app = IBApp()

app.connect(
    "127.0.0.1",
    7496,
    clientId=10
)

thread = threading.Thread(
    target=app.run,
    daemon=True
)

thread.start()

while app.isConnected():
    time.sleep(0.5)
