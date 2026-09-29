import math
import sqlite3
import statistics

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


HORIZONS = [
    60,
    180,
    300,
]


MICRO_NOT_READY_STATES = {
    "",
    "WARMING_UP",
    "UNAVAILABLE",
    "NONE",
    "NULL",
}


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


    return statistics.median(
        values
    )


def stddev(
    values,
):
    if len(
        values
    ) < 2:
        return None


    return statistics.stdev(
        values
    )


def percentile(
    values,
    fraction,
):
    if not values:
        return None


    ordered = sorted(
        values
    )


    if len(
        ordered
    ) == 1:
        return ordered[
            0
        ]


    position = (
        (
            len(
                ordered
            )
            -
            1
        )
        *
        fraction
    )


    lower = int(
        math.floor(
            position
        )
    )


    upper = int(
        math.ceil(
            position
        )
    )


    if lower == upper:
        return ordered[
            lower
        ]


    weight = (
        position
        -
        lower
    )


    return (
        ordered[
            lower
        ]
        *
        (
            1.0
            -
            weight
        )
        +
        ordered[
            upper
        ]
        *
        weight
    )


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


    usable = ordered[
        trim:
        len(
            ordered
        )
        -
        trim
    ]


    if not usable:
        return mean(
            ordered
        )


    return mean(
        usable
    )


def correlation(
    pairs,
):
    usable = []


    for (
        score,
        ret,
    ) in pairs:
        score = safe_float(
            score
        )


        ret = safe_float(
            ret
        )


        if (
            score is None
            or
            ret is None
        ):
            continue


        usable.append(
            (
                score,
                ret,
            )
        )


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


def fmt(
    value,
):
    if value is None:
        return "N/A"


    return (
        f"{value:+.3f}"
    )


def sign_of(
    value,
):
    value = safe_float(
        value
    )


    if value is None:
        return 0


    if value > 0:
        return 1


    if value < 0:
        return -1


    return 0


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

                o.v1_score,
                o.v2_score,

                s.micro_score,
                s.micro_state,
                s.micro_windows_json

            FROM early_v2_outcomes o

            JOIN snapshots s
              ON s.id = o.snapshot_id

            WHERE
                o.horizon_seconds = ?

            ORDER BY
                o.snapshot_ts ASC
            """,
            (
                horizon,
            ),
        ).fetchall()


        result = []


        for row in rows:
            v1 = safe_float(
                row[
                    "v1_score"
                ]
            )


            v2 = safe_float(
                row[
                    "v2_score"
                ]
            )


            micro = safe_float(
                row[
                    "micro_score"
                ]
            )


            ret = safe_float(
                row[
                    "directional_return_pct"
                ]
            )


            if ret is None:
                continue


            micro_state = (
                str(
                    row[
                        "micro_state"
                    ]
                    or
                    ""
                )
                .strip()
                .upper()
            )


            micro_windows_raw = (
                row[
                    "micro_windows_json"
                ]
            )


            micro_has_windows = (
                micro_windows_raw is not None
                and
                str(
                    micro_windows_raw
                ).strip()
                not in {
                    "",
                    "[]",
                    "{}",
                    "null",
                    "None",
                }
            )


            micro_ready = (
                micro is not None
                and
                micro_state
                not in
                MICRO_NOT_READY_STATES
                and
                micro_has_windows
            )


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
                        float(
                            row[
                                "snapshot_ts"
                            ]
                        ),

                    "return_pct":
                        ret,

                    "v1":
                        v1,

                    "v2":
                        v2,

                    "micro":
                        micro,

                    "micro_state":
                        micro_state,

                    "micro_ready":
                        micro_ready,
                }
            )


        return result


    finally:
        conn.close()


# ============================================================
# DEDUP
# ============================================================

def deduplicate(
    rows,
):
    last_ts = {}

    selected = []


    for row in sorted(
        rows,
        key=lambda item: item[
            "snapshot_ts"
        ],
    ):
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


        selected.append(
            row
        )


    return selected


# ============================================================
# SCALE ANALYSIS
# ============================================================

def describe_scale(
    rows,
    field,
):
    values = []


    for row in rows:
        value = safe_float(
            row.get(
                field
            )
        )


        if value is None:
            continue


        values.append(
            value
        )


    if not values:
        return None


    abs_values = [
        abs(
            value
        )
        for value
        in values
    ]


    return {
        "n":
            len(
                values
            ),

        "mean":
            mean(
                values
            ),

        "mean_abs":
            mean(
                abs_values
            ),

        "median_abs":
            median(
                abs_values
            ),

        "std":
            stddev(
                values
            ),

        "min":
            min(
                values
            ),

        "max":
            max(
                values
            ),

        "p25_abs":
            percentile(
                abs_values,
                0.25,
            ),

        "p50_abs":
            percentile(
                abs_values,
                0.50,
            ),

        "p75_abs":
            percentile(
                abs_values,
                0.75,
            ),

        "p90_abs":
            percentile(
                abs_values,
                0.90,
            ),
    }


def print_scale(
    name,
    stats,
):
    if stats is None:
        print(
            f"{name:<10} | no data"
        )

        return


    print(
        f"{name:<10} | "
        f"n={stats['n']:>3} | "
        f"mean={fmt(stats['mean'])} | "
        f"mean_abs={fmt(stats['mean_abs'])} | "
        f"median_abs={fmt(stats['median_abs'])} | "
        f"std={fmt(stats['std'])}"
    )


    print(
        f"{'':10} | "
        f"min={fmt(stats['min'])} | "
        f"max={fmt(stats['max'])} | "
        f"p25_abs={fmt(stats['p25_abs'])} | "
        f"p50_abs={fmt(stats['p50_abs'])} | "
        f"p75_abs={fmt(stats['p75_abs'])} | "
        f"p90_abs={fmt(stats['p90_abs'])}"
    )


# ============================================================
# RETURN STATS
# ============================================================

def return_stats(
    rows,
):
    returns = [
        safe_float(
            row[
                "return_pct"
            ]
        )
        for row
        in rows
    ]


    returns = [
        value
        for value
        in returns
        if value is not None
    ]


    if not returns:
        return None


    return {
        "n":
            len(
                returns
            ),

        "avg":
            mean(
                returns
            ),

        "median":
            median(
                returns
            ),

        "trim":
            trimmed_mean(
                returns
            ),

        "positive":
            (
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
            ),

        "move05":
            (
                sum(
                    1
                    for value
                    in returns
                    if value >= 0.50
                )
                /
                len(
                    returns
                )
                *
                100.0
            ),

        "adverse05":
            (
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
            ),
    }


def print_return_stats(
    label,
    rows,
):
    stats = (
        return_stats(
            rows
        )
    )


    if stats is None:
        print(
            f"{label:<30} | no data"
        )

        return


    print(
        f"{label:<30} | "
        f"n={stats['n']:>3} | "
        f"avg="
        f"{fmt(stats['avg'])}% | "
        f"median="
        f"{fmt(stats['median'])}% | "
        f"trim="
        f"{fmt(stats['trim'])}% | "
        f"positive="
        f"{stats['positive']:5.1f}% | "
        f">=0.5%="
        f"{stats['move05']:5.1f}% | "
        f"<=-0.5%="
        f"{stats['adverse05']:5.1f}%"
    )


# ============================================================
# MICRO STATE ANALYSIS
# ============================================================

def analyze_micro_states(
    rows,
):
    print()
    print(
        "MICRO STATE OUTCOMES"
    )


    states = sorted(
        {
            row[
                "micro_state"
            ]
            for row
            in rows
            if row[
                "micro_ready"
            ]
        }
    )


    if not states:
        print(
            "No MICRO READY states."
        )

        return


    for state in states:
        state_rows = [
            row
            for row
            in rows
            if (
                row[
                    "micro_ready"
                ]
                and
                row[
                    "micro_state"
                ]
                ==
                state
            )
        ]


        print_return_stats(
            state,
            state_rows,
        )


# ============================================================
# SIGN AGREEMENT ANALYSIS
# ============================================================

def analyze_sign_agreement(
    rows,
):
    ready = [
        row
        for row
        in rows
        if row[
            "micro_ready"
        ]
    ]


    print()
    print(
        "V2 / MICRO SIGN MATRIX"
    )


    buckets = {
        "V2+ MICRO+":
            [],

        "V2+ MICRO-":
            [],

        "V2- MICRO+":
            [],

        "V2- MICRO-":
            [],

        "V2 ZERO":
            [],

        "MICRO ZERO":
            [],
    }


    for row in ready:
        v2_sign = sign_of(
            row[
                "v2"
            ]
        )


        micro_sign = sign_of(
            row[
                "micro"
            ]
        )


        if v2_sign == 0:
            buckets[
                "V2 ZERO"
            ].append(
                row
            )

            continue


        if micro_sign == 0:
            buckets[
                "MICRO ZERO"
            ].append(
                row
            )

            continue


        if (
            v2_sign > 0
            and
            micro_sign > 0
        ):
            buckets[
                "V2+ MICRO+"
            ].append(
                row
            )


        elif (
            v2_sign > 0
            and
            micro_sign < 0
        ):
            buckets[
                "V2+ MICRO-"
            ].append(
                row
            )


        elif (
            v2_sign < 0
            and
            micro_sign > 0
        ):
            buckets[
                "V2- MICRO+"
            ].append(
                row
            )


        elif (
            v2_sign < 0
            and
            micro_sign < 0
        ):
            buckets[
                "V2- MICRO-"
            ].append(
                row
            )


    for label in (
        "V2+ MICRO+",
        "V2+ MICRO-",
        "V2- MICRO+",
        "V2- MICRO-",
        "V2 ZERO",
        "MICRO ZERO",
    ):
        print_return_stats(
            label,
            buckets[
                label
            ],
        )


# ============================================================
# AGREEMENT AS FILTER
# ============================================================

def analyze_filter_logic(
    rows,
):
    ready = [
        row
        for row
        in rows
        if row[
            "micro_ready"
        ]
    ]


    print()
    print(
        "FILTER / VETO TESTS"
    )


    v2_positive = [
        row
        for row
        in ready
        if sign_of(
            row[
                "v2"
            ]
        )
        >
        0
    ]


    v2_negative = [
        row
        for row
        in ready
        if sign_of(
            row[
                "v2"
            ]
        )
        <
        0
    ]


    positive_confirmed = [
        row
        for row
        in ready
        if (
            sign_of(
                row[
                    "v2"
                ]
            )
            >
            0
            and
            sign_of(
                row[
                    "micro"
                ]
            )
            >
            0
        )
    ]


    positive_vetoed = [
        row
        for row
        in ready
        if (
            sign_of(
                row[
                    "v2"
                ]
            )
            >
            0
            and
            sign_of(
                row[
                    "micro"
                ]
            )
            <=
            0
        )
    ]


    negative_confirmed = [
        row
        for row
        in ready
        if (
            sign_of(
                row[
                    "v2"
                ]
            )
            <
            0
            and
            sign_of(
                row[
                    "micro"
                ]
            )
            <
            0
        )
    ]


    negative_disagreed = [
        row
        for row
        in ready
        if (
            sign_of(
                row[
                    "v2"
                ]
            )
            <
            0
            and
            sign_of(
                row[
                    "micro"
                ]
            )
            >=
            0
        )
    ]


    print_return_stats(
        "V2 POSITIVE",
        v2_positive,
    )


    print_return_stats(
        "V2 POS + MICRO CONFIRM",
        positive_confirmed,
    )


    print_return_stats(
        "V2 POS + MICRO VETO",
        positive_vetoed,
    )


    print_return_stats(
        "V2 NEGATIVE",
        v2_negative,
    )


    print_return_stats(
        "V2 NEG + MICRO CONFIRM",
        negative_confirmed,
    )


    print_return_stats(
        "V2 NEG + MICRO DISAGREE",
        negative_disagreed,
    )


# ============================================================
# CORRELATION
# ============================================================

def analyze_correlations(
    rows,
):
    ready = [
        row
        for row
        in rows
        if row[
            "micro_ready"
        ]
    ]


    print()
    print(
        "MICRO READY CORRELATIONS"
    )


    v2_pairs = [
        (
            row[
                "v2"
            ],
            row[
                "return_pct"
            ],
        )
        for row
        in ready
        if row[
            "v2"
        ]
        is not None
    ]


    micro_pairs = [
        (
            row[
                "micro"
            ],
            row[
                "return_pct"
            ],
        )
        for row
        in ready
        if row[
            "micro"
        ]
        is not None
    ]


    v1_pairs = [
        (
            row[
                "v1"
            ],
            row[
                "return_pct"
            ],
        )
        for row
        in ready
        if row[
            "v1"
        ]
        is not None
    ]


    print(
        f"V1    | "
        f"n={len(v1_pairs):>3} | "
        f"corr="
        f"{fmt(correlation(v1_pairs))}"
    )


    print(
        f"V2    | "
        f"n={len(v2_pairs):>3} | "
        f"corr="
        f"{fmt(correlation(v2_pairs))}"
    )


    print(
        f"MICRO | "
        f"n={len(micro_pairs):>3} | "
        f"corr="
        f"{fmt(correlation(micro_pairs))}"
    )


# ============================================================
# HORIZON
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


    ready = [
        row
        for row
        in rows
        if row[
            "micro_ready"
        ]
    ]


    print()
    print(
        "=============================================================="
    )

    print(
        f"MICRO SCALE / FILTER RESEARCH | "
        f"{horizon}s"
    )

    print(
        "=============================================================="
    )


    print(
        f"raw="
        f"{len(raw)} | "
        f"dedup="
        f"{len(rows)} | "
        f"micro_ready="
        f"{len(ready)}"
    )


    print()
    print(
        "SCALE"
    )


    print_scale(
        "V2",
        describe_scale(
            ready,
            "v2",
        ),
    )


    print_scale(
        "MICRO",
        describe_scale(
            ready,
            "micro",
        ),
    )


    analyze_correlations(
        rows
    )


    analyze_micro_states(
        rows
    )


    analyze_sign_agreement(
        rows
    )


    analyze_filter_logic(
        rows
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX MICRO SCALE / FILTER RESEARCH"
    )

    print(
        "READ ONLY"
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
