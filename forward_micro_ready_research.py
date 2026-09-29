import json
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


EARLY_WEIGHT = 0.60

MICRO_WEIGHT = 0.40


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


def parse_windows(
    raw,
):
    if raw is None:
        return []


    if isinstance(
        raw,
        (
            list,
            tuple,
            dict,
        ),
    ):
        parsed = raw

    else:
        try:
            parsed = json.loads(
                str(
                    raw
                )
            )

        except Exception:
            return []


    if isinstance(
        parsed,
        list,
    ):
        return parsed


    if isinstance(
        parsed,
        tuple,
    ):
        return list(
            parsed
        )


    if isinstance(
        parsed,
        dict,
    ):
        return list(
            parsed.keys()
        )


    return []


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


            micro_windows = (
                parse_windows(
                    row[
                        "micro_windows_json"
                    ]
                )
            )


            micro_ready = (
                micro is not None
                and
                micro_state
                not in
                MICRO_NOT_READY_STATES
                and
                len(
                    micro_windows
                )
                >
                0
            )


            v1_micro = None

            v2_micro = None


            if micro_ready:
                if v1 is not None:
                    v1_micro = (
                        (
                            v1
                            *
                            EARLY_WEIGHT
                        )
                        +
                        (
                            micro
                            *
                            MICRO_WEIGHT
                        )
                    )


                if v2 is not None:
                    v2_micro = (
                        (
                            v2
                            *
                            EARLY_WEIGHT
                        )
                        +
                        (
                            micro
                            *
                            MICRO_WEIGHT
                        )
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

                    "micro_windows":
                        micro_windows,

                    "micro_ready":
                        micro_ready,

                    "v1_micro":
                        v1_micro,

                    "v2_micro":
                        v2_micro,
                }
            )


        return result


    finally:
        conn.close()


# ============================================================
# DEDUPLICATION
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
# MODEL STATS
# ============================================================

def model_pairs(
    rows,
    field,
):
    pairs = []


    for row in rows:
        score = safe_float(
            row.get(
                field
            )
        )


        ret = safe_float(
            row.get(
                "return_pct"
            )
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


    return pairs


def quintile_analysis(
    pairs,
):
    if len(
        pairs
    ) < 10:
        return None


    ordered = sorted(
        pairs,
        key=lambda item: item[
            0
        ],
    )


    bucket_size = max(
        1,
        len(
            ordered
        )
        //
        5,
    )


    low = ordered[
        :bucket_size
    ]


    high = ordered[
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
        "low_mean":
            mean(
                low_returns
            ),

        "high_mean":
            mean(
                high_returns
            ),

        "mean_spread":
            (
                mean(
                    high_returns
                )
                -
                mean(
                    low_returns
                )
            ),

        "low_median":
            median(
                low_returns
            ),

        "high_median":
            median(
                high_returns
            ),

        "median_spread":
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


def calculate_stats(
    rows,
    field,
):
    pairs = (
        model_pairs(
            rows,
            field,
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


    move05_rate = (
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
    )


    adverse05_rate = (
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

        "corr":
            correlation(
                pairs
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
            positive_rate,

        "move05":
            move05_rate,

        "adverse05":
            adverse05_rate,

        "quintiles":
            quintile_analysis(
                pairs
            ),
    }


# ============================================================
# PRINT MODEL
# ============================================================

def print_model(
    name,
    stats,
):
    if stats is None:
        print(
            f"{name:<14} | no data"
        )

        return


    print(
        f"{name:<14} | "
        f"n={stats['n']:>3} | "
        f"corr="
        f"{fmt(stats['corr'])} | "
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


    q = stats[
        "quintiles"
    ]


    if q is not None:
        print(
            f"{'':14} | "
            f"Q1="
            f"{fmt(q['low_mean'])}% | "
            f"Q5="
            f"{fmt(q['high_mean'])}% | "
            f"spread="
            f"{fmt(q['mean_spread'])}% | "
            f"Q1med="
            f"{fmt(q['low_median'])}% | "
            f"Q5med="
            f"{fmt(q['high_median'])}%"
        )


# ============================================================
# MICRO COVERAGE
# ============================================================

def print_micro_coverage(
    rows,
):
    total = len(
        rows
    )


    ready = [
        row
        for row
        in rows
        if row[
            "micro_ready"
        ]
    ]


    warming = [
        row
        for row
        in rows
        if row[
            "micro_state"
        ]
        ==
        "WARMING_UP"
    ]


    unavailable = [
        row
        for row
        in rows
        if row[
            "micro_state"
        ]
        ==
        "UNAVAILABLE"
    ]


    zero_ready = [
        row
        for row
        in ready
        if (
            safe_float(
                row[
                    "micro"
                ]
            )
            is not None
            and
            abs(
                float(
                    row[
                        "micro"
                    ]
                )
            )
            <
            0.000001
        )
    ]


    ready_pct = (
        (
            len(
                ready
            )
            /
            total
        )
        *
        100.0
        if total
        else
        0.0
    )


    print(
        "MICRO COVERAGE | "
        f"total={total} | "
        f"ready={len(ready)} "
        f"({ready_pct:.1f}%) | "
        f"warming={len(warming)} | "
        f"unavailable={len(unavailable)} | "
        f"ready_zero={len(zero_ready)}"
    )


    state_counts = {}


    for row in rows:
        state = (
            row[
                "micro_state"
            ]
            or
            "EMPTY"
        )


        state_counts[
            state
        ] = (
            state_counts.get(
                state,
                0,
            )
            +
            1
        )


    print(
        "MICRO STATES   | "
        +
        " | ".join(
            (
                f"{state}={count}"
            )
            for (
                state,
                count,
            )
            in sorted(
                state_counts.items()
            )
        )
    )


# ============================================================
# ALL EVENTS
# ============================================================

def analyze_all_events(
    rows,
    title,
):
    print()
    print(
        title
    )


    print_model(
        "V1",
        calculate_stats(
            rows,
            "v1",
        ),
    )


    print_model(
        "V2",
        calculate_stats(
            rows,
            "v2",
        ),
    )


# ============================================================
# MICRO READY
# ============================================================

def analyze_micro_ready(
    rows,
    title,
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
        title
    )


    print(
        f"paired_micro_ready="
        f"{len(ready)}"
    )


    print_model(
        "V1",
        calculate_stats(
            ready,
            "v1",
        ),
    )


    print_model(
        "V2",
        calculate_stats(
            ready,
            "v2",
        ),
    )


    print_model(
        "MICRO",
        calculate_stats(
            ready,
            "micro",
        ),
    )


    print_model(
        "V1+MICRO",
        calculate_stats(
            ready,
            "v1_micro",
        ),
    )


    print_model(
        "V2+MICRO",
        calculate_stats(
            ready,
            "v2_micro",
        ),
    )


# ============================================================
# HORIZON ANALYSIS
# ============================================================

def analyze_horizon(
    horizon,
):
    raw = (
        load_rows(
            horizon
        )
    )


    dedup = (
        deduplicate(
            raw
        )
    )


    print()
    print(
        "=============================================================="
    )

    print(
        f"FORWARD MICRO READY RESEARCH | "
        f"{horizon}s"
    )

    print(
        "=============================================================="
    )


    print(
        f"raw="
        f"{len(raw)} | "
        f"dedup="
        f"{len(dedup)} | "
        f"spacing="
        f"{EVENT_SPACING_SECONDS}s"
    )


    print_micro_coverage(
        dedup
    )


    analyze_all_events(
        dedup,
        "ALL EVENTS | ALL ACTIONS",
    )


    analyze_micro_ready(
        dedup,
        "MICRO READY ONLY | ALL ACTIONS",
    )


    for action in (
        "BUY",
        "SELL",
    ):
        action_rows = [
            row
            for row
            in dedup
            if row[
                "action"
            ]
            ==
            action
        ]


        analyze_all_events(
            action_rows,
            f"ALL EVENTS | {action}",
        )


        analyze_micro_ready(
            action_rows,
            f"MICRO READY ONLY | {action}",
        )


# ============================================================
# GLOBAL COVERAGE
# ============================================================

def print_global_coverage():
    conn = db_connect()


    try:
        meta = conn.execute(
            """
            SELECT
                forward_start_ts,
                tracker_version

            FROM early_v2_forward_meta

            WHERE id = 1
            """
        ).fetchone()


        outcome_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM early_v2_outcomes
            """
        ).fetchone()[
            0
        ]


    finally:
        conn.close()


    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX FORWARD MICRO READY RESEARCH"
    )

    print(
        "READ ONLY"
    )

    print(
        "=============================================================="
    )


    if meta is None:
        print(
            "Forward meta: MISSING"
        )

    else:
        print(
            f"Forward start : "
            f"{meta['forward_start_ts']}"
        )


        print(
            f"Tracker       : "
            f"{meta['tracker_version']}"
        )


    print(
        f"Outcome rows  : "
        f"{outcome_count}"
    )


    print(
        f"Early weight  : "
        f"{EARLY_WEIGHT:.2f}"
    )


    print(
        f"Micro weight  : "
        f"{MICRO_WEIGHT:.2f}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print_global_coverage()


    for horizon in HORIZONS:
        analyze_horizon(
            horizon
        )


if __name__ == "__main__":
    main()
