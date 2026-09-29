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


HORIZONS = [
    60,
    180,
    300,
]


COMPONENTS = [
    "price",
    "volr",
    "spread",
    "trigger",
    "hot",
    "vwap",
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


def correlation(
    pairs,
):
    pairs = [
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


    pairs = [
        (
            x,
            y,
        )
        for (
            x,
            y,
        )
        in pairs
        if (
            x is not None
            and
            y is not None
        )
    ]


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


    x_variance = sum(
        (
            x
            -
            x_mean
        )
        ** 2
        for x
        in xs
    )


    y_variance = sum(
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
        x_variance
        *
        y_variance
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

                s.early_score,
                s.early_components_json

            FROM outcomes o

            JOIN snapshots s
              ON s.id = o.snapshot_id

            WHERE
                o.horizon_seconds = ?
                AND s.early_components_json IS NOT NULL

            ORDER BY
                o.symbol ASC,
                o.action ASC,
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
                        row[
                            "snapshot_id"
                        ],

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

                    "early_score":
                        safe_float(
                            row[
                                "early_score"
                            ]
                        ),

                    "components":
                        components,
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


    for row in rows:
        key = (
            row[
                "symbol"
            ],
            row[
                "action"
            ],
        )


        ts = row[
            "snapshot_ts"
        ]


        if ts is None:
            continue


        previous = (
            last_ts.get(
                key
            )
        )


        if (
            previous is not None
            and
            ts
            -
            previous
            <
            EVENT_SPACING_SECONDS
        ):
            continue


        last_ts[
            key
        ] = ts


        selected.append(
            row
        )


    return selected


# ============================================================
# COMPONENT DATA
# ============================================================

def component_pairs(
    rows,
    component,
    action=None,
):
    pairs = []


    for row in rows:
        if (
            action is not None
            and
            row[
                "action"
            ]
            !=
            action
        ):
            continue


        value = safe_float(
            row[
                "components"
            ].get(
                component
            )
        )


        ret = safe_float(
            row[
                "return_pct"
            ]
        )


        if (
            value is None
            or
            ret is None
        ):
            continue


        pairs.append(
            (
                value,
                ret,
            )
        )


    return pairs


# ============================================================
# QUINTILE SPREAD
# ============================================================

def quintile_spread(
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


    low_return = mean(
        [
            ret
            for (
                score,
                ret,
            )
            in low
        ]
    )


    high_return = mean(
        [
            ret
            for (
                score,
                ret,
            )
            in high
        ]
    )


    return {
        "low_mean":
            low_return,

        "high_mean":
            high_return,

        "spread":
            (
                high_return
                -
                low_return
            ),
    }


# ============================================================
# PRINT COMPONENT
# ============================================================

def print_component_result(
    *,
    component,
    rows,
    action=None,
):
    pairs = (
        component_pairs(
            rows,
            component,
            action,
        )
    )


    corr = correlation(
        pairs
    )


    inverted_corr = correlation(
        [
            (
                -value,
                ret,
            )
            for (
                value,
                ret,
            )
            in pairs
        ]
    )


    spread = (
        quintile_spread(
            pairs
        )
    )


    if spread is None:
        spread_text = (
            "Qspread=N/A"
        )

    else:
        spread_text = (
            f"Qlow="
            f"{spread['low_mean']:+.3f}% | "
            f"Qhigh="
            f"{spread['high_mean']:+.3f}% | "
            f"Qspread="
            f"{spread['spread']:+.3f}%"
        )


    label = (
        component
        if action is None
        else
        f"{component}/{action}"
    )


    print(
        f"{label:<18} | "
        f"n={len(pairs):>4} | "
        f"corr={fmt(corr)} | "
        f"inverted={fmt(inverted_corr)} | "
        f"{spread_text}"
    )


# ============================================================
# CONTRIBUTION SIGN DIAGNOSTIC
# ============================================================

def contribution_diagnostic(
    rows,
):
    print()
    print(
        "COMPONENT SIGN DIAGNOSTIC"
    )


    for component in COMPONENTS:
        pairs = (
            component_pairs(
                rows,
                component,
            )
        )


        corr = correlation(
            pairs
        )


        if corr is None:
            recommendation = (
                "INSUFFICIENT"
            )

        elif corr <= -0.10:
            recommendation = (
                "LIKELY_INVERTED"
            )

        elif corr >= 0.10:
            recommendation = (
                "ALIGNED"
            )

        else:
            recommendation = (
                "WEAK"
            )


        print(
            f"{component:<10} | "
            f"corr={fmt(corr)} | "
            f"{recommendation}"
        )


# ============================================================
# COMPONENT COVERAGE
# ============================================================

def print_zero_coverage(
    rows,
):
    print()
    print(
        "COMPONENT COVERAGE"
    )


    for component in COMPONENTS:
        values = []


        for row in rows:
            value = safe_float(
                row[
                    "components"
                ].get(
                    component
                )
            )


            if value is not None:
                values.append(
                    value
                )


        if not values:
            print(
                f"{component:<10} | "
                f"n=0"
            )

            continue


        zero_count = sum(
            1
            for value
            in values
            if abs(
                value
            )
            <
            0.000001
        )


        total_count = len(
            values
        )


        nonzero_count = (
            total_count
            -
            zero_count
        )


        zero_rate = (
            zero_count
            /
            total_count
            *
            100.0
        )


        print(
            f"{component:<10} | "
            f"n={total_count:>4} | "
            f"nonzero={nonzero_count:>4} | "
            f"zero={zero_count:>4} | "
            f"zero_rate={zero_rate:5.1f}%"
        )


# ============================================================
# COMPONENT AVERAGE MAGNITUDE
# ============================================================

def print_component_magnitude(
    rows,
):
    print()
    print(
        "COMPONENT MAGNITUDE"
    )


    for component in COMPONENTS:
        values = []


        for row in rows:
            value = safe_float(
                row[
                    "components"
                ].get(
                    component
                )
            )


            if value is None:
                continue


            values.append(
                abs(
                    value
                )
            )


        if not values:
            print(
                f"{component:<10} | "
                f"avg_abs=N/A | "
                f"max_abs=N/A"
            )

            continue


        avg_abs = mean(
            values
        )


        max_abs = max(
            values
        )


        print(
            f"{component:<10} | "
            f"avg_abs={avg_abs:7.3f} | "
            f"max_abs={max_abs:7.3f}"
        )


# ============================================================
# ACTION-SPECIFIC SIGN DIAGNOSTIC
# ============================================================

def print_action_diagnostic(
    rows,
    action,
):
    print()
    print(
        f"{action} SIGN DIAGNOSTIC"
    )


    for component in COMPONENTS:
        pairs = (
            component_pairs(
                rows,
                component,
                action,
            )
        )


        corr = correlation(
            pairs
        )


        if corr is None:
            state = (
                "INSUFFICIENT"
            )

        elif corr <= -0.10:
            state = (
                "LIKELY_INVERTED"
            )

        elif corr >= 0.10:
            state = (
                "ALIGNED"
            )

        else:
            state = (
                "WEAK"
            )


        print(
            f"{component:<10} | "
            f"n={len(pairs):>4} | "
            f"corr={fmt(corr)} | "
            f"{state}"
        )


# ============================================================
# ANALYZE HORIZON
# ============================================================

def analyze_horizon(
    horizon,
):
    raw_rows = (
        load_rows(
            horizon
        )
    )


    rows = (
        deduplicate(
            raw_rows
        )
    )


    print()
    print(
        "=============================================================="
    )

    print(
        f"EARLY COMPONENT RESEARCH | "
        f"{horizon}s"
    )

    print(
        "=============================================================="
    )


    print(
        f"raw_samples="
        f"{len(raw_rows)} | "
        f"independent_events="
        f"{len(rows)} | "
        f"spacing="
        f"{EVENT_SPACING_SECONDS}s"
    )


    print()
    print(
        "ALL ACTIONS"
    )


    for component in COMPONENTS:
        print_component_result(
            component=
                component,

            rows=
                rows,
        )


    for action in (
        "BUY",
        "SELL",
    ):
        print()
        print(
            action
        )


        for component in COMPONENTS:
            print_component_result(
                component=
                    component,

                rows=
                    rows,

                action=
                    action,
            )


    contribution_diagnostic(
        rows
    )


    print_action_diagnostic(
        rows,
        "BUY",
    )


    print_action_diagnostic(
        rows,
        "SELL",
    )


    print_zero_coverage(
        rows
    )


    print_component_magnitude(
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
        "TRADINGMAX EARLY COMPONENT RESEARCH"
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
