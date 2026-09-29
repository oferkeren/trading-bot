import math
import os
import sqlite3

from pathlib import Path


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent


DEFAULT_DB_FILE = (
    BASE_DIR
    /
    "trading.db"
)


# ============================================================
# CONFIG
# ============================================================

MAX_TOTAL_MANAGED_EXPOSURE_USD = float(
    os.getenv(
        "MAX_TOTAL_MANAGED_EXPOSURE_USD",
        "300",
    )
)


ACTIVE_MANAGED_STATUSES = {
    "SUBMITTED",
    "ACCEPTED_WAITING_MARKET",
    "FILLED",
    "OPEN_POSITION",
    "CANCEL_REQUESTED",
    "CANCELLING",
    "CANCEL_PENDING",
    "CANCEL_UNKNOWN",
    "UNKNOWN",
    "ERROR",
}


# ============================================================
# ERROR
# ============================================================

class ExposureGuardError(Exception):
    pass


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


def db_connect(
    db_file=DEFAULT_DB_FILE,
):
    conn = sqlite3.connect(
        str(
            db_file
        ),
        timeout=10,
    )

    conn.row_factory = sqlite3.Row

    return conn


# ============================================================
# POSITION VALUE
# ============================================================

def calculate_signal_notional(
    *,
    quantity,
    entry,
):
    quantity = safe_float(
        quantity
    )

    entry = safe_float(
        entry
    )


    if quantity is None:
        raise ExposureGuardError(
            "quantity unavailable"
        )


    if entry is None:
        raise ExposureGuardError(
            "entry unavailable"
        )


    if quantity <= 0:
        raise ExposureGuardError(
            "quantity must be positive"
        )


    if entry <= 0:
        raise ExposureGuardError(
            "entry must be positive"
        )


    return (
        abs(
            quantity
        )
        *
        entry
    )


# ============================================================
# CURRENT MANAGED ALLOCATION
# ============================================================

def load_active_managed_allocation(
    db_file=DEFAULT_DB_FILE,
):
    conn = db_connect(
        db_file
    )


    try:
        placeholders = ",".join(
            "?"
            for _
            in ACTIVE_MANAGED_STATUSES
        )


        rows = conn.execute(
            f"""
            SELECT
                signal_id,
                symbol,
                action,
                quantity,
                entry,
                position_value,
                filled_quantity,
                entry_fill_price,
                status,
                parent_order_id,
                parent_perm_id

            FROM signals

            WHERE
                test_mode = 0

                AND execution_mode = 'LIVE'

                AND status IN (
                    {placeholders}
                )

                AND (
                    parent_order_id IS NOT NULL
                    OR
                    parent_perm_id IS NOT NULL
                )
            """,
            tuple(
                ACTIVE_MANAGED_STATUSES
            ),
        ).fetchall()


    finally:
        conn.close()


    items = []

    total = 0.0


    for row in rows:
        position_value = safe_float(
            row[
                "position_value"
            ]
        )


        if (
            position_value is not None
            and
            position_value > 0
        ):
            allocated = abs(
                position_value
            )

            source = (
                "position_value"
            )


        else:
            filled_quantity = safe_float(
                row[
                    "filled_quantity"
                ]
            )


            entry_fill_price = safe_float(
                row[
                    "entry_fill_price"
                ]
            )


            if (
                filled_quantity is not None
                and
                filled_quantity != 0
                and
                entry_fill_price is not None
                and
                entry_fill_price > 0
            ):
                allocated = (
                    abs(
                        filled_quantity
                    )
                    *
                    entry_fill_price
                )

                source = (
                    "filled_quantity*"
                    "entry_fill_price"
                )


            else:
                quantity = safe_float(
                    row[
                        "quantity"
                    ]
                )


                entry = safe_float(
                    row[
                        "entry"
                    ]
                )


                if (
                    quantity is None
                    or
                    quantity <= 0
                    or
                    entry is None
                    or
                    entry <= 0
                ):
                    raise ExposureGuardError(
                        (
                            "Cannot determine managed "
                            "allocation for signal "
                            f"{row['signal_id']}"
                        )
                    )


                allocated = (
                    quantity
                    *
                    entry
                )

                source = (
                    "quantity*entry"
                )


        total += (
            allocated
        )


        items.append(
            {
                "signal_id":
                    row[
                        "signal_id"
                    ],

                "symbol":
                    row[
                        "symbol"
                    ],

                "status":
                    row[
                        "status"
                    ],

                "allocated_usd":
                    round(
                        allocated,
                        4,
                    ),

                "source":
                    source,
            }
        )


    return {
        "allocated_usd":
            round(
                total,
                4,
            ),

        "position_count":
            len(
                items
            ),

        "positions":
            items,
    }


# ============================================================
# CHECK
# ============================================================

def check_total_managed_exposure(
    *,
    quantity,
    entry,
    db_file=DEFAULT_DB_FILE,
    max_total_exposure_usd=None,
):
    if max_total_exposure_usd is None:
        max_total_exposure_usd = (
            MAX_TOTAL_MANAGED_EXPOSURE_USD
        )


    max_total_exposure_usd = safe_float(
        max_total_exposure_usd
    )


    if (
        max_total_exposure_usd is None
        or
        max_total_exposure_usd <= 0
    ):
        raise ExposureGuardError(
            (
                "MAX_TOTAL_MANAGED_EXPOSURE_USD "
                "must be positive"
            )
        )


    current = (
        load_active_managed_allocation(
            db_file=db_file,
        )
    )


    proposed = (
        calculate_signal_notional(
            quantity=quantity,
            entry=entry,
        )
    )


    projected = (
        current[
            "allocated_usd"
        ]
        +
        proposed
    )


    allowed = (
        projected
        <=
        max_total_exposure_usd
        +
        1e-9
    )


    result = {
        "allowed":
            allowed,

        "current_allocated_usd":
            round(
                current[
                    "allocated_usd"
                ],
                4,
            ),

        "proposed_notional_usd":
            round(
                proposed,
                4,
            ),

        "projected_allocated_usd":
            round(
                projected,
                4,
            ),

        "max_total_exposure_usd":
            round(
                max_total_exposure_usd,
                4,
            ),

        "managed_position_count":
            current[
                "position_count"
            ],

        "managed_positions":
            current[
                "positions"
            ],
    }


    if not allowed:
        raise ExposureGuardError(
            (
                "Total managed exposure limit "
                "would be exceeded: "
                f"current=${current['allocated_usd']:.2f}, "
                f"proposed=${proposed:.2f}, "
                f"projected=${projected:.2f}, "
                f"limit=${max_total_exposure_usd:.2f}"
            )
        )


    return result


# ============================================================
# CLI
# ============================================================

def main():
    current = (
        load_active_managed_allocation()
    )


    print(
        "========================================"
    )

    print(
        "TRADINGMAX EXPOSURE GUARD"
    )

    print(
        "========================================"
    )


    print(
        f"Current managed allocation : "
        f"${current['allocated_usd']:.2f}"
    )


    print(
        f"Managed positions          : "
        f"{current['position_count']}"
    )


    print(
        f"Configured maximum         : "
        f"${MAX_TOTAL_MANAGED_EXPOSURE_USD:.2f}"
    )


    if current[
        "positions"
    ]:
        print()


        for item in current[
            "positions"
        ]:
            print(
                f"{item['symbol']:<8} "
                f"${item['allocated_usd']:.2f} "
                f"{item['status']} "
                f"[{item['source']}]"
            )


if __name__ == "__main__":
    main()
