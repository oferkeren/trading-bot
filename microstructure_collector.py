import math
import os
import signal
import sqlite3
import threading
import time

from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent


ENV_FILE = (
    BASE_DIR
    /
    ".env"
)


EARLY_DB_FILE = (
    BASE_DIR
    /
    "early_momentum.db"
)


MICRO_DB_FILE = (
    BASE_DIR
    /
    "microstructure.db"
)


load_dotenv(
    ENV_FILE
)


# ============================================================
# CONFIG
# ============================================================

IB_HOST = (
    os.getenv(
        "IB_HOST",
        "127.0.0.1",
    )
)


IB_PORT = int(
    os.getenv(
        "IB_PORT",
        "7496",
    )
)


IB_CLIENT_ID = int(
    os.getenv(
        "MICROSTRUCTURE_IB_CLIENT_ID",
        "81",
    )
)


MAX_SYMBOLS = int(
    os.getenv(
        "MICROSTRUCTURE_MAX_SYMBOLS",
        "8",
    )
)


WATCH_MAX_AGE_SECONDS = int(
    os.getenv(
        "MICROSTRUCTURE_WATCH_MAX_AGE_SECONDS",
        "180",
    )
)


WATCH_REFRESH_SECONDS = int(
    os.getenv(
        "MICROSTRUCTURE_WATCH_REFRESH_SECONDS",
        "15",
    )
)


FEATURE_PRINT_SECONDS = int(
    os.getenv(
        "MICROSTRUCTURE_FEATURE_PRINT_SECONDS",
        "15",
    )
)


RETENTION_SECONDS = int(
    os.getenv(
        "MICROSTRUCTURE_RETENTION_SECONDS",
        "3600",
    )
)


CONTRACT_TIMEOUT_SECONDS = float(
    os.getenv(
        "MICROSTRUCTURE_CONTRACT_TIMEOUT_SECONDS",
        "5",
    )
)


GENERIC_TICKS = (
    "233,293,294,295"
)


# ============================================================
# IBKR FIELD IDS
# ============================================================

BID_SIZE = 0
BID_PRICE = 1
ASK_PRICE = 2
ASK_SIZE = 3

RT_VOLUME = 48

TRADE_COUNT = 54
TRADE_RATE = 55
VOLUME_RATE = 56


# ============================================================
# STATE
# ============================================================

STOP_REQUESTED = False


# ============================================================
# DATABASE
# ============================================================

def micro_db_connect():
    conn = sqlite3.connect(
        MICRO_DB_FILE,
        timeout=10,
    )

    conn.row_factory = (
        sqlite3.Row
    )

    return conn


def early_db_connect():
    conn = sqlite3.connect(
        EARLY_DB_FILE,
        timeout=10,
    )

    conn.row_factory = (
        sqlite3.Row
    )

    return conn


def init_micro_db():
    conn = micro_db_connect()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                ts REAL NOT NULL,

                symbol TEXT NOT NULL,

                price REAL NOT NULL,
                size REAL NOT NULL,

                source TEXT NOT NULL
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_micro_trades_symbol_ts
            ON trades (
                symbol,
                ts
            )
            """
        )


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                ts REAL NOT NULL,

                symbol TEXT NOT NULL,

                bid REAL,
                ask REAL,

                bid_size REAL,
                ask_size REAL,

                spread_pct REAL
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_micro_quotes_symbol_ts
            ON quotes (
                symbol,
                ts
            )
            """
        )


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                ts REAL NOT NULL,

                symbol TEXT NOT NULL,

                trade_count REAL,
                trade_rate REAL,
                volume_rate REAL
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_micro_metrics_symbol_ts
            ON market_metrics (
                symbol,
                ts
            )
            """
        )


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                ts REAL NOT NULL,

                symbol TEXT NOT NULL,

                window_seconds INTEGER NOT NULL,

                trade_count INTEGER NOT NULL,

                local_trade_rate REAL NOT NULL,

                volume REAL NOT NULL,
                local_volume_rate REAL NOT NULL,

                ib_trade_count REAL,
                ib_trade_rate REAL,
                ib_volume_rate REAL,

                price_change_pct REAL,

                trade_rate_acceleration REAL,
                volume_rate_acceleration REAL,

                bid_ask_imbalance REAL,
                spread_pct REAL
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_micro_features_symbol_ts
            ON features (
                symbol,
                ts
            )
            """
        )


        conn.commit()

    finally:
        conn.close()


# ============================================================
# HELPERS
# ============================================================

def safe_float(
    value,
):
    if value is None:
        return None


    try:
        result = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


    if not math.isfinite(
        result
    ):
        return None


    return result


def spread_percent(
    bid,
    ask,
):
    bid = safe_float(
        bid
    )

    ask = safe_float(
        ask
    )


    if (
        bid is None
        or
        ask is None
        or
        bid <= 0
        or
        ask <= 0
        or
        ask < bid
    ):
        return None


    midpoint = (
        bid
        +
        ask
    ) / 2.0


    if midpoint <= 0:
        return None


    return (
        (
            ask
            -
            bid
        )
        /
        midpoint
        *
        100.0
    )


# ============================================================
# WATCHLIST
# ============================================================

def get_recent_symbols():
    if not EARLY_DB_FILE.exists():
        return []


    cutoff = (
        time.time()
        -
        WATCH_MAX_AGE_SECONDS
    )


    conn = early_db_connect()

    try:
        rows = conn.execute(
            """
            SELECT
                symbol,

                MAX(ts)
                    AS last_seen,

                MAX(
                    COALESCE(
                        effective_score,
                        scanner_score,
                        0
                    )
                )
                    AS score

            FROM snapshots

            WHERE
                ts >= ?

            GROUP BY symbol

            ORDER BY
                last_seen DESC,
                score DESC

            LIMIT ?
            """,
            (
                cutoff,
                MAX_SYMBOLS,
            ),
        ).fetchall()


        return [
            str(
                row[
                    "symbol"
                ]
            )
            .strip()
            .upper()

            for row
            in rows

            if row[
                "symbol"
            ]
        ]


    finally:
        conn.close()


# ============================================================
# APP
# ============================================================

class MicrostructureApp(
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


        self.contract_events = {}


        self.contract_results = defaultdict(
            list
        )


        self.req_counter = 81000


        self.req_to_symbol = {}


        self.symbol_to_req = {}


        self.state = {}


    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def nextValidId(
        self,
        orderId,
    ):
        print(
            "MICRO CONNECTED | "
            f"clientId={IB_CLIENT_ID} | "
            f"nextOrderId={orderId}",
            flush=True,
        )


        self.connected_event.set()


    def connectionClosed(
        self,
    ):
        print(
            "MICRO CONNECTION CLOSED",
            flush=True,
        )


    # --------------------------------------------------------
    # ERROR
    # --------------------------------------------------------

    def error(
        self,
        reqId,
        *args,
    ):
        error_time = None
        error_code = None
        error_string = ""


        if len(args) == 4:
            (
                error_time,
                error_code,
                error_string,
                advanced_json,
            ) = args


        elif len(args) == 3:
            (
                error_code,
                error_string,
                advanced_json,
            ) = args


        elif len(args) == 2:
            (
                error_code,
                error_string,
            ) = args


        elif len(args) == 1:
            error_string = str(
                args[
                    0
                ]
            )


        if error_code in {
            2104,
            2106,
            2158,
        }:
            return


        print(
            "MICRO IB ERROR | "
            f"reqId={reqId} | "
            f"code={error_code} | "
            f"{error_string}",
            flush=True,
        )


    # --------------------------------------------------------
    # CONTRACT
    # --------------------------------------------------------

    def contractDetails(
        self,
        reqId,
        contractDetails,
    ):
        self.contract_results[
            reqId
        ].append(
            contractDetails
        )


    def contractDetailsEnd(
        self,
        reqId,
    ):
        event = (
            self.contract_events.get(
                reqId
            )
        )


        if event is not None:
            event.set()


    # --------------------------------------------------------
    # BID / ASK
    # --------------------------------------------------------

    def tickPrice(
        self,
        reqId,
        tickType,
        price,
        attrib,
    ):
        symbol = (
            self.req_to_symbol.get(
                reqId
            )
        )


        if not symbol:
            return


        state = (
            self.state.setdefault(
                symbol,
                {},
            )
        )


        value = safe_float(
            price
        )


        if tickType == BID_PRICE:
            state[
                "bid"
            ] = value


        elif tickType == ASK_PRICE:
            state[
                "ask"
            ] = value


        else:
            return


        self.save_quote_if_ready(
            symbol
        )


    def tickSize(
        self,
        reqId,
        tickType,
        size,
    ):
        symbol = (
            self.req_to_symbol.get(
                reqId
            )
        )


        if not symbol:
            return


        state = (
            self.state.setdefault(
                symbol,
                {},
            )
        )


        value = safe_float(
            size
        )


        if tickType == BID_SIZE:
            state[
                "bid_size"
            ] = value


        elif tickType == ASK_SIZE:
            state[
                "ask_size"
            ] = value


        else:
            return


        self.save_quote_if_ready(
            symbol
        )


    def save_quote_if_ready(
        self,
        symbol,
    ):
        state = (
            self.state.setdefault(
                symbol,
                {},
            )
        )


        bid = safe_float(
            state.get(
                "bid"
            )
        )


        ask = safe_float(
            state.get(
                "ask"
            )
        )


        bid_size = safe_float(
            state.get(
                "bid_size"
            )
        )


        ask_size = safe_float(
            state.get(
                "ask_size"
            )
        )


        if (
            bid is None
            or
            ask is None
        ):
            return


        now = time.time()


        last_saved = safe_float(
            state.get(
                "last_quote_saved_ts"
            )
        )


        if (
            last_saved is not None
            and
            now
            -
            last_saved
            <
            0.20
        ):
            return


        state[
            "last_quote_saved_ts"
        ] = now


        conn = micro_db_connect()

        try:
            conn.execute(
                """
                INSERT INTO quotes (
                    ts,

                    symbol,

                    bid,
                    ask,

                    bid_size,
                    ask_size,

                    spread_pct
                )

                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    now,

                    symbol,

                    bid,
                    ask,

                    bid_size,
                    ask_size,

                    spread_percent(
                        bid,
                        ask,
                    ),
                ),
            )


            conn.commit()

        finally:
            conn.close()


    # --------------------------------------------------------
    # RT VOLUME
    # --------------------------------------------------------

    def tickString(
        self,
        reqId,
        tickType,
        value,
    ):
        if tickType != RT_VOLUME:
            return


        symbol = (
            self.req_to_symbol.get(
                reqId
            )
        )


        if not symbol:
            return


        parts = str(
            value
        ).split(
            ";"
        )


        if len(parts) < 3:
            return


        price = safe_float(
            parts[
                0
            ]
        )


        size = safe_float(
            parts[
                1
            ]
        )


        timestamp_ms = safe_float(
            parts[
                2
            ]
        )


        if (
            price is None
            or
            size is None
            or
            timestamp_ms is None
            or
            price <= 0
            or
            size < 0
        ):
            return


        trade_ts = (
            timestamp_ms
            /
            1000.0
        )


        conn = micro_db_connect()

        try:
            conn.execute(
                """
                INSERT INTO trades (
                    ts,
                    symbol,
                    price,
                    size,
                    source
                )

                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    trade_ts,

                    symbol,

                    price,
                    size,

                    "RTVOLUME",
                ),
            )


            conn.commit()

        finally:
            conn.close()


    # --------------------------------------------------------
    # TRADE METRICS
    # --------------------------------------------------------

    def tickGeneric(
        self,
        reqId,
        tickType,
        value,
    ):
        if tickType not in {
            TRADE_COUNT,
            TRADE_RATE,
            VOLUME_RATE,
        }:
            return


        symbol = (
            self.req_to_symbol.get(
                reqId
            )
        )


        if not symbol:
            return


        state = (
            self.state.setdefault(
                symbol,
                {},
            )
        )


        value = safe_float(
            value
        )


        if tickType == TRADE_COUNT:
            state[
                "ib_trade_count"
            ] = value


        elif tickType == TRADE_RATE:
            state[
                "ib_trade_rate"
            ] = value


        elif tickType == VOLUME_RATE:
            state[
                "ib_volume_rate"
            ] = value


        now = time.time()


        last_saved = safe_float(
            state.get(
                "last_metric_saved_ts"
            )
        )


        if (
            last_saved is not None
            and
            now
            -
            last_saved
            <
            1.0
        ):
            return


        state[
            "last_metric_saved_ts"
        ] = now


        conn = micro_db_connect()

        try:
            conn.execute(
                """
                INSERT INTO market_metrics (
                    ts,
                    symbol,
                    trade_count,
                    trade_rate,
                    volume_rate
                )

                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    now,

                    symbol,

                    safe_float(
                        state.get(
                            "ib_trade_count"
                        )
                    ),

                    safe_float(
                        state.get(
                            "ib_trade_rate"
                        )
                    ),

                    safe_float(
                        state.get(
                            "ib_volume_rate"
                        )
                    ),
                ),
            )


            conn.commit()

        finally:
            conn.close()


# ============================================================
# CONNECTION
# ============================================================

def network_loop(
    app,
):
    try:
        app.run()

    except Exception as exc:
        print(
            "MICRO NETWORK ERROR | "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )


def connect_app():
    app = (
        MicrostructureApp()
    )


    app.connect(
        IB_HOST,
        IB_PORT,
        IB_CLIENT_ID,
    )


    thread = threading.Thread(
        target=
            network_loop,

        args=(
            app,
        ),

        daemon=True,
    )


    thread.start()


    if not app.connected_event.wait(
        timeout=10
    ):
        raise RuntimeError(
            "Microstructure IBKR handshake timeout"
        )


    if not app.isConnected():
        raise RuntimeError(
            "Microstructure IBKR not connected"
        )


    app.reqMarketDataType(
        1
    )


    return app


# ============================================================
# CONTRACT
# ============================================================

def resolve_contract(
    app,
    symbol,
):
    req_id = (
        app.req_counter
    )


    app.req_counter += 1


    event = (
        threading.Event()
    )


    app.contract_events[
        req_id
    ] = event


    contract = (
        Contract()
    )


    contract.symbol = (
        symbol
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


    app.reqContractDetails(
        req_id,
        contract,
    )


    if not event.wait(
        timeout=
            CONTRACT_TIMEOUT_SECONDS
    ):
        app.contract_events.pop(
            req_id,
            None,
        )

        return None


    results = (
        app.contract_results.pop(
            req_id,
            [],
        )
    )


    app.contract_events.pop(
        req_id,
        None,
    )


    if not results:
        return None


    selected = (
        results[
            0
        ]
    )


    resolved = (
        selected.contract
    )


    resolved.exchange = (
        "SMART"
    )


    print(
        "MICRO CONTRACT | "
        f"{symbol} | "
        f"conId={resolved.conId} | "
        f"primary="
        f"{resolved.primaryExchange}",
        flush=True,
    )


    return resolved


# ============================================================
# SUBSCRIPTIONS
# ============================================================

def subscribe_symbol(
    app,
    symbol,
):
    if symbol in app.symbol_to_req:
        return True


    contract = (
        resolve_contract(
            app,
            symbol,
        )
    )


    if contract is None:
        print(
            "MICRO SUBSCRIBE SKIP | "
            f"{symbol} | "
            "contract unresolved",
            flush=True,
        )

        return False


    req_id = (
        app.req_counter
    )


    app.req_counter += 1


    app.req_to_symbol[
        req_id
    ] = symbol


    app.symbol_to_req[
        symbol
    ] = req_id


    app.state.setdefault(
        symbol,
        {},
    )


    app.reqMktData(
        req_id,
        contract,
        GENERIC_TICKS,
        False,
        False,
        [],
    )


    print(
        "MICRO SUBSCRIBED | "
        f"{symbol} | "
        f"reqId={req_id} | "
        f"ticks={GENERIC_TICKS}",
        flush=True,
    )


    return True


def unsubscribe_symbol(
    app,
    symbol,
):
    req_id = (
        app.symbol_to_req.pop(
            symbol,
            None,
        )
    )


    if req_id is None:
        return


    try:
        app.cancelMktData(
            req_id
        )

    except Exception:
        pass


    app.req_to_symbol.pop(
        req_id,
        None,
    )


    print(
        "MICRO UNSUBSCRIBED | "
        f"{symbol}",
        flush=True,
    )


def refresh_subscriptions(
    app,
):
    desired = set(
        get_recent_symbols()
    )


    current = set(
        app.symbol_to_req.keys()
    )


    for symbol in sorted(
        current
        -
        desired
    ):
        unsubscribe_symbol(
            app,
            symbol,
        )


    for symbol in sorted(
        desired
        -
        current
    ):
        subscribe_symbol(
            app,
            symbol,
        )


    print(
        "MICRO WATCH | "
        f"desired={len(desired)} | "
        f"active="
        f"{len(app.symbol_to_req)} | "
        f"symbols="
        f"{','.join(sorted(desired))}",
        flush=True,
    )


# ============================================================
# FEATURE READERS
# ============================================================

def get_trade_window(
    conn,
    symbol,
    start_ts,
    end_ts,
):
    rows = conn.execute(
        """
        SELECT
            ts,
            price,
            size

        FROM trades

        WHERE
            symbol = ?
            AND ts >= ?
            AND ts <= ?

        ORDER BY ts ASC
        """,
        (
            symbol,
            start_ts,
            end_ts,
        ),
    ).fetchall()


    return [
        dict(
            row
        )

        for row
        in rows
    ]


def get_latest_quote(
    conn,
    symbol,
    start_ts,
    end_ts,
):
    row = conn.execute(
        """
        SELECT *
        FROM quotes

        WHERE
            symbol = ?
            AND ts >= ?
            AND ts <= ?

        ORDER BY ts DESC

        LIMIT 1
        """,
        (
            symbol,
            start_ts,
            end_ts,
        ),
    ).fetchone()


    if row is None:
        return None


    return dict(
        row
    )


def get_latest_metric(
    conn,
    symbol,
    start_ts,
    end_ts,
):
    row = conn.execute(
        """
        SELECT *
        FROM market_metrics

        WHERE
            symbol = ?
            AND ts >= ?
            AND ts <= ?

        ORDER BY ts DESC

        LIMIT 1
        """,
        (
            symbol,
            start_ts,
            end_ts,
        ),
    ).fetchone()


    if row is None:
        return None


    return dict(
        row
    )


# ============================================================
# FEATURES
# ============================================================

def calculate_window_features(
    conn,
    symbol,
    window_seconds,
    now_ts,
):
    start_ts = (
        now_ts
        -
        window_seconds
    )


    half_ts = (
        now_ts
        -
        (
            window_seconds
            /
            2.0
        )
    )


    trades = (
        get_trade_window(
            conn,
            symbol,
            start_ts,
            now_ts,
        )
    )


    previous_half = [
        row
        for row
        in trades
        if row[
            "ts"
        ]
        <
        half_ts
    ]


    current_half = [
        row
        for row
        in trades
        if row[
            "ts"
        ]
        >=
        half_ts
    ]


    trade_count = (
        len(
            trades
        )
    )


    local_trade_rate = (
        trade_count
        /
        window_seconds
    )


    volume = sum(
        float(
            row[
                "size"
            ]
            or
            0.0
        )
        for row
        in trades
    )


    local_volume_rate = (
        volume
        /
        window_seconds
    )


    half_seconds = (
        window_seconds
        /
        2.0
    )


    previous_trade_rate = (
        len(
            previous_half
        )
        /
        half_seconds
    )


    current_trade_rate = (
        len(
            current_half
        )
        /
        half_seconds
    )


    previous_volume = sum(
        float(
            row[
                "size"
            ]
            or
            0.0
        )
        for row
        in previous_half
    )


    current_volume = sum(
        float(
            row[
                "size"
            ]
            or
            0.0
        )
        for row
        in current_half
    )


    previous_volume_rate = (
        previous_volume
        /
        half_seconds
    )


    current_volume_rate = (
        current_volume
        /
        half_seconds
    )


    if previous_trade_rate > 0:
        trade_rate_acceleration = (
            (
                current_trade_rate
                -
                previous_trade_rate
            )
            /
            previous_trade_rate
        )

    else:
        trade_rate_acceleration = (
            1.0
            if current_trade_rate > 0
            else 0.0
        )


    if previous_volume_rate > 0:
        volume_rate_acceleration = (
            (
                current_volume_rate
                -
                previous_volume_rate
            )
            /
            previous_volume_rate
        )

    else:
        volume_rate_acceleration = (
            1.0
            if current_volume_rate > 0
            else 0.0
        )


    price_change_pct = None


    if len(
        trades
    ) >= 2:

        first_price = safe_float(
            trades[
                0
            ][
                "price"
            ]
        )


        last_price = safe_float(
            trades[
                -1
            ][
                "price"
            ]
        )


        if (
            first_price is not None
            and
            last_price is not None
            and
            first_price > 0
        ):
            price_change_pct = (
                (
                    last_price
                    -
                    first_price
                )
                /
                first_price
                *
                100.0
            )


    quote = (
        get_latest_quote(
            conn,
            symbol,
            start_ts,
            now_ts,
        )
    )


    metric = (
        get_latest_metric(
            conn,
            symbol,
            start_ts,
            now_ts,
        )
    )


    imbalance = None

    spread = None


    if quote is not None:
        bid_size = safe_float(
            quote.get(
                "bid_size"
            )
        )


        ask_size = safe_float(
            quote.get(
                "ask_size"
            )
        )


        spread = safe_float(
            quote.get(
                "spread_pct"
            )
        )


        if (
            bid_size is not None
            and
            ask_size is not None
            and
            bid_size
            +
            ask_size
            >
            0
        ):
            imbalance = (
                (
                    bid_size
                    -
                    ask_size
                )
                /
                (
                    bid_size
                    +
                    ask_size
                )
            )


    return {
        "symbol":
            symbol,

        "window_seconds":
            window_seconds,

        "trade_count":
            trade_count,

        "local_trade_rate":
            local_trade_rate,

        "volume":
            volume,

        "local_volume_rate":
            local_volume_rate,

        "ib_trade_count":
            (
                safe_float(
                    metric.get(
                        "trade_count"
                    )
                )
                if metric
                else None
            ),

        "ib_trade_rate":
            (
                safe_float(
                    metric.get(
                        "trade_rate"
                    )
                )
                if metric
                else None
            ),

        "ib_volume_rate":
            (
                safe_float(
                    metric.get(
                        "volume_rate"
                    )
                )
                if metric
                else None
            ),

        "price_change_pct":
            price_change_pct,

        "trade_rate_acceleration":
            trade_rate_acceleration,

        "volume_rate_acceleration":
            volume_rate_acceleration,

        "bid_ask_imbalance":
            imbalance,

        "spread_pct":
            spread,
    }


def save_feature(
    conn,
    feature,
    ts,
):
    conn.execute(
        """
        INSERT INTO features (
            ts,

            symbol,
            window_seconds,

            trade_count,
            local_trade_rate,

            volume,
            local_volume_rate,

            ib_trade_count,
            ib_trade_rate,
            ib_volume_rate,

            price_change_pct,

            trade_rate_acceleration,
            volume_rate_acceleration,

            bid_ask_imbalance,
            spread_pct
        )

        VALUES (
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?
        )
        """,
        (
            ts,

            feature[
                "symbol"
            ],

            feature[
                "window_seconds"
            ],

            feature[
                "trade_count"
            ],

            feature[
                "local_trade_rate"
            ],

            feature[
                "volume"
            ],

            feature[
                "local_volume_rate"
            ],

            feature[
                "ib_trade_count"
            ],

            feature[
                "ib_trade_rate"
            ],

            feature[
                "ib_volume_rate"
            ],

            feature[
                "price_change_pct"
            ],

            feature[
                "trade_rate_acceleration"
            ],

            feature[
                "volume_rate_acceleration"
            ],

            feature[
                "bid_ask_imbalance"
            ],

            feature[
                "spread_pct"
            ],
        ),
    )


def calculate_features(
    app,
):
    now_ts = (
        time.time()
    )


    conn = micro_db_connect()

    try:
        for symbol in sorted(
            app.symbol_to_req.keys()
        ):
            for window in (
                15,
                30,
                60,
            ):
                feature = (
                    calculate_window_features(
                        conn,
                        symbol,
                        window,
                        now_ts,
                    )
                )


                save_feature(
                    conn,
                    feature,
                    now_ts,
                )


                print(
                    "MICRO FEATURE | "
                    f"{symbol} | "
                    f"{window}s | "
                    f"local_trades="
                    f"{feature['trade_count']} | "
                    f"local_rate="
                    f"{feature['local_trade_rate']:.2f}/s | "
                    f"ib_rate="
                    f"{feature['ib_trade_rate']} | "
                    f"ib_vol_rate="
                    f"{feature['ib_volume_rate']} | "
                    f"trade_acc="
                    f"{feature['trade_rate_acceleration']:+.2f} | "
                    f"vol_acc="
                    f"{feature['volume_rate_acceleration']:+.2f} | "
                    f"price="
                    f"{feature['price_change_pct']} | "
                    f"imbalance="
                    f"{feature['bid_ask_imbalance']} | "
                    f"spread="
                    f"{feature['spread_pct']}",
                    flush=True,
                )


        cutoff = (
            now_ts
            -
            RETENTION_SECONDS
        )


        conn.execute(
            """
            DELETE FROM trades
            WHERE ts < ?
            """,
            (
                cutoff,
            ),
        )


        conn.execute(
            """
            DELETE FROM quotes
            WHERE ts < ?
            """,
            (
                cutoff,
            ),
        )


        conn.execute(
            """
            DELETE FROM market_metrics
            WHERE ts < ?
            """,
            (
                cutoff,
            ),
        )


        conn.execute(
            """
            DELETE FROM features
            WHERE ts < ?
            """,
            (
                cutoff,
            ),
        )


        conn.commit()

    finally:
        conn.close()


# ============================================================
# SIGNAL HANDLER
# ============================================================

def stop_handler(
    signum,
    frame,
):
    global STOP_REQUESTED


    STOP_REQUESTED = True


    print(
        "MICRO STOP REQUESTED | "
        f"signal={signum}",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    global STOP_REQUESTED


    init_micro_db()


    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )


    signal.signal(
        signal.SIGINT,
        stop_handler,
    )


    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX MICROSTRUCTURE COLLECTOR V2"
    )

    print(
        "=============================================================="
    )

    print(
        f"IB clientId : "
        f"{IB_CLIENT_ID}"
    )

    print(
        f"Max symbols : "
        f"{MAX_SYMBOLS}"
    )

    print(
        f"Generic     : "
        f"{GENERIC_TICKS}"
    )

    print(
        "Mode        : OBSERVATIONAL ONLY"
    )

    print(
        "==============================================================",
        flush=True,
    )


    app = (
        connect_app()
    )


    last_watch_refresh = (
        0.0
    )


    last_feature_print = (
        0.0
    )


    try:
        while (
            not STOP_REQUESTED
        ):
            now = (
                time.monotonic()
            )


            if (
                now
                -
                last_watch_refresh
                >=
                WATCH_REFRESH_SECONDS
            ):
                refresh_subscriptions(
                    app
                )


                last_watch_refresh = (
                    now
                )


            if (
                now
                -
                last_feature_print
                >=
                FEATURE_PRINT_SECONDS
            ):
                calculate_features(
                    app
                )


                last_feature_print = (
                    now
                )


            time.sleep(
                0.5
            )


    finally:
        for symbol in list(
            app.symbol_to_req.keys()
        ):
            unsubscribe_symbol(
                app,
                symbol,
            )


        if app.isConnected():
            app.disconnect()


        time.sleep(
            1
        )


        print(
            "MICROSTRUCTURE COLLECTOR STOPPED",
            flush=True,
        )


if __name__ == "__main__":
    main()
