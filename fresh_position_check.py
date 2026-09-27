import os
import threading
import time

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

from position_policy import (
    evaluate_position_limits,
    PositionPolicyError
)


BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
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
# Dedicated read-only client id for this diagnostic.
#
IB_CLIENT_ID = int(
    os.getenv(
        "IB_POSITION_CHECK_CLIENT_ID",
        "60"
    )
)


MAX_MANAGED_POSITIONS = int(
    os.getenv(
        "MAX_MANAGED_POSITIONS",
        "3"
    )
)

MAX_TOTAL_BROKER_POSITIONS = int(
    os.getenv(
        "MAX_TOTAL_BROKER_POSITIONS",
        "10"
    )
)


DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db"
)


class IBPositionReader(
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

        self.ready = threading.Event()

        self.positions_done = (
            threading.Event()
        )

        self.accounts = []

        self.positions = []


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
            "symbol":
                contract.symbol.upper(),

            "quantity":
                quantity,

            "avg_cost":
                float(
                    avgCost
                ),

            "account":
                account
        })


    def positionEnd(
        self
    ):
        self.positions_done.set()


    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        #
        # Ignore normal farm/connectivity information.
        #
        if errorCode in {
            2104,
            2106,
            2158
        }:
            return


        print(
            f"IBKR {errorCode}: "
            f"{errorString}"
        )


def main():
    if not IB_ACCOUNT:
        raise RuntimeError(
            "IB_ACCOUNT not configured"
        )


    app = IBPositionReader()


    try:
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
                    "Configured account not "
                    "available from IBKR"
                )
            )


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


        result = evaluate_position_limits(
            db_file=
                DB_FILE,

            broker_positions=
                app.positions,

            max_managed_positions=
                MAX_MANAGED_POSITIONS,

            max_total_broker_positions=
                MAX_TOTAL_BROKER_POSITIONS
        )


        print(
            "========================================"
        )

        print(
            "FRESH IBKR POSITION POLICY"
        )

        print(
            "========================================"
        )


        print(
            f"Account            : "
            f"{IB_ACCOUNT}"
        )

        print(
            f"Broker positions   : "
            f"{result['broker_position_count']}"
        )

        print(
            f"Managed positions  : "
            f"{result['managed_position_count']}"
            f"/{MAX_MANAGED_POSITIONS}"
        )

        print(
            f"Legacy positions   : "
            f"{result['legacy_position_count']}"
        )

        print(
            f"Emergency total    : "
            f"{result['broker_position_count']}"
            f"/{MAX_TOTAL_BROKER_POSITIONS}"
        )


        print()
        print(
            "POSITIONS"
        )

        print(
            "----------------------------------------"
        )


        for position in (
            result[
                "managed_positions"
            ]
        ):
            print(
                f"MANAGED  "
                f"{position['symbol']:8} "
                f"qty="
                f"{position.get('quantity')}"
            )


        for position in (
            result[
                "legacy_positions"
            ]
        ):
            print(
                f"LEGACY   "
                f"{position['symbol']:8} "
                f"qty="
                f"{position.get('quantity')}"
            )


        print()
        print(
            "BLOCKERS"
        )

        print(
            "----------------------------------------"
        )


        if result[
            "blockers"
        ]:

            for blocker in result[
                "blockers"
            ]:
                print(
                    f"- {blocker}"
                )

        else:

            print(
                "None"
            )


    except PositionPolicyError as exc:

        print(
            "BLOCKED"
        )

        print(
            f"Position policy error: "
            f"{exc}"
        )


    finally:

        if app.isConnected():

            app.disconnect()


if __name__ == "__main__":
    main()
