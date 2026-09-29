import os
import traceback

import worker

from exposure_guard import (
    check_total_managed_exposure,
    ExposureGuardError,
)


# ============================================================
# CONFIG
# ============================================================

MAX_POSITION_USD = float(
    os.getenv(
        "MAX_POSITION_USD",
        "100",
    )
)


MAX_TOTAL_MANAGED_EXPOSURE_USD = float(
    os.getenv(
        "MAX_TOTAL_MANAGED_EXPOSURE_USD",
        "300",
    )
)


CANARY_BUY_ONLY = (
    os.getenv(
        "CANARY_BUY_ONLY",
        "true",
    )
    .strip()
    .lower()
    ==
    "true"
)


# ============================================================
# ORIGINAL WORKER FUNCTION
# ============================================================

ORIGINAL_PROCESS_SIGNAL = (
    worker.process_signal_mode_aware
)


# ============================================================
# HELPERS
# ============================================================

def safe_float(
    value,
):
    if value is None:
        return None

    try:
        return float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


def block_signal(
    signal,
    event_type,
    message,
    payload=None,
):
    signal_id = (
        signal.get(
            "signal_id"
        )
        or
        "UNKNOWN"
    )

    print(
        "CANARY BLOCK | "
        f"{signal_id} | "
        f"{event_type} | "
        f"{message}",
        flush=True,
    )

    try:
        worker.core.transition(
            signal_id,
            "BLOCKED",
            event_type,
            message=message,
            payload=payload,
        )

    except Exception as exc:
        print(
            "CANARY BLOCK TRANSITION ERROR | "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


# ============================================================
# GUARDED PROCESS SIGNAL
# ============================================================

def guarded_process_signal(
    signal,
):
    signal_id = (
        signal.get(
            "signal_id"
        )
        or
        "UNKNOWN"
    )

    execution_mode = (
        str(
            signal.get(
                "execution_mode"
            )
            or
            ""
        )
        .strip()
        .upper()
    )

    test_mode = int(
        signal.get(
            "test_mode"
        )
        or
        0
    )

    action = (
        str(
            signal.get(
                "action"
            )
            or
            ""
        )
        .strip()
        .upper()
    )


    # --------------------------------------------------------
    # PRELIVE / TEST
    # --------------------------------------------------------
    #
    # Do not interfere with existing PRELIVE behaviour.
    #
    if (
        test_mode == 1
        or
        execution_mode != "LIVE"
    ):
        return ORIGINAL_PROCESS_SIGNAL(
            signal
        )


    # --------------------------------------------------------
    # BUY ONLY CANARY
    # --------------------------------------------------------

    if (
        CANARY_BUY_ONLY
        and
        action != "BUY"
    ):
        block_signal(
            signal,
            "CANARY_DIRECTION_BLOCK",
            (
                "LIVE canary is BUY-only; "
                f"received action={action}"
            ),
            payload={
                "action":
                    action,

                "canary_buy_only":
                    CANARY_BUY_ONLY,
            },
        )

        return


    # --------------------------------------------------------
    # REQUIRED SIGNAL VALUES
    # --------------------------------------------------------

    quantity = safe_float(
        signal.get(
            "quantity"
        )
    )

    entry = safe_float(
        signal.get(
            "entry"
        )
    )


    if (
        quantity is None
        or
        quantity <= 0
    ):
        block_signal(
            signal,
            "CANARY_INVALID_QUANTITY",
            (
                "LIVE canary quantity "
                "is unavailable or invalid"
            ),
            payload={
                "quantity":
                    signal.get(
                        "quantity"
                    ),
            },
        )

        return


    if (
        entry is None
        or
        entry <= 0
    ):
        block_signal(
            signal,
            "CANARY_INVALID_ENTRY",
            (
                "LIVE canary entry "
                "is unavailable or invalid"
            ),
            payload={
                "entry":
                    signal.get(
                        "entry"
                    ),
            },
        )

        return


    proposed_notional = (
        abs(
            quantity
        )
        *
        entry
    )


    # --------------------------------------------------------
    # PER POSITION HARD CAP
    # --------------------------------------------------------

    if (
        proposed_notional
        >
        MAX_POSITION_USD
        +
        1e-9
    ):
        block_signal(
            signal,
            "CANARY_POSITION_CAP_BLOCK",
            (
                "LIVE canary position "
                "notional exceeds hard cap: "
                f"${proposed_notional:.2f} > "
                f"${MAX_POSITION_USD:.2f}"
            ),
            payload={
                "quantity":
                    quantity,

                "entry":
                    entry,

                "proposed_notional_usd":
                    proposed_notional,

                "max_position_usd":
                    MAX_POSITION_USD,
            },
        )

        return


    # --------------------------------------------------------
    # TOTAL MANAGED EXPOSURE HARD CAP
    # --------------------------------------------------------

    try:
        exposure = (
            check_total_managed_exposure(
                quantity=
                    quantity,

                entry=
                    entry,

                db_file=
                    worker.core.DB_FILE,

                max_total_exposure_usd=
                    MAX_TOTAL_MANAGED_EXPOSURE_USD,
            )
        )

    except ExposureGuardError as exc:
        block_signal(
            signal,
            "CANARY_EXPOSURE_BLOCK",
            str(
                exc
            ),
            payload={
                "quantity":
                    quantity,

                "entry":
                    entry,

                "proposed_notional_usd":
                    proposed_notional,

                "max_total_managed_exposure_usd":
                    MAX_TOTAL_MANAGED_EXPOSURE_USD,
            },
        )

        return

    except Exception as exc:
        block_signal(
            signal,
            "CANARY_EXPOSURE_ERROR",
            (
                "Exposure guard failed closed: "
                f"{type(exc).__name__}: {exc}"
            ),
            payload={
                "traceback":
                    traceback.format_exc(),
            },
        )

        return


    # --------------------------------------------------------
    # APPROVED
    # --------------------------------------------------------

    try:
        worker.core.record_event(
            db_file=
                worker.core.DB_FILE,

            signal_id=
                signal_id,

            event_type=
                "CANARY_EXPOSURE_APPROVED",

            source=
                "guarded_worker",

            message=(
                "LIVE canary exposure "
                "guard approved"
            ),

            payload=
                exposure,

            event_key=(
                f"canary-exposure:"
                f"{signal_id}"
            ),
        )

    except Exception as exc:
        #
        # Audit failure is treated as fail-closed.
        #
        block_signal(
            signal,
            "CANARY_AUDIT_ERROR",
            (
                "Could not persist exposure "
                "approval event: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

        return


    print(
        "CANARY EXPOSURE PASS | "
        f"{signal_id} | "
        f"proposed=${proposed_notional:.2f} | "
        f"current="
        f"${exposure['current_allocated_usd']:.2f} | "
        f"projected="
        f"${exposure['projected_allocated_usd']:.2f} | "
        f"limit="
        f"${exposure['max_total_exposure_usd']:.2f}",
        flush=True,
    )


    # --------------------------------------------------------
    # EXISTING WORKER
    # --------------------------------------------------------

    return ORIGINAL_PROCESS_SIGNAL(
        signal
    )


# ============================================================
# INSTALL WRAPPER
# ============================================================

worker.core.process_signal = (
    guarded_process_signal
)


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "========================================",
        flush=True,
    )

    print(
        "TradingMax GUARDED worker started",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )

    print(
        f"MAX_POSITION_USD="
        f"{MAX_POSITION_USD}",
        flush=True,
    )

    print(
        f"MAX_TOTAL_MANAGED_EXPOSURE_USD="
        f"{MAX_TOTAL_MANAGED_EXPOSURE_USD}",
        flush=True,
    )

    print(
        f"CANARY_BUY_ONLY="
        f"{CANARY_BUY_ONLY}",
        flush=True,
    )

    worker.core.main()


if __name__ == "__main__":
    main()
