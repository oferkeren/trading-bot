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
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"


class CloseAllApp(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.ready = threading.Event()
        self.positions_done = threading.Event()

        self.next_order_id = None
        self.positions = []

    # --------------------------------------------------------
    # Connection
    # --------------------------------------------------------

    def nextValidId(self, orderId):

        self.next_order_id = orderId

        print()
        print("====================================")
        print("CONNECTED TO TWS")
        print("====================================")
        print(f"Account       : {ACCOUNT}")
        print(f"Next Order ID : {orderId}")
        print("====================================")
        print()

        self.ready.set()

    def managedAccounts(self, accountsList):

        accounts = [
            x.strip()
            for x in accountsList.split(",")
            if x.strip()
        ]

        print("Available accounts:", accounts)

        if ACCOUNT not in accounts:
            print(f"ERROR: account {ACCOUNT} is not available")
            self.disconnect()

    # --------------------------------------------------------
    # Positions
    # --------------------------------------------------------

    def position(
        self,
        account,
        contract,
        position,
        avgCost
    ):

        if account != ACCOUNT:
            return

        if float(position) == 0:
            return

        self.positions.append({
            "contract": contract,
            "position": position,
            "avgCost": avgCost
        })

        print(
            f"POSITION | "
            f"{contract.symbol:<8} | "
            f"qty={position:<10} | "
            f"avg={avgCost}"
        )

    def positionEnd(self):

        print()
        print("Position download complete.")
        print()

        self.positions_done.set()

    # --------------------------------------------------------
    # Orders
    # --------------------------------------------------------

    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState
    ):

        print(
            f"OPEN ORDER | "
            f"id={orderId} | "
            f"{order.action} "
            f"{order.totalQuantity} "
            f"{contract.symbol} | "
            f"type={order.orderType} | "
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
            f"id={orderId} | "
            f"status={status} | "
            f"filled={filled} | "
            f"remaining={remaining} | "
            f"avgFill={avgFillPrice}"
        )

    def execDetails(
        self,
        reqId,
        contract,
        execution
    ):

        print(
            f"FILL | "
            f"{contract.symbol} | "
            f"{execution.side} "
            f"{execution.shares} | "
            f"price={execution.price}"
        )

    # --------------------------------------------------------
    # New API 10.45 error signature
    # --------------------------------------------------------

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
            f"code={errorCode} | "
            f"{errorString}"
        )


def make_close_order(position):

    order = Order()

    order.account = ACCOUNT
    order.orderType = "MKT"
    order.tif = "DAY"

    #
    # Long position -> SELL
    # Short position -> BUY
    #
    if float(position) > 0:
        order.action = "SELL"
    else:
        order.action = "BUY"

    order.totalQuantity = abs(position)

    order.transmit = True
    order.outsideRth = False

    return order


def main():

    if not LIVE_TRADING:

        print()
        print("LIVE_TRADING=false")
        print("Closing positions is BLOCKED.")
        print()

        raise SystemExit(1)

    app = CloseAllApp()

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

    if not app.ready.wait(timeout=10):

        print("ERROR: TWS connection timeout.")

        app.disconnect()

        raise SystemExit(1)

    #
    # Get current positions
    #
    app.reqPositions()

    if not app.positions_done.wait(timeout=10):

        print("ERROR: position request timeout.")

        app.disconnect()

        raise SystemExit(1)

    app.cancelPositions()

    if not app.positions:

        print("No positions found.")
        app.disconnect()
        raise SystemExit(0)

    # --------------------------------------------------------
    # Show exactly what will happen
    # --------------------------------------------------------

    print()
    print("==========================================")
    print("        CLOSE ALL LIVE POSITIONS")
    print("==========================================")
    print(f"ACCOUNT: {ACCOUNT}")
    print()

    for p in app.positions:

        contract = p["contract"]
        qty = float(p["position"])

        action = (
            "SELL"
            if qty > 0
            else "BUY"
        )

        print(
            f"{contract.symbol:<10} "
            f"{action:<5} "
            f"{abs(qty)} "
            f"@ MARKET"
        )

    print()
    print("==========================================")
    print()
    print(
        "WARNING: These are REAL MARKET orders."
    )
    print(
        "If the market is closed, they may wait "
        "until the next eligible trading session."
    )
    print()

    confirmation = input(
        'Type exactly "SELL ALL" to continue: '
    )

    if confirmation != "SELL ALL":

        print()
        print("Nothing submitted.")
        app.disconnect()
        raise SystemExit(0)

    # --------------------------------------------------------
    # Submit closing orders
    # --------------------------------------------------------

    order_id = app.next_order_id

    print()
    print("Submitting closing orders...")
    print()

    for p in app.positions:

        contract = p["contract"]
        position = p["position"]

        order = make_close_order(
            position
        )

        print(
            f"SEND | "
            f"id={order_id} | "
            f"{order.action} "
            f"{order.totalQuantity} "
            f"{contract.symbol} "
            f"@ MARKET"
        )

        app.placeOrder(
            order_id,
            contract,
            order
        )

        order_id += 1

        time.sleep(0.5)

    print()
    print("All closing orders submitted.")
    print()
    print(
        "Monitoring order status. "
        "Press Ctrl+C only after checking TWS."
    )
    print(
        "Ctrl+C disconnects this program; "
        "it does NOT cancel submitted orders."
    )
    print()

    try:

        while True:
            time.sleep(1)

    except KeyboardInterrupt:

        print()
        print("Disconnecting from TWS...")

        app.disconnect()


if __name__ == "__main__":
    main()
