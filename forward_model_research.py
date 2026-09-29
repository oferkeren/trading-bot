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


V1_WEIGHT = 0.60

MICRO_WEIGHT = 0.40


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
    usable = [
        (
            safe_float(
                score
            ),
            safe_float(
                ret
            ),
        )
        for (
            score,
            ret,
        )
        in pairs
    ]


    usable = [
        (
            score,
            ret,
        )
        for (
            score,
            ret,
        )
        in usable
        if (
            score is not None
            and
            ret is not None
        )
    ]


    if len(
        usable
    ) < 3:
        return None


    scores = [
        row[
            0
        ]
        for row
        in usable
    ]


    returns = [
        row[
            1
        ]
        for row
        in usable
    ]


    mean_score = mean(
        scores
    )


    mean_return = mean(
        returns
    )


    numerator = sum(
        (
            score
            -
            mean_score
        )
        *
        (
            ret
            -
            mean_return
        )
        for (
            score,
            ret,
        )
        in usable
    )


    score_var = sum(
        (
            score
            -
            mean_score
        )
        ** 2
        for score
        in scores
    )


    return_var = sum(
        (
            ret
            -
            mean_return
        )
        ** 2
        for ret
        in returns
    )


    denominator = math.sqrt(
        score_var
        *
        return_var
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

                s.micro_score

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


            v1_micro = None

            v2_micro = None


            if (
                v1 is not None
                and
                micro is not None
            ):
                v1_micro = (
                    (
                        v1
                        *
                        V1_WEIGHT
                    )
                    +
                    (
                        micro
                        *
                        MICRO_WEIGHT
                    )
                )


            if (
                v2 is not None
                and
                micro is not None
            ):
                v2_micro = (
                    (
                        v2
                        *
                        V1_WEIGHT
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
# MODEL DATA
# ============================================================

MODELS = {
    "V1":
        "v1",

    "V2":
        "v2",

    "MICRO":
        "micro",

    "V1+MICRO":
        "v1_micro",

    "V2+MICRO":
        "v2_micro",
}


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


# ============================================================
# QUINTILES
# ============================================================

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


    size = max(
        1,
        len(
            ordered
        )
        //
        5,
    )


    low = ordered[
        :size
    ]


    high = ordered[
        -size:
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


# ============================================================
# STATISTICS
# ============================================================

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


    positive = (
        sum(
            1
            for ret
            in returns
            if ret > 0
        )
        /
        len(
            returns
        )
        *
        100.0
    )


    move05 = (
        sum(
            1
            for ret
            in returns
            if ret >= 0.50
        )
        /
        len(
            returns
        )
        *
        100.0
    )


    adverse05 = (
        sum(
            1
            for ret
            in returns
            if ret <= -0.50
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
            positive,

        "move05":
            move05,

        "adverse05":
            adverse05,

        "quintiles":
            quintile_analysis(
                pairs
            ),
    }


# ============================================================
# PRINT
# ============================================================

def print_model(
    name,
    stats,
):
    if stats is None:
        print(
            f"{name:<12} | no data"
        )

        return


    print(
        f"{name:<12} | "
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
            f"{'':12} | "
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
# ANALYSIS
# ============================================================

def analyze_dataset(
    rows,
    title,
):
    print()
    print(
        title
    )


    for (
        model_name,
        field,
    ) in MODELS.items():

        stats = (
            calculate_stats(
                rows,
                field,
            )
        )


        print_model(
            model_name,
            stats,
        )


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
        f"TRUE FORWARD MODEL RESEARCH | "
        f"{horizon}s"
    )

    print(
        "=============================================================="
    )


    print(
        f"raw="
        f"{len(raw)} | "
        f"independent="
        f"{len(dedup)} | "
        f"spacing="
        f"{EVENT_SPACING_SECONDS}s"
    )


    analyze_dataset(
        dedup,
        "ALL",
    )


    for action in (
        "BUY",
        "SELL",
    ):
        rows = [
            row
            for row
            in dedup
            if row[
                "action"
            ]
            ==
            action
        ]


        analyze_dataset(
            rows,
            action,
        )


# ============================================================
# COVERAGE
# ============================================================

def print_coverage():
    conn = db_connect()


    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*)
                    AS total,

                COUNT(
                    DISTINCT snapshot_id
                )
                    AS snapshots,

                MIN(
                    snapshot_ts
                )
                    AS first_ts,

                MAX(
                    snapshot_ts
                )
                    AS last_ts

            FROM early_v2_outcomes
            """
        ).fetchone()


    finally:
        conn.close()


    print(
        "=============================================================="
    )

    print(
        "TRUE FORWARD RESEARCH COVERAGE"
    )

    print(
        "=============================================================="
    )


    print(
        f"Outcome rows      : "
        f"{int(row['total'] or 0)}"
    )


    print(
        f"Unique snapshots  : "
        f"{int(row['snapshots'] or 0)}"
    )


    print(
        f"First snapshot ts : "
        f"{row['first_ts']}"
    )


    print(
        f"Last snapshot ts  : "
        f"{row['last_ts']}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print_coverage()


    for horizon in HORIZONS:
        analyze_horizon(
            horizon
        )


if __name__ == "__main__":
    main()
