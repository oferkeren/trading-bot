import os
import threading
import time

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

from ai_market_intelligence import (
    MarketContext,
)


BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

load_dotenv(
    os.path.join(
        BASE_DIR,
        ".env",
    )
)


# ============================================================
# CONFIG
# ============================================================

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

IB_ACCOUNT = os.getenv(
    "IB_ACCOUNT",
    "",
)

IB_CLIENT_ID = int(
    os.getenv(
        "IB_MARKET_CONTEXT_CLIENT_ID",
        "79",
    )
)

CONNECT_TIMEOUT = 7

MARKET_DATA_WAIT_SECONDS = float(
    os.getenv(
        "MARKET_CONTEXT_WAIT_SECONDS",
        "5",
    )
)


# ============================================================
# INSTRUMENTS
# ============================================================

MARKET_INSTRUMENTS = {
    "SPY": {
        "symbol":
            "SPY",

        "secType":
            "STK",

        "exchange":
            "SMART",

        "primaryExchange":
            "ARCA",

        "currency":
            "USD",

        "data_type":
            1,

        "required":
            True,
    },

    "QQQ": {
        "symbol":
            "QQQ",

        "secType":
            "STK",

        "exchange":
            "SMART",

        "primaryExchange":
            "NASDAQ",

        "currency":
            "USD",

        "data_type":
            1,

        "required":
            True,
    },

    "IWM": {
        "symbol":
            "IWM",

        "secType":
            "STK",

        "exchange":
            "SMART",

        "primaryExchange":
            "ARCA",

        "currency":
            "USD",

        "data_type":
            1,

        "required":
            True,
    },

    "VIX": {
        "symbol":
            "VIX",

        "secType":
            "IND",

        "exchange":
            "CBOE",

        "primaryExchange":
            "",

        "currency":
            "USD",

        "data_type":
            3,

        "required":
            False,
    },
}


# ============================================================
# HELPERS
# ============================================================

def valid_price(
    value,
):
    try:
        value = float(
            value
        )

    except Exception:
        return None


    if value <= 0:
        return None


    if value > 1e100:
        return None


    return value


def percent_change(
    current,
    previous,
):
    current = valid_price(
        current
    )

    previous = valid_price(
        previous
    )


    if (
        current is None
        or
        previous is None
        or
        previous == 0
    ):
        return None


    return (
        (
            current
            -
            previous
        )
        /
        previous
        *
        100.0
    )


def make_contract(
    definition,
):
    contract = Contract()

    contract.symbol = (
        definition[
            "symbol"
        ]
    )

    contract.secType = (
        definition[
            "secType"
        ]
    )

    contract.exchange = (
        definition[
            "exchange"
        ]
    )

    contract.currency = (
        definition[
            "currency"
        ]
    )


    primary_exchange = (
        definition.get(
            "primaryExchange",
            "",
        )
    )


    if primary_exchange:
        contract.primaryExchange = (
            primary_exchange
        )


    return contract


# ============================================================
# IBKR APP
# ============================================================

class MarketContextApp(
    EWrapper,
    EClient,
):

    def __init__(
        self,
    ):
        EClient.__init__(
            self,
            self,
        )


        self.ready = (
            threading.Event()
        )

        self.accounts = []

        self.market_data = {}

        self.req_to_name = {}

        self.errors = []


    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def nextValidId(
        self,
        orderId,
    ):
        print(
            "IBKR CONNECTED | "
            f"clientId={IB_CLIENT_ID}",
            flush=True,
        )

        self.ready.set()


    def managedAccounts(
        self,
        accountsList,
    ):
        self.accounts = [
            account.strip()

            for account
            in accountsList.split(
                ","
            )

            if account.strip()
        ]


    # --------------------------------------------------------
    # MARKET DATA
    # --------------------------------------------------------

    def tickPrice(
        self,
        reqId,
        tickType,
        price,
        attrib,
    ):
        name = (
            self.req_to_name
            .get(
                reqId
            )
        )


        if name is None:
            return


        value = valid_price(
            price
        )


        if value is None:
            return


        item = (
            self.market_data
            .setdefault(
                name,
                {}
            )
        )


        mapping = {
            # Live
            1:
                "bid",

            2:
                "ask",

            4:
                "last",

            6:
                "high",

            7:
                "low",

            9:
                "close",

            14:
                "open",

            # Delayed
            66:
                "bid",

            67:
                "ask",

            68:
                "last",

            72:
                "high",

            73:
                "low",

            75:
                "close",

            76:
                "open",
        }


        field = (
            mapping.get(
                tickType
            )
        )


        if field is None:
            return


        item[
            field
        ] = value


    def marketDataType(
        self,
        reqId,
        marketDataType,
    ):
        name = (
            self.req_to_name
            .get(
                reqId
            )
        )


        if name is None:
            return


        self.market_data.setdefault(
            name,
            {}
        )[
            "market_data_type"
        ] = (
            marketDataType
        )


        print(
            "MARKET_DATA_TYPE | "
            f"{name} | "
            f"type={marketDataType}",
            flush=True,
        )


    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson="",
    ):
        informational = {
            2104,
            2106,
            2158,
            2108,
            2109,
        }


        if errorCode in informational:
            return


        item = {
            "req_id":
                reqId,

            "code":
                errorCode,

            "message":
                errorString,
        }


        self.errors.append(
            item
        )


        print(
            "IB ERROR | "
            f"reqId={reqId} | "
            f"code={errorCode} | "
            f"{errorString}",
            flush=True,
        )


# ============================================================
# QUOTE HELPERS
# ============================================================

def current_price(
    quote,
):
    last = valid_price(
        quote.get(
            "last"
        )
    )


    if last is not None:
        return (
            last,
            "LAST",
        )


    bid = valid_price(
        quote.get(
            "bid"
        )
    )

    ask = valid_price(
        quote.get(
            "ask"
        )
    )


    if (
        bid is not None
        and
        ask is not None
    ):
        return (
            (
                bid
                +
                ask
            )
            /
            2.0,
            "MID",
        )


    close = valid_price(
        quote.get(
            "close"
        )
    )


    if close is not None:
        return (
            close,
            "CLOSE",
        )


    return (
        None,
        "NONE",
    )


# ============================================================
# REQUESTS
# ============================================================

def request_one(
    app,
    req_id,
    name,
    definition,
):
    app.req_to_name[
        req_id
    ] = name


    app.market_data[
        name
    ] = {}


    data_type = (
        definition[
            "data_type"
        ]
    )


    app.reqMarketDataType(
        data_type
    )


    time.sleep(
        0.10
    )


    print(
        "REQUEST | "
        f"{name} | "
        f"reqId={req_id} | "
        f"requestedDataType={data_type}",
        flush=True,
    )


    app.reqMktData(
        req_id,
        make_contract(
            definition
        ),
        "",
        False,
        False,
        [],
    )


def request_market_data(
    app,
):
    req_id = 7900


    for name, definition in (
        MARKET_INSTRUMENTS.items()
    ):
        request_one(
            app,
            req_id,
            name,
            definition,
        )


        req_id += 1


        time.sleep(
            0.15
        )


    time.sleep(
        MARKET_DATA_WAIT_SECONDS
    )


def cancel_market_data(
    app,
):
    for req_id in (
        app.req_to_name.keys()
    ):
        try:
            app.cancelMktData(
                req_id
            )

        except Exception:
            pass


# ============================================================
# BUILD CONTEXT
# ============================================================

def build_context(
    market_data,
):
    calculated = {}


    for name in (
        MARKET_INSTRUMENTS.keys()
    ):
        quote = (
            market_data.get(
                name,
                {}
            )
        )


        price, source = (
            current_price(
                quote
            )
        )


        close = valid_price(
            quote.get(
                "close"
            )
        )


        change_pct = (
            percent_change(
                price,
                close,
            )
        )


        calculated[
            name
        ] = {
            "price":
                price,

            "price_source":
                source,

            "close":
                close,

            "change_pct":
                change_pct,

            "bid":
                quote.get(
                    "bid"
                ),

            "ask":
                quote.get(
                    "ask"
                ),

            "last":
                quote.get(
                    "last"
                ),

            "open":
                quote.get(
                    "open"
                ),

            "high":
                quote.get(
                    "high"
                ),

            "low":
                quote.get(
                    "low"
                ),

            "market_data_type":
                quote.get(
                    "market_data_type"
                ),
        }


    context = MarketContext(
        spy_change_pct=
            calculated[
                "SPY"
            ][
                "change_pct"
            ],

        qqq_change_pct=
            calculated[
                "QQQ"
            ][
                "change_pct"
            ],

        iwm_change_pct=
            calculated[
                "IWM"
            ][
                "change_pct"
            ],

        vix_value=
            calculated[
                "VIX"
            ][
                "price"
            ],

        vix_change_pct=
            calculated[
                "VIX"
            ][
                "change_pct"
            ],

        breadth_pct=
            None,
    )


    return (
        context,
        calculated,
    )


# ============================================================
# VALIDATION
# ============================================================

def validate_context(
    context,
    details,
):
    required_values = {
        "SPY":
            context.spy_change_pct,

        "QQQ":
            context.qqq_change_pct,

        "IWM":
            context.iwm_change_pct,
    }


    missing = [
        symbol

        for symbol, value
        in required_values.items()

        if value is None
    ]


    if missing:
        raise RuntimeError(
            "Incomplete required market context: "
            +
            ",".join(
                missing
            )
        )


    for symbol in [
        "SPY",
        "QQQ",
        "IWM",
    ]:
        data_type = (
            details[
                symbol
            ].get(
                "market_data_type"
            )
        )


        if (
            data_type is not None
            and
            data_type
            not in {
                1,
                2,
            }
        ):
            raise RuntimeError(
                f"{symbol} is not live/frozen "
                f"market data "
                f"(type={data_type})"
            )


    if (
        context.vix_value
        is None
    ):
        print(
            "WARNING | VIX unavailable | "
            "continuing without VIX context",
            flush=True,
        )


# ============================================================
# PUBLIC FUNCTION
# ============================================================

def collect_market_context():
    app = (
        MarketContextApp()
    )


    try:
        app.connect(
            IB_HOST,
            IB_PORT,
            clientId=
                IB_CLIENT_ID,
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
            0.50
        )


        if (
            IB_ACCOUNT
            and
            app.accounts
            and
            IB_ACCOUNT
            not in
            app.accounts
        ):
            raise RuntimeError(
                f"IB account "
                f"{IB_ACCOUNT} "
                f"not available"
            )


        request_market_data(
            app
        )


        context, details = (
            build_context(
                app.market_data
            )
        )


        validate_context(
            context,
            details,
        )


        return (
            context,
            details,
        )


    finally:
        cancel_market_data(
            app
        )


        if app.isConnected():
            app.disconnect()


        time.sleep(
            0.25
        )


# ============================================================
# SELF TEST
# ============================================================

def main():
    print(
        "========================================"
    )

    print(
        "TRADINGMAX MARKET CONTEXT"
    )

    print(
        "========================================"
    )

    print(
        "IB_HOST=",
        IB_HOST
    )

    print(
        "IB_PORT=",
        IB_PORT
    )

    print(
        "CLIENT_ID=",
        IB_CLIENT_ID
    )

    print()


    context, details = (
        collect_market_context()
    )


    print()
    print(
        "===== RAW MARKET ====="
    )


    for symbol in [
        "SPY",
        "QQQ",
        "IWM",
        "VIX",
    ]:
        item = (
            details[
                symbol
            ]
        )


        print(
            f"{symbol:4} | "
            f"price={item['price']} | "
            f"close={item['close']} | "
            f"change={item['change_pct']}% | "
            f"source={item['price_source']} | "
            f"dataType="
            f"{item['market_data_type']}"
        )


    print()
    print(
        "===== AI MARKET CONTEXT ====="
    )


    print(
        context.model_dump_json(
            indent=2
        )
    )


    print()
    print(
        "REQUIRED_CONTEXT="
        "SPY,QQQ,IWM"
    )

    print(
        "OPTIONAL_CONTEXT="
        "VIX"
    )


    print()
    print(
        "MARKET_CONTEXT_SELF_TEST=PASS"
    )


if __name__ == "__main__":
    main()
