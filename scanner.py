import os
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.scanner import ScannerSubscription
from ibapi.contract import Contract

import threading
import time
from datetime import datetime, timezone


# ============================================================
# CONFIG
# ============================================================

HOST = os.getenv(
    "IB_HOST",
    "127.0.0.1",
)

PORT = int(
    os.getenv(
        "IB_PORT",
        "7496",
    )
)

# Dedicated clients:
#
# Execution = 10
# Monitor   = 40
# Scanner   = 72
# Strategy  = 73
CLIENT_ID = 72


MIN_PRICE = 0.50
MAX_PRICE = 50.00

MIN_VOLUME = 100_000

ROWS_PER_SCAN = 35

SCANNER_TIMEOUT_SECONDS = 15

CONTRACT_DETAILS_TIMEOUT_SECONDS = 5

CONTRACT_DETAILS_REQUEST_START = 80000


SCAN_DEFINITIONS = [{'name': 'GAINERS', 'scan_code': 'TOP_PERC_GAIN', 'bias': 'LONG', 'req_id': 7001}, {'name': 'LOSERS', 'scan_code': 'TOP_PERC_LOSE', 'bias': 'SHORT', 'req_id': 7002}, {'name': 'MOST_ACTIVE', 'scan_code': 'MOST_ACTIVE', 'bias': 'NEUTRAL', 'req_id': 7003}, {'name': 'HOT_VOLUME', 'scan_code': 'HOT_BY_VOLUME', 'req_id': 7004, 'bias': 'NEUTRAL'}, {'name': 'VOLUME_RATE', 'scan_code': 'TOP_VOLUME_RATE', 'req_id': 7005, 'bias': 'NEUTRAL'}, {'name': 'GAP_UP', 'scan_code': 'HIGH_OPEN_GAP', 'bias': 'LONG', 'req_id': 7006}, {'name': 'GAP_DOWN', 'scan_code': 'LOW_OPEN_GAP', 'bias': 'SHORT', 'req_id': 7007}]


# ============================================================
# INSTRUMENT TYPE NORMALIZATION
# ============================================================

def normalize_instrument_type(
    value,
):
    text = str(
        value
        or ""
    ).strip().upper()

    if not text:
        return "UNKNOWN"

    aliases = {
        "COMMON STOCK":
            "COMMON",

        "COMMON":
            "COMMON",

        "STOCK":
            "COMMON",

        "ORD":
            "COMMON",

        "ADR":
            "ADR",

        "ETF":
            "ETF",

        "ETN":
            "ETN",

        "REIT":
            "REIT",

        "CEF":
            "CEF",

        "ETMF":
            "ETMF",

        "EFN":
            "EFN",
    }

    return aliases.get(
        text,
        text,
    )


# ============================================================
# CLIENT
# ============================================================

class TradingMaxScanner(
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

        self.connected_event = (
            threading.Event()
        )

        self.scan_events = {}

        self.rows = []

        self.errors = []

        #
        # Contract-details enrichment
        #
        self.contract_detail_events = {}

        self.contract_detail_req_to_conid = {}

        self.contract_metadata = {}

        self.conid_detail_req = {}

        self.next_contract_detail_req_id = (
            CONTRACT_DETAILS_REQUEST_START
        )

        self.contract_lock = (
            threading.Lock()
        )

    # ========================================================
    # CONNECTION
    # ========================================================

    def nextValidId(
        self,
        orderId,
    ):
        print(
            f"CONNECTED | "
            f"nextValidId={orderId}"
        )

        print(
            f"SERVER VERSION | "
            f"{self.serverVersion()}"
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

        print(
            f"{prefix} | "
            f"reqId={reqId} | "
            f"time={error_time} | "
            f"code={error_code} | "
            f"{error_string}"
        )

    # ========================================================
    # SCANNER CALLBACKS
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
        contract = (
            contractDetails.contract
        )

        scan_meta = (
            self.get_scan_meta(
                reqId
            )
        )

        row = {
            "req_id":
                reqId,

            "scan_name":
                scan_meta[
                    "name"
                ],

            "scan_code":
                scan_meta[
                    "scan_code"
                ],

            "bias":
                scan_meta[
                    "bias"
                ],

            "rank":
                rank,

            "symbol":
                contract.symbol,

            "exchange":
                contract.exchange,

            "primary_exchange":
                (
                    getattr(
                        contract,
                        "primaryExchange",
                        "",
                    )
                    or ""
                ),

            "currency":
                contract.currency,

            "con_id":
                contract.conId,

            #
            # Scanner callback often does not contain
            # full security classification.
            #
            # It will be enriched immediately after
            # scannerDataEnd.
            #
            "instrument_type":
                "UNKNOWN",

            "long_name":
                str(
                    getattr(
                        contractDetails,
                        "longName",
                        "",
                    )
                    or ""
                ),

            "industry":
                str(
                    getattr(
                        contractDetails,
                        "industry",
                        "",
                    )
                    or ""
                ),

            "category":
                str(
                    getattr(
                        contractDetails,
                        "category",
                        "",
                    )
                    or ""
                ),

            "subcategory":
                str(
                    getattr(
                        contractDetails,
                        "subcategory",
                        "",
                    )
                    or ""
                ),
        }

        self.rows.append(
            row
        )

        print(
            "SCANNER | "
            f"{row['scan_name']} | "
            f"rank={row['rank']} | "
            f"symbol={row['symbol']} | "
            f"primary={row['primary_exchange']} | "
            f"conId={row['con_id']}"
        )

    def scannerDataEnd(
        self,
        reqId,
    ):
        meta = (
            self.get_scan_meta(
                reqId
            )
        )

        count = len(
            [
                row
                for row
                in self.rows
                if row[
                    "req_id"
                ]
                ==
                reqId
            ]
        )

        print(
            "SCANNER END | "
            f"{meta['name']} | "
            f"reqId={reqId} | "
            f"results={count}"
        )

        event = (
            self.scan_events.get(
                reqId
            )
        )

        if event:

            event.set()

    # ========================================================
    # CONTRACT DETAILS CALLBACKS
    # ========================================================

    def contractDetails(
        self,
        reqId,
        contractDetails,
    ):
        con_id = (
            self.contract_detail_req_to_conid.get(
                reqId
            )
        )

        if con_id is None:

            return

        contract = (
            contractDetails.contract
        )

        stock_type = (
            normalize_instrument_type(
                getattr(
                    contractDetails,
                    "stockType",
                    "",
                )
            )
        )

        metadata = {
            "instrument_type":
                stock_type,

            "long_name":
                str(
                    getattr(
                        contractDetails,
                        "longName",
                        "",
                    )
                    or ""
                ),

            "industry":
                str(
                    getattr(
                        contractDetails,
                        "industry",
                        "",
                    )
                    or ""
                ),

            "category":
                str(
                    getattr(
                        contractDetails,
                        "category",
                        "",
                    )
                    or ""
                ),

            "subcategory":
                str(
                    getattr(
                        contractDetails,
                        "subcategory",
                        "",
                    )
                    or ""
                ),

            "primary_exchange":
                (
                    getattr(
                        contract,
                        "primaryExchange",
                        "",
                    )
                    or ""
                ),

            "symbol":
                (
                    contract.symbol
                    or ""
                ),
        }

        self.contract_metadata[
            con_id
        ] = metadata

    def contractDetailsEnd(
        self,
        reqId,
    ):
        event = (
            self.contract_detail_events.get(
                reqId
            )
        )

        if event:

            event.set()

    # ========================================================
    # HELPERS
    # ========================================================

    def get_scan_meta(
        self,
        req_id,
    ):
        for item in (
            SCAN_DEFINITIONS
        ):

            if (
                item[
                    "req_id"
                ]
                ==
                req_id
            ):

                return item

        return {
            "name":
                "UNKNOWN",

            "scan_code":
                "UNKNOWN",

            "bias":
                "NEUTRAL",

            "req_id":
                req_id,
        }

    def allocate_contract_detail_req_id(
        self,
    ):
        with self.contract_lock:

            req_id = (
                self.next_contract_detail_req_id
            )

            self.next_contract_detail_req_id += 1

        return req_id


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
            "NETWORK LOOP CRASHED | "
            f"{type(exc).__name__}: "
            f"{exc}"
        )


# ============================================================
# CONNECT
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

    thread = (
        threading.Thread(
            target=run_loop,
            args=(app,),
            daemon=True,
        )
    )

    thread.start()

    connected = (
        app.connected_event.wait(
            timeout=15
        )
    )

    if not connected:

        raise RuntimeError(
            "IBKR handshake timeout"
        )

    if not app.isConnected():

        raise RuntimeError(
            "IBKR socket not connected"
        )

    if app.serverVersion() is None:

        raise RuntimeError(
            "IBKR serverVersion is None"
        )

    print(
        "IBKR HANDSHAKE OK"
    )

    print(
        f"Client ID      : "
        f"{CLIENT_ID}"
    )

    print(
        f"Server version : "
        f"{app.serverVersion()}"
    )

    return thread


# ============================================================
# SCANNER SUBSCRIPTION
# ============================================================

def build_scanner_subscription(
    scan_code,
):
    subscription = (
        ScannerSubscription()
    )

    subscription.instrument = (
        "STK"
    )

    subscription.locationCode = (
        "STK.US.MAJOR"
    )

    subscription.scanCode = (
        scan_code
    )

    subscription.abovePrice = (
        MIN_PRICE
    )

    subscription.belowPrice = (
        MAX_PRICE
    )

    subscription.aboveVolume = (
        MIN_VOLUME
    )

    subscription.numberOfRows = (
        ROWS_PER_SCAN
    )

    #
    # Deliberately no stockTypeFilter.
    #
    # We want to see COMMON / ADR / ETF /
    # REIT / ETN / etc and classify them
    # after discovery.
    #

    return subscription


# ============================================================
# CONTRACT DETAILS ENRICHMENT
# ============================================================

def build_details_contract(
    row,
):
    contract = (
        Contract()
    )

    contract.conId = int(
        row[
            "con_id"
        ]
    )

    contract.secType = (
        "STK"
    )

    contract.exchange = (
        "SMART"
    )

    contract.currency = (
        row.get(
            "currency"
        )
        or
        "USD"
    )

    return contract


def enrich_row_contract_details(
    app,
    row,
):
    con_id = int(
        row[
            "con_id"
        ]
    )

    #
    # Already resolved by an earlier scan.
    #
    if con_id in (
        app.contract_metadata
    ):

        metadata = (
            app.contract_metadata[
                con_id
            ]
        )

        apply_metadata_to_row(
            row,
            metadata,
        )

        return

    #
    # Another request may already have
    # been allocated for this conId.
    #
    existing_req_id = (
        app.conid_detail_req.get(
            con_id
        )
    )

    if existing_req_id is not None:

        event = (
            app.contract_detail_events.get(
                existing_req_id
            )
        )

        if event:

            event.wait(
                timeout=
                    CONTRACT_DETAILS_TIMEOUT_SECONDS
            )

        metadata = (
            app.contract_metadata.get(
                con_id
            )
        )

        if metadata:

            apply_metadata_to_row(
                row,
                metadata,
            )

        return

    req_id = (
        app.allocate_contract_detail_req_id()
    )

    event = (
        threading.Event()
    )

    app.contract_detail_events[
        req_id
    ] = event

    app.contract_detail_req_to_conid[
        req_id
    ] = con_id

    app.conid_detail_req[
        con_id
    ] = req_id

    contract = (
        build_details_contract(
            row
        )
    )

    print(
        "DETAIL REQUEST | "
        f"{row['symbol']} | "
        f"conId={con_id} | "
        f"reqId={req_id}"
    )

    app.reqContractDetails(
        req_id,
        contract,
    )

    completed = (
        event.wait(
            timeout=
                CONTRACT_DETAILS_TIMEOUT_SECONDS
        )
    )

    if not completed:

        print(
            "DETAIL TIMEOUT | "
            f"{row['symbol']} | "
            f"conId={con_id}"
        )

        return

    metadata = (
        app.contract_metadata.get(
            con_id
        )
    )

    if metadata:

        apply_metadata_to_row(
            row,
            metadata,
        )

        print(
            "DETAIL RESULT | "
            f"{row['symbol']} | "
            f"type={row['instrument_type']} | "
            f"name={row['long_name'][:60]}"
        )

    else:

        print(
            "DETAIL RESULT | "
            f"{row['symbol']} | "
            "type=UNKNOWN"
        )


def apply_metadata_to_row(
    row,
    metadata,
):
    instrument_type = (
        metadata.get(
            "instrument_type"
        )
    )

    if instrument_type:

        row[
            "instrument_type"
        ] = instrument_type

    long_name = (
        metadata.get(
            "long_name"
        )
    )

    if long_name:

        row[
            "long_name"
        ] = long_name

    industry = (
        metadata.get(
            "industry"
        )
    )

    if industry:

        row[
            "industry"
        ] = industry

    category = (
        metadata.get(
            "category"
        )
    )

    if category:

        row[
            "category"
        ] = category

    subcategory = (
        metadata.get(
            "subcategory"
        )
    )

    if subcategory:

        row[
            "subcategory"
        ] = subcategory

    primary_exchange = (
        metadata.get(
            "primary_exchange"
        )
    )

    if primary_exchange:

        row[
            "primary_exchange"
        ] = primary_exchange


def enrich_scan_rows(
    app,
    scanner_req_id,
):
    rows = [
        row
        for row
        in app.rows
        if row[
            "req_id"
        ]
        ==
        scanner_req_id
    ]

    if not rows:

        return

    print()
    print(
        "ENRICHING CONTRACT DETAILS | "
        f"rows={len(rows)}"
    )

    seen = set()

    for row in rows:

        con_id = (
            row[
                "con_id"
            ]
        )

        if con_id in seen:

            continue

        seen.add(
            con_id
        )

        enrich_row_contract_details(
            app,
            row,
        )

        #
        # Tiny pause keeps contract-details
        # requests civilised.
        #
        time.sleep(
            0.05
        )

    #
    # Apply cached metadata to duplicate rows
    # returned by this scan.
    #
    for row in rows:

        metadata = (
            app.contract_metadata.get(
                row[
                    "con_id"
                ]
            )
        )

        if metadata:

            apply_metadata_to_row(
                row,
                metadata,
            )


# ============================================================
# RUN ONE SCAN
# ============================================================

def run_scan(
    app,
    scan_definition,
):
    req_id = (
        scan_definition[
            "req_id"
        ]
    )

    name = (
        scan_definition[
            "name"
        ]
    )

    scan_code = (
        scan_definition[
            "scan_code"
        ]
    )

    print()
    print(
        "========================================"
    )

    print(
        f"SCAN: {name}"
    )

    print(
        "========================================"
    )

    event = (
        threading.Event()
    )

    app.scan_events[
        req_id
    ] = event

    subscription = (
        build_scanner_subscription(
            scan_code
        )
    )

    app.reqScannerSubscription(
        req_id,
        subscription,
        [],
        [],
    )

    completed = (
        event.wait(
            timeout=
                SCANNER_TIMEOUT_SECONDS
        )
    )

    if not completed:

        print(
            "SCAN TIMEOUT | "
            f"{name}"
        )

    try:

        app.cancelScannerSubscription(
            req_id
        )

    except Exception as exc:

        print(
            "SCAN CANCEL WARNING | "
            f"{name} | "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

    #
    # Important v3 step:
    #
    # Scanner returns the opportunity.
    # ContractDetails resolves what the thing
    # actually is.
    #
    enrich_scan_rows(
        app,
        req_id,
    )

    time.sleep(
        0.25
    )


# ============================================================
# MERGE / DEDUPE
# ============================================================

def build_dynamic_watchlist(
    rows,
):
    merged = {}

    for row in rows:

        symbol = (
            row[
                "symbol"
            ]
        )

        if symbol not in merged:

            merged[
                symbol
            ] = {
                "symbol":
                    symbol,

                "con_id":
                    row[
                        "con_id"
                    ],

                "exchange":
                    row[
                        "exchange"
                    ],

                "primary_exchange":
                    row[
                        "primary_exchange"
                    ],

                "currency":
                    row[
                        "currency"
                    ],

                "instrument_type":
                    row[
                        "instrument_type"
                    ],

                "long_name":
                    row[
                        "long_name"
                    ],

                "industry":
                    row[
                        "industry"
                    ],

                "category":
                    row[
                        "category"
                    ],

                "subcategory":
                    row[
                        "subcategory"
                    ],

                "scans":
                    [],

                "long_score":
                    0,

                "short_score":
                    0,

                "activity_score":
                    0,
            }

        item = (
            merged[
                symbol
            ]
        )

        if (
            item[
                "instrument_type"
            ]
            ==
            "UNKNOWN"
            and
            row[
                "instrument_type"
            ]
            !=
            "UNKNOWN"
        ):

            item[
                "instrument_type"
            ] = (
                row[
                    "instrument_type"
                ]
            )

        if (
            not item[
                "long_name"
            ]
            and
            row[
                "long_name"
            ]
        ):

            item[
                "long_name"
            ] = (
                row[
                    "long_name"
                ]
            )

        if (
            not item[
                "industry"
            ]
            and
            row[
                "industry"
            ]
        ):

            item[
                "industry"
            ] = (
                row[
                    "industry"
                ]
            )

        if (
            not item[
                "category"
            ]
            and
            row[
                "category"
            ]
        ):

            item[
                "category"
            ] = (
                row[
                    "category"
                ]
            )

        if (
            not item[
                "subcategory"
            ]
            and
            row[
                "subcategory"
            ]
        ):

            item[
                "subcategory"
            ] = (
                row[
                    "subcategory"
                ]
            )

        scan_name = (
            row[
                "scan_name"
            ]
        )

        rank = (
            row[
                "rank"
            ]
        )

        item[
            "scans"
        ].append(
            {
                "name":
                    scan_name,

                "rank":
                    rank,
            }
        )

        score = max(
            ROWS_PER_SCAN
            -
            rank,
            1,
        )

        if (
            row[
                "bias"
            ]
            ==
            "LONG"
        ):

            item[
                "long_score"
            ] += score

        elif (
            row[
                "bias"
            ]
            ==
            "SHORT"
        ):

            item[
                "short_score"
            ] += score

        else:

            item[
                "activity_score"
            ] += score

    results = []

    for item in (
        merged.values()
    ):

        long_score = (
            item[
                "long_score"
            ]
        )

        short_score = (
            item[
                "short_score"
            ]
        )

        activity_score = (
            item[
                "activity_score"
            ]
        )

        if (
            long_score
            >
            short_score
            and
            long_score
            >
            0
        ):

            bias = (
                "LONG"
            )

        elif (
            short_score
            >
            long_score
            and
            short_score
            >
            0
        ):

            bias = (
                "SHORT"
            )

        else:

            bias = (
                "NEUTRAL"
            )

        total_score = (
            max(
                long_score,
                short_score,
            )
            +
            (
                activity_score
                *
                0.5
            )
        )

        item[
            "bias"
        ] = bias

        item[
            "score"
        ] = round(
            total_score,
            2,
        )

        results.append(
            item
        )

    results.sort(
        key=lambda item:
            item[
                "score"
            ],
        reverse=True,
    )

    return results


# ============================================================
# DISPLAY
# ============================================================

def print_watchlist(
    watchlist,
):
    now = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    print()
    print(
        "================================================================================"
    )

    print(
        "TRADINGMAX DYNAMIC WATCHLIST"
    )

    print(
        "MULTI-INSTRUMENT UNIVERSE"
    )

    print(
        "================================================================================"
    )

    print(
        f"Generated UTC : "
        f"{now}"
    )

    print(
        f"Symbols       : "
        f"{len(watchlist)}"
    )

    print(
        "================================================================================"
    )

    print()

    header = (
        f"{'#':>2} "
        f"{'SYMBOL':<8} "
        f"{'TYPE':<10} "
        f"{'BIAS':<8} "
        f"{'SCORE':>7} "
        f"{'PRIMARY':<10} "
        f"{'NAME'}"
    )

    print(
        header
    )

    print(
        "-" * len(
            header
        )
    )

    for index, item in enumerate(
        watchlist,
        start=1,
    ):

        print(
            f"{index:>2} "
            f"{item['symbol']:<8} "
            f"{item['instrument_type']:<10} "
            f"{item['bias']:<8} "
            f"{item['score']:>7.2f} "
            f"{item['primary_exchange']:<10} "
            f"{item['long_name'][:50]}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    app = (
        TradingMaxScanner()
    )

    try:

        connect_and_wait(
            app
        )

        for scan_definition in (
            SCAN_DEFINITIONS
        ):

            run_scan(
                app,
                scan_definition,
            )

        watchlist = (
            build_dynamic_watchlist(
                app.rows
            )
        )

        print_watchlist(
            watchlist
        )

    except KeyboardInterrupt:

        print(
            "\nInterrupted by user"
        )

    except Exception as exc:

        print(
            "SCANNER FAILED | "
            f"{type(exc).__name__}: "
            f"{exc}"
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
