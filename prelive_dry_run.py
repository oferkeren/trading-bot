import math
import os
import threading
import time

from datetime import datetime, timezone

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

from execution_guard import (
    check_pre_execution,
    ExecutionBlocked
)

from execution_freshness import (
    check_execution_freshness,
    SignalFreshnessError
)

from market_guard import (
    evaluate_liquid_hours,
    MarketSessionError
)

from position_policy import (
    evaluate_position_limits,
    PositionPolicyError
)


# ============================================================
# ENV
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db"
)

load_dotenv(
    os.path.join(
        BASE_DIR,
        ".env"
    )
)


IB_HOST = os.getenv(
    "IB_HOST",
    "127.0.0.1"
)

IB_PORT = int(
    os.getenv(
        "IB_PORT",
        "7496"
    )
)

IB_ACCOUNT = os.getenv(
    "IB_ACCOUNT",
    ""
).strip()


#
# Separate diagnostic client.
#
IB_CLIENT_ID = int(
    os.getenv(
        "IB_PRELIVE_CLIENT_ID",
        "61"
    )
)


MAX_MANAGED_POSITIONS = int(
    os.getenv(
        "MAX_MANAGED_POSITIONS",
        os.getenv(
            "MAX_OPEN_POSITIONS",
            "3"
        )
    )
)


MAX_TOTAL_BROKER_POSITIONS = int(
    os.getenv(
        "MAX_TOTAL_BROKER_POSITIONS",
        "10"
    )
)


MAX_TRADES_PER_DAY = int(
    os.getenv(
        "MAX_TRADES_PER_DAY",
        "5"
    )
)


MAX_DAILY_LOSS_USD = float(
    os.getenv(
        "MAX_DAILY_LOSS_USD",
        "100"
    )
)


MIN_ACCOUNT_EQUITY_USD = float(
    os.getenv(
        "MIN_ACCOUNT_EQUITY_USD",
        "100"
    )
)


STATUS_MAX_AGE_SECONDS = int(
    os.getenv(
        "STATUS_MAX_AGE_SECONDS",
        "30"
    )
)


MAX_SIGNAL_AGE_SECONDS = int(
    os.getenv(
        "MAX_SIGNAL_AGE_SECONDS",
        "60"
    )
)


MAX_SIGNAL_FUTURE_SKEW_SECONDS = int(
    os.getenv(
        "MAX_SIGNAL_FUTURE_SKEW_SECONDS",
        "10"
    )
)


BLOCK_LIVE_ON_PENDING_CANCEL = (
    os.getenv(
        "BLOCK_LIVE_ON_PENDING_CANCEL",
        "true"
    ).lower()
    == "true"
)


REQUIRE_LIQUID_SESSION = (
    os.getenv(
        "REQUIRE_LIQUID_SESSION",
        "true"
    ).lower()
    == "true"
)


# ============================================================
# CONTRACT
# ============================================================

def stock_contract(
    symbol
):
    contract = Contract()

    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    return contract


# ============================================================
# IBKR READ-ONLY CLIENT
# ============================================================

class DryRunIB(
    EWrapper,
    EClient
):

    def __init__(self):
        EClient.__init__(
            self,
            self
        )

        self.ready = threading.Event()

        self.positions_done = (
            threading.Event()
        )

        self.open_orders_done = (
            threading.Event()
        )

        self.pnl_done = (
            threading.Event()
        )

        self.contract_details_done = (
            threading.Event()
        )

        self.accounts = []

        self.positions = []

        self.open_orders = []

        self.daily_pnl = None

        self.contract_details = []


    def nextValidId(
        self,
        orderId
    ):
        self.ready.set()


    def managedAccounts(
        self,
        accountsList
    ):
        self.accounts = [
            account.strip()
            for account
            in accountsList.split(",")
            if account.strip()
        ]


    # --------------------------------------------------------
    # POSITIONS
    # --------------------------------------------------------

    def position(
        self,
        account,
        contract,
        position,
        avgCost
    ):
        if account != IB_ACCOUNT:
            return


        quantity = float(
            position
        )


        if quantity == 0:
            return


        self.positions.append({
            "account":
                account,

            "symbol":
                contract.symbol.upper(),

            "quantity":
                quantity,

            "avg_cost":
                float(
                    avgCost
                )
        })


    def positionEnd(
        self
    ):
        self.positions_done.set()


    # --------------------------------------------------------
    # ALL OPEN ORDERS
    # --------------------------------------------------------

    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState
    ):
        if (
            order.account
            != IB_ACCOUNT
        ):
            return


        self.open_orders.append({
            "order_id":
                orderId,

            "perm_id":
                getattr(
                    order,
                    "permId",
                    0
                ),

            "symbol":
                (
                    contract.symbol.upper()
                    if contract.symbol
                    else ""
                ),

            "status":
                orderState.status,

            "parent_id":
                getattr(
                    order,
                    "parentId",
                    0
                ),

            "order_ref":
                getattr(
                    order,
                    "orderRef",
                    ""
                )
                or "",

            "action":
                getattr(
                    order,
                    "action",
                    ""
                ),

            "order_type":
                getattr(
                    order,
                    "orderType",
                    ""
                )
        })


    def openOrderEnd(
        self
    ):
        self.open_orders_done.set()


    # --------------------------------------------------------
    # P/L
    # --------------------------------------------------------

    def pnl(
        self,
        reqId,
        dailyPnL,
        unrealizedPnL,
        realizedPnL
    ):
        try:
            value = float(
                dailyPnL
            )

            if (
                math.isfinite(
                    value
                )
                and
                abs(
                    value
                ) <= 1e100
            ):
                self.daily_pnl = value

            else:
                self.daily_pnl = None

        except Exception:
            self.daily_pnl = None


        self.pnl_done.set()


    # --------------------------------------------------------
    # CONTRACT DETAILS
    # --------------------------------------------------------

    def contractDetails(
        self,
        reqId,
        contractDetails
    ):
        self.contract_details.append(
            contractDetails
        )


    def contractDetailsEnd(
        self,
        reqId
    ):
        self.contract_details_done.set()


    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

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
            2158,
            2109
        }:
            return


        print(
            f"IBKR {errorCode}: "
            f"{errorString}"
        )


# ============================================================
# CONNECTION
# ============================================================

def connect_ib():
    if not IB_ACCOUNT:
        raise RuntimeError(
            "IB_ACCOUNT is not configured"
        )


    app = DryRunIB()


    app.connect(
        IB_HOST,
        IB_PORT,
        clientId=
            IB_CLIENT_ID
    )


    thread = threading.Thread(
        target=app.run,
        daemon=True
    )

    thread.start()


    if not app.ready.wait(
        timeout=5
    ):
        raise RuntimeError(
            "IBKR connection timeout"
        )


    time.sleep(
        0.3
    )


    if (
        IB_ACCOUNT
        not in app.accounts
    ):
        raise RuntimeError(
            (
                "Configured account "
                "not available from IBKR"
            )
        )


    return app


# ============================================================
# FRESH BROKER DATA
# ============================================================

def read_positions(
    app
):
    app.positions = []

    app.positions_done.clear()


    app.reqPositions()


    if not app.positions_done.wait(
        timeout=5
    ):
        raise RuntimeError(
            "IBKR positions timeout"
        )


    try:
        app.cancelPositions()

    except Exception:
        pass


    return app.positions


def read_all_open_orders(
    app
):
    app.open_orders = []

    app.open_orders_done.clear()


    #
    # Important:
    # account-wide inspection, not only clientId 61 orders.
    #
    app.reqAllOpenOrders()


    if not app.open_orders_done.wait(
        timeout=5
    ):
        raise RuntimeError(
            "IBKR all-open-orders timeout"
        )


    return app.open_orders


def read_daily_pnl(
    app
):
    request_id = 6101


    app.daily_pnl = None

    app.pnl_done.clear()


    app.reqPnL(
        request_id,
        IB_ACCOUNT,
        ""
    )


    if not app.pnl_done.wait(
        timeout=5
    ):
        raise RuntimeError(
            "IBKR daily P/L timeout"
        )


    try:
        app.cancelPnL(
            request_id
        )

    except Exception:
        pass


    if app.daily_pnl is None:
        raise RuntimeError(
            "IBKR daily P/L unavailable"
        )


    return app.daily_pnl


# ============================================================
# MARKET
# ============================================================

def market_check(
    app,
    symbol
):
    app.contract_details = []

    app.contract_details_done.clear()


    app.reqContractDetails(
        6102,
        stock_contract(
            symbol
        )
    )


    if not app.contract_details_done.wait(
        timeout=5
    ):
        raise MarketSessionError(
            "IBKR contract details timeout"
        )


    candidates = []


    for details in (
        app.contract_details
    ):

        contract = (
            details.contract
        )


        if (
            str(
                contract.symbol
            ).upper()
            == symbol.upper()

            and

            contract.secType
            == "STK"

            and

            contract.currency
            == "USD"
        ):
            candidates.append(
                details
            )


    if not candidates:
        raise MarketSessionError(
            (
                "No matching USD stock "
                f"contract for {symbol}"
            )
        )


    selected = (
        candidates[0]
    )


    result = evaluate_liquid_hours(
        liquid_hours=
            getattr(
                selected,
                "liquidHours",
                None
            ),

        timezone_name=
            getattr(
                selected,
                "timeZoneId",
                None
            )
    )


    result[
        "symbol"
    ] = symbol


    result[
        "primary_exchange"
    ] = getattr(
        selected.contract,
        "primaryExchange",
        ""
    )


    return result


# ============================================================
# OUTPUT
# ============================================================

def title(
    value
):
    print()

    print(
        "=" * 72
    )

    print(
        value
    )

    print(
        "=" * 72
    )


# ============================================================
# MAIN
# ============================================================

def main():
    blockers = []


    title(
        "TRADINGMAX PRE-LIVE DRY RUN"
    )


    print(
        "MODE: READ ONLY"
    )

    print(
        "placeOrder(): NOT PRESENT"
    )

    print(
        "cancelOrder(): NOT PRESENT"
    )

    print(
        f"Account: {IB_ACCOUNT}"
    )


    # --------------------------------------------------------
    # EXISTING SNAPSHOT GUARD
    # --------------------------------------------------------

    title(
        "1. EXECUTION GUARD"
    )


    try:
        result = check_pre_execution(
            db_file=
                DB_FILE,

            max_snapshot_age_seconds=
                STATUS_MAX_AGE_SECONDS,

            max_open_positions=
                MAX_MANAGED_POSITIONS,

            max_trades_per_day=
                MAX_TRADES_PER_DAY,

            max_daily_loss_usd=
                MAX_DAILY_LOSS_USD,

            minimum_account_equity=
                MIN_ACCOUNT_EQUITY_USD,

            block_on_pending_cancel=
                BLOCK_LIVE_ON_PENDING_CANCEL
        )


        print(
            "PASS"
        )

        print(
            f"Managed positions : "
            f"{result['managed_positions']}"
            f"/{MAX_MANAGED_POSITIONS}"
        )

        print(
            f"Legacy positions  : "
            f"{result['legacy_positions']}"
        )

        print(
            f"Total positions   : "
            f"{result['total_broker_positions']}"
            f"/{MAX_TOTAL_BROKER_POSITIONS}"
        )

        print(
            f"Trades today      : "
            f"{result['trades_today']}"
            f"/{MAX_TRADES_PER_DAY}"
        )


    except ExecutionBlocked as exc:

        print(
            "BLOCKED"
        )

        print(
            exc
        )

        blockers.append(
            f"Execution guard: {exc}"
        )


    app = None


    try:

        # ----------------------------------------------------
        # CONNECT
        # ----------------------------------------------------

        title(
            "2. TWS CONNECTION"
        )


        app = connect_ib()


        print(
            "PASS"
        )

        print(
            f"Client ID : "
            f"{IB_CLIENT_ID}"
        )


        # ----------------------------------------------------
        # POSITIONS
        # ----------------------------------------------------

        title(
            "3. FRESH TWS POSITIONS"
        )


        positions = read_positions(
            app
        )


        try:
            position_state = (
                evaluate_position_limits(
                    db_file=
                        DB_FILE,

                    broker_positions=
                        positions,

                    max_managed_positions=
                        MAX_MANAGED_POSITIONS,

                    max_total_broker_positions=
                        MAX_TOTAL_BROKER_POSITIONS
                )
            )


            print(
                f"Broker positions  : "
                f"{position_state['broker_position_count']}"
            )

            print(
                f"Managed positions : "
                f"{position_state['managed_position_count']}"
                f"/{MAX_MANAGED_POSITIONS}"
            )

            print(
                f"Legacy positions  : "
                f"{position_state['legacy_position_count']}"
            )

            print(
                f"Emergency total   : "
                f"{position_state['broker_position_count']}"
                f"/{MAX_TOTAL_BROKER_POSITIONS}"
            )


            if position_state[
                "blockers"
            ]:

                for blocker in position_state[
                    "blockers"
                ]:

                    print(
                        f"BLOCKED: {blocker}"
                    )

                    blockers.append(
                        blocker
                    )

            else:

                print(
                    "PASS"
                )


        except PositionPolicyError as exc:

            print(
                f"BLOCKED: {exc}"
            )

            blockers.append(
                f"Fresh position policy: {exc}"
            )


        # ----------------------------------------------------
        # OPEN ORDERS
        # ----------------------------------------------------

        title(
            "4. ALL OPEN ORDERS"
        )


        orders = read_all_open_orders(
            app
        )


        print(
            f"Count: {len(orders)}"
        )


        pending = []


        for order in orders:

            print(
                f"id="
                f"{order['order_id']} "
                f"symbol="
                f"{order['symbol']} "
                f"status="
                f"{order['status']} "
                f"permId="
                f"{order['perm_id']} "
                f"ref="
                f"{order['order_ref'] or '-'}"
            )


            if (
                order[
                    "status"
                ]
                == "PendingCancel"
            ):

                pending.append(
                    order
                )


        if pending:

            message = (
                f"{len(pending)} "
                "PendingCancel order(s)"
            )

            print(
                f"BLOCKED: {message}"
            )

            blockers.append(
                message
            )

        else:

            print(
                "PASS: no PendingCancel"
            )


        # ----------------------------------------------------
        # P/L
        # ----------------------------------------------------

        title(
            "5. FRESH DAILY P/L"
        )


        daily_pnl = read_daily_pnl(
            app
        )


        print(
            f"Daily P/L: "
            f"{daily_pnl:.2f}"
        )


        if (
            daily_pnl
            <=
            -abs(
                MAX_DAILY_LOSS_USD
            )
        ):

            message = (
                "Daily loss limit reached"
            )

            print(
                f"BLOCKED: {message}"
            )

            blockers.append(
                message
            )

        else:

            print(
                "PASS"
            )


        # ----------------------------------------------------
        # FRESHNESS
        # ----------------------------------------------------

        title(
            "6. SYNTHETIC SIGNAL FRESHNESS"
        )


        synthetic_signal_time = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )


        try:
            freshness = (
                check_execution_freshness(
                    signal_time=
                        synthetic_signal_time,

                    max_age_seconds=
                        MAX_SIGNAL_AGE_SECONDS,

                    max_future_skew_seconds=
                        MAX_SIGNAL_FUTURE_SKEW_SECONDS
                )
            )


            print(
                "PASS"
            )

            print(
                f"Signal age: "
                f"{freshness['age_seconds']:.3f}s"
            )


        except SignalFreshnessError as exc:

            print(
                f"BLOCKED: {exc}"
            )

            blockers.append(
                f"Freshness: {exc}"
            )


        # ----------------------------------------------------
        # MARKET SESSION
        # ----------------------------------------------------

        title(
            "7. MARKET SESSION"
        )


        if not REQUIRE_LIQUID_SESSION:

            print(
                "SKIPPED: "
                "REQUIRE_LIQUID_SESSION=false"
            )


        else:

            #
            # Use a liquid, unambiguous US stock
            # solely for calendar/session discovery.
            #
            try:

                session = market_check(
                    app,
                    "MSFT"
                )


                print(
                    f"Symbol       : "
                    f"{session['symbol']}"
                )

                print(
                    f"Timezone     : "
                    f"{session['timezone']}"
                )

                print(
                    f"Local time   : "
                    f"{session['local_time']}"
                )

                print(
                    f"Reason       : "
                    f"{session['reason']}"
                )

                print(
                    f"Session open : "
                    f"{session['is_open']}"
                )


                if not session[
                    "is_open"
                ]:

                    blockers.append(
                        (
                            "Market liquid session "
                            "is currently closed"
                        )
                    )

                    print(
                        "BLOCKED"
                    )

                else:

                    print(
                        "PASS"
                    )


            except Exception as exc:

                print(
                    f"BLOCKED: {exc}"
                )

                blockers.append(
                    f"Market session: {exc}"
                )


    except Exception as exc:

        blockers.append(
            f"TWS dry-run failure: {exc}"
        )

        print(
            f"DRY RUN ERROR: {exc}"
        )


    finally:

        if (
            app is not None
            and
            app.isConnected()
        ):

            app.disconnect()


    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    title(
        "PRE-LIVE RESULT"
    )


    unique = []


    for blocker in blockers:

        if blocker not in unique:
            unique.append(
                blocker
            )


    if unique:

        print(
            "RESULT: BLOCKED"
        )

        print()


        for blocker in unique:

            print(
                f" - {blocker}"
            )


    else:

        print(
            "RESULT: ALL READ-ONLY CHECKS PASSED"
        )


    print()

    print(
        "NO ORDERS WERE SUBMITTED."
    )


if __name__ == "__main__":
    main()
