import math
import random
import sqlite3
import statistics

from collections import Counter, defaultdict
from datetime import datetime, timezone
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

ACTION = "BUY"


HORIZONS = [
    180,
    300,
]


EVENT_SPACING_SECONDS = 300


BOOTSTRAP_SAMPLES = 5000


RANDOM_SEED = 20260929


WINSOR_LOW = 0.05

WINSOR_HIGH = 0.95


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


def correlation(
    pairs,
):
    usable = []


    for (
        x,
        y,
    ) in pairs:
        x = safe_float(
            x
        )


        y = safe_float(
            y
        )


        if (
            x is None
            or
            y is None
        ):
            continue


        usable.append(
            (
                x,
                y,
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
        f"{value:+.4f}"
    )


def utc_day(
    timestamp,
):
    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    ).strftime(
        "%Y-%m-%d"
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
                snapshot_id,
                symbol,
                action,
                snapshot_ts,
                directional_return_pct,
                v1_score,
                v2_score

            FROM early_v2_outcomes

            WHERE
                horizon_seconds = ?
                AND action = ?

            ORDER BY
                snapshot_ts ASC
            """,
            (
                horizon,
                ACTION,
            ),
        ).fetchall()


        result = []


        for row in rows:
            ret = safe_float(
                row[
                    "directional_return_pct"
                ]
            )


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


            if (
                ret is None
                or
                v2 is None
            ):
                continue


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

                    "day":
                        utc_day(
                            float(
                                row[
                                    "snapshot_ts"
                                ]
                            )
                        ),

                    "return_pct":
                        ret,

                    "v1":
                        v1,

                    "v2":
                        v2,
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
# QUINTILE SPREAD
# ============================================================

def quintile_spread(
    rows,
):
    if len(
        rows
    ) < 10:
        return None


    ordered = sorted(
        rows,
        key=lambda row: row[
            "v2"
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
        row[
            "return_pct"
        ]
        for row
        in low
    ]


    high_returns = [
        row[
            "return_pct"
        ]
        for row
        in high
    ]


    return {
        "bucket_size":
            bucket_size,

        "q1_mean":
            mean(
                low_returns
            ),

        "q5_mean":
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

        "q1_median":
            median(
                low_returns
            ),

        "q5_median":
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


# ============================================================
# POSITIVE / NEGATIVE V2
# ============================================================

def sign_analysis(
    rows,
):
    positive = [
        row
        for row
        in rows
        if row[
            "v2"
        ]
        >
        0
    ]


    negative = [
        row
        for row
        in rows
        if row[
            "v2"
        ]
        <
        0
    ]


    zeros = [
        row
        for row
        in rows
        if row[
            "v2"
        ]
        ==
        0
    ]


    def describe(
        group,
    ):
        returns = [
            row[
                "return_pct"
            ]
            for row
            in group
        ]


        if not returns:
            return {
                "n": 0,
                "avg": None,
                "median": None,
                "positive_rate": None,
            }


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

            "positive_rate":
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
        }


    pos = describe(
        positive
    )


    neg = describe(
        negative
    )


    zero = describe(
        zeros
    )


    spread = None


    if (
        pos[
            "avg"
        ]
        is not None
        and
        neg[
            "avg"
        ]
        is not None
    ):
        spread = (
            pos[
                "avg"
            ]
            -
            neg[
                "avg"
            ]
        )


    return {
        "positive":
            pos,

        "negative":
            neg,

        "zero":
            zero,

        "positive_minus_negative":
            spread,
    }


# ============================================================
# BOOTSTRAP
# ============================================================

def bootstrap_metric(
    rows,
    metric_function,
    samples,
    seed,
):
    if len(
        rows
    ) < 3:
        return None


    rng = random.Random(
        seed
    )


    values = []


    n = len(
        rows
    )


    for _ in range(
        samples
    ):
        sample = [
            rows[
                rng.randrange(
                    n
                )
            ]
            for _ in range(
                n
            )
        ]


        value = (
            metric_function(
                sample
            )
        )


        value = safe_float(
            value
        )


        if value is not None:
            values.append(
                value
            )


    if not values:
        return None


    return {
        "n_boot":
            len(
                values
            ),

        "mean":
            mean(
                values
            ),

        "median":
            median(
                values
            ),

        "ci_low":
            percentile(
                values,
                0.025,
            ),

        "ci_high":
            percentile(
                values,
                0.975,
            ),

        "positive_probability":
            (
                sum(
                    1
                    for value
                    in values
                    if value > 0
                )
                /
                len(
                    values
                )
                *
                100.0
            ),
    }


def metric_corr(
    rows,
):
    return correlation(
        [
            (
                row[
                    "v2"
                ],
                row[
                    "return_pct"
                ],
            )
            for row
            in rows
        ]
    )


def metric_quintile_spread(
    rows,
):
    result = (
        quintile_spread(
            rows
        )
    )


    if result is None:
        return None


    return result[
        "mean_spread"
    ]


def metric_sign_spread(
    rows,
):
    result = (
        sign_analysis(
            rows
        )
    )


    return result[
        "positive_minus_negative"
    ]


# ============================================================
# WINSORIZATION
# ============================================================

def winsorize_returns(
    rows,
):
    if not rows:
        return []


    values = [
        row[
            "return_pct"
        ]
        for row
        in rows
    ]


    lower = percentile(
        values,
        WINSOR_LOW,
    )


    upper = percentile(
        values,
        WINSOR_HIGH,
    )


    result = []


    for row in rows:
        copy = dict(
            row
        )


        value = copy[
            "return_pct"
        ]


        copy[
            "return_pct"
        ] = min(
            upper,
            max(
                lower,
                value,
            ),
        )


        result.append(
            copy
        )


    return result


# ============================================================
# DAY ANALYSIS
# ============================================================

def analyze_days(
    rows,
):
    groups = defaultdict(
        list
    )


    for row in rows:
        groups[
            row[
                "day"
            ]
        ].append(
            row
        )


    print()
    print(
        "DAY-BY-DAY"
    )


    for day in sorted(
        groups
    ):
        group = groups[
            day
        ]


        corr = metric_corr(
            group
        )


        spread = (
            quintile_spread(
                group
            )
        )


        sign = (
            sign_analysis(
                group
            )
        )


        qspread = (
            None
            if spread is None
            else
            spread[
                "mean_spread"
            ]
        )


        print(
            f"{day} | "
            f"n={len(group):>3} | "
            f"corr={fmt(corr)} | "
            f"Qspread={fmt(qspread)}% | "
            f"V2pos-neg="
            f"{fmt(sign['positive_minus_negative'])}%"
        )


# ============================================================
# SYMBOL ANALYSIS
# ============================================================

def analyze_symbols(
    rows,
):
    counts = Counter(
        row[
            "symbol"
        ]
        for row
        in rows
    )


    print()
    print(
        "SYMBOL CONCENTRATION"
    )


    print(
        f"Unique symbols : "
        f"{len(counts)}"
    )


    print(
        f"Total events   : "
        f"{len(rows)}"
    )


    if not counts:
        return


    print(
        "Top symbols:"
    )


    for (
        symbol,
        count,
    ) in counts.most_common(
        10
    ):
        pct = (
            count
            /
            len(
                rows
            )
            *
            100.0
        )


        print(
            f"  "
            f"{symbol:<8} | "
            f"n={count:>3} | "
            f"{pct:5.1f}%"
        )


# ============================================================
# LEAVE ONE SYMBOL OUT
# ============================================================

def leave_one_symbol_out(
    rows,
):
    symbols = sorted(
        {
            row[
                "symbol"
            ]
            for row
            in rows
        }
    )


    results = []


    for symbol in symbols:
        subset = [
            row
            for row
            in rows
            if row[
                "symbol"
            ]
            !=
            symbol
        ]


        corr = metric_corr(
            subset
        )


        spread = (
            quintile_spread(
                subset
            )
        )


        qspread = (
            None
            if spread is None
            else
            spread[
                "mean_spread"
            ]
        )


        results.append(
            {
                "removed":
                    symbol,

                "n":
                    len(
                        subset
                    ),

                "corr":
                    corr,

                "qspread":
                    qspread,
            }
        )


    print()
    print(
        "LEAVE-ONE-SYMBOL-OUT"
    )


    valid_corr = [
        row
        for row
        in results
        if row[
            "corr"
        ]
        is not None
    ]


    if valid_corr:
        worst_corr = min(
            valid_corr,
            key=lambda row: row[
                "corr"
            ],
        )


        best_corr = max(
            valid_corr,
            key=lambda row: row[
                "corr"
            ],
        )


        print(
            "Correlation range:"
        )


        print(
            f"  worst after removing "
            f"{worst_corr['removed']:<8} | "
            f"n={worst_corr['n']:>3} | "
            f"corr={fmt(worst_corr['corr'])}"
        )


        print(
            f"  best  after removing "
            f"{best_corr['removed']:<8} | "
            f"n={best_corr['n']:>3} | "
            f"corr={fmt(best_corr['corr'])}"
        )


    valid_q = [
        row
        for row
        in results
        if row[
            "qspread"
        ]
        is not None
    ]


    if valid_q:
        worst_q = min(
            valid_q,
            key=lambda row: row[
                "qspread"
            ],
        )


        best_q = max(
            valid_q,
            key=lambda row: row[
                "qspread"
            ],
        )


        print(
            "Q5-Q1 range:"
        )


        print(
            f"  worst after removing "
            f"{worst_q['removed']:<8} | "
            f"Qspread="
            f"{fmt(worst_q['qspread'])}%"
        )


        print(
            f"  best  after removing "
            f"{best_q['removed']:<8} | "
            f"Qspread="
            f"{fmt(best_q['qspread'])}%"
        )


# ============================================================
# PRINT BOOTSTRAP
# ============================================================

def print_bootstrap(
    label,
    result,
    suffix="",
):
    if result is None:
        print(
            f"{label:<24} | no data"
        )

        return


    print(
        f"{label:<24} | "
        f"boot={result['n_boot']:>4} | "
        f"mean={fmt(result['mean'])}{suffix} | "
        f"median={fmt(result['median'])}{suffix} | "
        f"95% CI=["
        f"{fmt(result['ci_low'])}, "
        f"{fmt(result['ci_high'])}"
        f"]{suffix} | "
        f"P(>0)="
        f"{result['positive_probability']:5.1f}%"
    )


# ============================================================
# MAIN ANALYSIS
# ============================================================

def analyze_horizon(
    horizon,
):
    raw = load_rows(
        horizon
    )


    rows = deduplicate(
        raw
    )


    print()
    print(
        "=============================================================="
    )

    print(
        f"V2 FORWARD ROBUSTNESS | "
        f"BUY | {horizon}s"
    )

    print(
        "=============================================================="
    )


    print(
        f"raw="
        f"{len(raw)} | "
        f"independent="
        f"{len(rows)} | "
        f"spacing="
        f"{EVENT_SPACING_SECONDS}s"
    )


    if not rows:
        print(
            "No data."
        )

        return


    corr = metric_corr(
        rows
    )


    q = quintile_spread(
        rows
    )


    sign = sign_analysis(
        rows
    )


    print()
    print(
        "BASE RESULT"
    )


    print(
        f"V2 correlation       : "
        f"{fmt(corr)}"
    )


    if q is not None:
        print(
            f"Q1 mean              : "
            f"{fmt(q['q1_mean'])}%"
        )


        print(
            f"Q5 mean              : "
            f"{fmt(q['q5_mean'])}%"
        )


        print(
            f"Q5-Q1 mean spread    : "
            f"{fmt(q['mean_spread'])}%"
        )


        print(
            f"Q1 median            : "
            f"{fmt(q['q1_median'])}%"
        )


        print(
            f"Q5 median            : "
            f"{fmt(q['q5_median'])}%"
        )


        print(
            f"Q5-Q1 median spread  : "
            f"{fmt(q['median_spread'])}%"
        )


    print()
    print(
        "V2 SIGN"
    )


    print(
        f"V2 positive | "
        f"n={sign['positive']['n']:>3} | "
        f"avg="
        f"{fmt(sign['positive']['avg'])}% | "
        f"median="
        f"{fmt(sign['positive']['median'])}% | "
        f"positive-rate="
        f"{sign['positive']['positive_rate']}"
    )


    print(
        f"V2 negative | "
        f"n={sign['negative']['n']:>3} | "
        f"avg="
        f"{fmt(sign['negative']['avg'])}% | "
        f"median="
        f"{fmt(sign['negative']['median'])}% | "
        f"positive-rate="
        f"{sign['negative']['positive_rate']}"
    )


    print(
        f"POS minus NEG avg    : "
        f"{fmt(sign['positive_minus_negative'])}%"
    )


    print()
    print(
        "BOOTSTRAP"
    )


    bootstrap_corr = bootstrap_metric(
        rows,
        metric_corr,
        BOOTSTRAP_SAMPLES,
        RANDOM_SEED
        +
        horizon,
    )


    bootstrap_q = bootstrap_metric(
        rows,
        metric_quintile_spread,
        BOOTSTRAP_SAMPLES,
        RANDOM_SEED
        +
        horizon
        +
        1000,
    )


    bootstrap_sign = bootstrap_metric(
        rows,
        metric_sign_spread,
        BOOTSTRAP_SAMPLES,
        RANDOM_SEED
        +
        horizon
        +
        2000,
    )


    print_bootstrap(
        "V2 correlation",
        bootstrap_corr,
    )


    print_bootstrap(
        "Q5-Q1 spread",
        bootstrap_q,
        "%",
    )


    print_bootstrap(
        "V2 POS-NEG avg",
        bootstrap_sign,
        "%",
    )


    winsorized = (
        winsorize_returns(
            rows
        )
    )


    print()
    print(
        "OUTLIER SENSITIVITY"
    )


    print(
        f"Original corr        : "
        f"{fmt(corr)}"
    )


    print(
        f"Winsorized corr      : "
        f"{fmt(metric_corr(winsorized))}"
    )


    original_q = quintile_spread(
        rows
    )


    winsor_q = quintile_spread(
        winsorized
    )


    print(
        f"Original Q spread    : "
        f"{fmt(None if original_q is None else original_q['mean_spread'])}%"
    )


    print(
        f"Winsorized Q spread  : "
        f"{fmt(None if winsor_q is None else winsor_q['mean_spread'])}%"
    )


    analyze_days(
        rows
    )


    analyze_symbols(
        rows
    )


    leave_one_symbol_out(
        rows
    )


# ============================================================
# ENTRY
# ============================================================

def main():
    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX V2 TRUE FORWARD ROBUSTNESS"
    )

    print(
        "READ ONLY"
    )

    print(
        f"ACTION={ACTION}"
    )

    print(
        f"BOOTSTRAP={BOOTSTRAP_SAMPLES}"
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
