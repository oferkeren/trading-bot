import math
import sqlite3
import statistics

from collections import defaultdict
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


SCORE_COLUMNS = {
    "EARLY":
        "early_score",

    "MICRO":
        "micro_score",

    "COMBINED":
        "combined_score",
}


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
    trim_fraction=0.10,
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


    trim_count = int(
        len(
            ordered
        )
        *
        trim_fraction
    )


    if trim_count <= 0:
        return mean(
            ordered
        )


    if (
        trim_count
        *
        2
        >=
        len(
            ordered
        )
    ):
        return mean(
            ordered
        )


    trimmed = ordered[
        trim_count:
        len(
            ordered
        )
        -
        trim_count
    ]


    return mean(
        trimmed
    )


def correlation(
    pairs,
):
    if len(
        pairs
    ) < 3:
        return None


    xs = [
        x
        for (
            x,
            y,
        )
        in pairs
    ]


    ys = [
        y
        for (
            x,
            y,
        )
        in pairs
    ]


    x_mean = mean(
        xs
    )


    y_mean = mean(
        ys
    )


    numerator = sum(
        (
            x
            -
            x_mean
        )
        *
        (
            y
            -
            y_mean
        )
        for (
            x,
            y,
        )
        in pairs
    )


    x_var = sum(
        (
            x
            -
            x_mean
        )
        ** 2
        for x
        in xs
    )


    y_var = sum(
        (
            y
            -
            y_mean
        )
        ** 2
        for y
        in ys
    )


    denominator = math.sqrt(
        x_var
        *
        y_var
    )


    if denominator <= 0:
        return None


    return (
        numerator
        /
        denominator
    )


def fmt_number(
    value,
    digits=3,
):
    if value is None:
        return "N/A"


    return (
        f"{value:+.{digits}f}"
    )


# ============================================================
# LOAD DATA
# ============================================================

def load_rows(
    score_column,
    horizon,
):
    conn = db_connect()


    try:
        rows = conn.execute(
            f"""
            SELECT
                id,

                snapshot_id,

                symbol,
                action,

                snapshot_ts,

                horizon_seconds,

                {score_column}
                    AS score,

                directional_return_pct
                    AS return_pct

            FROM outcomes

            WHERE
                horizon_seconds = ?
                AND {score_column} IS NOT NULL

            ORDER BY
                symbol ASC,
                action ASC,
                snapshot_ts ASC
            """,
            (
                horizon,
            ),
        ).fetchall()


        return [
            dict(
                row
            )
            for row
            in rows
        ]


    finally:
        conn.close()


# ============================================================
# EVENT DEDUPLICATION
# ============================================================

def deduplicate_events(
    rows,
):
    last_event_ts = {}


    selected = []


    for row in rows:
        symbol = (
            str(
                row[
                    "symbol"
                ]
            )
            .strip()
            .upper()
        )


        action = (
            str(
                row[
                    "action"
                ]
            )
            .strip()
            .upper()
        )


        snapshot_ts = safe_float(
            row[
                "snapshot_ts"
            ]
        )


        score = safe_float(
            row[
                "score"
            ]
        )


        return_pct = safe_float(
            row[
                "return_pct"
            ]
        )


        if (
            snapshot_ts is None
            or
            score is None
            or
            return_pct is None
        ):
            continue


        key = (
            symbol,
            action,
        )


        previous_ts = (
            last_event_ts.get(
                key
            )
        )


        if (
            previous_ts is not None
            and
            snapshot_ts
            -
            previous_ts
            <
            EVENT_SPACING_SECONDS
        ):
            continue


        last_event_ts[
            key
        ] = snapshot_ts


        selected.append(
            {
                "symbol":
                    symbol,

                "action":
                    action,

                "snapshot_ts":
                    snapshot_ts,

                "score":
                    score,

                "return_pct":
                    return_pct,
            }
        )


    return selected


# ============================================================
# SCORE BUCKET
# ============================================================

def score_bucket(
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
# QUANTILES
# ============================================================

def quantile_groups(
    rows,
    groups=5,
):
    if not rows:
        return []


    ordered = sorted(
        rows,
        key=lambda row: row[
            "score"
        ],
    )


    total = len(
        ordered
    )


    result = []


    for index in range(
        groups
    ):
        start = int(
            total
            *
            index
            /
            groups
        )


        end = int(
            total
            *
            (
                index
                +
                1
            )
            /
            groups
        )


        bucket_rows = ordered[
            start:
            end
        ]


        if not bucket_rows:
            continue


        result.append(
            (
                index
                +
                1,

                bucket_rows,
            )
        )


    return result


# ============================================================
# STATS
# ============================================================

def calculate_stats(
    rows,
):
    if not rows:
        return None


    returns = [
        row[
            "return_pct"
        ]
        for row
        in rows
    ]


    scores = [
        row[
            "score"
        ]
        for row
        in rows
    ]


    positive_count = sum(
        1
        for value
        in returns
        if value > 0
    )


    move05_count = sum(
        1
        for value
        in returns
        if value >= 0.50
    )


    adverse05_count = sum(
        1
        for value
        in returns
        if value <= -0.50
    )


    symbols = {
        row[
            "symbol"
        ]
        for row
        in rows
    }


    return {
        "n":
            len(
                rows
            ),

        "symbols":
            len(
                symbols
            ),

        "score_min":
            min(
                scores
            ),

        "score_max":
            max(
                scores
            ),

        "score_avg":
            mean(
                scores
            ),

        "return_mean":
            mean(
                returns
            ),

        "return_median":
            median(
                returns
            ),

        "return_trimmed":
            trimmed_mean(
                returns
            ),

        "positive_rate":
            (
                positive_count
                /
                len(
                    rows
                )
                *
                100.0
            ),

        "move05_rate":
            (
                move05_count
                /
                len(
                    rows
                )
                *
                100.0
            ),

        "adverse05_rate":
            (
                adverse05_count
                /
                len(
                    rows
                )
                *
                100.0
            ),

        "correlation":
            correlation(
                [
                    (
                        row[
                            "score"
                        ],

                        row[
                            "return_pct"
                        ],
                    )
                    for row
                    in rows
                ]
            ),
    }


# ============================================================
# PRINT HELPERS
# ============================================================

def print_stats_line(
    label,
    stats,
):
    if stats is None:
        print(
            f"{label:<14} | no data"
        )

        return


    print(
        f"{label:<14} | "
        f"n={stats['n']:>4} | "
        f"symbols={stats['symbols']:>3} | "
        f"avg={fmt_number(stats['return_mean'])}% | "
        f"median={fmt_number(stats['return_median'])}% | "
        f"trim={fmt_number(stats['return_trimmed'])}% | "
        f"positive={stats['positive_rate']:5.1f}% | "
        f">=0.5%={stats['move05_rate']:5.1f}% | "
        f"<=-0.5%={stats['adverse05_rate']:5.1f}% | "
        f"corr={fmt_number(stats['correlation'])}"
    )


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_signal(
    signal_name,
    score_column,
):
    print()
    print(
        "=============================================================="
    )

    print(
        f"{signal_name} ROBUST RESEARCH"
    )

    print(
        "=============================================================="
    )


    for horizon in HORIZONS:
        raw_rows = (
            load_rows(
                score_column,
                horizon,
            )
        )


        rows = (
            deduplicate_events(
                raw_rows
            )
        )


        print()
        print(
            f"HORIZON {horizon}s"
        )


        print(
            f"raw_samples="
            f"{len(raw_rows)} | "
            f"independent_events="
            f"{len(rows)} | "
            f"spacing="
            f"{EVENT_SPACING_SECONDS}s"
        )


        print_stats_line(
            "ALL",
            calculate_stats(
                rows
            ),
        )


        # ====================================================
        # BUY / SELL
        # ====================================================

        for action in (
            "BUY",
            "SELL",
        ):
            action_rows = [
                row
                for row
                in rows
                if row[
                    "action"
                ]
                ==
                action
            ]


            print_stats_line(
                action,
                calculate_stats(
                    action_rows
                ),
            )


        # ====================================================
        # SCORE STATES
        # ====================================================

        grouped = defaultdict(
            list
        )


        for row in rows:
            grouped[
                score_bucket(
                    row[
                        "score"
                    ]
                )
            ].append(
                row
            )


        print()
        print(
            "STATE BUCKETS"
        )


        for state in (
            "ACCELERATING",
            "BUILDING",
            "NEUTRAL",
            "WEAKENING",
            "REVERSING",
        ):
            print_stats_line(
                state,
                calculate_stats(
                    grouped.get(
                        state,
                        []
                    )
                ),
            )


        # ====================================================
        # QUANTILES
        # ====================================================

        print()
        print(
            "SCORE QUINTILES"
        )


        for (
            quintile,
            quintile_rows,
        ) in quantile_groups(
            rows,
            groups=5,
        ):
            stats = (
                calculate_stats(
                    quintile_rows
                )
            )


            if stats is None:
                continue


            label = (
                f"Q{quintile} "
                f"["
                f"{stats['score_min']:+.1f},"
                f"{stats['score_max']:+.1f}"
                f"]"
            )


            print_stats_line(
                label,
                stats,
            )


# ============================================================
# INVERTED EARLY TEST
# ============================================================

def analyze_inverted_early():
    print()
    print(
        "=============================================================="
    )

    print(
        "EARLY SIGN DIAGNOSTIC"
    )

    print(
        "=============================================================="
    )


    for horizon in HORIZONS:
        rows = (
            deduplicate_events(
                load_rows(
                    "early_score",
                    horizon,
                )
            )
        )


        normal_pairs = [
            (
                row[
                    "score"
                ],
                row[
                    "return_pct"
                ],
            )
            for row
            in rows
        ]


        inverted_pairs = [
            (
                -
                row[
                    "score"
                ],
                row[
                    "return_pct"
                ],
            )
            for row
            in rows
        ]


        normal_corr = (
            correlation(
                normal_pairs
            )
        )


        inverted_corr = (
            correlation(
                inverted_pairs
            )
        )


        print(
            f"{horizon:>3}s | "
            f"normal="
            f"{fmt_number(normal_corr)} | "
            f"inverted="
            f"{fmt_number(inverted_corr)} | "
            f"events="
            f"{len(rows)}"
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

                SUM(
                    CASE
                        WHEN early_score IS NOT NULL
                            THEN 1
                        ELSE 0
                    END
                )
                    AS early_count,

                SUM(
                    CASE
                        WHEN micro_score IS NOT NULL
                            THEN 1
                        ELSE 0
                    END
                )
                    AS micro_count,

                SUM(
                    CASE
                        WHEN combined_score IS NOT NULL
                            THEN 1
                        ELSE 0
                    END
                )
                    AS combined_count

            FROM outcomes
            """
        ).fetchone()


    finally:
        conn.close()


    print(
        "=============================================================="
    )

    print(
        "FUSION RESEARCH COVERAGE"
    )

    print(
        "=============================================================="
    )


    print(
        f"Outcome rows : "
        f"{int(row['total'] or 0)}"
    )


    print(
        f"Early        : "
        f"{int(row['early_count'] or 0)}"
    )


    print(
        f"Micro        : "
        f"{int(row['micro_count'] or 0)}"
    )


    print(
        f"Combined     : "
        f"{int(row['combined_count'] or 0)}"
    )


    print(
        f"Event spacing: "
        f"{EVENT_SPACING_SECONDS}s"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print_coverage()


    for (
        signal_name,
        score_column,
    ) in SCORE_COLUMNS.items():

        analyze_signal(
            signal_name,
            score_column,
        )


    analyze_inverted_early()


if __name__ == "__main__":
    main()
