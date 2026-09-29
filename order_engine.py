import os
import sys
import time
import threading

from dotenv import load_dotenv
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order

from signal_contract import (
    normalize_action,
    trade_direction,
    validate_price_structure,
    SignalContractError,
)


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

HOST = os.getenv(
    "IB_HOST",
    "127.0.0.1"
)

PORT = int(
    os.getenv(
        "IB_PORT",
        "7496"
    )
)

CLIENT_ID = int(
    os.getenv(
        "IB_CLIENT_ID",
        "10"
    )
)

ACCOUNT = os.getenv(
    "IB_ACCOUNT"
)


LIVE_TRADING = (
    os.getenv(
        "LIVE_TRADING",
        "false"
    ).lower()
    == "true"
)


MAX_POSITION_USD = float(
    os.getenv(
        "MAX_POSITION_USD",
        "500"
    )
)

MAX_TRADES_PER_DAY = int(
    os.getenv(
        "MAX_TRADES_PER_DAY",
        "5"
    )
)

MAX_OPEN_POSITIONS = int(
    os.getenv(
        "MAX_OPEN_POSITIONS",
        "3"
    )
)


# ============================================================
# IBKR APP
# ============================================================

class TradingApp(
    EWrapper,
    EClient
):

    def __init__(
        self
    ):
        EClient.__init__(
            self,
            self
        )

        self.next_order_id = None

        self.ready = threading.Event()

        self.account_ok = (
            threading.Event()
        )

        self.accounts = []

        self.positions = {}

        self.open_orders = []


    # --------------------------------------------------------
    # Connection ready
    # --------------------------------------------------------

    def nextValidId(
        self,
        orderId
    ):
        self.next_order_id = (
            orderId
        )

        print()
        print(
            "===================================="
        )
        print(
            "CONNECTED TO TWS"
        )
        print(
            "===================================="
        )
        print(
            f"Account       : "
            f"{ACCOUNT}"
        )
        print(
            f"Next Order ID : "
            f"{orderId}"
        )
        print(
            "===================================="
        )
        print()

        self.ready.set()


    # --------------------------------------------------------
    # Accounts
    # --------------------------------------------------------

    def managedAccounts(
        self,
        accountsList
    ):
        self.accounts = [
            account.strip()

            for account
            in accountsList.split(
                ","
            )

            if account.strip()
        ]


        print(
            "Available accounts:"
        )


        for account in self.accounts:

            print(
                f"  {account}"
            )


        print()


        if (
            ACCOUNT
            not in self.accounts
        ):

            print(
                f"ERROR: configured account "
                f"{ACCOUNT} is not available."
            )

            self.disconnect()

            return


        print(
            f"Account {ACCOUNT} verified."
        )

        print()


        self.account_ok.set()


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


        self.positions[
            contract.symbol
        ] = {
            "quantity":
                position,

            "avg_cost":
                avgCost
        }


        print(
            f"POSITION | "
            f"{contract.symbol:<8} "
            f"qty={position:<10} "
            f"avg={avgCost}"
        )


    def positionEnd(
        self
    ):
        print()

        print(
            "Position download complete."
        )

        print()


    # --------------------------------------------------------
    # Open orders
    # --------------------------------------------------------

    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState
    ):
        self.open_orders.append({
            "order_id":
                orderId,

            "symbol":
                contract.symbol,

            "action":
                order.action,

            "type":
                order.orderType,

            "quantity":
                order.totalQuantity,

            "status":
                orderState.status
        })


        print(
            f"OPEN ORDER | "
            f"id={orderId} | "
            f"{order.action} "
            f"{order.totalQuantity} "
            f"{contract.symbol} | "
            f"type={order.orderType} | "
            f"status={orderState.status}"
        )


    def openOrderEnd(
        self
    ):
        print()

        print(
            "Open order download complete."
        )

        print()


    # --------------------------------------------------------
    # Order status
    # --------------------------------------------------------

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
            f"avgFill={avgFillPrice} | "
            f"parent={parentId}"
        )


    # --------------------------------------------------------
    # Executions
    # --------------------------------------------------------

    def execDetails(
        self,
        reqId,
        contract,
        execution
    ):
        print()

        print(
            f"FILL | "
            f"{contract.symbol} | "
            f"shares={execution.shares} | "
            f"price={execution.price}"
        )

        print()


    # --------------------------------------------------------
    # Errors / messages
    # --------------------------------------------------------

    def error(
        self,
        reqId,
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


# ============================================================
# CONTRACT
# ============================================================

def create_stock_contract(
    symbol
):
    contract = Contract()

    contract.symbol = (
        symbol.upper()
    )

    contract.secType = (
        "STK"
    )

    contract.exchange = (
        "SMART"
    )

    contract.currency = (
        "USD"
    )

    return contract


# ============================================================
# ORDER HELPERS
# ============================================================

def create_base_order():
    order = Order()

    order.account = (
        ACCOUNT
    )


    #
    # Compatibility with the ibapi version currently installed
    #
    order.eTradeOnly = False

    order.firmQuoteOnly = False


    return order


# ============================================================
# BRACKET ORDER
# ============================================================

def create_bracket_order(
    parent_id,
    quantity,
    action,
    entry_price,
    target_price,
    stop_price,
    outside_rth=False
):
    try:
        structure = (
            validate_price_structure(
                action=
                    action,

                entry=
                    entry_price,

                stop=
                    stop_price,

                target=
                    target_price
            )
        )

    except SignalContractError as exc:

        raise ValueError(
            str(
                exc
            )
        ) from exc


    entry_action = (
        structure[
            "action"
        ]
    )

    exit_action = (
        "SELL"

        if entry_action
        == "BUY"

        else "BUY"
    )


    # --------------------------------------------------------
    # Parent ENTRY
    # --------------------------------------------------------

    parent = create_base_order()

    parent.orderId = (
        parent_id
    )

    parent.action = (
        entry_action
    )

    parent.orderType = (
        "LMT"
    )

    parent.totalQuantity = (
        quantity
    )

    parent.lmtPrice = (
        entry_price
    )

    parent.tif = (
        "DAY"
    )

    parent.outsideRth = (
        outside_rth
    )

    parent.transmit = (
        False
    )


    # --------------------------------------------------------
    # TAKE PROFIT
    # --------------------------------------------------------

    take_profit = (
        create_base_order()
    )

    take_profit.orderId = (
        parent_id
        +
        1
    )

    take_profit.action = (
        exit_action
    )

    take_profit.orderType = (
        "LMT"
    )

    take_profit.totalQuantity = (
        quantity
    )

    take_profit.lmtPrice = (
        target_price
    )

    take_profit.parentId = (
        parent_id
    )

    take_profit.tif = (
        "GTC"
    )

    take_profit.outsideRth = (
        False
    )

    take_profit.transmit = (
        False
    )


    # --------------------------------------------------------
    # STOP LOSS
    # --------------------------------------------------------

    stop_loss = (
        create_base_order()
    )

    stop_loss.orderId = (
        parent_id
        +
        2
    )

    stop_loss.action = (
        exit_action
    )

    stop_loss.orderType = (
        "STP"
    )

    stop_loss.totalQuantity = (
        quantity
    )

    stop_loss.auxPrice = (
        stop_price
    )

    stop_loss.parentId = (
        parent_id
    )

    stop_loss.tif = (
        "GTC"
    )

    stop_loss.outsideRth = (
        False
    )

    #
    # Last order transmits entire bracket
    #
    stop_loss.transmit = (
        True
    )


    return [
        parent,
        take_profit,
        stop_loss
    ]


# ============================================================
# VALIDATION
# ============================================================

def validate_trade(
    symbol,
    quantity,
    action,
    entry,
    target,
    stop
):
    if not ACCOUNT:

        raise ValueError(
            "IB_ACCOUNT missing from .env"
        )


    if not symbol:

        raise ValueError(
            "Symbol is missing"
        )


    if quantity <= 0:

        raise ValueError(
            (
                "Quantity must be "
                "greater than zero"
            )
        )


    try:
        structure = (
            validate_price_structure(
                action=
                    action,

                entry=
                    entry,

                stop=
                    stop,

                target=
                    target
            )
        )

    except SignalContractError as exc:

        raise ValueError(
            str(
                exc
            )
        ) from exc


    position_value = (
        quantity
        *
        entry
    )


    if (
        position_value
        >
        MAX_POSITION_USD
    ):

        raise ValueError(
            (
                f"Position value "
                f"${position_value:.2f} "
                f"exceeds "
                f"MAX_POSITION_USD="
                f"${MAX_POSITION_USD:.2f}"
            )
        )


    return structure


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # CLI validation
    # --------------------------------------------------------

    if len(
        sys.argv
    ) != 7:

        print()

        print(
            "Usage:"
        )

        print(
            "python order_engine.py "
            "ACTION SYMBOL QTY ENTRY TARGET STOP"
        )

        print()


        print(
            "LONG example:"
        )

        print(
            "python order_engine.py "
            "BUY AAPL 1 100.00 110.00 95.00"
        )

        print()


        print(
            "SHORT example:"
        )

        print(
            "python order_engine.py "
            "SELL AAPL 1 100.00 90.00 105.00"
        )

        print()

        sys.exit(
            1
        )


    # --------------------------------------------------------
    # Parse input
    # --------------------------------------------------------

    action = normalize_action(
        sys.argv[
            1
        ]
    )

    direction = trade_direction(
        action
    )


    symbol = (
        sys.argv[
            2
        ].upper()
    )


    quantity = int(
        sys.argv[
            3
        ]
    )


    entry = float(
        sys.argv[
            4
        ]
    )


    target = float(
        sys.argv[
            5
        ]
    )


    stop = float(
        sys.argv[
            6
        ]
    )


    # --------------------------------------------------------
    # LIVE switch
    # --------------------------------------------------------

    if not LIVE_TRADING:

        print()

        print(
            "LIVE_TRADING=false"
        )

        print(
            "Order execution blocked."
        )

        print()

        sys.exit(
            1
        )


    # --------------------------------------------------------
    # Validate
    # --------------------------------------------------------

    structure = validate_trade(
        symbol,
        quantity,
        action,
        entry,
        target,
        stop
    )


    position_value = (
        quantity
        *
        entry
    )


    risk_per_share = (
        structure[
            "risk_per_share"
        ]
    )


    reward_per_share = (
        structure[
            "reward_per_share"
        ]
    )


    total_risk = (
        risk_per_share
        *
        quantity
    )


    potential_profit = (
        reward_per_share
        *
        quantity
    )


    if total_risk > 0:

        reward_risk = (
            potential_profit
            /
            total_risk
        )

    else:

        reward_risk = (
            0
        )


    # --------------------------------------------------------
    # Show order
    # --------------------------------------------------------

    print()

    print(
        "========================================"
    )

    print(
        "             LIVE ORDER"
    )

    print(
        "========================================"
    )


    print(
        f"ACCOUNT        : "
        f"{ACCOUNT}"
    )

    print(
        f"SYMBOL         : "
        f"{symbol}"
    )

    print(
        f"ACTION         : "
        f"{action}"
    )

    print(
        f"DIRECTION      : "
        f"{direction}"
    )

    print(
        f"QUANTITY       : "
        f"{quantity}"
    )

    print(
        f"ENTRY          : "
        f"${entry:.4f}"
    )

    print(
        f"TARGET         : "
        f"${target:.4f}"
    )

    print(
        f"STOP           : "
        f"${stop:.4f}"
    )

    print(
        f"POSITION VALUE : "
        f"${position_value:.2f}"
    )

    print(
        f"MAX LOSS       : "
        f"${total_risk:.2f}"
    )

    print(
        f"TARGET PROFIT  : "
        f"${potential_profit:.2f}"
    )

    print(
        f"REWARD/RISK    : "
        f"{reward_risk:.2f}"
    )


    print(
        "========================================"
    )

    print()


    # --------------------------------------------------------
    # Human confirmation
    # --------------------------------------------------------

    confirmation = input(
        'Type "LIVE" to submit REAL orders: '
    )


    if confirmation != "LIVE":

        print()

        print(
            "Order cancelled locally."
        )

        sys.exit(
            0
        )


    # --------------------------------------------------------
    # Connect
    # --------------------------------------------------------

    app = TradingApp()


    app.connect(
        HOST,
        PORT,
        clientId=
            CLIENT_ID
    )


    api_thread = (
        threading.Thread(
            target=
                app.run,

            daemon=
                True
        )
    )


    api_thread.start()


    # --------------------------------------------------------
    # Wait for TWS
    # --------------------------------------------------------

    if not app.ready.wait(
        timeout=10
    ):

        print(
            "ERROR: TWS connection timeout."
        )

        app.disconnect()

        sys.exit(
            1
        )


    if not app.account_ok.wait(
        timeout=5
    ):

        print(
            "ERROR: Account validation failed."
        )

        app.disconnect()

        sys.exit(
            1
        )


    # --------------------------------------------------------
    # Download positions and orders
    # --------------------------------------------------------

    app.reqPositions()

    app.reqOpenOrders()


    time.sleep(
        2
    )


    # --------------------------------------------------------
    # Prevent duplicate position
    # --------------------------------------------------------

    existing_position = (
        app.positions.get(
            symbol
        )
    )


    if existing_position:

        existing_qty = (
            existing_position[
                "quantity"
            ]
        )


        if existing_qty != 0:

            print()

            print(
                f"BLOCKED: existing position "
                f"in {symbol}: "
                f"{existing_qty}"
            )

            app.disconnect()

            sys.exit(
                1
            )


    # --------------------------------------------------------
    # Prevent duplicate open orders
    # --------------------------------------------------------

    duplicate_orders = [
        order

        for order
        in app.open_orders

        if (
            order[
                "symbol"
            ]
            ==
            symbol
        )
    ]


    if duplicate_orders:

        print()

        print(
            f"BLOCKED: existing open "
            f"order found for {symbol}"
        )


        for order in duplicate_orders:

            print(
                order
            )


        app.disconnect()

        sys.exit(
            1
        )


    # --------------------------------------------------------
    # Create bracket
    # --------------------------------------------------------

    contract = create_stock_contract(
        symbol
    )


    orders = create_bracket_order(
        parent_id=
            app.next_order_id,

        quantity=
            quantity,

        action=
            action,

        entry_price=
            entry,

        target_price=
            target,

        stop_price=
            stop,

        outside_rth=
            False
    )


    # --------------------------------------------------------
    # Send
    # --------------------------------------------------------

    print()

    print(
        "Submitting bracket order..."
    )

    print()


    for order in orders:

        if (
            order.orderType
            == "LMT"
        ):

            price = (
                order.lmtPrice
            )


        elif (
            order.orderType
            == "STP"
        ):

            price = (
                order.auxPrice
            )


        else:

            price = (
                "-"
            )


        print(
            f"SEND | "
            f"id={order.orderId} | "
            f"{order.action} | "
            f"{order.orderType} | "
            f"qty={order.totalQuantity} | "
            f"price={price} | "
            f"parent={order.parentId}"
        )


        app.placeOrder(
            order.orderId,
            contract,
            order
        )


        time.sleep(
            0.25
        )


    print()

    print(
        "Bracket submitted to TWS."
    )

    print(
        "The process will remain connected "
        "so order updates are displayed."
    )

    print(
        "Ctrl+C disconnects this program."
    )

    print(
        "IMPORTANT: Ctrl+C does NOT cancel "
        "orders already submitted to IBKR."
    )

    print()


    # --------------------------------------------------------
    # Monitor
    # --------------------------------------------------------

    try:

        while True:

            time.sleep(
                1
            )


    except KeyboardInterrupt:

        print()

        print(
            "Disconnecting from TWS..."
        )

        app.disconnect()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
