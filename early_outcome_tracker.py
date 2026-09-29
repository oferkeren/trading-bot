import math
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

OUTCOME_HORIZONS_SECONDS = [
    60,
    180,
    300,
]


OUTCOME_MIN_RATIO = 0.90

OUTCOME_MAX_RATIO = 1.25


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
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                snapshot_id INTEGER NOT NULL,

                symbol TEXT NOT NULL,
                action TEXT NOT NULL,

                snapshot_ts REAL NOT NULL,
                evaluation_ts REAL NOT NULL,

                horizon_seconds INTEGER NOT NULL,
                actual_age_seconds REAL NOT NULL,

                entry_price REAL NOT NULL,
                outcome_price REAL NOT NULL,

                raw_return_pct REAL NOT NULL,
                directional_return_pct REAL NOT NULL,

                early_score REAL,
                early_state TEXT,

                micro_score REAL,
                micro_state TEXT,

                combined_score REAL,

                qualified INTEGER NOT NULL DEFAULT 0,

                created_at REAL NOT NULL,

                UNIQUE (
                    snapshot_id,
                    horizon_seconds
                )
            )
            """
        )


        columns = {
            "micro_score":
                "REAL",

            "micro_state":
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
                "outcomes",
                column,
                definition,
            )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_outcomes_snapshot_horizon
            ON outcomes (
                snapshot_id,
                horizon_seconds
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_outcomes_early_score
            ON outcomes (
                early_score,
                horizon_seconds
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_outcomes_micro_score
            ON outcomes (
                micro_score,
                horizon_seconds
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_outcomes_combined_score
            ON outcomes (
                combined_score,
                horizon_seconds
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


def classify_score(
    score,
):
    score = safe_float(
        score
    )


    if score is None:
        return None


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
# OUTCOME LOOKUP
# ============================================================

def outcome_exists(
    conn,
    snapshot_id,
    horizon_seconds,
):
    row = conn.execute(
        """
        SELECT 1
        FROM outcomes

        WHERE
            snapshot_id = ?
            AND horizon_seconds = ?

        LIMIT 1
        """,
        (
            snapshot_id,
            horizon_seconds,
        ),
    ).fetchone()


    return (
        row is not None
    )


def find_future_snapshot(
    conn,
    *,
    symbol,
    action,
    snapshot_ts,
    horizon_seconds,
):
    target_ts = (
        snapshot_ts
        +
        horizon_seconds
    )


    minimum_ts = (
        snapshot_ts
        +
        (
            horizon_seconds
            *
            OUTCOME_MIN_RATIO
        )
    )


    maximum_ts = (
        snapshot_ts
        +
        (
            horizon_seconds
            *
            OUTCOME_MAX_RATIO
        )
    )


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
            AND price IS NOT NULL

        ORDER BY
            distance ASC,
            ts ASC

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


# ============================================================
# RECORD
# ============================================================

def record_outcome(
    conn,
    *,
    snapshot,
    future,
    horizon_seconds,
):
    entry_price = safe_float(
        snapshot.get(
            "price"
        )
    )


    outcome_price = safe_float(
        future.get(
            "price"
        )
    )


    if (
        entry_price is None
        or
        outcome_price is None
        or
        entry_price <= 0
    ):
        return False


    raw_return_pct = (
        (
            outcome_price
            -
            entry_price
        )
        /
        entry_price
        *
        100.0
    )


    directional_return_pct = (
        raw_return_pct
        *
        direction_factor(
            snapshot.get(
                "action"
            )
        )
    )


    actual_age_seconds = (
        float(
            future[
                "ts"
            ]
        )
        -
        float(
            snapshot[
                "ts"
            ]
        )
    )


    early_score = safe_float(
        snapshot.get(
            "early_score"
        )
    )


    micro_score = safe_float(
        snapshot.get(
            "micro_score"
        )
    )


    combined_score = safe_float(
        snapshot.get(
            "combined_score"
        )
    )


    early_state = (
        snapshot.get(
            "early_state"
        )
    )


    micro_state = (
        snapshot.get(
            "micro_state"
        )
    )


    conn.execute(
        """
        INSERT OR IGNORE INTO outcomes (
            snapshot_id,

            symbol,
            action,

            snapshot_ts,
            evaluation_ts,

            horizon_seconds,
            actual_age_seconds,

            entry_price,
            outcome_price,

            raw_return_pct,
            directional_return_pct,

            early_score,
            early_state,

            micro_score,
            micro_state,

            combined_score,

            qualified,

            created_at
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
            ?
        )
        """,
        (
            int(
                snapshot[
                    "id"
                ]
            ),

            snapshot[
                "symbol"
            ],

            snapshot[
                "action"
            ],

            float(
                snapshot[
                    "ts"
                ]
            ),

            float(
                future[
                    "ts"
                ]
            ),

            int(
                horizon_seconds
            ),

            actual_age_seconds,

            entry_price,
            outcome_price,

            raw_return_pct,
            directional_return_pct,

            early_score,
            early_state,

            micro_score,
            micro_state,

            combined_score,

            int(
                snapshot.get(
                    "qualified",
                    0,
                )
                or
                0
            ),

            time.time(),
        ),
    )


    print(
        "EARLY OUTCOME | "
        f"{snapshot['symbol']} | "
        f"action="
        f"{snapshot['action']} | "
        f"horizon="
        f"{horizon_seconds}s | "
        f"age="
        f"{actual_age_seconds:.1f}s | "
        f"early="
        f"{early_score} | "
        f"micro="
        f"{micro_score} | "
        f"combined="
        f"{combined_score} | "
        f"return="
        f"{directional_return_pct:+.3f}%",
        flush=True,
    )


    return True


# ============================================================
# PROCESS
# ============================================================

def process_pending(
    limit=5000,
):
    init_db()


    now = time.time()


    oldest_required_ts = (
        now
        -
        3600
    )


    conn = db_connect()


    created = 0


    try:
        rows = conn.execute(
            """
            SELECT *
            FROM snapshots

            WHERE
                price IS NOT NULL
                AND ts >= ?

            ORDER BY ts ASC

            LIMIT ?
            """,
            (
                oldest_required_ts,
                int(
                    limit
                ),
            ),
        ).fetchall()


        snapshots = [
            dict(
                row
            )
            for row
            in rows
        ]


        for snapshot in snapshots:
            age_now = (
                now
                -
                float(
                    snapshot[
                        "ts"
                    ]
                )
            )


            for horizon in (
                OUTCOME_HORIZONS_SECONDS
            ):
                if age_now < horizon:
                    continue


                if outcome_exists(
                    conn,
                    snapshot[
                        "id"
                    ],
                    horizon,
                ):
                    continue


                future = (
                    find_future_snapshot(
                        conn,
                        symbol=
                            snapshot[
                                "symbol"
                            ],
                        action=
                            snapshot[
                                "action"
                            ],
                        snapshot_ts=
                            float(
                                snapshot[
                                    "ts"
                                ]
                            ),
                        horizon_seconds=
                            horizon,
                    )
                )


                if future is None:
                    continue


                if record_outcome(
                    conn,
                    snapshot=
                        snapshot,
                    future=
                        future,
                    horizon_seconds=
                        horizon,
                ):
                    created += 1


        conn.commit()

    finally:
        conn.close()


    print(
        "EARLY OUTCOME PROCESS | "
        f"created={created}",
        flush=True,
    )


    return created


# ============================================================
# SUMMARY QUERIES
# ============================================================

def build_signal_summary(
    score_column,
):
    conn = db_connect()

    try:
        rows = conn.execute(
            f"""
            SELECT
                horizon_seconds,

                CASE
                    WHEN {score_column} >= 35
                        THEN 'ACCELERATING'

                    WHEN {score_column} >= 15
                        THEN 'BUILDING'

                    WHEN {score_column} <= -35
                        THEN 'REVERSING'

                    WHEN {score_column} <= -15
                        THEN 'WEAKENING'

                    ELSE 'NEUTRAL'
                END
                    AS score_bucket,

                COUNT(*)
                    AS samples,

                AVG(
                    directional_return_pct
                )
                    AS avg_return_pct,

                AVG(
                    CASE
                        WHEN directional_return_pct > 0
                            THEN 1.0
                        ELSE 0.0
                    END
                )
                    AS positive_rate,

                AVG(
                    CASE
                        WHEN directional_return_pct >= 0.50
                            THEN 1.0
                        ELSE 0.0
                    END
                )
                    AS move_05_rate,

                AVG(
                    CASE
                        WHEN directional_return_pct >= 1.00
                            THEN 1.0
                        ELSE 0.0
                    END
                )
                    AS move_10_rate,

                AVG(
                    CASE
                        WHEN directional_return_pct <= -0.50
                            THEN 1.0
                        ELSE 0.0
                    END
                )
                    AS adverse_05_rate

            FROM outcomes

            WHERE
                {score_column}
                IS NOT NULL

            GROUP BY
                horizon_seconds,
                score_bucket

            ORDER BY
                horizon_seconds ASC,

                CASE score_bucket
                    WHEN 'ACCELERATING'
                        THEN 1

                    WHEN 'BUILDING'
                        THEN 2

                    WHEN 'NEUTRAL'
                        THEN 3

                    WHEN 'WEAKENING'
                        THEN 4

                    WHEN 'REVERSING'
                        THEN 5

                    ELSE 6
                END
            """
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
# CORRELATION
# ============================================================

def calculate_correlation(
    score_column,
    horizon,
):
    conn = db_connect()

    try:
        rows = conn.execute(
            f"""
            SELECT
                {score_column} AS score,
                directional_return_pct AS ret

            FROM outcomes

            WHERE
                horizon_seconds = ?
                AND {score_column} IS NOT NULL
            """,
            (
                horizon,
            ),
        ).fetchall()


    finally:
        conn.close()


    values = [
        (
            safe_float(
                row[
                    "score"
                ]
            ),
            safe_float(
                row[
                    "ret"
                ]
            ),
        )
        for row
        in rows
    ]


    values = [
        (
            score,
            ret,
        )
        for (
            score,
            ret,
        )
        in values
        if (
            score is not None
            and
            ret is not None
        )
    ]


    if len(
        values
    ) < 3:
        return None


    scores = [
        item[
            0
        ]
        for item
        in values
    ]


    returns = [
        item[
            1
        ]
        for item
        in values
    ]


    mean_score = (
        sum(
            scores
        )
        /
        len(
            scores
        )
    )


    mean_return = (
        sum(
            returns
        )
        /
        len(
            returns
        )
    )


    covariance = sum(
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
        in values
    )


    score_variance = sum(
        (
            score
            -
            mean_score
        )
        ** 2
        for score
        in scores
    )


    return_variance = sum(
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
        score_variance
        *
        return_variance
    )


    if denominator <= 0:
        return None


    return (
        covariance
        /
        denominator
    )


# ============================================================
# PRINT
# ============================================================

def print_signal_summary(
    title,
    score_column,
):
    rows = (
        build_signal_summary(
            score_column
        )
    )


    print()
    print(
        "=============================================================="
    )

    print(
        title
    )

    print(
        "=============================================================="
    )


    if not rows:
        print(
            "No outcomes yet."
        )

        return


    current_horizon = None


    for row in rows:
        horizon = int(
            row[
                "horizon_seconds"
            ]
        )


        if horizon != current_horizon:
            current_horizon = horizon


            correlation = (
                calculate_correlation(
                    score_column,
                    horizon,
                )
            )


            correlation_text = (
                "N/A"
                if correlation is None
                else
                f"{correlation:+.3f}"
            )


            print()
            print(
                f"{horizon}s | "
                f"correlation="
                f"{correlation_text}"
            )


        positive = (
            float(
                row[
                    "positive_rate"
                ]
                or
                0.0
            )
            *
            100.0
        )


        move05 = (
            float(
                row[
                    "move_05_rate"
                ]
                or
                0.0
            )
            *
            100.0
        )


        move10 = (
            float(
                row[
                    "move_10_rate"
                ]
                or
                0.0
            )
            *
            100.0
        )


        adverse05 = (
            float(
                row[
                    "adverse_05_rate"
                ]
                or
                0.0
            )
            *
            100.0
        )


        avg_return = float(
            row[
                "avg_return_pct"
            ]
            or
            0.0
        )


        print(
            f"  "
            f"{row['score_bucket']:<12} | "
            f"n="
            f"{int(row['samples']):>4} | "
            f"avg="
            f"{avg_return:+7.3f}% | "
            f"positive="
            f"{positive:5.1f}% | "
            f">=0.5%="
            f"{move05:5.1f}% | "
            f">=1.0%="
            f"{move10:5.1f}% | "
            f"<=-0.5%="
            f"{adverse05:5.1f}%",
            flush=True,
        )


def print_summary():
    print_signal_summary(
        "EARLY SCORE FORWARD OUTCOMES",
        "early_score",
    )


    print_signal_summary(
        "MICRO SCORE FORWARD OUTCOMES",
        "micro_score",
    )


    print_signal_summary(
        "COMBINED SCORE FORWARD OUTCOMES",
        "combined_score",
    )


# ============================================================
# DATA COVERAGE
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

            FROM snapshots
            """
        ).fetchone()


    finally:
        conn.close()


    total = int(
        row[
            "total"
        ]
        or
        0
    )


    early_count = int(
        row[
            "early_count"
        ]
        or
        0
    )


    micro_count = int(
        row[
            "micro_count"
        ]
        or
        0
    )


    combined_count = int(
        row[
            "combined_count"
        ]
        or
        0
    )


    print()
    print(
        "=============================================================="
    )

    print(
        "FUSION DATA COVERAGE"
    )

    print(
        "=============================================================="
    )


    print(
        f"Snapshots : {total}"
    )


    print(
        f"Early     : {early_count}"
    )


    print(
        f"Micro     : {micro_count}"
    )


    print(
        f"Combined  : {combined_count}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    init_db()


    process_pending(
        limit=
            5000
    )


    print_coverage()


    print_summary()


if __name__ == "__main__":
    main()
