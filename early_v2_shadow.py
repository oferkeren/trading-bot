import json
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

V2_VERSION = (
    "v2-shadow-1"
)


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
            CREATE TABLE IF NOT EXISTS early_v2_shadow (
                snapshot_id INTEGER PRIMARY KEY,

                ts REAL NOT NULL,

                symbol TEXT NOT NULL,
                action TEXT NOT NULL,

                v1_score REAL,

                price_component REAL,
                spread_component REAL,

                v2_score REAL NOT NULL,
                v2_state TEXT NOT NULL,

                formula TEXT NOT NULL,
                version TEXT NOT NULL,

                created_at REAL NOT NULL
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_early_v2_symbol_ts
            ON early_v2_shadow (
                symbol,
                ts
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_early_v2_score_ts
            ON early_v2_shadow (
                v2_score,
                ts
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


def classify(
    score,
):
    if score >= 15:
        return "FAVORABLE"


    if score <= -15:
        return "UNFAVORABLE"


    return "NEUTRAL"


# ============================================================
# FORMULA
# ============================================================

def calculate_v2(
    *,
    action,
    price_component,
    spread_component,
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


    price_component = (
        safe_float(
            price_component
        )
        or
        0.0
    )


    spread_component = (
        safe_float(
            spread_component
        )
        or
        0.0
    )


    if action == "BUY":
        #
        # Holdout result:
        #
        # -price was the most robust BUY formulation
        # across 180s and 300s.
        #
        score = (
            -
            price_component
        )


        formula = (
            "-price"
        )


    elif action == "SELL":
        #
        # SELL benefited from adding inverted spread,
        # especially on the longer horizon.
        #
        score = (
            (
                -
                price_component
            )
            -
            (
                0.50
                *
                spread_component
            )
        )


        formula = (
            "-price-0.5*spread"
        )


    else:
        raise ValueError(
            f"Unsupported action: {action}"
        )


    score = clamp(
        score
    )


    return {
        "v2_score":
            round(
                score,
                2,
            ),

        "v2_state":
            classify(
                score
            ),

        "formula":
            formula,
    }


# ============================================================
# PROCESS ONE SNAPSHOT
# ============================================================

def process_snapshot(
    conn,
    snapshot,
):
    raw_components = (
        snapshot[
            "early_components_json"
        ]
    )


    if not raw_components:
        return False


    try:
        components = json.loads(
            raw_components
        )

    except Exception:
        return False


    price_component = safe_float(
        components.get(
            "price"
        )
    )


    spread_component = safe_float(
        components.get(
            "spread"
        )
    )


    if price_component is None:
        price_component = 0.0


    if spread_component is None:
        spread_component = 0.0


    result = (
        calculate_v2(
            action=
                snapshot[
                    "action"
                ],

            price_component=
                price_component,

            spread_component=
                spread_component,
        )
    )


    conn.execute(
        """
        INSERT OR REPLACE INTO early_v2_shadow (
            snapshot_id,

            ts,

            symbol,
            action,

            v1_score,

            price_component,
            spread_component,

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
            ?
        )
        """,
        (
            int(
                snapshot[
                    "id"
                ]
            ),

            float(
                snapshot[
                    "ts"
                ]
            ),

            str(
                snapshot[
                    "symbol"
                ]
            )
            .strip()
            .upper(),

            str(
                snapshot[
                    "action"
                ]
            )
            .strip()
            .upper(),

            safe_float(
                snapshot[
                    "early_score"
                ]
            ),

            price_component,
            spread_component,

            result[
                "v2_score"
            ],

            result[
                "v2_state"
            ],

            result[
                "formula"
            ],

            V2_VERSION,

            time.time(),
        ),
    )


    return True


# ============================================================
# PROCESS PENDING
# ============================================================

def process_pending():
    init_db()


    conn = db_connect()


    processed = 0


    try:
        rows = conn.execute(
            """
            SELECT s.*

            FROM snapshots s

            LEFT JOIN early_v2_shadow v2
              ON v2.snapshot_id = s.id

            WHERE
                v2.snapshot_id IS NULL
                AND s.early_components_json IS NOT NULL

            ORDER BY s.ts ASC
            """
        ).fetchall()


        for row in rows:
            snapshot = dict(
                row
            )


            if process_snapshot(
                conn,
                snapshot,
            ):
                processed += 1


        conn.commit()

    finally:
        conn.close()


    print(
        "EARLY V2 SHADOW PROCESS | "
        f"processed={processed}",
        flush=True,
    )


    return processed


# ============================================================
# CURRENT SCORES
# ============================================================

def print_current_scores():
    conn = db_connect()

    try:
        rows = conn.execute(
            """
            SELECT
                symbol,
                action,
                v1_score,
                price_component,
                spread_component,
                v2_score,
                v2_state,
                formula,
                ts

            FROM early_v2_shadow

            WHERE snapshot_id IN (
                SELECT MAX(snapshot_id)

                FROM early_v2_shadow

                GROUP BY
                    symbol,
                    action
            )

            ORDER BY
                v2_score DESC,
                symbol ASC
            """
        ).fetchall()


    finally:
        conn.close()


    print()
    print(
        "=============================================================="
    )

    print(
        "EARLY V2 SHADOW SCORES"
    )

    print(
        "=============================================================="
    )


    if not rows:
        print(
            "No V2 shadow scores."
        )

        return


    for row in rows:
        print(
            "EARLY V2 | "
            f"{row['symbol']:<8} | "
            f"action="
            f"{row['action']:<4} | "
            f"v1="
            f"{float(row['v1_score'] or 0):+7.2f} | "
            f"price="
            f"{float(row['price_component'] or 0):+7.2f} | "
            f"spread="
            f"{float(row['spread_component'] or 0):+7.2f} | "
            f"v2="
            f"{float(row['v2_score']):+7.2f} | "
            f"state="
            f"{row['v2_state']:<11} | "
            f"formula="
            f"{row['formula']}"
        )


# ============================================================
# SELF TEST
# ============================================================

def run_self_test():
    print(
        "=============================================================="
    )

    print(
        "EARLY V2 SHADOW SELF TEST"
    )

    print(
        "=============================================================="
    )


    buy = (
        calculate_v2(
            action=
                "BUY",

            price_component=
                12.0,

            spread_component=
                6.0,
        )
    )


    sell = (
        calculate_v2(
            action=
                "SELL",

            price_component=
                12.0,

            spread_component=
                6.0,
        )
    )


    print(
        "BUY_RESULT="
        +
        json.dumps(
            buy,
            sort_keys=True,
        )
    )


    print(
        "SELL_RESULT="
        +
        json.dumps(
            sell,
            sort_keys=True,
        )
    )


    if (
        buy[
            "v2_score"
        ]
        !=
        -12.0
    ):
        print(
            "EARLY_V2_SELF_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    if (
        sell[
            "v2_score"
        ]
        !=
        -15.0
    ):
        print(
            "EARLY_V2_SELF_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    print(
        "EARLY_V2_SELF_TEST=PASS"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    import sys


    init_db()


    if (
        "--self-test"
        in
        sys.argv
    ):
        run_self_test()

        return


    process_pending()

    print_current_scores()


if __name__ == "__main__":
    main()
