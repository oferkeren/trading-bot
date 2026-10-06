import os
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

import json
import threading
import time

from datetime import datetime, timezone, time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo

import scanner


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
CLIENT_ID = 73

MAX_CANDIDATES = 30
CORE_FRACTION = 0.60
HOT_POOL_SIZE = 16
HOT_CORE_TARGET = 11
HOT_ROTATE_TARGET = 5

MAX_QUOTE_AGE_SECONDS = 15.0

STATE_DIR = Path.home() / ".cache" / "tradingmax"

ROTATION_STATE_FILE = (
    STATE_DIR
    /
    "strategy_rotation_state.json"
)

STATE_RETENTION_SECONDS = (
    3
    *
    24
    *
    60
    *
    60
)

HISTORICAL_DURATION = "2 D"
HISTORICAL_BAR_SIZE = "5 mins"
HISTORICAL_TIMEOUT_SECONDS = 15
MARKET_DATA_WAIT_SECONDS = 5

BAR_SECONDS = 300

EMA_FAST = 9
EMA_SLOW = 21

RSI_PERIOD = 14
ATR_PERIOD = 14

BREAKOUT_LOOKBACK = 20
VOLR20_LOOKBACK = 20

ATR_STOP_MULTIPLIER = 1.50
REWARD_RISK = 2.0

LONG_RSI_MIN = 52.0
LONG_RSI_MAX = 78.0

SHORT_RSI_MIN = 22.0
SHORT_RSI_MAX = 48.0


NEW_YORK = ZoneInfo(
    "America/New_York"
)

RTH_START = datetime_time(
    9,
    30,
)

RTH_END = datetime_time(
    16,
    0,
)

EXTENDED_START = datetime_time(
    hour=4,
    minute=0,
)

EXTENDED_END = datetime_time(
    hour=20,
    minute=0,
)



INSTRUMENT_PROFILES = {
    "COMMON": {
        "min_volr20": 1.20,
        "max_spread_pct": 1.50,
        "min_support": 3,
    },

    "CORP": {
        "min_volr20": 1.20,
        "max_spread_pct": 1.50,
        "min_support": 3,
    },

    "ADR": {
        "min_volr20": 1.20,
        "max_spread_pct": 1.25,
        "min_support": 3,
    },

    "ETF": {
        "min_volr20": 1.00,
        "max_spread_pct": 0.75,
        "min_support": 3,
    },

    "ETN": {
        "min_volr20": 1.10,
        "max_spread_pct": 0.75,
        "min_support": 3,
    },

    "REIT": {
        "min_volr20": 1.00,
        "max_spread_pct": 1.00,
        "min_support": 3,
    },

    "CEF": {
        "min_volr20": 1.00,
        "max_spread_pct": 1.00,
        "min_support": 3,
    },

    "ETMF": {
        "min_volr20": 1.10,
        "max_spread_pct": 0.75,
        "min_support": 3,
    },

    "EFN": {
        "min_volr20": 1.10,
        "max_spread_pct": 0.75,
        "min_support": 3,
    },

    "UNKNOWN": {
        "min_volr20": 1.25,
        "max_spread_pct": 1.00,
        "min_support": 4,
    },
}


def get_instrument_profile(
    instrument_type,
):
    key = str(
        instrument_type
        or
        "UNKNOWN"
    ).strip().upper()

    return INSTRUMENT_PROFILES.get(
        key,
        INSTRUMENT_PROFILES[
            "UNKNOWN"
        ],
    )


def load_rotation_state():

    default = {
        "cycle": 0,
        "symbols": {},
    }

    try:

        if not ROTATION_STATE_FILE.exists():

            return default


        data = json.loads(
            ROTATION_STATE_FILE.read_text(
                encoding="utf-8"
            )
        )


        if not isinstance(
            data,
            dict,
        ):

            return default


        if not isinstance(
            data.get(
                "symbols"
            ),
            dict,
        ):

            data[
                "symbols"
            ] = {}


        data[
            "cycle"
        ] = int(
            data.get(
                "cycle",
                0,
            )
            or
            0
        )


        return data


    except Exception as exc:

        print(
            "ROTATION STATE WARNING | "
            "load failed | "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        return default


def save_rotation_state(
    state,
):

    try:

        STATE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )


        temporary = (
            ROTATION_STATE_FILE
            .with_suffix(
                ".tmp"
            )
        )


        temporary.write_text(
            json.dumps(
                state,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


        temporary.replace(
            ROTATION_STATE_FILE
        )


    except Exception as exc:

        print(
            "ROTATION STATE WARNING | "
            "save failed | "
            f"{type(exc).__name__}: "
            f"{exc}"
        )


def prune_rotation_state(
    state,
):

    now = time.time()


    symbols = state.get(
        "symbols",
        {},
    )


    stale = []


    for symbol, item in symbols.items():

        last_seen = float(
            item.get(
                "last_seen_ts",
                0.0,
            )
            or
            0.0
        )


        if (
            last_seen > 0
            and
            now - last_seen
            >
            STATE_RETENTION_SECONDS
        ):

            stale.append(
                symbol
            )


    for symbol in stale:

        symbols.pop(
            symbol,
            None,
        )


def prior_near_trigger_bonus(
    symbol_state,
):

    distance = symbol_state.get(
        "trigger_distance_pct"
    )


    if distance is None:

        return 0.0


    try:

        distance = float(
            distance
        )

    except Exception:

        return 0.0


    if distance <= 0.15:

        return 4.0


    if distance <= 0.35:

        return 2.5


    if distance <= 0.75:

        return 1.25


    return 0.0


def recent_selection_penalty(
    symbol_state,
    cycle,
):

    last_cycle = int(
        symbol_state.get(
            "last_selected_cycle",
            0,
        )
        or
        0
    )


    if last_cycle <= 0:

        return 0.0


    age = (
        cycle
        -
        last_cycle
    )


    if age <= 1:

        return 4.0


    if age == 2:

        return 2.0


    if age == 3:

        return 1.0


    return 0.0


def effective_candidate_score(
    candidate,
    symbol_state,
    cycle,
):

    base_score = float(
        candidate.get(
            "score",
            0.0,
        )
        or
        0.0
    )


    qualified_bonus = (
        2.0
        if symbol_state.get(
            "last_qualified"
        )
        else
        0.0
    )


    return (
        base_score
        +
        prior_near_trigger_bonus(
            symbol_state
        )
        +
        qualified_bonus
        -
        recent_selection_penalty(
            symbol_state,
            cycle,
        )
    )



# ============================================================
# IBKR SYMBOL QUARANTINE
# ============================================================

IBKR_SYMBOL_QUARANTINE_FILE = (
    Path.home()
    / ".cache"
    / "tradingmax"
    / "ibkr_symbol_quarantine.json"
)


def load_ibkr_symbol_quarantine():
    now = time.time()

    if not IBKR_SYMBOL_QUARANTINE_FILE.exists():
        return {}

    try:
        data = json.loads(
            IBKR_SYMBOL_QUARANTINE_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            return {}

        active = {}
        changed = False

        for symbol, item in data.items():
            if not isinstance(item, dict):
                changed = True
                continue

            expires_at = float(
                item.get("expires_at", 0)
                or 0
            )

            if expires_at > now:
                active[
                    str(symbol).strip().upper()
                ] = item
            else:
                changed = True

        if changed:
            IBKR_SYMBOL_QUARANTINE_FILE.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            tmp = (
                IBKR_SYMBOL_QUARANTINE_FILE
                .with_suffix(".tmp")
            )

            tmp.write_text(
                json.dumps(
                    active,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            tmp.replace(
                IBKR_SYMBOL_QUARANTINE_FILE
            )

        return active

    except Exception as exc:
        print(
            "IBKR QUARANTINE WARNING | "
            f"{type(exc).__name__}: {exc}"
        )

        return {}

def select_rotating_candidates(
    directional,
):

    if not directional:

        return []


    quarantine = load_ibkr_symbol_quarantine()

    if quarantine:
        filtered = []

        for candidate in directional:
            symbol = str(
                candidate.get(
                    "symbol",
                    "",
                )
            ).strip().upper()

            if symbol in quarantine:
                remaining = max(
                    0,
                    int(
                        quarantine[
                            symbol
                        ].get(
                            "expires_at",
                            0,
                        )
                        - time.time()
                    ),
                )

                print(
                    "QUARANTINE SKIP | "
                    f"{symbol} | "
                    f"remaining={remaining}s"
                )

                continue

            filtered.append(
                candidate
            )

        directional = filtered

    if not directional:
        return []


    state = load_rotation_state()


    prune_rotation_state(
        state
    )


    state[
        "cycle"
    ] = (
        int(
            state.get(
                "cycle",
                0,
            )
            or
            0
        )
        +
        1
    )


    cycle = state[
        "cycle"
    ]


    symbols_state = state[
        "symbols"
    ]


    now = time.time()


    enriched = []


    for candidate in directional:

        symbol = str(
            candidate[
                "symbol"
            ]
        ).strip().upper()


        symbol_state = (
            symbols_state.setdefault(
                symbol,
                {},
            )
        )


        symbol_state[
            "last_seen_ts"
        ] = now


        last_cycle = int(
            symbol_state.get(
                "last_selected_cycle",
                0,
            )
            or
            0
        )


        item = dict(
            candidate
        )


        item[
            "effective_score"
        ] = (
            effective_candidate_score(
                candidate,
                symbol_state,
                cycle,
            )
        )


        item[
            "cycles_since_selected"
        ] = (
            cycle
            -
            last_cycle
            if last_cycle
            else
            999999
        )


        enriched.append(
            item
        )


    limit = min(
        MAX_CANDIDATES,
        len(
            enriched
        ),
    )


    if limit <= 0:

        save_rotation_state(
            state
        )

        return []


    core_count = max(
        1,
        min(
            int(
                round(
                    limit
                    *
                    CORE_FRACTION
                )
            ),
            limit,
        ),
    )


    rotation_count = (
        limit
        -
        core_count
    )


    core_ranked = sorted(
        enriched,
        key=lambda item: (
            float(
                item.get(
                    "effective_score",
                    0.0,
                )
            ),
            float(
                item.get(
                    "score",
                    0.0,
                )
            ),
        ),
        reverse=True,
    )


    selected = []

    selected_symbols = set()


    for item in (
        core_ranked[
            :core_count
        ]
    ):

        item[
            "selection_bucket"
        ] = "CORE"


        selected.append(
            item
        )


        selected_symbols.add(
            item[
                "symbol"
            ]
        )


    fresh_pool = [
        item

        for item
        in enriched

        if item[
            "symbol"
        ]
        not in
        selected_symbols
    ]


    fresh_pool.sort(
        key=lambda item: (
            item.get(
                "cycles_since_selected",
                999999,
            ),
            float(
                item.get(
                    "score",
                    0.0,
                )
            ),
        ),
        reverse=True,
    )


    for item in (
        fresh_pool[
            :rotation_count
        ]
    ):

        item[
            "selection_bucket"
        ] = "ROTATE"


        selected.append(
            item
        )


        selected_symbols.add(
            item[
                "symbol"
            ]
        )


    if len(
        selected
    ) < limit:

        for item in core_ranked:

            if (
                item[
                    "symbol"
                ]
                in
                selected_symbols
            ):

                continue


            item[
                "selection_bucket"
            ] = "FILL"


            selected.append(
                item
            )


            selected_symbols.add(
                item[
                    "symbol"
                ]
            )


            if len(
                selected
            ) >= limit:

                break


    for item in selected:

        symbol_state = (
            symbols_state.setdefault(
                item[
                    "symbol"
                ],
                {},
            )
        )


        symbol_state[
            "last_selected_cycle"
        ] = cycle


        symbol_state[
            "times_selected"
        ] = (
            int(
                symbol_state.get(
                    "times_selected",
                    0,
                )
                or
                0
            )
            +
            1
        )


        symbol_state[
            "last_selected_ts"
        ] = now


    save_rotation_state(
        state
    )


    core_actual = sum(
        1
        for item in selected
        if item.get(
            "selection_bucket"
        )
        ==
        "CORE"
    )


    print()


    print(
        "DYNAMIC UNIVERSE | "
        f"cycle={cycle} | "
        f"directional={len(directional)} | "
        f"selected={len(selected)} | "
        f"core={core_actual} | "
        f"rotation={len(selected) - core_actual}"
    )


    for item in selected:

        print(
            "UNIVERSE PICK | "
            f"{item['symbol']:<8} | "
            f"{item.get('selection_bucket', 'N/A'):<6} | "
            f"bias={item['bias']:<5} | "
            f"scanner={float(item.get('score', 0.0)):.2f} | "
            f"effective={float(item.get('effective_score', 0.0)):.2f}"
        )


    return selected


def update_analysis_state(
    candidate,
    result,
):

    symbol = str(
        candidate[
            "symbol"
        ]
    ).strip().upper()


    state = load_rotation_state()


    symbols_state = (
        state.setdefault(
            "symbols",
            {},
        )
    )


    symbol_state = (
        symbols_state.setdefault(
            symbol,
            {},
        )
    )


    symbol_state[
        "last_analysis_ts"
    ] = time.time()


    symbol_state[
        "last_qualified"
    ] = bool(
        result.get(
            "qualified",
            False,
        )
    )


    symbol_state[
        "last_support"
    ] = int(
        result.get(
            "support",
            0,
        )
        or
        0
    )


    distance = result.get(
        "trigger_distance_pct"
    )


    symbol_state[
        "trigger_distance_pct"
    ] = (
        None
        if distance is None
        else
        float(
            distance
        )
    )


    save_rotation_state(
        state
    )


class TradingMaxStrategy(
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


        self.historical_events = {}

        self.historical_bars = {}

        self.market_data = {}

        self.errors = []


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


    def error(
        self,
        reqId,
        *args,
    ):

        error_time = None

        error_code = None

        error_string = ""

        advanced_order_reject_json = ""


        if len(
            args
        ) == 4:

            (
                error_time,
                error_code,
                error_string,
                advanced_order_reject_json,
            ) = args


        elif len(
            args
        ) == 3:

            (
                error_code,
                error_string,
                advanced_order_reject_json,
            ) = args


        elif len(
            args
        ) == 2:

            (
                error_code,
                error_string,
            ) = args


        elif len(
            args
        ) == 1:

            error_string = str(
                args[
                    0
                ]
            )


        else:

            print(
                "ERROR CALLBACK | "
                f"reqId={reqId} | "
                f"unexpected_args={args}"
            )

            return


        self.errors.append(
            {
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
            if error_code
            in
            info_codes
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


    def historicalData(
        self,
        reqId,
        bar,
    ):

        self.historical_bars.setdefault(
            reqId,
            [],
        )


        try:

            timestamp = int(
                str(
                    bar.date
                )
            )

        except Exception:

            timestamp = None


        try:

            volume = float(
                bar.volume
            )

        except Exception:

            volume = 0.0


        try:

            wap = float(
                bar.wap
            )

        except Exception:

            wap = 0.0


        self.historical_bars[
            reqId
        ].append(
            {
                "timestamp":
                    timestamp,

                "date":
                    str(
                        bar.date
                    ),

                "open":
                    float(
                        bar.open
                    ),

                "high":
                    float(
                        bar.high
                    ),

                "low":
                    float(
                        bar.low
                    ),

                "close":
                    float(
                        bar.close
                    ),

                "volume":
                    volume,

                "wap":
                    wap,
            }
        )


    def historicalDataEnd(
        self,
        reqId,
        start,
        end,
    ):

        count = len(
            self.historical_bars.get(
                reqId,
                [],
            )
        )


        print(
            "HISTORICAL END | "
            f"reqId={reqId} | "
            f"bars={count}"
        )


        event = (
            self.historical_events.get(
                reqId
            )
        )


        if event:

            event.set()


    def marketDataType(
        self,
        reqId,
        marketDataType,
    ):

        item = (
            self.market_data.setdefault(
                reqId,
                {},
            )
        )


        item[
            "market_data_type"
        ] = marketDataType


    def tickPrice(
        self,
        reqId,
        tickType,
        price,
        attrib,
    ):

        if (
            price is None
            or
            price <= 0
        ):

            return


        item = (
            self.market_data.setdefault(
                reqId,
                {},
            )
        )


        item[
            "last_tick_ts"
        ] = time.time()


        if tickType == 1:

            item[
                "bid"
            ] = float(
                price
            )


        elif tickType == 2:

            item[
                "ask"
            ] = float(
                price
            )


        elif tickType == 4:

            item[
                "last"
            ] = float(
                price
            )


        elif tickType == 6:

            item[
                "high"
            ] = float(
                price
            )


        elif tickType == 7:

            item[
                "low"
            ] = float(
                price
            )


        elif tickType == 9:

            item[
                "close"
            ] = float(
                price
            )


def network_loop(
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


def connect_strategy(
    app,
):

    print()


    print(
        "Connecting strategy engine "
        "to IBKR..."
    )


    app.connect(
        HOST,
        PORT,
        CLIENT_ID,
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
        timeout=15
    ):

        raise RuntimeError(
            "Strategy IBKR "
            "handshake timeout"
        )


    if not app.isConnected():

        raise RuntimeError(
            "Strategy IBKR "
            "socket not connected"
        )


    print(
        "STRATEGY IBKR "
        "HANDSHAKE OK"
    )


def make_contract(
    candidate,
):

    contract = Contract()


    contract.conId = int(
        candidate[
            "con_id"
        ]
    )


    contract.symbol = (
        candidate[
            "symbol"
        ]
    )


    contract.secType = "STK"

    contract.exchange = "SMART"

    contract.currency = "USD"


    primary = (
        candidate.get(
            "primary_exchange"
        )
    )


    if primary:

        contract.primaryExchange = (
            primary
        )


    return contract


def ema(
    values,
    period,
):

    if len(
        values
    ) < period:

        return None


    multiplier = (
        2.0
        /
        (
            period
            +
            1
        )
    )


    current = (
        sum(
            values[
                :period
            ]
        )
        /
        period
    )


    for value in (
        values[
            period:
        ]
    ):

        current = (
            (
                value
                -
                current
            )
            *
            multiplier
            +
            current
        )


    return current


def rsi(
    values,
    period,
):

    if len(
        values
    ) < (
        period
        +
        1
    ):

        return None


    gains = []

    losses = []


    for index in range(
        1,
        len(
            values
        ),
    ):

        change = (
            values[
                index
            ]
            -
            values[
                index
                -
                1
            ]
        )


        gains.append(
            max(
                change,
                0.0,
            )
        )


        losses.append(
            max(
                -change,
                0.0,
            )
        )


    avg_gain = (
        sum(
            gains[
                :period
            ]
        )
        /
        period
    )


    avg_loss = (
        sum(
            losses[
                :period
            ]
        )
        /
        period
    )


    for index in range(
        period,
        len(
            gains
        ),
    ):

        avg_gain = (
            (
                avg_gain
                *
                (
                    period
                    -
                    1
                )
            )
            +
            gains[
                index
            ]
        ) / period


        avg_loss = (
            (
                avg_loss
                *
                (
                    period
                    -
                    1
                )
            )
            +
            losses[
                index
            ]
        ) / period


    if avg_loss == 0:

        return (
            50.0
            if avg_gain == 0
            else
            100.0
        )


    rs = (
        avg_gain
        /
        avg_loss
    )


    return (
        100.0
        -
        (
            100.0
            /
            (
                1.0
                +
                rs
            )
        )
    )


def atr(
    bars,
    period,
):

    if len(
        bars
    ) < (
        period
        +
        1
    ):

        return None


    values = []


    previous_close = (
        bars[
            0
        ][
            "close"
        ]
    )


    for bar in (
        bars[
            1:
        ]
    ):

        true_range = max(

            bar[
                "high"
            ]
            -
            bar[
                "low"
            ],

            abs(
                bar[
                    "high"
                ]
                -
                previous_close
            ),

            abs(
                bar[
                    "low"
                ]
                -
                previous_close
            ),
        )


        values.append(
            true_range
        )


        previous_close = (
            bar[
                "close"
            ]
        )


    current = (
        sum(
            values[
                :period
            ]
        )
        /
        period
    )


    for value in (
        values[
            period:
        ]
    ):

        current = (
            (
                current
                *
                (
                    period
                    -
                    1
                )
            )
            +
            value
        ) / period


    return current


def completed_bars(
    bars,
):

    now = int(
        time.time()
    )


    return [
        bar

        for bar
        in bars

        if (
            bar[
                "timestamp"
            ]
            is not None
            and
            bar[
                "timestamp"
            ]
            +
            BAR_SECONDS
            <=
            now
        )
    ]


def bar_time_et(
    bar,
):

    timestamp = (
        bar.get(
            "timestamp"
        )
    )


    if timestamp is None:

        return None


    return (
        datetime.fromtimestamp(
            timestamp,
            timezone.utc,
        )
        .astimezone(
            NEW_YORK
        )
    )


def is_regular_trading_hours_now():

    now = (
        datetime.now(
            NEW_YORK
        )
    )


    if now.weekday() >= 5:

        return False


    return (
        RTH_START
        <=
        now.time()
        <
        RTH_END
    )


def is_extended_trading_hours_now():

    now = (
        datetime.now(
            NEW_YORK
        )
    )

    if now.weekday() >= 5:

        return False

    return (
        EXTENDED_START
        <=
        now.time()
        <
        EXTENDED_END
    )


def is_extended_session_bar(
    bar,
):

    dt = (
        bar_time_et(
            bar
        )
    )

    if (
        dt is None
        or
        dt.weekday() >= 5
    ):

        return False

    return (
        EXTENDED_START
        <=
        dt.time()
        <
        EXTENDED_END
    )


def extended_session_bars(
    bars,
):

    return [
        bar

        for bar
        in bars

        if is_extended_session_bar(
            bar
        )
    ]


def is_regular_session_bar(
    bar,
):

    dt = (
        bar_time_et(
            bar
        )
    )


    if (
        dt is None
        or
        dt.weekday() >= 5
    ):

        return False


    return (
        RTH_START
        <=
        dt.time()
        <
        RTH_END
    )


def regular_session_bars(
    bars,
):

    return [
        bar

        for bar
        in bars

        if is_regular_session_bar(
            bar
        )
    ]


def calculate_vwap(
    bars,
):

    total_value = 0.0

    total_volume = 0.0


    for bar in bars:

        volume = (
            bar.get(
                "volume"
            )
            or
            0.0
        )


        if volume <= 0:

            continue


        price = (
            bar.get(
                "wap"
            )
            or
            0.0
        )


        if price <= 0:

            price = (
                (
                    bar[
                        "high"
                    ]
                    +
                    bar[
                        "low"
                    ]
                    +
                    bar[
                        "close"
                    ]
                )
                /
                3.0
            )


        total_value += (
            price
            *
            volume
        )


        total_volume += (
            volume
        )


    if total_volume <= 0:

        return None


    return (
        total_value
        /
        total_volume
    )


def session_vwap(
    bars,
):

    today = (
        datetime.now(
            NEW_YORK
        ).date()
    )


    regular_bars = []


    for bar in bars:

        dt = (
            bar_time_et(
                bar
            )
        )


        if (
            dt is None
            or
            dt.date()
            !=
            today
        ):

            continue


        if is_extended_session_bar(
            bar
        ):

            regular_bars.append(
                bar
            )


    if not regular_bars:

        return (
            None,
            "NONE",
        )


    return (
        calculate_vwap(
            regular_bars
        ),
        "EXTENDED",
    )


def bar_volume_ratio_20(
    bars,
):

    if len(
        bars
    ) < (
        VOLR20_LOOKBACK
        +
        1
    ):

        return None


    current = (
        bars[
            -1
        ][
            "volume"
        ]
        or
        0.0
    )


    previous = [
        (
            bar[
                "volume"
            ]
            or
            0.0
        )

        for bar
        in (
            bars[
                -(VOLR20_LOOKBACK + 1):-1
            ]
        )
    ]


    previous = [
        value

        for value
        in previous

        if value > 0
    ]


    if not previous:

        return None


    average = (
        sum(
            previous
        )
        /
        len(
            previous
        )
    )


    if average <= 0:

        return None


    return (
        current
        /
        average
    )


def live_price(
    quote,
):

    last = (
        quote.get(
            "last"
        )
    )


    if (
        last is not None
        and
        last > 0
    ):

        return (
            last,
            "LAST",
        )


    bid = (
        quote.get(
            "bid"
        )
    )


    ask = (
        quote.get(
            "ask"
        )
    )


    if (
        bid is not None
        and
        ask is not None
        and
        bid > 0
        and
        ask >= bid
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


    return (
        None,
        "NONE",
    )


def spread_percent(
    quote,
):

    bid = quote.get(
        "bid"
    )


    ask = quote.get(
        "ask"
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


    middle = (
        bid
        +
        ask
    ) / 2.0


    return (
        (
            ask
            -
            bid
        )
        /
        middle
        *
        100.0
    )


def quote_age_seconds(
    quote,
):

    timestamp = (
        quote.get(
            "last_tick_ts"
        )
    )


    if timestamp is None:

        return None


    try:

        return max(
            0.0,
            time.time()
            -
            float(
                timestamp
            ),
        )


    except Exception:

        return None


def hot_proxy_distance_percent(
    candidate,
    quote,
):

    price, _ = live_price(
        quote
    )


    if (
        price is None
        or
        price <= 0
    ):

        return None


    bias = (
        candidate.get(
            "bias"
        )
    )


    if bias == "LONG":

        reference = (
            quote.get(
                "high"
            )
        )


        if (
            reference is None
            or
            reference <= 0
        ):

            return None


        if price >= reference:

            return 0.0


        return (
            (
                reference
                -
                price
            )
            /
            reference
            *
            100.0
        )


    if bias == "SHORT":

        reference = (
            quote.get(
                "low"
            )
        )


        if (
            reference is None
            or
            reference <= 0
        ):

            return None


        if price <= reference:

            return 0.0


        return (
            (
                price
                -
                reference
            )
            /
            reference
            *
            100.0
        )


    return None


def resolve_hot_distance(
    candidate,
    quote,
    symbol_state,
):

    live_distance = (
        hot_proxy_distance_percent(
            candidate,
            quote,
        )
    )


    if live_distance is not None:

        return (
            live_distance,
            "LIVE_HIGH_LOW",
        )


    previous_distance = (
        symbol_state.get(
            "trigger_distance_pct"
        )
    )


    if previous_distance is not None:

        try:

            previous_distance = float(
                previous_distance
            )


            if previous_distance >= 0:

                return (
                    previous_distance,
                    "STATE_TRIGGER",
                )


        except Exception:

            pass


    return (
        None,
        "NONE",
    )


def hot_distance_bonus(
    distance,
):

    if distance is None:

        return -1.0


    if distance <= 0.10:

        return 10.0


    if distance <= 0.25:

        return 8.0


    if distance <= 0.50:

        return 6.0


    if distance <= 1.00:

        return 3.0


    if distance <= 2.00:

        return 1.0


    return 0.0


def hot_freshness_bonus(
    symbol_state,
):

    last_analysis = (
        symbol_state.get(
            "last_analysis_ts"
        )
    )


    if last_analysis is None:

        return 3.0


    try:

        age = max(
            0.0,
            time.time()
            -
            float(
                last_analysis
            ),
        )


    except Exception:

        return 0.0


    if age >= 300:

        return 2.0


    if age >= 120:

        return 1.0


    return 0.0


def build_hot_candidate(
    candidate,
    quote,
    symbol_state,
):

    instrument_type = (
        candidate.get(
            "instrument_type"
        )
        or
        "UNKNOWN"
    ).upper()


    profile = (
        get_instrument_profile(
            instrument_type
        )
    )


    price, _ = live_price(
        quote
    )


    spread = spread_percent(
        quote
    )


    quote_age = quote_age_seconds(
        quote
    )


    data_type = quote.get(
        "market_data_type"
    )


    (
        proxy_distance,
        distance_source,
    ) = (
        resolve_hot_distance(
            candidate,
            quote,
            symbol_state,
        )
    )


    effective_score = float(
        candidate.get(
            "effective_score",
            candidate.get(
                "score",
                0.0,
            ),
        )
        or
        0.0
    )


    scanner_component = (
        max(
            0.0,
            min(
                effective_score,
                50.0,
            ),
        )
        *
        0.45
    )


    distance_component = (
        hot_distance_bonus(
            proxy_distance
        )
    )


    freshness_component = (
        hot_freshness_bonus(
            symbol_state
        )
    )


    rotation_component = (
        1.5
        if candidate.get(
            "selection_bucket"
        )
        ==
        "ROTATE"
        else
        0.0
    )


    max_spread = float(
        profile[
            "max_spread_pct"
        ]
    )


    spread_penalty = 0.0


    if spread is None:

        spread_penalty = 6.0


    elif spread > max_spread:

        spread_penalty = 8.0


    elif max_spread > 0:

        spread_penalty = min(
            2.0,
            (
                spread
                /
                max_spread
            )
            *
            2.0,
        )


    live_penalty = 0.0


    if data_type != 1:

        live_penalty += 15.0


    if price is None:

        live_penalty += 15.0


    if (
        quote_age is None
        or
        quote_age
        >
        MAX_QUOTE_AGE_SECONDS
    ):

        live_penalty += 8.0


    hot_score = (
        scanner_component
        +
        distance_component
        +
        freshness_component
        +
        rotation_component
        -
        spread_penalty
        -
        live_penalty
    )


    hot_eligible = (
        data_type == 1
        and
        price is not None
        and
        spread is not None
        and
        spread <= max_spread
        and
        quote_age is not None
        and
        quote_age
        <=
        MAX_QUOTE_AGE_SECONDS
    )


    item = dict(
        candidate
    )


    item[
        "hot_score"
    ] = hot_score


    item[
        "hot_proxy_distance_pct"
    ] = proxy_distance


    item[
        "hot_distance_source"
    ] = distance_source


    item[
        "hot_quote_age_seconds"
    ] = quote_age


    item[
        "hot_spread_pct"
    ] = spread


    item[
        "hot_max_spread_pct"
    ] = max_spread


    item[
        "hot_eligible"
    ] = hot_eligible


    return item


def select_hot_pool(
    candidates,
    app,
    mapping,
):

    state = load_rotation_state()


    symbols_state = (
        state.get(
            "symbols",
            {},
        )
    )


    ranked = []


    for candidate in candidates:

        symbol = (
            candidate[
                "symbol"
            ]
        )


        req_id = (
            mapping.get(
                symbol
            )
        )


        quote = (
            app.market_data.get(
                req_id,
                {},
            )
            if req_id is not None
            else
            {}
        )


        symbol_state = (
            symbols_state.get(
                symbol,
                {},
            )
        )


        ranked.append(
            build_hot_candidate(
                candidate,
                quote,
                symbol_state,
            )
        )


    eligible = [
        item

        for item
        in ranked

        if item.get(
            "hot_eligible"
        )
    ]


    def rank_key(
        item,
    ):

        return (
            float(
                item.get(
                    "hot_score",
                    0.0,
                )
            ),

            float(
                item.get(
                    "effective_score",
                    0.0,
                )
            ),

            float(
                item.get(
                    "score",
                    0.0,
                )
            ),
        )


    eligible.sort(
        key=
            rank_key,
        reverse=True,
    )


    core_pool = [
        item

        for item
        in eligible

        if item.get(
            "selection_bucket"
        )
        ==
        "CORE"
    ]


    rotate_pool = [
        item

        for item
        in eligible

        if item.get(
            "selection_bucket"
        )
        ==
        "ROTATE"
    ]


    core_pool.sort(
        key=
            rank_key,
        reverse=True,
    )


    rotate_pool.sort(
        key=
            rank_key,
        reverse=True,
    )


    selected = []

    selected_symbols = set()


    def take_from(
        pool,
        count,
    ):

        added = 0


        for item in pool:

            if len(
                selected
            ) >= HOT_POOL_SIZE:

                break


            if (
                item[
                    "symbol"
                ]
                in
                selected_symbols
            ):

                continue


            selected.append(
                item
            )


            selected_symbols.add(
                item[
                    "symbol"
                ]
            )


            added += 1


            if added >= count:

                break


        return added


    take_from(
        core_pool,
        HOT_CORE_TARGET,
    )


    take_from(
        rotate_pool,
        HOT_ROTATE_TARGET,
    )


    if len(
        selected
    ) < HOT_POOL_SIZE:

        remaining = [
            item

            for item
            in eligible

            if item[
                "symbol"
            ]
            not in
            selected_symbols
        ]


        remaining.sort(
            key=
                rank_key,
            reverse=True,
        )


        take_from(
            remaining,
            HOT_POOL_SIZE
            -
            len(
                selected
            ),
        )


    selected_core = sum(
        1

        for item
        in selected

        if item.get(
            "selection_bucket"
        )
        ==
        "CORE"
    )


    selected_rotate = sum(
        1

        for item
        in selected

        if item.get(
            "selection_bucket"
        )
        ==
        "ROTATE"
    )


    print()


    print(
        "HOT POOL | "
        f"source={len(candidates)} | "
        f"eligible={len(eligible)} | "
        f"selected={len(selected)} | "
        f"core={selected_core} | "
        f"rotate={selected_rotate} | "
        f"limit={HOT_POOL_SIZE}"
    )


    for item in sorted(
        ranked,
        key=lambda row: float(
            row.get(
                "hot_score",
                -999.0,
            )
        ),
        reverse=True,
    ):

        status = (
            "HOT"
            if item[
                "symbol"
            ]
            in
            selected_symbols
            else
            "SKIP"
        )


        print(
            "HOT RANK | "
            f"{status:<4} | "
            f"{item['symbol']:<8} | "
            f"bucket="
            f"{str(item.get('selection_bucket') or 'N/A'):<6} | "
            f"score="
            f"{float(item.get('hot_score', 0.0)):>6.2f} | "
            f"proxyDist="
            f"{fmt(item.get('hot_proxy_distance_pct'), 3):>7}% | "
            f"source="
            f"{str(item.get('hot_distance_source') or 'NONE'):<13} | "
            f"spread="
            f"{fmt(item.get('hot_spread_pct'), 3):>7}% | "
            f"maxSpread="
            f"{fmt(item.get('hot_max_spread_pct'), 3):>7}% | "
            f"age="
            f"{fmt(item.get('hot_quote_age_seconds'), 1):>5}s | "
            f"eligible="
            f"{item.get('hot_eligible')}"
        )


    return selected


def request_history(
    app,
    candidate,
    req_id,
):

    event = (
        threading.Event()
    )


    app.historical_events[
        req_id
    ] = event


    app.historical_bars[
        req_id
    ] = []


    app.reqHistoricalData(
        req_id,
        make_contract(
            candidate
        ),
        "",
        HISTORICAL_DURATION,
        HISTORICAL_BAR_SIZE,
        "TRADES",
        0,
        2,
        False,
        [],
    )


    if not event.wait(
        timeout=
            HISTORICAL_TIMEOUT_SECONDS
    ):

        print(
            "HISTORICAL TIMEOUT | "
            f"{candidate['symbol']}"
        )


        try:

            app.cancelHistoricalData(
                req_id
            )

        except Exception:

            pass


    return completed_bars(
        app.historical_bars.get(
            req_id,
            [],
        )
    )


def trigger_distance_percent(
    bias,
    price,
    previous_high,
    previous_low,
):

    if (
        price is None
        or
        price <= 0
    ):

        return None


    if (
        bias == "LONG"
        and
        previous_high is not None
        and
        previous_high > 0
    ):

        if price >= previous_high:

            return 0.0


        return (
            (
                previous_high
                -
                price
            )
            /
            previous_high
            *
            100.0
        )


    if (
        bias == "SHORT"
        and
        previous_low is not None
        and
        previous_low > 0
    ):

        if price <= previous_low:

            return 0.0


        return (
            (
                price
                -
                previous_low
            )
            /
            previous_low
            *
            100.0
        )


    return None


def analyze(
    candidate,
    bars,
    quote,
):

    rth_bars = (
        extended_session_bars(
            bars
        )
    )


    if len(
        rth_bars
    ) < 35:

        return None


    instrument_type = (
        candidate.get(
            "instrument_type"
        )
        or
        "UNKNOWN"
    ).upper()


    profile = (
        get_instrument_profile(
            instrument_type
        )
    )


    closes = [
        bar[
            "close"
        ]

        for bar
        in rth_bars
    ]


    ema9 = (
        ema(
            closes,
            EMA_FAST,
        )
    )


    ema21 = (
        ema(
            closes,
            EMA_SLOW,
        )
    )


    rsi_value = (
        rsi(
            closes,
            RSI_PERIOD,
        )
    )


    atr_value = (
        atr(
            rth_bars,
            ATR_PERIOD,
        )
    )


    volr20 = (
        bar_volume_ratio_20(
            rth_bars
        )
    )


    (
        vwap_value,
        vwap_scope,
    ) = (
        session_vwap(
            rth_bars
        )
    )


    (
        price,
        price_source,
    ) = (
        live_price(
            quote
        )
    )


    spread = (
        spread_percent(
            quote
        )
    )


    quote_age = (
        quote_age_seconds(
            quote
        )
    )


    session_ok = (
        is_extended_trading_hours_now()
    )


    hard_failures = []


    if not session_ok:

        hard_failures.append(
            "SESSION"
        )


    market_data_type = (
        quote.get(
            "market_data_type"
        )
    )


    if market_data_type != 1:

        hard_failures.append(
            "LIVE_DATA"
        )


    if (
        quote_age is None
        or
        quote_age
        >
        MAX_QUOTE_AGE_SECONDS
    ):

        hard_failures.append(
            "STALE_QUOTE"
        )


    if price is None:

        hard_failures.append(
            "LIVE_PRICE"
        )


    if (
        atr_value is None
        or
        atr_value <= 0
    ):

        hard_failures.append(
            "ATR"
        )


    if vwap_value is None:

        hard_failures.append(
            "VWAP"
        )


    if spread is None:

        hard_failures.append(
            "NO_SPREAD"
        )


    elif (
        spread
        >
        profile[
            "max_spread_pct"
        ]
    ):

        hard_failures.append(
            "SPREAD"
        )


    bias = (
        candidate[
            "bias"
        ]
    )


    action = (
        "BUY"
        if bias
        ==
        "LONG"
        else
        "SELL"
    )


    if price is None:

        result = {

            "symbol":
                candidate[
                    "symbol"
                ],

            "instrument_type":
                instrument_type,

            "action":
                action,

            "qualified":
                False,

            "hard_pass":
                False,

            "hard_failures":
                hard_failures,

            "support":
                0,

            "support_total":
                4,

            "entry":
                None,

            "stop":
                None,

            "target":
                None,

            "scanner_score":
                candidate[
                    "score"
                ],

            "effective_score":
                candidate.get(
                    "effective_score"
                ),

            "selection_bucket":
                candidate.get(
                    "selection_bucket"
                ),

            "hot_score":
                candidate.get(
                    "hot_score"
                ),

            "hot_proxy_distance_pct":
                candidate.get(
                    "hot_proxy_distance_pct"
                ),

            "hot_distance_source":
                candidate.get(
                    "hot_distance_source"
                ),

            "quote_age_seconds":
                quote_age,

            "trigger_distance_pct":
                None,
        }


        update_analysis_state(
            candidate,
            result,
        )


        return result


    previous = (
        rth_bars[
            -BREAKOUT_LOOKBACK:
        ]
    )


    previous_high = max(
        bar[
            "high"
        ]

        for bar
        in previous
    )


    previous_low = min(
        bar[
            "low"
        ]

        for bar
        in previous
    )


    distance = (
        trigger_distance_percent(
            bias,
            price,
            previous_high,
            previous_low,
        )
    )


    if bias == "LONG":

        action = "BUY"


        if not (
            price
            >
            previous_high
        ):

            hard_failures.append(
                "BREAKOUT"
            )


        support_conditions = [

            (
                "EMA9>EMA21",
                (
                    ema9 is not None
                    and
                    ema21 is not None
                    and
                    ema9 > ema21
                ),
            ),

            (
                "RSI_LONG",
                (
                    rsi_value is not None
                    and
                    LONG_RSI_MIN
                    <=
                    rsi_value
                    <=
                    LONG_RSI_MAX
                ),
            ),

            (
                "ABOVE_VWAP",
                (
                    vwap_value is not None
                    and
                    price > vwap_value
                ),
            ),

            (
                "VOLR20",
                (
                    volr20 is not None
                    and
                    volr20
                    >=
                    profile[
                        "min_volr20"
                    ]
                ),
            ),
        ]


    elif bias == "SHORT":

        action = "SELL"


        if not (
            price
            <
            previous_low
        ):

            hard_failures.append(
                "BREAKDOWN"
            )


        support_conditions = [

            (
                "EMA9<EMA21",
                (
                    ema9 is not None
                    and
                    ema21 is not None
                    and
                    ema9 < ema21
                ),
            ),

            (
                "RSI_SHORT",
                (
                    rsi_value is not None
                    and
                    SHORT_RSI_MIN
                    <=
                    rsi_value
                    <=
                    SHORT_RSI_MAX
                ),
            ),

            (
                "BELOW_VWAP",
                (
                    vwap_value is not None
                    and
                    price < vwap_value
                ),
            ),

            (
                "VOLR20",
                (
                    volr20 is not None
                    and
                    volr20
                    >=
                    profile[
                        "min_volr20"
                    ]
                ),
            ),
        ]


    else:

        return None


    support = sum(
        1

        for _, passed
        in support_conditions

        if passed
    )


    hard_pass = (
        len(
            hard_failures
        )
        ==
        0
    )


    support_pass = (
        support
        >=
        profile[
            "min_support"
        ]
    )


    qualified = (
        hard_pass
        and
        support_pass
    )


    risk = (
        atr_value
        *
        ATR_STOP_MULTIPLIER
        if atr_value is not None
        else
        None
    )


    stop = None

    target = None


    if risk is not None:

        if action == "BUY":

            stop = (
                price
                -
                risk
            )


            target = (
                price
                +
                (
                    risk
                    *
                    REWARD_RISK
                )
            )


        else:

            stop = (
                price
                +
                risk
            )


            target = (
                price
                -
                (
                    risk
                    *
                    REWARD_RISK
                )
            )


    result = {

        "symbol":
            candidate[
                "symbol"
            ],

        "instrument_type":
            instrument_type,

        "action":
            action,

        "qualified":
            qualified,

        "hard_pass":
            hard_pass,

        "hard_failures":
            hard_failures,

        "support":
            support,

        "support_total":
            len(
                support_conditions
            ),

        "support_conditions":
            support_conditions,

        "entry":
            price,

        "stop":
            stop,

        "target":
            target,

        "price_source":
            price_source,

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

        "spread_pct":
            spread,

        "ema9":
            ema9,

        "ema21":
            ema21,

        "rsi":
            rsi_value,

        "atr":
            atr_value,

        "volr20":
            volr20,

        "rvol":
            volr20,

        "vwap":
            vwap_value,

        "vwap_scope":
            vwap_scope,

        "previous_high":
            previous_high,

        "previous_low":
            previous_low,

        "trigger_distance_pct":
            distance,

        "session_ok":
            session_ok,

        "quote_age_seconds":
            quote_age,

        "scanner_score":
            candidate[
                "score"
            ],

        "effective_score":
            candidate.get(
                "effective_score"
            ),

        "selection_bucket":
            candidate.get(
                "selection_bucket"
            ),

        "hot_score":
            candidate.get(
                "hot_score"
            ),

        "hot_proxy_distance_pct":
            candidate.get(
                "hot_proxy_distance_pct"
            ),

        "hot_distance_source":
            candidate.get(
                "hot_distance_source"
            ),

        "profile":
            profile,
    }


    update_analysis_state(
        candidate,
        result,
    )


    return result


def get_candidates():

    scan_app = (
        scanner.TradingMaxScanner()
    )


    try:

        scanner.connect_and_wait(
            scan_app
        )


        for definition in (
            scanner.SCAN_DEFINITIONS
        ):

            scanner.run_scan(
                scan_app,
                definition,
            )


        watchlist = (
            scanner.build_dynamic_watchlist(
                scan_app.rows
            )
        )

        # Publish the ORIGINAL IBKR Top Gainers scan.
        # This does not change strategy selection.
        from strategy_status import record_top_gainers

        record_top_gainers(
            [
                row
                for row in scan_app.rows
                if row.get("scan_name") == "GAINERS"
            ]
        )



    finally:

        if scan_app.isConnected():

            scan_app.disconnect()


            time.sleep(
                1
            )


    directional = [

        item

        for item
        in watchlist

        if item[
            "bias"
        ]
        in {
            "LONG",
            "SHORT",
        }
    ]


    selected = (
select_rotating_candidates(
            directional
        )
    )

    try:
        import strategy_status

        scanner_cycle = int(
            time.time()
        )

        strategy_status.publish_scanner_universe(
            scanner_cycle,
            selected,
        )

        print(
            "SCANNER SNAPSHOT | "
            f"cycle={scanner_cycle} | "
            f"symbols={len(selected)}",
            flush=True,
        )

    except Exception as exc:
        print(
            "SCANNER SNAPSHOT ERROR | "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    return selected


def start_live(
    app,
    candidates,
):

    app.reqMarketDataType(
        1
    )


    mapping = {}


    for index, candidate in enumerate(
        candidates
    ):

        req_id = (
            30000
            +
            index
        )


        mapping[
            candidate[
                "symbol"
            ]
        ] = req_id


        app.market_data[
            req_id
        ] = {}


        app.reqMktData(
            req_id,
            make_contract(
                candidate
            ),
            "",
            False,
            False,
            [],
        )


        time.sleep(
            0.10
        )


    time.sleep(
        MARKET_DATA_WAIT_SECONDS
    )


    return mapping


def stop_live(
    app,
    mapping,
):

    for req_id in (
        mapping.values()
    ):

        try:

            app.cancelMktData(
                req_id
            )

        except Exception:

            pass


def fmt(
    value,
    digits=2,
):

    if value is None:

        return "N/A"


    return (
        f"{value:.{digits}f}"
    )


def print_results(
    results,
):

    results.sort(
        key=lambda item: (
            item[
                "qualified"
            ],
            item.get(
                "support",
                0,
            ),
            item[
                "scanner_score"
            ],
        ),
        reverse=True,
    )


    print()


    print(
        "=" * 150
    )


    print(
        "TRADINGMAX MULTI-INSTRUMENT STRATEGY"
    )


    print(
        "ANALYSIS ONLY - NO ORDERS"
    )


    print(
        "=" * 150
    )


    header = (
        f"{'#':>2} "
        f"{'SYMBOL':<8} "
        f"{'TYPE':<8} "
        f"{'BUCKET':<6} "
        f"{'HOT':>6} "
        f"{'ACT':<4} "
        f"{'OK':<3} "
        f"{'SUP':>5} "
        f"{'PRICE':>8} "
        f"{'RSI':>7} "
        f"{'VOLR20':>7} "
        f"{'ATR':>8} "
        f"{'SPRD%':>7} "
        f"{'DIST%':>7} "
        f"{'STOP':>8} "
        f"{'TARGET':>8}"
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
        results,
        start=1,
    ):

        print(
            f"{index:>2} "
            f"{item['symbol']:<8} "
            f"{item['instrument_type']:<8} "
            f"{str(item.get('selection_bucket') or 'N/A'):<6} "
            f"{fmt(item.get('hot_score')):>6} "
            f"{item['action']:<4} "
            f"{'YES' if item['qualified'] else 'NO':<3} "
            f"{item.get('support', 0):>2}/"
            f"{item.get('support_total', 4):<2} "
            f"{fmt(item.get('entry')):>8} "
            f"{fmt(item.get('rsi')):>7} "
            f"{fmt(item.get('volr20')):>7} "
            f"{fmt(item.get('atr'), 4):>8} "
            f"{fmt(item.get('spread_pct')):>7} "
            f"{fmt(item.get('trigger_distance_pct')):>7} "
            f"{fmt(item.get('stop')):>8} "
            f"{fmt(item.get('target')):>8}"
        )


    print()


    print(
        "QUALIFIED"
    )


    print(
        "-" * 60
    )


    qualified = [

        item

        for item
        in results

        if item[
            "qualified"
        ]
    ]


    if not qualified:

        print(
            "None."
        )


    for item in qualified:

        print()


        print(
            f"{item['symbol']} "
            f"{item['instrument_type']} "
            f"{item['action']}"
        )


        print(
            f"  entry       : "
            f"{item['entry']:.4f}"
        )


        print(
            f"  stop        : "
            f"{item['stop']:.4f}"
        )


        print(
            f"  target      : "
            f"{item['target']:.4f}"
        )


        print(
            f"  spread      : "
            f"{item['spread_pct']:.4f}%"
        )


        print(
            f"  VOLR20      : "
            f"{fmt(item['volr20'])}"
        )


        print(
            f"  VWAP        : "
            f"{fmt(item['vwap'], 4)} "
            f"({item['vwap_scope']})"
        )


        print(
            f"  trigger dist: "
            f"{fmt(item['trigger_distance_pct'], 3)}%"
        )


        print(
            f"  hot score   : "
            f"{fmt(item.get('hot_score'), 2)}"
        )


        print(
            f"  hot source  : "
            f"{item.get('hot_distance_source')}"
        )


        print(
            f"  selection   : "
            f"{item.get('selection_bucket')}"
        )


    print()


    print(
        "HARD GATE REJECTIONS"
    )


    print(
        "-" * 60
    )


    for item in results:

        failures = (
            item.get(
                "hard_failures"
            )
            or
            []
        )


        if failures:

            print(
                f"{item['symbol']:<8} "
                f"{item['instrument_type']:<8} "
                f"{item['action']:<4} | "
                f"{', '.join(failures)}"
            )


def main():

    print()


    print(
        "=" * 62
    )


    print(
        "TRADINGMAX MULTI-INSTRUMENT STRATEGY"
    )


    print(
        "ANALYSIS ONLY - NO ORDERS"
    )


    print(
        "=" * 62
    )


    candidates = (
        get_candidates()
    )


    print()


    print(
        f"Universe candidates: "
        f"{len(candidates)}"
    )


    if not candidates:

        return


    app = (
        TradingMaxStrategy()
    )


    mapping = None


    try:

        connect_strategy(
            app
        )


        mapping = (
            start_live(
                app,
                candidates,
            )
        )


        hot_candidates = (
            select_hot_pool(
                candidates,
                app,
                mapping,
            )
        )


        print()


        print(
            f"Hot candidates: "
            f"{len(hot_candidates)}"
        )


        if not hot_candidates:

            return


        results = []


        for index, candidate in enumerate(
            hot_candidates
        ):

            req_id = (
                20000
                +
                index
            )


            bars = (
                request_history(
                    app,
                    candidate,
                    req_id,
                )
            )


            quote = (
                app.market_data.get(
                    mapping[
                        candidate[
                            "symbol"
                        ]
                    ],
                    {},
                )
            )


            print(
                "ANALYZE | "
                f"{candidate['symbol']} | "
                f"type="
                f"{candidate['instrument_type']} | "
                f"bucket="
                f"{candidate.get('selection_bucket')} | "
                f"hot="
                f"{fmt(candidate.get('hot_score'), 2)} | "
                f"bars="
                f"{len(bars)} | "
                f"bid="
                f"{quote.get('bid')} | "
                f"ask="
                f"{quote.get('ask')} | "
                f"last="
                f"{quote.get('last')} | "
                f"dataType="
                f"{quote.get('market_data_type')}"
            )


            result = (
                analyze(
                    candidate,
                    bars,
                    quote,
                )
            )


            if result is not None:

                results.append(
                    result
                )


            time.sleep(
                0.20
            )


        print_results(
            results
        )


    finally:

        if mapping is not None:

            stop_live(
                app,
                mapping,
            )


        if app.isConnected():

            print()


            print(
                "Disconnecting strategy engine..."
            )


            app.disconnect()


            time.sleep(
                1
            )


if __name__ == "__main__":

    main()
