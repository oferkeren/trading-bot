import json
import math
import os
import sqlite3
import sys
import time

from pathlib import Path

from dotenv import load_dotenv


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


MICRO_DB_FILE = (
    BASE_DIR
    /
    "microstructure.db"
)


EARLY_DB_FILE = (
    BASE_DIR
    /
    "early_momentum.db"
)


load_dotenv(
    ENV_FILE
)


# ============================================================
# CONFIG
# ============================================================

MAX_FEATURE_AGE_SECONDS = float(
    os.getenv(
        "MICRO_SCORE_MAX_FEATURE_AGE_SECONDS",
        "45",
    )
)


RECENT_WINDOW_SECONDS = float(
    os.getenv(
        "MICRO_SCORE_RECENT_WINDOW_SECONDS",
        "15",
    )
)


BASELINE_WINDOW_SECONDS = float(
    os.getenv(
        "MICRO_SCORE_BASELINE_WINDOW_SECONDS",
        "60",
    )
)


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


def average(
    values,
):
    usable = [
        float(
            value
        )
        for value
        in values
        if safe_float(
            value
        )
        is not None
    ]


    if not usable:
        return None


    return (
        sum(
            usable
        )
        /
        len(
            usable
        )
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
# ACTION LOOKUP
# ============================================================

def get_latest_action(
    symbol,
):
    if not EARLY_DB_FILE.exists():
        return None


    conn = early_db_connect()

    try:
        row = conn.execute(
            """
            SELECT action
            FROM snapshots

            WHERE symbol = ?

            ORDER BY ts DESC

            LIMIT 1
            """,
            (
                symbol,
            ),
        ).fetchone()


        if row is None:
            return None


        action = (
            str(
                row[
                    "action"
                ]
            )
            .strip()
            .upper()
        )


        if action not in {
            "BUY",
            "SELL",
        }:
            return None


        return action


    finally:
        conn.close()


# ============================================================
# FEATURE LOOKUP
# ============================================================

def get_latest_feature(
    conn,
    symbol,
    window_seconds,
):
    row = conn.execute(
        """
        SELECT *
        FROM features

        WHERE
            symbol = ?
            AND window_seconds = ?

        ORDER BY ts DESC

        LIMIT 1
        """,
        (
            symbol,
            window_seconds,
        ),
    ).fetchone()


    if row is None:
        return None


    result = dict(
        row
    )


    age = (
        time.time()
        -
        float(
            result[
                "ts"
            ]
        )
    )


    if age > MAX_FEATURE_AGE_SECONDS:
        return None


    return result


def get_latest_features(
    conn,
    symbol,
):
    return {
        15:
            get_latest_feature(
                conn,
                symbol,
                15,
            ),

        30:
            get_latest_feature(
                conn,
                symbol,
                30,
            ),

        60:
            get_latest_feature(
                conn,
                symbol,
                60,
            ),
    }


# ============================================================
# IBKR METRIC ACCELERATION
# ============================================================

def get_metric_rows(
    conn,
    symbol,
    start_ts,
    end_ts,
):
    rows = conn.execute(
        """
        SELECT
            ts,
            trade_count,
            trade_rate,
            volume_rate

        FROM market_metrics

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


def relative_acceleration(
    recent_value,
    baseline_value,
):
    recent_value = safe_float(
        recent_value
    )

    baseline_value = safe_float(
        baseline_value
    )


    if (
        recent_value is None
        or
        baseline_value is None
    ):
        return 0.0


    if baseline_value > 0:
        return (
            (
                recent_value
                -
                baseline_value
            )
            /
            baseline_value
        )


    if recent_value > 0:
        return 1.0


    return 0.0


def get_ib_metric_acceleration(
    conn,
    symbol,
):
    now = time.time()


    recent_start = (
        now
        -
        RECENT_WINDOW_SECONDS
    )


    baseline_start = (
        now
        -
        BASELINE_WINDOW_SECONDS
    )


    baseline_end = (
        recent_start
    )


    recent_rows = (
        get_metric_rows(
            conn,
            symbol,
            recent_start,
            now,
        )
    )


    baseline_rows = (
        get_metric_rows(
            conn,
            symbol,
            baseline_start,
            baseline_end,
        )
    )


    recent_trade_rate = average(
        [
            row[
                "trade_rate"
            ]
            for row
            in recent_rows
        ]
    )


    baseline_trade_rate = average(
        [
            row[
                "trade_rate"
            ]
            for row
            in baseline_rows
        ]
    )


    recent_volume_rate = average(
        [
            row[
                "volume_rate"
            ]
            for row
            in recent_rows
        ]
    )


    baseline_volume_rate = average(
        [
            row[
                "volume_rate"
            ]
            for row
            in baseline_rows
        ]
    )


    return {
        "recent_trade_rate":
            recent_trade_rate,

        "baseline_trade_rate":
            baseline_trade_rate,

        "trade_rate_acceleration":
            relative_acceleration(
                recent_trade_rate,
                baseline_trade_rate,
            ),

        "recent_volume_rate":
            recent_volume_rate,

        "baseline_volume_rate":
            baseline_volume_rate,

        "volume_rate_acceleration":
            relative_acceleration(
                recent_volume_rate,
                baseline_volume_rate,
            ),
    }


# ============================================================
# COMPONENTS
# ============================================================

def price_component(
    features,
    action,
):
    direction = (
        direction_factor(
            action
        )
    )


    weighted = []


    weights = {
        15:
            0.50,

        30:
            0.30,

        60:
            0.20,
    }


    for window in (
        15,
        30,
        60,
    ):
        row = features.get(
            window
        )


        if row is None:
            continue


        change = safe_float(
            row.get(
                "price_change_pct"
            )
        )


        if change is None:
            continue


        directional_change = (
            change
            *
            direction
        )


        score = clamp(
            directional_change
            *
            20.0,
            -20.0,
            20.0,
        )


        weighted.append(
            (
                score,
                weights[
                    window
                ],
            )
        )


    if not weighted:
        return 0.0


    numerator = sum(
        score
        *
        weight
        for (
            score,
            weight,
        )
        in weighted
    )


    denominator = sum(
        weight
        for (
            score,
            weight,
        )
        in weighted
    )


    return (
        numerator
        /
        denominator
    )


def local_trade_component(
    features,
):
    values = []


    for window in (
        15,
        30,
        60,
    ):
        row = features.get(
            window
        )


        if row is None:
            continue


        value = safe_float(
            row.get(
                "trade_rate_acceleration"
            )
        )


        if value is None:
            continue


        if window == 15:
            weight = 0.50

        elif window == 30:
            weight = 0.30

        else:
            weight = 0.20


        values.append(
            (
                value,
                weight,
            )
        )


    if not values:
        return 0.0


    weighted_acc = (
        sum(
            value
            *
            weight
            for (
                value,
                weight,
            )
            in values
        )
        /
        sum(
            weight
            for (
                value,
                weight,
            )
            in values
        )
    )


    return clamp(
        weighted_acc
        *
        12.0,
        -15.0,
        15.0,
    )


def local_volume_component(
    features,
):
    values = []


    for window in (
        15,
        30,
        60,
    ):
        row = features.get(
            window
        )


        if row is None:
            continue


        value = safe_float(
            row.get(
                "volume_rate_acceleration"
            )
        )


        if value is None:
            continue


        if window == 15:
            weight = 0.50

        elif window == 30:
            weight = 0.30

        else:
            weight = 0.20


        values.append(
            (
                value,
                weight,
            )
        )


    if not values:
        return 0.0


    weighted_acc = (
        sum(
            value
            *
            weight
            for (
                value,
                weight,
            )
            in values
        )
        /
        sum(
            weight
            for (
                value,
                weight,
            )
            in values
        )
    )


    return clamp(
        weighted_acc
        *
        12.0,
        -15.0,
        15.0,
    )


def imbalance_component(
    features,
    action,
):
    direction = (
        direction_factor(
            action
        )
    )


    latest = (
        features.get(
            15
        )
        or
        features.get(
            30
        )
        or
        features.get(
            60
        )
    )


    if latest is None:
        return 0.0


    imbalance = safe_float(
        latest.get(
            "bid_ask_imbalance"
        )
    )


    if imbalance is None:
        return 0.0


    directional = (
        imbalance
        *
        direction
    )


    return clamp(
        directional
        *
        15.0,
        -15.0,
        15.0,
    )


def spread_component(
    features,
):
    latest = (
        features.get(
            15
        )
        or
        features.get(
            30
        )
        or
        features.get(
            60
        )
    )


    if latest is None:
        return 0.0


    spread = safe_float(
        latest.get(
            "spread_pct"
        )
    )


    if spread is None:
        return -3.0


    if spread <= 0.15:
        return 2.0


    if spread <= 0.35:
        return 0.0


    if spread <= 0.75:
        return -4.0


    if spread <= 1.25:
        return -8.0


    return -12.0


def ib_trade_component(
    ib_metrics,
):
    acceleration = safe_float(
        ib_metrics.get(
            "trade_rate_acceleration"
        )
    )


    if acceleration is None:
        return 0.0


    return clamp(
        acceleration
        *
        10.0,
        -12.0,
        12.0,
    )


def ib_volume_component(
    ib_metrics,
):
    acceleration = safe_float(
        ib_metrics.get(
            "volume_rate_acceleration"
        )
    )


    if acceleration is None:
        return 0.0


    return clamp(
        acceleration
        *
        10.0,
        -12.0,
        12.0,
    )


# ============================================================
# CLASSIFICATION
# ============================================================

def classify_micro_state(
    score,
):
    if score >= 35:
        return "ACCELERATING"


    if score >= 15:
        return "BUILDING"


    if score <= -35:
        return "REVERSING"


    if score <= -15:
        return "WEAKENING"


    return "NEUTRAL"


# ============================================================
# CORE SCORER
# ============================================================

def calculate_micro_score_from_data(
    *,
    symbol,
    action,
    features,
    ib_metrics,
):
    components = {
        "price":
            price_component(
                features,
                action,
            ),

        "local_trade":
            local_trade_component(
                features
            ),

        "local_volume":
            local_volume_component(
                features
            ),

        "imbalance":
            imbalance_component(
                features,
                action,
            ),

        "ib_trade":
            ib_trade_component(
                ib_metrics
            ),

        "ib_volume":
            ib_volume_component(
                ib_metrics
            ),

        "spread":
            spread_component(
                features
            ),
    }


    raw_score = sum(
        components.values()
    )


    score = clamp(
        raw_score,
        -100.0,
        100.0,
    )


    state = (
        classify_micro_state(
            score
        )
    )


    available_windows = [
        window
        for window
        in (
            15,
            30,
            60,
        )
        if features.get(
            window
        )
        is not None
    ]


    return {
        "symbol":
            symbol,

        "action":
            action,

        "micro_score":
            round(
                score,
                2,
            ),

        "micro_state":
            state,

        "available_windows":
            available_windows,

        "components": {
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

        "ib_metrics": {
            key:
                (
                    None
                    if value is None
                    else
                    round(
                        float(
                            value
                        ),
                        4,
                    )
                )
            for (
                key,
                value,
            )
            in ib_metrics.items()
        },
    }


# ============================================================
# PUBLIC API
# ============================================================

def get_micro_score(
    symbol,
    action=None,
):
    symbol = (
        str(
            symbol
        )
        .strip()
        .upper()
    )


    if not symbol:
        raise ValueError(
            "symbol is required"
        )


    if action is None:
        action = (
            get_latest_action(
                symbol
            )
        )


    action = (
        str(
            action
            or
            ""
        )
        .strip()
        .upper()
    )


    if action not in {
        "BUY",
        "SELL",
    }:
        raise ValueError(
            f"Could not determine action "
            f"for {symbol}"
        )


    if not MICRO_DB_FILE.exists():
        return {
            "symbol":
                symbol,

            "action":
                action,

            "micro_score":
                0.0,

            "micro_state":
                "WARMING_UP",

            "available_windows":
                [],

            "components":
                {},

            "ib_metrics":
                {},
        }


    conn = micro_db_connect()

    try:
        features = (
            get_latest_features(
                conn,
                symbol,
            )
        )


        available = [
            row
            for row
            in features.values()
            if row is not None
        ]


        if not available:
            return {
                "symbol":
                    symbol,

                "action":
                    action,

                "micro_score":
                    0.0,

                "micro_state":
                    "WARMING_UP",

                "available_windows":
                    [],

                "components":
                    {},

                "ib_metrics":
                    {},
            }


        ib_metrics = (
            get_ib_metric_acceleration(
                conn,
                symbol,
            )
        )


        return (
            calculate_micro_score_from_data(
                symbol=
                    symbol,

                action=
                    action,

                features=
                    features,

                ib_metrics=
                    ib_metrics,
            )
        )


    finally:
        conn.close()


# ============================================================
# ACTIVE SYMBOLS
# ============================================================

def get_active_symbols():
    if not MICRO_DB_FILE.exists():
        return []


    cutoff = (
        time.time()
        -
        MAX_FEATURE_AGE_SECONDS
    )


    conn = micro_db_connect()

    try:
        rows = conn.execute(
            """
            SELECT
                symbol,
                MAX(ts) AS last_ts

            FROM features

            WHERE ts >= ?

            GROUP BY symbol

            ORDER BY last_ts DESC
            """,
            (
                cutoff,
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
        ]


    finally:
        conn.close()


# ============================================================
# SELF TEST
# ============================================================

def run_self_test():
    print(
        "=============================================================="
    )

    print(
        "MICROSTRUCTURE SCORE SELF TEST"
    )

    print(
        "=============================================================="
    )


    synthetic_features = {
        15: {
            "price_change_pct":
                0.45,

            "trade_rate_acceleration":
                1.50,

            "volume_rate_acceleration":
                2.00,

            "bid_ask_imbalance":
                0.55,

            "spread_pct":
                0.20,
        },

        30: {
            "price_change_pct":
                0.35,

            "trade_rate_acceleration":
                0.80,

            "volume_rate_acceleration":
                1.00,

            "bid_ask_imbalance":
                0.40,

            "spread_pct":
                0.22,
        },

        60: {
            "price_change_pct":
                0.20,

            "trade_rate_acceleration":
                0.30,

            "volume_rate_acceleration":
                0.40,

            "bid_ask_imbalance":
                0.30,

            "spread_pct":
                0.25,
        },
    }


    synthetic_ib = {
        "recent_trade_rate":
            12.0,

        "baseline_trade_rate":
            5.0,

        "trade_rate_acceleration":
            1.40,

        "recent_volume_rate":
            5000.0,

        "baseline_volume_rate":
            1800.0,

        "volume_rate_acceleration":
            1.7778,
    }


    result = (
        calculate_micro_score_from_data(
            symbol=
                "TMXMICROTEST",

            action=
                "BUY",

            features=
                synthetic_features,

            ib_metrics=
                synthetic_ib,
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
            "micro_score"
        ]
        <=
        0
    ):
        print(
            "MICRO_SCORE_SELF_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    if (
        result[
            "micro_state"
        ]
        not in {
            "BUILDING",
            "ACCELERATING",
        }
    ):
        print(
            "MICRO_SCORE_SELF_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    print(
        "MICRO_SCORE_SELF_TEST=PASS"
    )


# ============================================================
# PRINT CURRENT SCORES
# ============================================================

def print_current_scores():
    symbols = (
        get_active_symbols()
    )


    print()
    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX MICROSTRUCTURE SCORES"
    )

    print(
        "=============================================================="
    )


    if not symbols:
        print(
            "No fresh microstructure features."
        )

        return


    for symbol in symbols:
        try:
            result = (
                get_micro_score(
                    symbol
                )
            )


            print(
                "MICRO SCORE | "
                f"{symbol:<8} | "
                f"action="
                f"{result['action']:<4} | "
                f"score="
                f"{result['micro_score']:+6.2f} | "
                f"state="
                f"{result['micro_state']:<12} | "
                f"windows="
                f"{result['available_windows']}"
            )


            if result[
                "components"
            ]:

                components = (
                    result[
                        "components"
                    ]
                )


                print(
                    "MICRO COMPONENTS | "
                    f"{symbol:<8} | "
                    f"price="
                    f"{components.get('price', 0):+6.2f} | "
                    f"local_trade="
                    f"{components.get('local_trade', 0):+6.2f} | "
                    f"local_vol="
                    f"{components.get('local_volume', 0):+6.2f} | "
                    f"imbalance="
                    f"{components.get('imbalance', 0):+6.2f} | "
                    f"ib_trade="
                    f"{components.get('ib_trade', 0):+6.2f} | "
                    f"ib_vol="
                    f"{components.get('ib_volume', 0):+6.2f} | "
                    f"spread="
                    f"{components.get('spread', 0):+6.2f}"
                )


        except Exception as exc:
            print(
                "MICRO SCORE ERROR | "
                f"{symbol} | "
                f"{type(exc).__name__}: "
                f"{exc}"
            )


# ============================================================
# MAIN
# ============================================================

def main():
    if (
        "--self-test"
        in
        sys.argv
    ):
        run_self_test()

        return


    print_current_scores()


if __name__ == "__main__":
    main()
