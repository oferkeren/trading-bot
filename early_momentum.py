import json
import math
import os
import sqlite3
import time

from pathlib import Path


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent


DB_FILE = (
    BASE_DIR
    /
    "early_momentum.db"
)


# ============================================================
# CONFIG
# ============================================================

RETENTION_SECONDS = int(
    os.getenv(
        "EARLY_MOMENTUM_RETENTION_SECONDS",
        str(
            7
            *
            24
            *
            60
            *
            60
        ),
    )
)


MIN_SNAPSHOT_INTERVAL_SECONDS = float(
    os.getenv(
        "EARLY_MOMENTUM_MIN_SNAPSHOT_SECONDS",
        "5",
    )
)


COMBINED_EARLY_WEIGHT = float(
    os.getenv(
        "COMBINED_EARLY_WEIGHT",
        "0.60",
    )
)


COMBINED_MICRO_WEIGHT = float(
    os.getenv(
        "COMBINED_MICRO_WEIGHT",
        "0.40",
    )
)


WINDOWS = [
    30,
    60,
    180,
]


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10,
    )

    conn.row_factory = (
        sqlite3.Row
    )

    return conn


def column_exists(
    conn,
    table,
    column,
):
    rows = conn.execute(
        f"""
        PRAGMA table_info({table})
        """
    ).fetchall()

    return (
        column
        in {
            row[1]
            for row
            in rows
        }
    )


def ensure_column(
    conn,
    table,
    column,
    definition,
):
    if not column_exists(
        conn,
        table,
        column,
    ):
        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )


def init_db():
    conn = db_connect()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                ts REAL NOT NULL,

                symbol TEXT NOT NULL,
                action TEXT NOT NULL,

                instrument_type TEXT,

                price REAL,
                bid REAL,
                ask REAL,

                spread_pct REAL,

                volr20 REAL,
                vwap REAL,

                trigger_distance_pct REAL,

                scanner_score REAL,
                effective_score REAL,
                hot_score REAL,

                qualified INTEGER NOT NULL DEFAULT 0,

                early_score REAL,
                early_state TEXT,
                early_components_json TEXT,
                early_history_windows_json TEXT,

                micro_score REAL,
                micro_state TEXT,
                micro_components_json TEXT,
                micro_windows_json TEXT,

                combined_score REAL,

                payload_json TEXT
            )
            """
        )


        columns = {
            "early_score":
                "REAL",

            "early_state":
                "TEXT",

            "early_components_json":
                "TEXT",

            "early_history_windows_json":
                "TEXT",

            "micro_score":
                "REAL",

            "micro_state":
                "TEXT",

            "micro_components_json":
                "TEXT",

            "micro_windows_json":
                "TEXT",

            "combined_score":
                "REAL",
        }


        for (
            column,
            definition,
        ) in columns.items():

            ensure_column(
                conn,
                "snapshots",
                column,
                definition,
            )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_early_momentum_symbol_action_ts
            ON snapshots (
                symbol,
                action,
                ts
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_early_score_ts
            ON snapshots (
                early_score,
                ts
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_micro_score_ts
            ON snapshots (
                micro_score,
                ts
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_combined_score_ts
            ON snapshots (
                combined_score,
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


def clamp(
    value,
    minimum,
    maximum,
):
    return max(
        minimum,
        min(
            maximum,
            value,
        ),
    )


def direction_factor(
    action,
):
    action = (
        str(
            action
            or
            ""
        )
        .strip()
        .upper()
    )


    if action == "BUY":
        return 1.0


    if action == "SELL":
        return -1.0


    return 0.0


# ============================================================
# SNAPSHOT
# ============================================================

def build_snapshot(
    result,
):
    symbol = (
        str(
            result.get(
                "symbol",
                "",
            )
        )
        .strip()
        .upper()
    )


    action = (
        str(
            result.get(
                "action",
                "",
            )
        )
        .strip()
        .upper()
    )


    if not symbol:
        raise ValueError(
            "symbol is required"
        )


    if action not in {
        "BUY",
        "SELL",
    }:
        raise ValueError(
            f"unsupported action: {action}"
        )


    price = safe_float(
        result.get(
            "entry"
        )
    )


    if price is None:
        price = safe_float(
            result.get(
                "last"
            )
        )


    return {
        "ts":
            time.time(),

        "symbol":
            symbol,

        "action":
            action,

        "instrument_type":
            result.get(
                "instrument_type"
            ),

        "price":
            price,

        "bid":
            safe_float(
                result.get(
                    "bid"
                )
            ),

        "ask":
            safe_float(
                result.get(
                    "ask"
                )
            ),

        "spread_pct":
            safe_float(
                result.get(
                    "spread_pct"
                )
            ),

        "volr20":
            safe_float(
                (
                    result.get(
                        "volr20"
                    )
                    if
                    result.get(
                        "volr20"
                    )
                    is not None
                    else
                    result.get(
                        "rvol"
                    )
                )
            ),

        "vwap":
            safe_float(
                result.get(
                    "vwap"
                )
            ),

        "trigger_distance_pct":
            safe_float(
                result.get(
                    "trigger_distance_pct"
                )
            ),

        "scanner_score":
            safe_float(
                result.get(
                    "scanner_score"
                )
            ),

        "effective_score":
            safe_float(
                result.get(
                    "effective_score"
                )
            ),

        "hot_score":
            safe_float(
                result.get(
                    "hot_score"
                )
            ),

        "qualified":
            bool(
                result.get(
                    "qualified",
                    False,
                )
            ),
    }


# ============================================================
# PERSISTENCE
# ============================================================

def get_latest_snapshot(
    symbol,
    action,
):
    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT *
            FROM snapshots

            WHERE
                symbol = ?
                AND action = ?

            ORDER BY ts DESC

            LIMIT 1
            """,
            (
                symbol,
                action,
            ),
        ).fetchone()


        if row is None:
            return None


        return dict(
            row
        )

    finally:
        conn.close()


def save_snapshot(
    snapshot,
    momentum,
    micro=None,
    combined_score=None,
):
    latest = (
        get_latest_snapshot(
            snapshot[
                "symbol"
            ],
            snapshot[
                "action"
            ],
        )
    )


    if latest is not None:
        age = (
            snapshot[
                "ts"
            ]
            -
            float(
                latest[
                    "ts"
                ]
            )
        )


        if (
            age
            <
            MIN_SNAPSHOT_INTERVAL_SECONDS
        ):
            return False


    micro = (
        micro
        or
        {}
    )


    payload = dict(
        snapshot
    )


    payload[
        "early_score"
    ] = momentum[
        "early_score"
    ]


    payload[
        "early_state"
    ] = momentum[
        "early_state"
    ]


    payload[
        "early_components"
    ] = momentum[
        "components"
    ]


    payload[
        "early_history_windows"
    ] = momentum[
        "history_windows"
    ]


    payload[
        "micro_score"
    ] = micro.get(
        "micro_score"
    )


    payload[
        "micro_state"
    ] = micro.get(
        "micro_state"
    )


    payload[
        "micro_components"
    ] = micro.get(
        "components"
    )


    payload[
        "micro_windows"
    ] = micro.get(
        "available_windows"
    )


    payload[
        "combined_score"
    ] = combined_score


    conn = db_connect()

    try:
        conn.execute(
            """
            INSERT INTO snapshots (
                ts,

                symbol,
                action,

                instrument_type,

                price,
                bid,
                ask,

                spread_pct,

                volr20,
                vwap,

                trigger_distance_pct,

                scanner_score,
                effective_score,
                hot_score,

                qualified,

                early_score,
                early_state,
                early_components_json,
                early_history_windows_json,

                micro_score,
                micro_state,
                micro_components_json,
                micro_windows_json,

                combined_score,

                payload_json
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
                snapshot[
                    "ts"
                ],

                snapshot[
                    "symbol"
                ],

                snapshot[
                    "action"
                ],

                snapshot[
                    "instrument_type"
                ],

                snapshot[
                    "price"
                ],

                snapshot[
                    "bid"
                ],

                snapshot[
                    "ask"
                ],

                snapshot[
                    "spread_pct"
                ],

                snapshot[
                    "volr20"
                ],

                snapshot[
                    "vwap"
                ],

                snapshot[
                    "trigger_distance_pct"
                ],

                snapshot[
                    "scanner_score"
                ],

                snapshot[
                    "effective_score"
                ],

                snapshot[
                    "hot_score"
                ],

                1
                if snapshot[
                    "qualified"
                ]
                else
                0,

                momentum[
                    "early_score"
                ],

                momentum[
                    "early_state"
                ],

                json.dumps(
                    momentum[
                        "components"
                    ],
                    sort_keys=True,
                ),

                json.dumps(
                    momentum[
                        "history_windows"
                    ]
                ),

                safe_float(
                    micro.get(
                        "micro_score"
                    )
                ),

                micro.get(
                    "micro_state"
                ),

                json.dumps(
                    micro.get(
                        "components",
                        {},
                    ),
                    sort_keys=True,
                ),

                json.dumps(
                    micro.get(
                        "available_windows",
                        [],
                    )
                ),

                safe_float(
                    combined_score
                ),

                json.dumps(
                    payload,
                    sort_keys=True,
                    default=str,
                ),
            ),
        )


        cutoff = (
            snapshot[
                "ts"
            ]
            -
            RETENTION_SECONDS
        )


        conn.execute(
            """
            DELETE FROM snapshots
            WHERE ts < ?
            """,
            (
                cutoff,
            ),
        )


        conn.commit()


        return True

    finally:
        conn.close()


# ============================================================
# HISTORICAL LOOKUP
# ============================================================

def find_snapshot_near_age(
    symbol,
    action,
    current_ts,
    target_age,
):
    target_ts = (
        current_ts
        -
        target_age
    )


    tolerance = max(
        20.0,
        target_age
        *
        0.35,
    )


    minimum_ts = (
        target_ts
        -
        tolerance
    )

    maximum_ts = (
        target_ts
        +
        tolerance
    )


    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT *,
                   ABS(ts - ?) AS distance

            FROM snapshots

            WHERE
                symbol = ?
                AND action = ?
                AND ts >= ?
                AND ts <= ?

            ORDER BY distance ASC

            LIMIT 1
            """,
            (
                target_ts,
                symbol,
                action,
                minimum_ts,
                maximum_ts,
            ),
        ).fetchone()


        if row is None:
            return None


        return dict(
            row
        )

    finally:
        conn.close()


# ============================================================
# COMPONENTS
# ============================================================

def price_component(
    current,
    previous,
    action,
    window,
):
    current_price = safe_float(
        current.get(
            "price"
        )
    )


    previous_price = safe_float(
        previous.get(
            "price"
        )
    )


    if (
        current_price is None
        or
        previous_price is None
        or
        previous_price <= 0
    ):
        return 0.0


    raw_change_pct = (
        (
            current_price
            -
            previous_price
        )
        /
        previous_price
        *
        100.0
    )


    directional_change = (
        raw_change_pct
        *
        direction_factor(
            action
        )
    )


    if window <= 60:
        scale = 12.0

    else:
        scale = 6.0


    return clamp(
        directional_change
        *
        scale,
        -25.0,
        25.0,
    )


def volr_component(
    current,
    previous,
):
    current_value = safe_float(
        current.get(
            "volr20"
        )
    )


    previous_value = safe_float(
        previous.get(
            "volr20"
        )
    )


    if (
        current_value is None
        or
        previous_value is None
        or
        previous_value <= 0
    ):
        return 0.0


    change_ratio = (
        (
            current_value
            -
            previous_value
        )
        /
        previous_value
    )


    return clamp(
        change_ratio
        *
        20.0,
        -20.0,
        20.0,
    )


def spread_component(
    current,
    previous,
):
    current_spread = safe_float(
        current.get(
            "spread_pct"
        )
    )


    previous_spread = safe_float(
        previous.get(
            "spread_pct"
        )
    )


    if (
        current_spread is None
        or
        previous_spread is None
        or
        previous_spread <= 0
    ):
        return 0.0


    improvement_ratio = (
        (
            previous_spread
            -
            current_spread
        )
        /
        previous_spread
    )


    return clamp(
        improvement_ratio
        *
        15.0,
        -15.0,
        15.0,
    )


def trigger_component(
    current,
    previous,
):
    current_distance = safe_float(
        current.get(
            "trigger_distance_pct"
        )
    )


    previous_distance = safe_float(
        previous.get(
            "trigger_distance_pct"
        )
    )


    if (
        current_distance is None
        or
        previous_distance is None
    ):
        return 0.0


    improvement = (
        abs(
            previous_distance
        )
        -
        abs(
            current_distance
        )
    )


    return clamp(
        improvement
        *
        8.0,
        -15.0,
        15.0,
    )


def hot_component(
    current,
    previous,
):
    current_score = safe_float(
        current.get(
            "hot_score"
        )
    )


    previous_score = safe_float(
        previous.get(
            "hot_score"
        )
    )


    if (
        current_score is None
        or
        previous_score is None
    ):
        return 0.0


    return clamp(
        (
            current_score
            -
            previous_score
        )
        *
        0.75,
        -15.0,
        15.0,
    )


def vwap_component(
    current,
    action,
):
    price = safe_float(
        current.get(
            "price"
        )
    )


    vwap = safe_float(
        current.get(
            "vwap"
        )
    )


    if (
        price is None
        or
        vwap is None
        or
        vwap <= 0
    ):
        return 0.0


    distance_pct = (
        (
            price
            -
            vwap
        )
        /
        vwap
        *
        100.0
    )


    directional_distance = (
        distance_pct
        *
        direction_factor(
            action
        )
    )


    return clamp(
        directional_distance
        *
        3.0,
        -10.0,
        10.0,
    )


# ============================================================
# SCORE
# ============================================================

def classify_state(
    score,
    history_count,
):
    if history_count == 0:
        return "WARMING_UP"


    if score >= 35:
        return "ACCELERATING"


    if score >= 15:
        return "BUILDING"


    if score <= -35:
        return "REVERSING"


    if score <= -15:
        return "WEAKENING"


    return "NEUTRAL"


def calculate_score(
    current,
):
    symbol = (
        current[
            "symbol"
        ]
    )


    action = (
        current[
            "action"
        ]
    )


    history = {}


    for window in WINDOWS:
        history[
            window
        ] = (
            find_snapshot_near_age(
                symbol=
                    symbol,

                action=
                    action,

                current_ts=
                    current[
                        "ts"
                    ],

                target_age=
                    window,
            )
        )


    usable = [
        (
            window,
            row,
        )

        for (
            window,
            row,
        )
        in history.items()

        if row is not None
    ]


    components = {
        "price":
            0.0,

        "volr":
            0.0,

        "spread":
            0.0,

        "trigger":
            0.0,

        "hot":
            0.0,

        "vwap":
            vwap_component(
                current,
                action,
            ),
    }


    if usable:
        component_rows = []


        for (
            window,
            previous,
        ) in usable:

            component_rows.append(
                {
                    "price":
                        price_component(
                            current,
                            previous,
                            action,
                            window,
                        ),

                    "volr":
                        volr_component(
                            current,
                            previous,
                        ),

                    "spread":
                        spread_component(
                            current,
                            previous,
                        ),

                    "trigger":
                        trigger_component(
                            current,
                            previous,
                        ),

                    "hot":
                        hot_component(
                            current,
                            previous,
                        ),
                }
            )


        for key in [
            "price",
            "volr",
            "spread",
            "trigger",
            "hot",
        ]:

            values = [
                row[
                    key
                ]
                for row
                in component_rows
            ]


            components[
                key
            ] = (
                sum(
                    values
                )
                /
                len(
                    values
                )
            )


    score = (
        components[
            "price"
        ]
        +
        components[
            "volr"
        ]
        +
        components[
            "spread"
        ]
        +
        components[
            "trigger"
        ]
        +
        components[
            "hot"
        ]
        +
        components[
            "vwap"
        ]
    )


    score = clamp(
        score,
        -100.0,
        100.0,
    )


    state = (
        classify_state(
            score,
            len(
                usable
            ),
        )
    )


    return {
        "symbol":
            symbol,

        "action":
            action,

        "early_score":
            round(
                score,
                2,
            ),

        "early_state":
            state,

        "history_windows":
            [
                window
                for (
                    window,
                    row,
                )
                in usable
            ],

        "components":
            {
                key:
                    round(
                        value,
                        2,
                    )
                for (
                    key,
                    value,
                )
                in components.items()
            },
    }


# ============================================================
# MICROSTRUCTURE
# ============================================================

def get_micro_context(
    symbol,
    action,
):
    try:
        import microstructure_score


        result = (
            microstructure_score
            .get_micro_score(
                symbol,
                action,
            )
        )


        return result


    except Exception as exc:
        print(
            "MICRO CONTEXT ERROR | "
            f"{symbol} | "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )


        return {
            "symbol":
                symbol,

            "action":
                action,

            "micro_score":
                None,

            "micro_state":
                "UNAVAILABLE",

            "available_windows":
                [],

            "components":
                {},

            "ib_metrics":
                {},
        }


def calculate_combined_score(
    momentum,
    micro,
):
    early_score = safe_float(
        momentum.get(
            "early_score"
        )
    )


    micro_score = safe_float(
        micro.get(
            "micro_score"
        )
    )


    micro_state = (
        str(
            micro.get(
                "micro_state",
                "",
            )
        )
        .strip()
        .upper()
    )


    windows = (
        micro.get(
            "available_windows"
        )
        or
        []
    )


    if early_score is None:
        return None


    if (
        micro_score is None
        or
        micro_state
        in {
            "",
            "WARMING_UP",
            "UNAVAILABLE",
        }
        or
        not windows
    ):
        return None


    total_weight = (
        COMBINED_EARLY_WEIGHT
        +
        COMBINED_MICRO_WEIGHT
    )


    if total_weight <= 0:
        return None


    combined = (
        (
            early_score
            *
            COMBINED_EARLY_WEIGHT
        )
        +
        (
            micro_score
            *
            COMBINED_MICRO_WEIGHT
        )
    ) / total_weight


    return round(
        clamp(
            combined,
            -100.0,
            100.0,
        ),
        2,
    )


# ============================================================
# PUBLIC API
# ============================================================

def observe_result(
    result,
):
    init_db()


    snapshot = (
        build_snapshot(
            result
        )
    )


    momentum = (
        calculate_score(
            snapshot
        )
    )


    micro = (
        get_micro_context(
            snapshot[
                "symbol"
            ],
            snapshot[
                "action"
            ],
        )
    )


    combined_score = (
        calculate_combined_score(
            momentum,
            micro,
        )
    )


    saved = (
        save_snapshot(
            snapshot,
            momentum,
            micro,
            combined_score,
        )
    )


    print(
        "EARLY MOMENTUM | "
        f"{momentum['symbol']} | "
        f"action={momentum['action']} | "
        f"score="
        f"{momentum['early_score']:+.2f} | "
        f"state="
        f"{momentum['early_state']} | "
        f"history="
        f"{momentum['history_windows']} | "
        f"saved={saved}",
        flush=True,
    )


    print(
        "EARLY COMPONENTS | "
        f"{momentum['symbol']} | "
        f"price="
        f"{momentum['components']['price']:+.2f} | "
        f"volr="
        f"{momentum['components']['volr']:+.2f} | "
        f"spread="
        f"{momentum['components']['spread']:+.2f} | "
        f"trigger="
        f"{momentum['components']['trigger']:+.2f} | "
        f"hot="
        f"{momentum['components']['hot']:+.2f} | "
        f"vwap="
        f"{momentum['components']['vwap']:+.2f}",
        flush=True,
    )


    micro_score = safe_float(
        micro.get(
            "micro_score"
        )
    )


    micro_text = (
        "N/A"
        if micro_score is None
        else
        f"{micro_score:+.2f}"
    )


    combined_text = (
        "N/A"
        if combined_score is None
        else
        f"{combined_score:+.2f}"
    )


    print(
        "MOMENTUM FUSION | "
        f"{momentum['symbol']} | "
        f"early="
        f"{momentum['early_score']:+.2f} | "
        f"micro="
        f"{micro_text} | "
        f"micro_state="
        f"{micro.get('micro_state')} | "
        f"combined="
        f"{combined_text} | "
        f"windows="
        f"{micro.get('available_windows', [])}",
        flush=True,
    )


    enriched = dict(
        momentum
    )


    enriched[
        "micro_score"
    ] = micro_score


    enriched[
        "micro_state"
    ] = micro.get(
        "micro_state"
    )


    enriched[
        "micro_components"
    ] = micro.get(
        "components",
        {},
    )


    enriched[
        "micro_windows"
    ] = micro.get(
        "available_windows",
        [],
    )


    enriched[
        "combined_score"
    ] = combined_score


    return enriched


# ============================================================
# SELF TEST
# ============================================================

def synthetic_result(
    *,
    symbol,
    price,
    spread,
    volr,
    trigger,
    hot,
):
    return {
        "symbol":
            symbol,

        "instrument_type":
            "COMMON",

        "action":
            "BUY",

        "qualified":
            False,

        "entry":
            price,

        "bid":
            price
            -
            0.01,

        "ask":
            price
            +
            0.01,

        "spread_pct":
            spread,

        "volr20":
            volr,

        "vwap":
            10.00,

        "trigger_distance_pct":
            trigger,

        "scanner_score":
            30.0,

        "effective_score":
            34.0,

        "hot_score":
            hot,
    }


def run_self_test():
    print(
        "=============================================================="
    )


    print(
        "EARLY MOMENTUM SELF TEST"
    )


    print(
        "=============================================================="
    )


    symbol = (
        "TMXEARLYTEST"
    )


    conn = db_connect()

    try:
        conn.execute(
            """
            DELETE FROM snapshots
            WHERE symbol = ?
            """,
            (
                symbol,
            ),
        )


        conn.commit()

    finally:
        conn.close()


    now = time.time()


    old = (
        build_snapshot(
            synthetic_result(
                symbol=
                    symbol,

                price=
                    10.00,

                spread=
                    1.00,

                volr=
                    1.20,

                trigger=
                    2.00,

                hot=
                    20.0,
            )
        )
    )


    old[
        "ts"
    ] = (
        now
        -
        65
    )


    old_momentum = {
        "early_score":
            0.0,

        "early_state":
            "WARMING_UP",

        "history_windows":
            [],

        "components": {
            "price":
                0.0,

            "volr":
                0.0,

            "spread":
                0.0,

            "trigger":
                0.0,

            "hot":
                0.0,

            "vwap":
                0.0,
        },
    }


    save_snapshot(
        old,
        old_momentum,
        None,
        None,
    )


    current = (
        build_snapshot(
            synthetic_result(
                symbol=
                    symbol,

                price=
                    10.20,

                spread=
                    0.50,

                volr=
                    2.40,

                trigger=
                    0.60,

                hot=
                    30.0,
            )
        )
    )


    current[
        "ts"
    ] = now


    result = (
        calculate_score(
            current
        )
    )


    print(
        json.dumps(
            result,
            indent=2,
            sort_keys=True,
        )
    )


    if (
        result[
            "early_score"
        ]
        <=
        0
    ):
        print(
            "EARLY_MOMENTUM_SELF_TEST=FAIL"
        )


        raise SystemExit(
            1
        )


    if (
        60
        not in
        result[
            "history_windows"
        ]
    ):
        print(
            "EARLY_MOMENTUM_SELF_TEST=FAIL"
        )


        raise SystemExit(
            1
        )


    synthetic_micro = {
        "micro_score":
            40.0,

        "micro_state":
            "ACCELERATING",

        "available_windows": [
            15,
            30,
            60,
        ],

        "components": {},
    }


    combined = (
        calculate_combined_score(
            result,
            synthetic_micro,
        )
    )


    if combined is None:
        print(
            "EARLY_MOMENTUM_SELF_TEST=FAIL"
        )


        raise SystemExit(
            1
        )


    expected = (
        (
            result[
                "early_score"
            ]
            *
            COMBINED_EARLY_WEIGHT
        )
        +
        (
            40.0
            *
            COMBINED_MICRO_WEIGHT
        )
    ) / (
        COMBINED_EARLY_WEIGHT
        +
        COMBINED_MICRO_WEIGHT
    )


    if abs(
        combined
        -
        round(
            expected,
            2,
        )
    ) > 0.01:

        print(
            "EARLY_MOMENTUM_SELF_TEST=FAIL"
        )


        raise SystemExit(
            1
        )


    print(
        "COMBINED TEST | "
        f"early="
        f"{result['early_score']:+.2f} | "
        f"micro=+40.00 | "
        f"combined="
        f"{combined:+.2f}"
    )


    print(
        "EARLY_MOMENTUM_SELF_TEST=PASS"
    )


if __name__ == "__main__":
    init_db()

    run_self_test()
