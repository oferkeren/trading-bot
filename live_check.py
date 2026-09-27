import os
import threading
import time

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract


load_dotenv()

HOST = os.getenv("IB_HOST", "127.0.0.1")
PORT = int(os.getenv("IB_PORT", "7496"))
CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "10"))
ACCOUNT = os.getenv("IB_ACCOUNT")


class IBApp(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.accounts = []
        self.connected_ready = False

        self.bid = None
        self.ask = None
        self.last = None

    def nextValidId(self, orderId):
        print(f"\n✅ Connected to TWS")
        print(f"Next Order ID: {orderId}")

        self.connected_ready = True

        self.reqPositions()

    def managedAccounts(self, accountsList):
        self.accounts = [
            a.strip()
            for a in accountsList.split(",")
            if a.strip()
        ]

        print("\nManaged accounts:")
        for account in self.accounts:
            print(f"  {account}")

        if ACCOUNT not in self.accounts:
            print("\n❌ Configured trading account not available.")
            self.disconnect()
            return

        print("\n✅ Configured trading account is available.")

    def position(
        self,
        account,
        contract,
        position,
        avgCost
    ):

        if account != ACCOUNT:
            return

        print(
            f"POSITION | "
            f"{contract.symbol:8} | "
            f"qty={position} | "
            f"avg={avgCost}"
        )

    def positionEnd(self):

        print("\n✅ Position download complete.")

        # Test market data using AAPL
        contract = Contract()

        contract.symbol = "AAPL"
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        # 1 = Live
        self.reqMarketDataType(1)

        self.reqMktData(
            1001,
            contract,
            "",
            False,
            False,
            []
        )

        print("\nRequesting LIVE AAPL quote...\n")

    def tickPrice(
        self,
        reqId,
        tickType,
        price,
        attrib
    ):

        if reqId != 1001:
            return

        # IB tick types
        # 1 = Bid
        # 2 = Ask
        # 4 = Last

        if tickType == 1:
            self.bid = price
            print(f"BID  : {price}")

        elif tickType == 2:
            self.ask = price
            print(f"ASK  : {price}")

        elif tickType == 4:
            self.last = price
            print(f"LAST : {price}")

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):

        # Normal informational messages
        if errorCode in (
            2104,
            2106,
            2158
        ):
            return

        print(
            f"IB MESSAGE | "
            f"id={reqId} | "
            f"code={errorCode} | "
            f"{errorString}"
        )


app = IBApp()

app.connect(
    HOST,
    PORT,
    clientId=CLIENT_ID
)

thread = threading.Thread(
    target=app.run,
    daemon=True
)

thread.start()

try:

    timeout = 20
    start = time.time()

    while time.time() - start < timeout:

        if (
            app.bid is not None
            or app.ask is not None
            or app.last is not None
        ):
            time.sleep(3)
            break

        time.sleep(0.25)

finally:

    if app.isConnected():

        try:
            app.cancelMktData(1001)
        except Exception:
            pass

        app.disconnect()

print("\nFinished.")
