import os
import threading
import time

from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order


BASE_DIR = "/home/oferke/trading-bot"

load_dotenv(
    os.path.join(
        BASE_DIR,
        ".env",
    )
)


IB_HOST = os.getenv(
    "IB_HOST",
    "127.0.0.1",
)

IB_PORT = int(
    os.getenv(
        "IB_PORT",
        "7496",
    )
)

IB_ACCOUNT = "U26224108"

CLIENT_ID = 78

CONNECT_TIMEOUT = 7
POSITIONS_TIMEOUT = 7
ORDERS_TIMEOUT = 7
ACK_TIMEOUT = 15

ORDER_TYPE = "MKT"
TIF = "DAY"


class IBApp(
    EWrapper,
    EClient,
):

    def __init__(self):
        EClient.__init__(
            self,
            self,
        )

        self.ready = threading.Event()
        self.positions_done = threading.Event()
        self.open_orders_done = threading.Event()

        self.accounts = []

        self.next_order_id = None

        self.positions = {}

        self.open_orders = []

        self.order_status = {}
        self.order_status_events = {}

        self.errors = []


    # ========================================================
    # CONNECTION
    # ========================================================

    def nextValidId(
        self,
        orderId,
    ):
        self.next_order_id = (
            orderId
        )

        print(
            "NEXT_VALID_ID=",
            orderId,
            flush=True,
        )

        self.ready.set()


    def managedAccounts(
        self,
        accountsList,
    ):
        self.accounts = [
            item.strip()

            for item
            in accountsList.split(
                ","
            )

            if item.strip()
        ]

        print(
            "ACCOUNTS=",
            ",".join(
                self.accounts
            ),
            flush=True,
        )


    # ========================================================
    # POSITIONS
    # ========================================================

    def position(
        self,
        account,
        contract,
        position,
        avgCost,
    ):
        if (
            account
            !=
            IB_ACCOUNT
        ):
            return


        quantity = float(
            position
        )


        if quantity == 0:
            return


        symbol = (
            contract.symbol
            .strip()
            .upper()
        )


        self.positions[
            symbol
        ] = {
            "symbol":
                symbol,

            "quantity":
                quantity,

            "avg_cost":
                float(
                    avgCost
                ),

            "secType":
                contract.secType,

            "currency":
                contract.currency,
        }


    def positionEnd(
        self,
    ):
        self.positions_done.set()


    # ========================================================
    # OPEN ORDERS
    # ========================================================

    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState,
    ):
        if (
            order.account
            !=
            IB_ACCOUNT
        ):
            return


        self.open_orders.append(
            {
                "order_id":
                    orderId,

                "symbol":
                    contract.symbol,

                "action":
                    order.action,

                "quantity":
                    float(
                        order.totalQuantity
                    ),

                "order_type":
                    order.orderType,

                "status":
                    orderState.status,

                "parent_id":
                    order.parentId,

                "order_ref":
                    getattr(
                        order,
                        "orderRef",
                        "",
                    ),
            }
        )


    def openOrderEnd(
        self,
    ):
        self.open_orders_done.set()


    # ========================================================
    # ORDER STATUS
    # ========================================================

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
        mktCapPrice,
    ):
        self.order_status[
            orderId
        ] = {
            "status":
                status,

            "filled":
                float(
                    filled
                ),

            "remaining":
                float(
                    remaining
                ),

            "avg_fill_price":
                float(
                    avgFillPrice
                ),

            "perm_id":
                permId,
        }


        print(
            "ORDER_STATUS |",
            orderId,
            "|",
            status,
            "| filled=",
            filled,
            "| remaining=",
            remaining,
            "| avg=",
            avgFillPrice,
            flush=True,
        )


        event = (
            self.order_status_events
            .get(
                orderId
            )
        )


        if event is not None:
            event.set()


    # ========================================================
    # ERRORS
    # ========================================================

    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson="",
    ):
        if errorCode in {
            2104,
            2106,
            2158,
            2109,
        }:
            return


        print(
            "IB_ERROR |",
            reqId,
            "|",
            errorCode,
            "|",
            errorString,
            flush=True,
        )


        self.errors.append(
            (
                reqId,
                errorCode,
                errorString,
            )
        )


def is_regular_market_hours():
    now_ny = datetime.now(
        ZoneInfo(
            "America/New_York"
        )
    )


    if now_ny.weekday() >= 5:
        return False


    current_minutes = (
        now_ny.hour
        *
        60
        +
        now_ny.minute
    )


    open_minutes = (
        9
        *
        60
        +
        30
    )

    close_minutes = (
        16
        *
        60
    )


    return (
        open_minutes
        <=
        current_minutes
        <
        close_minutes
    )


def make_contract(
    symbol,
):
    contract = Contract()

    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    return contract


def make_close_order(
    order_id,
    symbol,
    quantity,
):
    order = Order()

    order.orderId = (
        order_id
    )

    order.account = (
        IB_ACCOUNT
    )

    order.action = "SELL"

    order.orderType = (
        ORDER_TYPE
    )

    order.totalQuantity = (
        quantity
    )

    order.tif = (
        TIF
    )

    order.outsideRth = False

    order.transmit = True

    order.orderRef = (
        f"TM:MANUAL-CLOSE:{symbol}"
    )

    return order


def main():
    print(
        "======================================"
    )

    print(
        "      TRADINGMAX CLOSE POSITIONS"
    )

    print(
        "======================================"
    )

    print(
        "ACCOUNT=",
        IB_ACCOUNT
    )

    print(
        "ORDER_TYPE=",
        ORDER_TYPE
    )

    print(
        "OUTSIDE_RTH=False"
    )


    if not is_regular_market_hours():

        now_ny = datetime.now(
            ZoneInfo(
                "America/New_York"
            )
        )

        print()
        print(
            "MARKET_TIME_NY=",
            now_ny.isoformat()
        )

        raise SystemExit(
            "BLOCKED: US regular market is closed"
        )


    app = IBApp()


    try:
        app.connect(
            IB_HOST,
            IB_PORT,
            clientId=
                CLIENT_ID,
        )


        thread = threading.Thread(
            target=
                app.run,

            daemon=True,
        )

        thread.start()


        if not app.ready.wait(
            timeout=
                CONNECT_TIMEOUT
        ):
            raise RuntimeError(
                "IBKR connection timeout"
            )


        time.sleep(
            0.5
        )


        if (
            IB_ACCOUNT
            not in
            app.accounts
        ):
            raise RuntimeError(
                "Trading account unavailable"
            )


        # ====================================================
        # POSITION SNAPSHOT
        # ====================================================

        app.reqPositions()


        if not app.positions_done.wait(
            timeout=
                POSITIONS_TIMEOUT
        ):
            raise RuntimeError(
                "Position snapshot timeout"
            )


        try:
            app.cancelPositions()

        except Exception:
            pass


        # ====================================================
        # OPEN ORDER SNAPSHOT
        # ====================================================

        app.reqOpenOrders()


        if not app.open_orders_done.wait(
            timeout=
                ORDERS_TIMEOUT
        ):
            raise RuntimeError(
                "Open-order snapshot timeout"
            )


        print()
        print(
            "===== CURRENT POSITIONS ====="
        )


        if not app.positions:
            print(
                "NONE"
            )

            print()
            print(
                "NOTHING TO CLOSE"
            )

            return


        for position in (
            app.positions.values()
        ):
            print(
                position
            )


        print()
        print(
            "===== OPEN ORDERS ====="
        )


        if app.open_orders:
            for item in (
                app.open_orders
            ):
                print(
                    item
                )


            raise RuntimeError(
                "BLOCKED: existing broker "
                "orders detected"
            )


        print(
            "NONE"
        )


        # ====================================================
        # SAFETY VALIDATION
        # ====================================================

        positions_to_close = []


        for symbol, position in (
            app.positions.items()
        ):

            quantity = (
                position[
                    "quantity"
                ]
            )


            if (
                position[
                    "secType"
                ]
                !=
                "STK"
            ):
                raise RuntimeError(
                    f"BLOCKED: {symbol} "
                    "is not STK"
                )


            if (
                position[
                    "currency"
                ]
                !=
                "USD"
            ):
                raise RuntimeError(
                    f"BLOCKED: {symbol} "
                    "is not USD"
                )


            if quantity < 0:
                raise RuntimeError(
                    f"BLOCKED: {symbol} "
                    "is already SHORT"
                )


            if quantity == 0:
                continue


            positions_to_close.append(
                {
                    "symbol":
                        symbol,

                    "quantity":
                        quantity,

                    "avg_cost":
                        position[
                            "avg_cost"
                        ],
                }
            )


        if not positions_to_close:
            print(
                "NO LONG POSITIONS TO CLOSE"
            )

            return


        print()
        print(
            "===== CLOSE PLAN ====="
        )


        for position in (
            positions_to_close
        ):
            print(
                f"SELL "
                f"{position['quantity']} "
                f"{position['symbol']} "
                f"MKT"
            )


        print()
        print(
            "TOTAL_POSITIONS=",
            len(
                positions_to_close
            )
        )


        # ====================================================
        # FINAL FRESH SNAPSHOT
        # ====================================================

        app.positions = {}

        app.positions_done.clear()

        app.reqPositions()


        if not app.positions_done.wait(
            timeout=
                POSITIONS_TIMEOUT
        ):
            raise RuntimeError(
                "Final position snapshot timeout"
            )


        try:
            app.cancelPositions()

        except Exception:
            pass


        for planned in (
            positions_to_close
        ):
            symbol = (
                planned[
                    "symbol"
                ]
            )

            planned_qty = (
                planned[
                    "quantity"
                ]
            )


            current = (
                app.positions
                .get(
                    symbol
                )
            )


            if current is None:
                raise RuntimeError(
                    f"BLOCKED: {symbol} "
                    "position disappeared"
                )


            current_qty = (
                current[
                    "quantity"
                ]
            )


            if (
                current_qty
                !=
                planned_qty
            ):
                raise RuntimeError(
                    f"BLOCKED: {symbol} "
                    f"quantity changed "
                    f"{planned_qty} -> "
                    f"{current_qty}"
                )


            if current_qty <= 0:
                raise RuntimeError(
                    f"BLOCKED: {symbol} "
                    "is no longer LONG"
                )


        # ====================================================
        # SUBMIT
        # ====================================================

        print()
        print(
            "===== SUBMITTING CLOSE ORDERS ====="
        )


        next_order_id = (
            app.next_order_id
        )


        if next_order_id is None:
            raise RuntimeError(
                "No IBKR order ID available"
            )


        submitted = []


        for index, position in enumerate(
            positions_to_close
        ):
            symbol = (
                position[
                    "symbol"
                ]
            )

            quantity = (
                position[
                    "quantity"
                ]
            )

            order_id = (
                next_order_id
                +
                index
            )


            contract = make_contract(
                symbol
            )


            order = make_close_order(
                order_id=
                    order_id,

                symbol=
                    symbol,

                quantity=
                    quantity,
            )


            app.order_status_events[
                order_id
            ] = (
                threading.Event()
            )


            print(
                "PLACE_ORDER |",
                order_id,
                "| SELL",
                quantity,
                symbol,
                "| MKT",
                flush=True,
            )


            app.placeOrder(
                order_id,
                contract,
                order,
            )


            submitted.append(
                {
                    "order_id":
                        order_id,

                    "symbol":
                        symbol,

                    "quantity":
                        quantity,
                }
            )


            time.sleep(
                0.25
            )


        # ====================================================
        # WAIT FOR BROKER ACK
        # ====================================================

        print()
        print(
            "===== BROKER ACK ====="
        )


        for item in submitted:

            order_id = (
                item[
                    "order_id"
                ]
            )


            event = (
                app.order_status_events[
                    order_id
                ]
            )


            event.wait(
                timeout=
                    ACK_TIMEOUT
            )


            status = (
                app.order_status
                .get(
                    order_id
                )
            )


            print(
                item[
                    "symbol"
                ],
                "=",
                status,
            )


        fatal_errors = [
            error

            for error
            in app.errors

            if error[
                1
            ]
            not in {
                2104,
                2106,
                2158,
                2109,
            }
        ]


        print()
        print(
            "===== RESULT ====="
        )

        print(
            "ORDERS_SUBMITTED=",
            len(
                submitted
            )
        )

        print(
            "FATAL_ERRORS=",
            fatal_errors
        )


        if fatal_errors:
            raise RuntimeError(
                "One or more broker errors occurred"
            )


        print()
        print(
            "CLOSE ORDERS SENT SUCCESSFULLY"
        )


    finally:

        try:
            app.cancelPositions()

        except Exception:
            pass


        try:
            app.disconnect()

        except Exception:
            pass


        time.sleep(
            0.5
        )


if __name__ == "__main__":
    main()
