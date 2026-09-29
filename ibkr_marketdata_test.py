from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.scanner import ScannerSubscription

import threading
import time


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 71


class MarketDataTest(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.connected_event = threading.Event()
        self.scanner_finished_event = threading.Event()

        self.next_order_id = None

        self.scanner_rows = []
        self.errors = []

        self.market_data_types = {}
        self.price_ticks = {}
        self.size_ticks = {}
        self.string_ticks = {}

        self.request_labels = {}

    # ========================================================
    # CONNECTION
    # ========================================================

    def nextValidId(
        self,
        orderId,
    ):
        self.next_order_id = orderId

        print(
            f"CONNECTED | nextValidId={orderId}"
        )

        print(
            f"SERVER VERSION | {self.serverVersion()}"
        )

        self.connected_event.set()

    def connectionClosed(
        self,
    ):
        print(
            "CONNECTION CLOSED"
        )

    # ========================================================
    # ERROR HANDLING
    # ========================================================

    def error(
        self,
        reqId,
        *args,
    ):
        error_time = None
        error_code = None
        error_string = ""
        advanced_order_reject_json = ""

        if len(args) == 4:
            (
                error_time,
                error_code,
                error_string,
                advanced_order_reject_json,
            ) = args

        elif len(args) == 3:
            (
                error_code,
                error_string,
                advanced_order_reject_json,
            ) = args

        elif len(args) == 2:
            (
                error_code,
                error_string,
            ) = args

        elif len(args) == 1:
            error_string = str(
                args[0]
            )

        else:
            print(
                "ERROR CALLBACK | "
                f"reqId={reqId} | "
                f"unexpected_args={args}"
            )
            return

        record = {
            "reqId":
                reqId,

            "errorTime":
                error_time,

            "errorCode":
                error_code,

            "errorString":
                error_string,

            "advancedOrderRejectJson":
                advanced_order_reject_json,
        }

        self.errors.append(
            record
        )

        info_codes = {
            2104,
            2106,
            2107,
            2108,
            2158,
        }

        prefix = (
            "IB INFO"
            if error_code in info_codes
            else
            "IB ERROR"
        )

        label = self.request_labels.get(
            reqId,
            "",
        )

        label_text = (
            f" | {label}"
            if label
            else ""
        )

        print(
            f"{prefix} | "
            f"reqId={reqId}"
            f"{label_text} | "
            f"time={error_time} | "
            f"code={error_code} | "
            f"{error_string}"
        )

        if advanced_order_reject_json:
            print(
                "ADVANCED ERROR JSON | "
                f"{advanced_order_reject_json}"
            )

    # ========================================================
    # SCANNER
    # ========================================================

    def scannerData(
        self,
        reqId,
        rank,
        contractDetails,
        distance,
        benchmark,
        projection,
        legsStr,
    ):
        contract = contractDetails.contract

        row = {
            "rank":
                rank,

            "symbol":
                contract.symbol,

            "exchange":
                contract.exchange,

            "primaryExchange":
                getattr(
                    contract,
                    "primaryExchange",
                    "",
                )
                or "",

            "currency":
                contract.currency,

            "conId":
                contract.conId,
        }

        self.scanner_rows.append(
            row
        )

        print(
            "SCANNER | "
            f"rank={rank} | "
            f"symbol={row['symbol']} | "
            f"exchange={row['exchange']} | "
            f"primary={row['primaryExchange']} | "
            f"currency={row['currency']} | "
            f"conId={row['conId']}"
        )

    def scannerDataEnd(
        self,
        reqId,
    ):
        print(
            "SCANNER END | "
            f"reqId={reqId} | "
            f"results={len(self.scanner_rows)}"
        )

        self.scanner_finished_event.set()

    # ========================================================
    # MARKET DATA
    # ========================================================

    def marketDataType(
        self,
        reqId,
        marketDataType,
    ):
        self.market_data_types[
            reqId
        ] = marketDataType

        type_name = {
            1: "LIVE",
            2: "FROZEN",
            3: "DELAYED",
            4: "DELAYED_FROZEN",
        }.get(
            marketDataType,
            "UNKNOWN",
        )

        label = self.request_labels.get(
            reqId,
            "",
        )

        print(
            "MARKET DATA TYPE | "
            f"reqId={reqId} | "
            f"{label} | "
            f"type={marketDataType} | "
            f"name={type_name}"
        )

    def tickPrice(
        self,
        reqId,
        tickType,
        price,
        attrib,
    ):
        self.price_ticks.setdefault(
            reqId,
            [],
        )

        self.price_ticks[
            reqId
        ].append(
            {
                "tickType":
                    tickType,

                "price":
                    price,
            }
        )

        label = self.request_labels.get(
            reqId,
            "",
        )

        print(
            "PRICE | "
            f"reqId={reqId} | "
            f"{label} | "
            f"tickType={tickType} | "
            f"price={price}"
        )

    def tickSize(
        self,
        reqId,
        tickType,
        size,
    ):
        self.size_ticks.setdefault(
            reqId,
            [],
        )

        self.size_ticks[
            reqId
        ].append(
            {
                "tickType":
                    tickType,

                "size":
                    str(
                        size
                    ),
            }
        )

        label = self.request_labels.get(
            reqId,
            "",
        )

        print(
            "SIZE | "
            f"reqId={reqId} | "
            f"{label} | "
            f"tickType={tickType} | "
            f"size={size}"
        )

    def tickString(
        self,
        reqId,
        tickType,
        value,
    ):
        self.string_ticks.setdefault(
            reqId,
            [],
        )

        self.string_ticks[
            reqId
        ].append(
            {
                "tickType":
                    tickType,

                "value":
                    value,
            }
        )

        label = self.request_labels.get(
            reqId,
            "",
        )

        print(
            "STRING | "
            f"reqId={reqId} | "
            f"{label} | "
            f"tickType={tickType} | "
            f"value={value}"
        )


# ============================================================
# CONTRACT HELPERS
# ============================================================

def make_stock(
    symbol,
    exchange,
):
    contract = Contract()

    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = exchange
    contract.currency = "USD"

    return contract


# ============================================================
# NETWORK LOOP
# ============================================================

def run_loop(
    app,
):
    try:
        app.run()

    except Exception as exc:
        print(
            "IBKR NETWORK LOOP CRASHED | "
            f"{type(exc).__name__}: {exc}"
        )


# ============================================================
# CONNECTION
# ============================================================

def connect_and_wait(
    app,
):
    print(
        "Connecting to IBKR..."
    )

    app.connect(
        HOST,
        PORT,
        CLIENT_ID,
    )

    thread = threading.Thread(
        target=run_loop,
        args=(app,),
        daemon=True,
    )

    thread.start()

    connected = app.connected_event.wait(
        timeout=15
    )

    if not connected:
        raise RuntimeError(
            "IBKR handshake did not complete within 15 seconds"
        )

    if not app.isConnected():
        raise RuntimeError(
            "IBKR reports socket is not connected"
        )

    if app.serverVersion() is None:
        raise RuntimeError(
            "IBKR serverVersion is None after handshake"
        )

    print(
        "IBKR HANDSHAKE OK"
    )

    print(
        f"Client ID      : {CLIENT_ID}"
    )

    print(
        f"Server version : {app.serverVersion()}"
    )

    return thread


# ============================================================
# SCANNER TEST
# ============================================================

def test_scanner(
    app,
):
    print()
    print(
        "========================================"
    )
    print(
        "TEST 1: IBKR MARKET SCANNER"
    )
    print(
        "========================================"
    )

    scanner = ScannerSubscription()

    scanner.instrument = (
        "STK"
    )

    scanner.locationCode = (
        "STK.US.MAJOR"
    )

    scanner.scanCode = (
        "TOP_PERC_GAIN"
    )

    scanner.abovePrice = (
        1.0
    )

    scanner.belowPrice = (
        30.0
    )

    scanner.aboveVolume = (
        500000
    )

    scanner.numberOfRows = (
        20
    )

    app.scanner_finished_event.clear()

    app.request_labels[
        9001
    ] = "SCANNER"

    print(
        "REQUESTING SCANNER..."
    )

    app.reqScannerSubscription(
        9001,
        scanner,
        [],
        [],
    )

    completed = app.scanner_finished_event.wait(
        timeout=15
    )

    if not completed:
        print(
            "SCANNER TIMEOUT"
        )

    try:
        app.cancelScannerSubscription(
            9001
        )

    except Exception as exc:
        print(
            "SCANNER CANCEL WARNING | "
            f"{exc}"
        )

    print()
    print(
        f"SCANNER RESULT COUNT | "
        f"{len(app.scanner_rows)}"
    )


# ============================================================
# FREE LIVE TEST
# ============================================================

def test_free_live_market_data(
    app,
):
    print()
    print(
        "========================================"
    )
    print(
        "TEST 2: FREE LIVE MARKET DATA"
    )
    print(
        "========================================"
    )

    print(
        "REQUESTING MARKET DATA TYPE 1 = LIVE"
    )

    app.reqMarketDataType(
        1
    )

    time.sleep(
        1
    )

    tests = [
        {
            "reqId":
                9201,

            "symbol":
                "AAPL",

            "exchange":
                "IEX",
        },
        {
            "reqId":
                9202,

            "symbol":
                "MSFT",

            "exchange":
                "IEX",
        },
        {
            "reqId":
                9203,

            "symbol":
                "AMD",

            "exchange":
                "IEX",
        },
        {
            "reqId":
                9301,

            "symbol":
                "AAPL",

            "exchange":
                "SMART",
        },
        {
            "reqId":
                9302,

            "symbol":
                "MSFT",

            "exchange":
                "SMART",
        },
        {
            "reqId":
                9303,

            "symbol":
                "AMD",

            "exchange":
                "SMART",
        },
    ]

    for item in tests:

        req_id = item[
            "reqId"
        ]

        symbol = item[
            "symbol"
        ]

        exchange = item[
            "exchange"
        ]

        label = (
            f"{symbol}@{exchange}"
        )

        app.request_labels[
            req_id
        ] = label

        contract = make_stock(
            symbol,
            exchange,
        )

        print(
            "REQUESTING LIVE DATA | "
            f"{label} | "
            f"reqId={req_id}"
        )

        app.reqMktData(
            req_id,
            contract,
            "",
            False,
            False,
            [],
        )

        time.sleep(
            0.3
        )

    print()
    print(
        "Waiting 15 seconds for live ticks..."
    )

    time.sleep(
        15
    )

    print()
    print(
        "Cancelling subscriptions..."
    )

    for item in tests:

        req_id = item[
            "reqId"
        ]

        try:
            app.cancelMktData(
                req_id
            )

        except Exception as exc:
            print(
                "MARKET DATA CANCEL WARNING | "
                f"reqId={req_id} | "
                f"{exc}"
            )

    print()
    print(
        "FREE LIVE RESULTS"
    )

    print(
        "----------------------------------------"
    )

    for item in tests:

        req_id = item[
            "reqId"
        ]

        label = app.request_labels.get(
            req_id,
            "",
        )

        market_data_type = (
            app.market_data_types.get(
                req_id
            )
        )

        prices = app.price_ticks.get(
            req_id,
            [],
        )

        sizes = app.size_ticks.get(
            req_id,
            [],
        )

        print(
            f"{label}:"
        )

        print(
            "  market_data_type = "
            f"{market_data_type}"
        )

        print(
            "  price_ticks      = "
            f"{len(prices)}"
        )

        print(
            "  size_ticks       = "
            f"{len(sizes)}"
        )


# ============================================================
# SUMMARY
# ============================================================

def print_summary(
    app,
):
    print()
    print(
        "========================================"
    )
    print(
        "TEST SUMMARY"
    )
    print(
        "========================================"
    )

    print(
        f"Connected       : "
        f"{app.isConnected()}"
    )

    print(
        f"Server version  : "
        f"{app.serverVersion()}"
    )

    print(
        f"Scanner results : "
        f"{len(app.scanner_rows)}"
    )

    info_codes = {
        2104,
        2106,
        2107,
        2108,
        2158,
    }

    real_errors = [
        item
        for item in app.errors
        if (
            item.get(
                "errorCode"
            )
            not in info_codes
            and
            not (
                item.get(
                    "errorCode"
                )
                ==
                162
                and
                item.get(
                    "reqId"
                )
                ==
                9001
            )
        )
    ]

    print(
        f"IB real errors : "
        f"{len(real_errors)}"
    )

    if real_errors:

        print()
        print(
            "REAL ERRORS:"
        )

        for item in real_errors:

            label = app.request_labels.get(
                item.get(
                    "reqId"
                ),
                "",
            )

            print(
                " - "
                f"{label} | "
                f"reqId={item.get('reqId')} | "
                f"code={item.get('errorCode')} | "
                f"{item.get('errorString')}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    app = MarketDataTest()

    try:

        connect_and_wait(
            app
        )

        test_scanner(
            app
        )

        test_free_live_market_data(
            app
        )

        print_summary(
            app
        )

    except KeyboardInterrupt:

        print(
            "\nInterrupted by user"
        )

    except Exception as exc:

        print(
            "TEST FAILED | "
            f"{type(exc).__name__}: {exc}"
        )

    finally:

        if app.isConnected():

            print()
            print(
                "Disconnecting..."
            )

            app.disconnect()

            time.sleep(
                1
            )


if __name__ == "__main__":
    main()
