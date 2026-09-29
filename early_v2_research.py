import json
import math
import sqlite3

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

EVENT_SPACING_SECONDS = 300

TRAIN_RATIO = 0.60


HORIZONS = [
    60,
    180,
    300,
]


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10,
    )

    conn.row_factory = sqlite3.Row

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


def mean(
    values,
):
    if not values:
        return None


    return (
        sum(
            values
        )
        /
        len(
            values
        )
    )


def median(
    values,
):
    if not values:
        return None


    ordered = sorted(
        values
    )


    count = len(
        ordered
    )


    middle = (
        count
        //
        2
    )


    if count % 2:
        return ordered[
            middle
        ]


    return (
        ordered[
            middle - 1
        ]
        +
        ordered[
            middle
        ]
    ) / 2.0


def trimmed_mean(
    values,
    fraction=0.10,
):
    if not values:
        return None


    ordered = sorted(
        values
    )


    if len(
        ordered
    ) < 10:
        return mean(
            ordered
        )


    trim = int(
        len(
            ordered
        )
        *
        fraction
    )


    if trim <= 0:
        return mean(
            ordered
        )


    remaining = ordered[
        trim:
        len(
            ordered
        )
        -
        trim
    ]


    if not remaining:
        return mean(
            ordered
        )


    return mean(
        remaining
    )


def correlation(
    pairs,
):
    usable = [
        (
            safe_float(
                x
            ),
            safe_float(
                y
            ),
        )
        for (
            x,
            y,
        )
        in pairs
    ]


    usable = [
        (
            x,
            y,
        )
        for (
            x,
            y,
        )
        in usable
        if (
            x is not None
            and
            y is not None
        )
    ]


    if len(
        usable
    ) < 3:
        return None


    xs = [
        row[
            0
        ]
        for row
        in usable
    ]


    ys = [
        row[
            1
        ]
        for row
        in usable
    ]


    mx = mean(
        xs
    )


    my = mean(
        ys
    )


    numerator = sum(
        (
            x
            -
            mx
        )
        *
        (
            y
            -
            my
        )
        for (
            x,
            y,
        )
        in usable
    )


    vx = sum(
        (
            x
            -
            mx
        )
        ** 2
        for x
        in xs
    )


    vy = sum(
        (
            y
            -
            my
        )
        ** 2
        for y
        in ys
    )


    denominator = math.sqrt(
        vx
        *
        vy
    )


    if denominator <= 0:
        return None


    return (
        numerator
        /
        denominator
    )


def clamp(
    value,
    minimum=-100.0,
    maximum=100.0,
):
    return max(
        minimum,
        min(
            maximum,
            value,
        ),
    )


def fmt(
    value,
):
    if value is None:
        return "N/A"


    return (
        f"{value:+.3f}"
    )


# ============================================================
# LOAD
# ============================================================

def load_rows(
    horizon,
):
    conn = db_connect()


    try:
        rows = conn.execute(
            """
            SELECT
                o.snapshot_id,
                o.symbol,
                o.action,
                o.snapshot_ts,
                o.directional_return_pct,

                s.early_score,
                s.early_components_json

            FROM outcomes o

            JOIN snapshots s
              ON s.id = o.snapshot_id

            WHERE
                o.horizon_seconds = ?
                AND s.early_components_json IS NOT NULL
                AND o.directional_return_pct IS NOT NULL

            ORDER BY
                o.snapshot_ts ASC
            """,
            (
                horizon,
            ),
        ).fetchall()


        result = []


        for row in rows:
            try:
                components = json.loads(
                    row[
                        "early_components_json"
                    ]
                    or
                    "{}"
                )

            except Exception:
                components = {}


            result.append(
                {
                    "snapshot_id":
                        int(
                            row[
                                "snapshot_id"
                            ]
                        ),

                    "symbol":
                        str(
                            row[
                                "symbol"
                            ]
                        )
                        .strip()
                        .upper(),

                    "action":
                        str(
                            row[
                                "action"
                            ]
                        )
                        .strip()
                        .upper(),

                    "snapshot_ts":
                        safe_float(
                            row[
                                "snapshot_ts"
                            ]
                        ),

                    "return_pct":
                        safe_float(
                            row[
                                "directional_return_pct"
                            ]
                        ),

                    "v1":
                        safe_float(
                            row[
                                "early_score"
                            ]
                        ),

                    "price":
                        safe_float(
                            components.get(
                                "price"
                            )
                        )
                        or
                        0.0,

                    "spread":
                        safe_float(
                            components.get(
                                "spread"
                            )
                        )
                        or
                        0.0,

                    "trigger":
                        safe_float(
                            components.get(
                                "trigger"
                            )
                        )
                        or
                        0.0,
                }
            )


        return result


    finally:
        conn.close()


# ============================================================
# DEDUPLICATE
# ============================================================

def deduplicate(
    rows,
):
    rows = sorted(
        rows,
        key=lambda row: row[
            "snapshot_ts"
        ],
    )


    last_ts = {}


    result = []


    for row in rows:
        key = (
            row[
                "symbol"
            ],
            row[
                "action"
            ],
        )


        previous = (
            last_ts.get(
                key
            )
        )


        if (
            previous is not None
            and
            row[
                "snapshot_ts"
            ]
            -
            previous
            <
            EVENT_SPACING_SECONDS
        ):
            continue


        last_ts[
            key
        ] = row[
            "snapshot_ts"
        ]


        result.append(
            row
        )


    return result


# ============================================================
# FORMULAS
# ============================================================

def score_v1(
    row,
):
    return (
        row[
            "v1"
        ]
    )


def score_v2a(
    row,
):
    return clamp(
        -
        row[
            "price"
        ]
    )


def score_v2b(
    row,
):
    return clamp(
        (
            -
            row[
                "price"
            ]
        )
        -
        (
            0.50
            *
            row[
                "spread"
            ]
        )
    )


def score_v2c(
    row,
):
    return clamp(
        (
            -
            row[
                "price"
            ]
        )
        -
        (
            0.50
            *
            row[
                "spread"
            ]
        )
        -
        (
            0.25
            *
            row[
                "trigger"
            ]
        )
    )


FORMULAS = {
    "V1":
        score_v1,

    "V2A_PRICE_INV":
        score_v2a,

    "V2B_PRICE_SPREAD_INV":
        score_v2b,

    "V2C_PRICE_SPREAD_TRIGGER_INV":
        score_v2c,
}


# ============================================================
# SPLIT
# ============================================================

def chronological_split(
    rows,
):
    ordered = sorted(
        rows,
        key=lambda row: row[
            "snapshot_ts"
        ],
    )


    if len(
        ordered
    ) < 5:
        return (
            ordered,
            [],
        )


    split_index = int(
        len(
            ordered
        )
        *
        TRAIN_RATIO
    )


    split_index = max(
        1,
        min(
            len(
                ordered
            )
            -
            1,
            split_index,
        ),
    )


    return (
        ordered[
            :split_index
        ],
        ordered[
            split_index:
        ],
    )


# ============================================================
# STATS
# ============================================================

def formula_stats(
    rows,
    formula,
):
    pairs = []


    for row in rows:
        score = safe_float(
            formula(
                row
            )
        )


        ret = safe_float(
            row[
                "return_pct"
            ]
        )


        if (
            score is None
            or
            ret is None
        ):
            continue


        pairs.append(
            (
                score,
                ret,
            )
        )


    if not pairs:
        return None


    returns = [
        item[
            1
        ]
        for item
        in pairs
    ]


    positive_rate = (
        sum(
            1
            for value
            in returns
            if value > 0
        )
        /
        len(
            returns
        )
        *
        100.0
    )


    adverse_rate = (
        sum(
            1
            for value
            in returns
            if value <= -0.50
        )
        /
        len(
            returns
        )
        *
        100.0
    )


    return {
        "n":
            len(
                pairs
            ),

        "correlation":
            correlation(
                pairs
            ),

        "mean":
            mean(
                returns
            ),

        "median":
            median(
                returns
            ),

        "trimmed":
            trimmed_mean(
                returns
            ),

        "positive_rate":
            positive_rate,

        "adverse_rate":
            adverse_rate,
    }


def quintile_stats(
    rows,
    formula,
):
    scored = []


    for row in rows:
        score = safe_float(
            formula(
                row
            )
        )


        ret = safe_float(
            row[
                "return_pct"
            ]
        )


        if (
            score is None
            or
            ret is None
        ):
            continue


        scored.append(
            (
                score,
                ret,
            )
        )


    if len(
        scored
    ) < 10:
        return None


    scored.sort(
        key=lambda item: item[
            0
        ]
    )


    bucket_size = max(
        1,
        len(
            scored
        )
        //
        5,
    )


    low = scored[
        :bucket_size
    ]


    high = scored[
        -bucket_size:
    ]


    low_returns = [
        item[
            1
        ]
        for item
        in low
    ]


    high_returns = [
        item[
            1
        ]
        for item
        in high
    ]


    return {
        "low_score_min":
            low[
                0
            ][
                0
            ],

        "low_score_max":
            low[
                -1
            ][
                0
            ],

        "high_score_min":
            high[
                0
            ][
                0
            ],

        "high_score_max":
            high[
                -1
            ][
                0
            ],

        "low_mean":
            mean(
                low_returns
            ),

        "low_median":
            median(
                low_returns
            ),

        "high_mean":
            mean(
                high_returns
            ),

        "high_median":
            median(
                high_returns
            ),

        "spread_mean":
            (
                mean(
                    high_returns
                )
                -
                mean(
                    low_returns
                )
            ),

        "spread_median":
            (
                median(
                    high_returns
                )
                -
                median(
                    low_returns
                )
            ),
    }


# ============================================================
# PRINT
# ============================================================

def print_formula(
    label,
    rows,
    formula,
):
    stats = (
        formula_stats(
            rows,
            formula,
        )
    )


    quintiles = (
        quintile_stats(
            rows,
            formula,
        )
    )


    if stats is None:
        print(
            f"{label:<28} | no data"
        )

        return


    print(
        f"{label:<28} | "
        f"n={stats['n']:>3} | "
        f"corr={fmt(stats['correlation'])} | "
        f"avg={fmt(stats['mean'])}% | "
        f"median={fmt(stats['median'])}% | "
        f"trim={fmt(stats['trimmed'])}% | "
        f"positive="
        f"{stats['positive_rate']:5.1f}% | "
        f"adverse="
        f"{stats['adverse_rate']:5.1f}%"
    )


    if quintiles is not None:
        print(
            f"{'':28} | "
            f"Q1_mean="
            f"{fmt(quintiles['low_mean'])}% | "
            f"Q5_mean="
            f"{fmt(quintiles['high_mean'])}% | "
            f"spread="
            f"{fmt(quintiles['spread_mean'])}% | "
            f"Q1_med="
            f"{fmt(quintiles['low_median'])}% | "
            f"Q5_med="
            f"{fmt(quintiles['high_median'])}%"
        )


# ============================================================
# ACTION FILTER
# ============================================================

def filter_action(
    rows,
    action,
):
    return [
        row
        for row
        in rows
        if row[
            "action"
        ]
        ==
        action
    ]


# ============================================================
# ANALYSIS
# ============================================================

def analyze_horizon(
    horizon,
):
    raw = (
        load_rows(
            horizon
        )
    )


    rows = (
        deduplicate(
            raw
        )
    )


    (
        train,
        test,
    ) = chronological_split(
        rows
    )


    print()
    print(
        "=============================================================="
    )

    print(
        f"EARLY V2 RESEARCH | {horizon}s"
    )

    print(
        "=============================================================="
    )


    print(
        f"raw={len(raw)} | "
        f"events={len(rows)} | "
        f"train={len(train)} | "
        f"test={len(test)} | "
        f"split="
        f"{TRAIN_RATIO:.0%}/{1.0 - TRAIN_RATIO:.0%}"
    )


    for dataset_name, dataset in (
        (
            "TRAIN",
            train,
        ),
        (
            "TEST",
            test,
        ),
    ):

        print()
        print(
            dataset_name
        )


        for (
            formula_name,
            formula,
        ) in FORMULAS.items():

            print_formula(
                formula_name,
                dataset,
                formula,
            )


        for action in (
            "BUY",
            "SELL",
        ):
            action_rows = (
                filter_action(
                    dataset,
                    action,
                )
            )


            print()
            print(
                f"{dataset_name} {action}"
            )


            for (
                formula_name,
                formula,
            ) in FORMULAS.items():

                print_formula(
                    formula_name,
                    action_rows,
                    formula,
                )


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX EARLY V2 RESEARCH"
    )

    print(
        "READ ONLY / CHRONOLOGICAL HOLDOUT"
    )

    print(
        "=============================================================="
    )


    for horizon in HORIZONS:
        analyze_horizon(
            horizon
        )


if __name__ == "__main__":
    main()
