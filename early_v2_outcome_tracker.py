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

TRACKER_VERSION = (
    "v2-forward-1"
)


HORIZONS = [
    60,
    180,
    300,
]


MIN_RATIO = 0.90

MAX_RATIO = 1.25


LOOKBACK_LIMIT_SECONDS = 3600


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


def init_db():
    conn = db_connect()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS early_v2_forward_meta (
                id INTEGER PRIMARY KEY CHECK(id = 1),

                forward_start_ts REAL NOT NULL,

                tracker_version TEXT NOT NULL,

                created_at REAL NOT NULL
            )
            """
        )


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS early_v2_outcomes (
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

                directional_return_pct REAL NOT NULL,

                v1_score REAL,
                v2_score REAL NOT NULL,
                v2_state TEXT NOT NULL,

                formula TEXT NOT NULL,
                version TEXT NOT NULL,

                created_at REAL NOT NULL,

                UNIQUE (
                    snapshot_id,
                    horizon_seconds
                )
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_v2_outcomes_horizon_score
            ON early_v2_outcomes (
                horizon_seconds,
                v2_score
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_v2_outcomes_symbol_ts
            ON early_v2_outcomes (
                symbol,
                snapshot_ts
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


# ============================================================
# FORWARD START
# ============================================================

def get_forward_start_ts():
    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT
                forward_start_ts,
                tracker_version

            FROM early_v2_forward_meta

            WHERE id = 1
            """
        ).fetchone()


        if row is not None:
            return float(
                row[
                    "forward_start_ts"
                ]
            )


        start_ts = time.time()


        conn.execute(
            """
            INSERT INTO early_v2_forward_meta (
                id,
                forward_start_ts,
                tracker_version,
                created_at
            )

            VALUES (
                1,
                ?,
                ?,
                ?
            )
            """,
            (
                start_ts,
                TRACKER_VERSION,
                time.time(),
            ),
        )


        conn.commit()


        print(
            "EARLY V2 FORWARD START CREATED | "
            f"ts={start_ts:.3f} | "
            f"version={TRACKER_VERSION}",
            flush=True,
        )


        return start_ts

    finally:
        conn.close()


# ============================================================
# LOOKUPS
# ============================================================

def outcome_exists(
    conn,
    snapshot_id,
    horizon,
):
    row = conn.execute(
        """
        SELECT 1
        FROM early_v2_outcomes

        WHERE
            snapshot_id = ?
            AND horizon_seconds = ?

        LIMIT 1
        """,
        (
            snapshot_id,
            horizon,
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
    horizon,
):
    target_ts = (
        snapshot_ts
        +
        horizon
    )


    minimum_ts = (
        snapshot_ts
        +
        (
            horizon
            *
            MIN_RATIO
        )
    )


    maximum_ts = (
        snapshot_ts
        +
        (
            horizon
            *
            MAX_RATIO
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
    shadow,
    snapshot,
    future,
    horizon,
):
    entry_price = safe_float(
        snapshot[
            "price"
        ]
    )


    outcome_price = safe_float(
        future[
            "price"
        ]
    )


    if (
        entry_price is None
        or
        outcome_price is None
        or
        entry_price <= 0
    ):
        return False


    raw_return = (
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


    directional_return = (
        raw_return
        *
        direction_factor(
            snapshot[
                "action"
            ]
        )
    )


    actual_age = (
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


    conn.execute(
        """
        INSERT OR IGNORE INTO early_v2_outcomes (
            snapshot_id,

            symbol,
            action,

            snapshot_ts,
            evaluation_ts,

            horizon_seconds,
            actual_age_seconds,

            entry_price,
            outcome_price,

            directional_return_pct,

            v1_score,
            v2_score,
            v2_state,

            formula,
            version,

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
                horizon
            ),

            actual_age,

            entry_price,
            outcome_price,

            directional_return,

            safe_float(
                shadow[
                    "v1_score"
                ]
            ),

            float(
                shadow[
                    "v2_score"
                ]
            ),

            shadow[
                "v2_state"
            ],

            shadow[
                "formula"
            ],

            shadow[
                "version"
            ],

            time.time(),
        ),
    )


    print(
        "EARLY V2 FORWARD OUTCOME | "
        f"{snapshot['symbol']} | "
        f"action={snapshot['action']} | "
        f"horizon={horizon}s | "
        f"age={actual_age:.1f}s | "
        f"v1="
        f"{safe_float(shadow['v1_score'])} | "
        f"v2="
        f"{float(shadow['v2_score']):+.2f} | "
        f"state="
        f"{shadow['v2_state']} | "
        f"return="
        f"{directional_return:+.3f}%",
        flush=True,
    )


    return True


# ============================================================
# PROCESS
# ============================================================

def process_pending():
    init_db()


    forward_start_ts = (
        get_forward_start_ts()
    )


    now = time.time()


    cutoff = max(
        forward_start_ts,
        now
        -
        LOOKBACK_LIMIT_SECONDS,
    )


    conn = db_connect()


    created = 0


    try:
        rows = conn.execute(
            """
            SELECT
                s.*,

                v.v1_score AS shadow_v1_score,
                v.v2_score AS shadow_v2_score,
                v.v2_state AS shadow_v2_state,
                v.formula AS shadow_formula,
                v.version AS shadow_version

            FROM snapshots s

            JOIN early_v2_shadow v
              ON v.snapshot_id = s.id

            WHERE
                s.ts >= ?
                AND s.price IS NOT NULL

            ORDER BY s.ts ASC
            """,
            (
                cutoff,
            ),
        ).fetchall()


        for raw in rows:
            snapshot = dict(
                raw
            )


            snapshot_ts = float(
                snapshot[
                    "ts"
                ]
            )


            if (
                snapshot_ts
                <
                forward_start_ts
            ):
                continue


            shadow = {
                "v1_score":
                    snapshot[
                        "shadow_v1_score"
                    ],

                "v2_score":
                    snapshot[
                        "shadow_v2_score"
                    ],

                "v2_state":
                    snapshot[
                        "shadow_v2_state"
                    ],

                "formula":
                    snapshot[
                        "shadow_formula"
                    ],

                "version":
                    snapshot[
                        "shadow_version"
                    ],
            }


            age_now = (
                now
                -
                snapshot_ts
            )


            for horizon in HORIZONS:
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
                            snapshot_ts,
                        horizon=
                            horizon,
                    )
                )


                if future is None:
                    continue


                if record_outcome(
                    conn,
                    shadow=
                        shadow,
                    snapshot=
                        snapshot,
                    future=
                        future,
                    horizon=
                        horizon,
                ):
                    created += 1


        conn.commit()

    finally:
        conn.close()


    print(
        "EARLY V2 FORWARD PROCESS | "
        f"created={created} | "
        f"forward_start="
        f"{forward_start_ts:.3f}",
        flush=True,
    )


    return created


# ============================================================
# CORRELATION
# ============================================================

def correlation(
    pairs,
):
    if len(
        pairs
    ) < 3:
        return None


    xs = [
        pair[
            0
        ]
        for pair
        in pairs
    ]


    ys = [
        pair[
            1
        ]
        for pair
        in pairs
    ]


    mean_x = (
        sum(
            xs
        )
        /
        len(
            xs
        )
    )


    mean_y = (
        sum(
            ys
        )
        /
        len(
            ys
        )
    )


    numerator = sum(
        (
            x
            -
            mean_x
        )
        *
        (
            y
            -
            mean_y
        )
        for (
            x,
            y,
        )
        in pairs
    )


    var_x = sum(
        (
            x
            -
            mean_x
        )
        ** 2
        for x
        in xs
    )


    var_y = sum(
        (
            y
            -
            mean_y
        )
        ** 2
        for y
        in ys
    )


    denominator = math.sqrt(
        var_x
        *
        var_y
    )


    if denominator <= 0:
        return None


    return (
        numerator
        /
        denominator
    )


# ============================================================
# SUMMARY
# ============================================================

def get_correlation(
    horizon,
    action,
):
    conn = db_connect()

    try:
        rows = conn.execute(
            """
            SELECT
                v2_score,
                directional_return_pct

            FROM early_v2_outcomes

            WHERE
                horizon_seconds = ?
                AND action = ?
            """,
            (
                horizon,
                action,
            ),
        ).fetchall()

    finally:
        conn.close()


    pairs = []


    for row in rows:
        score = safe_float(
            row[
                "v2_score"
            ]
        )


        ret = safe_float(
            row[
                "directional_return_pct"
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


    return correlation(
        pairs
    )


def print_summary():
    conn = db_connect()

    try:
        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM early_v2_outcomes
            """
        ).fetchone()[
            0
        ]


        print()
        print(
            "=============================================================="
        )

        print(
            "EARLY V2 TRUE FORWARD SUMMARY"
        )

        print(
            "=============================================================="
        )


        print(
            f"Forward outcomes: {total}"
        )


        if total == 0:
            print(
                "Waiting for new forward outcomes."
            )

            return


        rows = conn.execute(
            """
            SELECT
                horizon_seconds,
                action,
                v2_state,

                COUNT(*)
                    AS samples,

                AVG(
                    directional_return_pct
                )
                    AS avg_return,

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
                    AS move05_rate,

                AVG(
                    CASE
                        WHEN directional_return_pct <= -0.50
                            THEN 1.0
                        ELSE 0.0
                    END
                )
                    AS adverse05_rate

            FROM early_v2_outcomes

            GROUP BY
                horizon_seconds,
                action,
                v2_state

            ORDER BY
                horizon_seconds,
                action,
                v2_state
            """
        ).fetchall()


    finally:
        conn.close()


    previous_key = None


    for row in rows:
        key = (
            int(
                row[
                    "horizon_seconds"
                ]
            ),
            row[
                "action"
            ],
        )


        if key != previous_key:
            previous_key = key


            corr = (
                get_correlation(
                    key[
                        0
                    ],
                    key[
                        1
                    ],
                )
            )


            corr_text = (
                "N/A"
                if corr is None
                else
                f"{corr:+.3f}"
            )


            print()
            print(
                f"{key[0]}s | "
                f"{key[1]} | "
                f"corr={corr_text}"
            )


        positive = (
            float(
                row[
                    "positive_rate"
                ]
                or
                0
            )
            *
            100.0
        )


        move05 = (
            float(
                row[
                    "move05_rate"
                ]
                or
                0
            )
            *
            100.0
        )


        adverse05 = (
            float(
                row[
                    "adverse05_rate"
                ]
                or
                0
            )
            *
            100.0
        )


        print(
            f"  "
            f"{row['v2_state']:<12} | "
            f"n="
            f"{int(row['samples']):>4} | "
            f"avg="
            f"{float(row['avg_return'] or 0):+7.3f}% | "
            f"positive="
            f"{positive:5.1f}% | "
            f">=0.5%="
            f"{move05:5.1f}% | "
            f"<=-0.5%="
            f"{adverse05:5.1f}%"
        )


# ============================================================
# MAIN
# ============================================================

def main():
    init_db()

    process_pending()

    print_summary()


if __name__ == "__main__":
    main()
