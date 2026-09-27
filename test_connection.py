from ibapi.client import EClient
from ibapi.wrapper import EWrapper
import threading
import time


class IBApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

    def nextValidId(self, orderId: int):
        print(f"Connected. Next valid order ID: {orderId}")

    def managedAccounts(self, accountsList: str):
        print(f"Accounts: {accountsList}")

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        print(f"ERROR {errorCode}: {errorString}")


app = IBApp()

app.connect(
    host="127.0.0.1",
    port=7496,
    clientId=10
)

thread = threading.Thread(target=app.run, daemon=True)
thread.start()

time.sleep(5)

print("Connected:", app.isConnected())

app.disconnect()
