import os
import time

import worker_core as core


# ============================================================
# TRADINGMAX WORKER WRAPPER
#
# Safety model:
#
# PRELIVE
#   -> always PRELIVE_DRY_RUN=True
#
# LIVE
#   -> requires LIVE_ARMED=true
#   -> BUY only
#   -> quantity <= LIVE_MAX_QTY
#   -> optional ACTIVE one-shot protection
#
# ACTIVE one-shot means:
#   An active or uncertain LIVE lifecycle blocks another LIVE.
#
# Historical terminal LIVE signals do NOT block forever.
# ============================================================


def parse_bool(
    value,
    default=False,
):
    if value is None:
        return default

    return (
        str(value)
        .strip()
        .lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )


LIVE_ARMED = parse_bool(
    os.getenv(
        "LIVE_ARMED"
    ),
    False,
)


LIVE_ONE_SHOT = parse_bool(
    os.getenv(
        "LIVE_ONE_SHOT"
    ),
    True,
)


LIVE_MAX_QTY = int(
    os.getenv(
        "LIVE_MAX_QTY",
        "1",
    )
)


IB_RECONNECT_COOLDOWN_SECONDS = float(
    os.getenv(
        "IB_RECONNECT_COOLDOWN_SECONDS",
        "1.5",
    )
)


ACTIVE_LIVE_STATUSES = {
    "QUEUED",
    "PROCESSING",

    "SUBMITTED",
    "ACCEPTED_WAITING_MARKET",

    "FILLED",
    "OPEN_POSITION",

    "CANCEL_REQUESTED",
    "CANCELLING",
    "CANCEL_PENDING",
    "CANCEL_UNKNOWN",

    "UNKNOWN",
}


PRELIVE_ALLOWED_STATUSES = {
    "QUEUED",
    "PROCESSING",
    "BLOCKED",
    "REJECTED",
    "TESTED",
    "ERROR",
}


BROKER_IDENTITY_FIELDS = {
    "parent_order_id",
    "entry_order_id",
    "target_order_id",
    "stop_order_id",

    "parent_perm_id",
    "target_perm_id",
    "stop_perm_id",

    "entry_order_ref",
    "target_order_ref",
    "stop_order_ref",
}


_original_transition = (
    core.transition
)

_original_reconcile_orders = (
    core.reconcile_orders
)

_original_process_signal = (
    core.process_signal
)


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_execution_mode(
    signal_id
):
    if not signal_id:
        return None

    conn = core.db_connect()

    try:
        row = conn.execute(
            """
            SELECT execution_mode

            FROM signals

            WHERE signal_id = ?
            """,
            (
                signal_id,
            ),
        ).fetchone()

        if row is None:
            return None

        return (
            row[
                "execution_mode"
            ]
            or ""
        ).strip().upper()

    finally:
        conn.close()


def get_active_live_signal(
    exclude_signal_id=None,
):
    placeholders = ",".join(
        "?"
        for _
        in ACTIVE_LIVE_STATUSES
    )

    params = list(
        sorted(
            ACTIVE_LIVE_STATUSES
        )
    )

    sql = f"""
        SELECT
            signal_id,
            symbol,
            quantity,
            status,
            parent_order_id,
            entry_order_id,
            target_order_id,
            stop_order_id,
            created_at,
            updated_at

        FROM signals

        WHERE
            execution_mode = 'LIVE'

            AND status IN (
                {placeholders}
            )
    """


    if exclude_signal_id:
        sql += (
            """
            AND signal_id <> ?
            """
        )

        params.append(
            exclude_signal_id
        )


    sql += (
        """
        ORDER BY created_at ASC
        LIMIT 1
        """
    )


    conn = core.db_connect()

    try:
        row = conn.execute(
            sql,
            params,
        ).fetchone()

        if row is None:
            return None

        return dict(
            row
        )

    finally:
        conn.close()


# ============================================================
# PRELIVE SAFE TRANSITION
# ============================================================

def safe_transition(
    signal_id,
    status,
    event_type,
    message=None,
    payload=None,
    fields=None,
    event_key=None,
    force=False,
):
    execution_mode = (
        get_execution_mode(
            signal_id
        )
    )

    requested_status = (
        str(
            status
        )
        .strip()
        .upper()
    )


    if (
        execution_mode
        ==
        "PRELIVE"
        and
        requested_status
        not in PRELIVE_ALLOWED_STATUSES
    ):
        original_fields = (
            dict(fields)
            if fields
            else {}
        )


        removed_identity = {
            key:
                original_fields.get(
                    key
                )

            for key
            in BROKER_IDENTITY_FIELDS

            if key
            in original_fields
        }


        safe_fields = {
            key:
                value

            for key, value
            in original_fields.items()

            if key
            not in BROKER_IDENTITY_FIELDS
        }


        safe_payload = {
            "execution_mode":
                "PRELIVE",

            "requested_status":
                requested_status,

            "requested_event_type":
                event_type,

            "original_message":
                message,

            "removed_broker_identity_fields":
                removed_identity,
        }


        if payload is not None:
            safe_payload[
                "original_payload"
            ] = payload


        print(
            f"PRELIVE TRANSITION GUARD | "
            f"{signal_id} | "
            f"requested={requested_status} | "
            f"converted=BLOCKED | "
            f"event={event_type}",
            flush=True,
        )


        return _original_transition(
            signal_id,
            "BLOCKED",
            "PRELIVE_EXCEPTION_BLOCK",

            message=(
                "PRELIVE execution attempted "
                f"forbidden status "
                f"{requested_status}; "
                "converted safely to BLOCKED"
            ),

            payload=
                safe_payload,

            fields=(
                safe_fields
                if safe_fields
                else None
            ),

            event_key=(
                "prelive-exception-block:"
                f"{signal_id}:"
                f"{event_type}:"
                f"{requested_status}"
            ),

            force=
                force,
        )


    return _original_transition(
        signal_id,
        status,
        event_type,

        message=
            message,

        payload=
            payload,

        fields=
            fields,

        event_key=
            event_key,

        force=
            force,
    )


# ============================================================
# MODE-AWARE EXECUTION
# ============================================================

def process_signal_mode_aware(
    signal
):
    signal_id = (
        signal.get(
            "signal_id"
        )
    )


    execution_mode = (
        str(
            signal.get(
                "execution_mode"
            )
            or
            get_execution_mode(
                signal_id
            )
            or
            ""
        )
        .strip()
        .upper()
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


    try:
        quantity = int(
            signal.get(
                "quantity"
            )
            or
            0
        )

    except Exception:
        quantity = 0


    # --------------------------------------------------------
    # MODE
    # --------------------------------------------------------

    if execution_mode not in {
        "PRELIVE",
        "LIVE",
    }:
        safe_transition(
            signal_id,
            "BLOCKED",
            "EXECUTION_MODE_BLOCK",

            message=(
                "Invalid or missing "
                f"execution_mode="
                f"{execution_mode!r}"
            ),
        )

        return


    # --------------------------------------------------------
    # PRELIVE
    # --------------------------------------------------------

    if execution_mode == "PRELIVE":
        desired_prelive_dry_run = (
            True
        )


    # --------------------------------------------------------
    # LIVE
    # --------------------------------------------------------

    else:
        if not LIVE_ARMED:
            safe_transition(
                signal_id,
                "BLOCKED",
                "LIVE_NOT_ARMED",

                message=(
                    "LIVE signal blocked because "
                    "LIVE_ARMED=false"
                ),
            )

            print(
                f"LIVE BLOCKED | "
                f"{signal_id} | "
                "LIVE_ARMED=false",
                flush=True,
            )

            return


        if action != "BUY":
            safe_transition(
                signal_id,
                "BLOCKED",
                "LIVE_ACTION_BLOCK",

                message=(
                    "LIVE execution currently "
                    "supports BUY only"
                ),
            )

            return


        if quantity < 1:
            safe_transition(
                signal_id,
                "BLOCKED",
                "LIVE_QUANTITY_BLOCK",

                message=(
                    f"Invalid LIVE quantity="
                    f"{quantity}"
                ),
            )

            return


        if quantity > LIVE_MAX_QTY:
            safe_transition(
                signal_id,
                "BLOCKED",
                "LIVE_MAX_QTY_BLOCK",

                message=(
                    f"LIVE quantity="
                    f"{quantity} exceeds "
                    f"LIVE_MAX_QTY="
                    f"{LIVE_MAX_QTY}"
                ),
            )

            print(
                f"LIVE BLOCKED | "
                f"{signal_id} | "
                f"quantity={quantity} | "
                f"LIVE_MAX_QTY="
                f"{LIVE_MAX_QTY}",
                flush=True,
            )

            return


        if LIVE_ONE_SHOT:
            existing = (
                get_active_live_signal(
                    exclude_signal_id=
                        signal_id
                )
            )

            if existing is not None:
                safe_transition(
                    signal_id,
                    "BLOCKED",
                    "LIVE_ONE_SHOT_BLOCK",

                    message=(
                        "Another active LIVE "
                        "signal exists: "
                        f"{existing['signal_id']} "
                        f"status="
                        f"{existing['status']}"
                    ),

                    payload={
                        "existing_live_signal":
                            existing,
                    },
                )

                print(
                    f"LIVE BLOCKED | "
                    f"{signal_id} | "
                    "LIVE_ONE_SHOT=true | "
                    f"existing="
                    f"{existing['signal_id']} | "
                    f"status="
                    f"{existing['status']}",
                    flush=True,
                )

                return


        desired_prelive_dry_run = (
            False
        )


    # --------------------------------------------------------
    # CORE
    # --------------------------------------------------------

    previous_prelive_dry_run = (
        core.PRELIVE_DRY_RUN
    )


    try:
        core.PRELIVE_DRY_RUN = (
            desired_prelive_dry_run
        )


        print(
            f"EXECUTION MODE | "
            f"{signal_id} | "
            f"mode={execution_mode} | "
            f"action={action} | "
            f"quantity={quantity} | "
            f"LIVE_ARMED={LIVE_ARMED} | "
            f"LIVE_ONE_SHOT="
            f"{LIVE_ONE_SHOT} | "
            f"LIVE_MAX_QTY="
            f"{LIVE_MAX_QTY} | "
            f"PRELIVE_DRY_RUN="
            f"{core.PRELIVE_DRY_RUN}",
            flush=True,
        )


        _original_process_signal(
            signal
        )


    finally:
        core.PRELIVE_DRY_RUN = (
            previous_prelive_dry_run
        )


# ============================================================
# LIVE-ONLY RECONCILIATION
# ============================================================

def get_reconcile_candidates_live_only():
    conn = core.db_connect()

    try:
        rows = conn.execute(
            """
            SELECT *

            FROM signals

            WHERE
                test_mode = 0

                AND execution_mode = 'LIVE'

                AND status IN (
                    'PROCESSING',
                    'SUBMITTED',
                    'ACCEPTED_WAITING_MARKET',
                    'FILLED',
                    'OPEN_POSITION',

                    'CANCEL_REQUESTED',
                    'CANCELLING',
                    'CANCEL_PENDING',
                    'CANCEL_UNKNOWN',

                    'UNKNOWN',
                    'ERROR'
                )

            ORDER BY created_at ASC
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
# LIVE-ONLY RECOVERY
# ============================================================

def recover_stuck_live_work_only():
    cutoff = (
        core.datetime.now(
            core.timezone.utc
        )
        -
        core.timedelta(
            minutes=
                core.PROCESSING_RECOVERY_MINUTES
        )
    ).isoformat()


    conn = core.db_connect()

    try:
        rows = conn.execute(
            """
            SELECT
                signal_id,
                status

            FROM signals

            WHERE
                execution_mode = 'LIVE'

                AND status IN (
                    'PROCESSING',
                    'CANCELLING'
                )

                AND updated_at < ?
            """,
            (
                cutoff,
            ),
        ).fetchall()

    finally:
        conn.close()


    for row in rows:

        if (
            row[
                "status"
            ]
            ==
            "PROCESSING"
        ):
            safe_transition(
                row[
                    "signal_id"
                ],
                "UNKNOWN",
                "WORKER_RECOVERY",

                message=(
                    "Worker restarted while "
                    "LIVE signal was PROCESSING"
                ),
            )

        else:
            safe_transition(
                row[
                    "signal_id"
                ],
                "CANCEL_UNKNOWN",
                "CANCEL_RECOVERY",

                message=(
                    "Worker restarted while "
                    "LIVE cancellation was active"
                ),
            )


# ============================================================
# IBKR CLIENT COOLDOWN
# ============================================================

def reconcile_orders_with_cooldown():
    try:
        return (
            _original_reconcile_orders()
        )

    finally:
        print(
            "IBKR CLIENT COOLDOWN | "
            f"{IB_RECONNECT_COOLDOWN_SECONDS:.2f}s "
            "after reconciliation",
            flush=True,
        )

        time.sleep(
            IB_RECONNECT_COOLDOWN_SECONDS
        )


# ============================================================
# INSTALL OVERRIDES
# ============================================================

core.transition = (
    safe_transition
)

core.process_signal = (
    process_signal_mode_aware
)

core.get_reconcile_candidates = (
    get_reconcile_candidates_live_only
)

core.recover_stuck_work = (
    recover_stuck_live_work_only
)

core.reconcile_orders = (
    reconcile_orders_with_cooldown
)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print(
        "TradingMax worker wrapper started",
        flush=True,
    )

    print(
        "broker_reconciliation=LIVE_ONLY",
        flush=True,
    )

    print(
        "execution_mode_policy=STRICT",
        flush=True,
    )

    print(
        f"LIVE_ARMED={LIVE_ARMED}",
        flush=True,
    )

    print(
        f"LIVE_ONE_SHOT={LIVE_ONE_SHOT}",
        flush=True,
    )

    print(
        "LIVE_ONE_SHOT_SCOPE="
        "ACTIVE_OR_UNCERTAIN_ONLY",
        flush=True,
    )

    print(
        f"LIVE_MAX_QTY="
        f"{LIVE_MAX_QTY}",
        flush=True,
    )

    print(
        "prelive_policy=ALWAYS_DRY_RUN",
        flush=True,
    )

    print(
        "ib_reconnect_cooldown="
        f"{IB_RECONNECT_COOLDOWN_SECONDS:.2f}s",
        flush=True,
    )

    core.main()
