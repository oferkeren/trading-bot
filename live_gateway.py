import os
import sqlite3
import subprocess

from fastapi import FastAPI, HTTPException

import signal_server as core

from execution_mode_guard import (
    assert_execution_mode_guard,
    ensure_execution_mode_guard,
    get_execution_mode_guard_state,
)


# ============================================================
# CONFIG
# ============================================================

MODE = "LIVE"
EXECUTION_MODE = "LIVE"

WORKER_SERVICE = (
    "trading-worker.service"
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


LIVE_GATEWAY_MAX_QTY = int(
    os.getenv(
        "LIVE_GATEWAY_MAX_QTY",
        "1",
    )
)


LIVE_GATEWAY_ONE_SHOT = parse_bool(
    os.getenv(
        "LIVE_GATEWAY_ONE_SHOT"
    ),
    True,
)


app = FastAPI(
    title="TradingMax LIVE Gateway",
    version="2.1",
)


# ============================================================
# SYSTEMD / WORKER STATE
# ============================================================

def service_is_active(
    service_name
):
    result = subprocess.run(
        [
            "systemctl",
            "is-active",
            service_name,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    return (
        result.stdout
        .strip()
        ==
        "active"
    )


def worker_environment():
    result = subprocess.run(
        [
            "systemctl",
            "show",
            WORKER_SERVICE,
            "-p",
            "Environment",
            "--value",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    env = {}

    for token in (
        result.stdout
        .strip()
        .split()
    ):
        if "=" not in token:
            continue

        key, value = (
            token.split(
                "=",
                1,
            )
        )

        env[
            key.strip()
        ] = (
            value.strip()
        )

    return env


def get_worker_state():
    env = (
        worker_environment()
    )

    active = (
        service_is_active(
            WORKER_SERVICE
        )
    )

    live_armed = parse_bool(
        env.get(
            "LIVE_ARMED"
        ),
        False,
    )

    live_one_shot = parse_bool(
        env.get(
            "LIVE_ONE_SHOT"
        ),
        True,
    )


    try:
        live_max_qty = int(
            env.get(
                "LIVE_MAX_QTY",
                "0",
            )
        )

    except Exception:
        live_max_qty = 0


    return {
        "service":
            WORKER_SERVICE,

        "active":
            active,

        "live_armed":
            live_armed,

        "live_one_shot":
            live_one_shot,

        "live_max_qty":
            live_max_qty,

        "safe_for_live":
            (
                active
                and
                live_armed
                and
                live_max_qty
                >= 1
            ),
    }


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_control_state():
    return (
        core.get_control_state(
            core.DB_FILE
        )
    )


def existing_active_live_signal():
    placeholders = ",".join(
        "?"
        for _
        in ACTIVE_LIVE_STATUSES
    )


    conn = sqlite3.connect(
        core.DB_FILE
    )

    conn.row_factory = (
        sqlite3.Row
    )


    try:
        row = conn.execute(
            f"""
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

            ORDER BY created_at ASC

            LIMIT 1
            """,
            list(
                sorted(
                    ACTIVE_LIVE_STATUSES
                )
            ),
        ).fetchone()


        if row is None:
            return None


        return dict(
            row
        )

    finally:
        conn.close()


def ensure_no_duplicate(
    signal_id
):
    conn = sqlite3.connect(
        core.DB_FILE
    )

    try:
        row = conn.execute(
            """
            SELECT
                status,
                execution_mode

            FROM signals

            WHERE signal_id = ?
            """,
            (
                signal_id,
            ),
        ).fetchone()

    finally:
        conn.close()


    if row is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Duplicate signal_id"
            ),
        )


# ============================================================
# LIVE SAFETY
# ============================================================

def get_live_safety():
    try:
        result = (
            core.check_pre_execution(
                db_file=
                    core.DB_FILE,

                max_snapshot_age_seconds=
                    core.STATUS_MAX_AGE_SECONDS,

                max_open_positions=
                    core.MAX_MANAGED_POSITIONS,

                max_trades_per_day=
                    core.MAX_TRADES_PER_DAY,

                max_daily_loss_usd=
                    core.MAX_DAILY_LOSS_USD,

                minimum_account_equity=
                    core.MIN_ACCOUNT_EQUITY_USD,

                block_on_pending_cancel=
                    core.BLOCK_LIVE_ON_PENDING_CANCEL,
            )
        )

        return {
            "ready":
                True,

            "result":
                result,

            "error":
                None,
        }

    except Exception as exc:
        return {
            "ready":
                False,

            "result":
                None,

            "error":
                str(
                    exc
                ),
        }


def build_live_state():
    guard = (
        get_execution_mode_guard_state(
            core.DB_FILE
        )
    )

    worker = (
        get_worker_state()
    )

    control = (
        get_control_state()
    )

    safety = (
        get_live_safety()
    )

    existing = (
        existing_active_live_signal()
    )


    blockers = []


    if not guard.get(
        "ready"
    ):
        blockers.append(
            "execution_mode_guard_not_ready"
        )


    if not worker[
        "active"
    ]:
        blockers.append(
            "worker_not_active"
        )


    if not worker[
        "live_armed"
    ]:
        blockers.append(
            "LIVE_ARMED=false"
        )


    if (
        worker[
            "live_max_qty"
        ]
        !=
        LIVE_GATEWAY_MAX_QTY
    ):
        blockers.append(
            (
                "gateway_worker_qty_mismatch: "
                f"gateway="
                f"{LIVE_GATEWAY_MAX_QTY}, "
                f"worker="
                f"{worker['live_max_qty']}"
            )
        )


    if control.get(
        "kill_switch"
    ):
        blockers.append(
            "kill_switch_enabled"
        )


    if not safety[
        "ready"
    ]:
        blockers.append(
            (
                "live_safety_blocked: "
                +
                str(
                    safety[
                        "error"
                    ]
                )
            )
        )


    if (
        LIVE_GATEWAY_ONE_SHOT
        and
        existing is not None
    ):
        blockers.append(
            (
                "active_live_signal: "
                f"{existing['signal_id']} "
                f"status="
                f"{existing['status']}"
            )
        )


    ready = (
        len(
            blockers
        )
        ==
        0
    )


    return {
        "status":
            "ok",

        "mode":
            MODE,

        "execution_mode":
            EXECUTION_MODE,

        "test_mode":
            False,

        "live":
            True,

        "ready_for_live_ingestion":
            ready,

        "blockers":
            blockers,

        "gateway_max_qty":
            LIVE_GATEWAY_MAX_QTY,

        "gateway_one_shot":
            LIVE_GATEWAY_ONE_SHOT,

        "one_shot_scope":
            "ACTIVE_OR_UNCERTAIN_ONLY",

        "worker":
            worker,

        "kill_switch":
            control.get(
                "kill_switch"
            ),

        "kill_switch_reason":
            control.get(
                "reason"
            ),

        "existing_active_live_signal":
            existing,

        "execution_mode_guard":
            guard,

        "live_safety":
            safety,
    }


def assert_live_ingestion_ready():
    state = (
        build_live_state()
    )

    if not state[
        "ready_for_live_ingestion"
    ]:
        raise HTTPException(
            status_code=409,

            detail={
                "message":
                    "LIVE ingestion blocked",

                "blockers":
                    state[
                        "blockers"
                    ],
            },
        )

    return state


# ============================================================
# LIVE SIZING
# ============================================================

def resolve_live_sizing(
    signal
):
    sizing = dict(
        core.resolve_position_size(
            signal
        )
    )


    original_quantity = int(
        sizing[
            "quantity"
        ]
    )


    final_quantity = min(
        original_quantity,
        LIVE_GATEWAY_MAX_QTY,
    )


    if final_quantity < 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "LIVE quantity resolved below 1"
            ),
        )


    risk_per_share = abs(
        float(
            signal.entry
        )
        -
        float(
            signal.stop
        )
    )


    sizing[
        "original_quantity"
    ] = original_quantity

    sizing[
        "quantity"
    ] = final_quantity

    sizing[
        "risk_per_share"
    ] = risk_per_share

    sizing[
        "planned_risk"
    ] = (
        risk_per_share
        *
        final_quantity
    )

    sizing[
        "position_value"
    ] = (
        float(
            signal.entry
        )
        *
        final_quantity
    )

    sizing[
        "sizing_mode"
    ] = (
        "FIRST_LIVE_CAP"
    )


    return sizing


# ============================================================
# STARTUP
# ============================================================

@app.on_event(
    "startup"
)
def startup():
    ensure_execution_mode_guard(
        core.DB_FILE
    )

    assert_execution_mode_guard(
        core.DB_FILE
    )

    print(
        "TradingMax LIVE gateway started",
        flush=True,
    )

    print(
        "execution_mode=LIVE",
        flush=True,
    )

    print(
        f"LIVE_GATEWAY_MAX_QTY="
        f"{LIVE_GATEWAY_MAX_QTY}",
        flush=True,
    )

    print(
        f"LIVE_GATEWAY_ONE_SHOT="
        f"{LIVE_GATEWAY_ONE_SHOT}",
        flush=True,
    )

    print(
        "LIVE_ONE_SHOT_SCOPE="
        "ACTIVE_OR_UNCERTAIN_ONLY",
        flush=True,
    )

    print(
        "fail_closed=true",
        flush=True,
    )


# ============================================================
# HEALTH
# ============================================================

@app.get(
    "/health"
)
def health():
    return (
        build_live_state()
    )


# ============================================================
# WEBHOOK
# ============================================================

@app.post(
    "/webhook/tradingview"
)
def webhook(
    signal:
        core.TradeSignal,
):
    core.validate_secret(
        signal.secret
    )


    signal_id = (
        signal.signal_id
        or ""
    ).strip()


    if not signal_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "signal_id is required"
            ),
        )


    if len(
        signal_id
    ) > 120:
        raise HTTPException(
            status_code=400,
            detail=(
                "signal_id is too long"
            ),
        )


    core.validate_prices(
        signal
    )


    metadata = (
        core.validate_signal_contract(
            signal
        )
    )


    # --------------------------------------------------------
    # ABSOLUTE LIVE GATE
    # --------------------------------------------------------

    live_state = (
        assert_live_ingestion_ready()
    )


    ensure_no_duplicate(
        signal_id
    )


    sizing = (
        resolve_live_sizing(
            signal
        )
    )


    if (
        sizing[
            "quantity"
        ]
        >
        LIVE_GATEWAY_MAX_QTY
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Gateway quantity cap violated"
            ),
        )


    timestamp = (
        core.now_iso()
    )


    conn = sqlite3.connect(
        core.DB_FILE,
        timeout=10,
    )


    try:
        conn.execute(
            "BEGIN IMMEDIATE"
        )


        # ----------------------------------------------------
        # RECHECK ONE-SHOT INSIDE WRITE WINDOW
        # ----------------------------------------------------

        if LIVE_GATEWAY_ONE_SHOT:
            placeholders = ",".join(
                "?"
                for _
                in ACTIVE_LIVE_STATUSES
            )

            existing = conn.execute(
                f"""
                SELECT
                    signal_id,
                    status

                FROM signals

                WHERE
                    execution_mode='LIVE'

                    AND status IN (
                        {placeholders}
                    )

                LIMIT 1
                """,
                list(
                    sorted(
                        ACTIVE_LIVE_STATUSES
                    )
                ),
            ).fetchone()


            if existing is not None:
                conn.rollback()

                raise HTTPException(
                    status_code=409,

                    detail={
                        "message":
                            "LIVE one-shot blocked",

                        "existing_signal":
                            existing[
                                0
                            ],

                        "existing_status":
                            existing[
                                1
                            ],
                    },
                )


        conn.execute(
            """
            INSERT INTO signals (
                signal_id,

                strategy,
                timeframe,
                signal_time,
                signal_age_seconds,

                symbol,
                action,

                quantity,

                entry,
                target,
                stop,

                status,

                created_at,
                updated_at,

                test_mode,
                attempts,

                sizing_mode,

                position_value,
                planned_risk,
                risk_per_share,

                account_equity,
                available_funds_at_signal,

                risk_budget,
                capital_budget,

                equity_risk_budget,
                equity_capital_budget,

                execution_mode
            )

            VALUES (
                ?,

                ?, ?, ?, ?,

                ?, ?,

                ?,

                ?, ?, ?,

                ?,

                ?, ?,

                ?, ?,

                ?,

                ?, ?, ?,

                ?, ?,

                ?, ?,

                ?, ?,

                ?
            )
            """,

            (
                signal_id,

                metadata[
                    "strategy"
                ],

                metadata[
                    "timeframe"
                ],

                metadata[
                    "signal_time"
                ],

                metadata[
                    "signal_age_seconds"
                ],

                signal.symbol
                .strip()
                .upper(),

                signal.action
                .strip()
                .upper(),

                sizing[
                    "quantity"
                ],

                signal.entry,
                signal.target,
                signal.stop,

                "QUEUED",

                timestamp,
                timestamp,

                0,
                0,

                sizing[
                    "sizing_mode"
                ],

                sizing[
                    "position_value"
                ],

                sizing[
                    "planned_risk"
                ],

                sizing[
                    "risk_per_share"
                ],

                sizing.get(
                    "account_equity"
                ),

                sizing.get(
                    "available_funds"
                ),

                sizing.get(
                    "risk_budget"
                ),

                sizing.get(
                    "capital_budget"
                ),

                sizing.get(
                    "equity_risk_budget"
                ),

                sizing.get(
                    "equity_capital_budget"
                ),

                "LIVE",
            ),
        )


        conn.commit()


    except HTTPException:
        raise


    except Exception:
        conn.rollback()
        raise


    finally:
        conn.close()


    core.record_event(
        db_file=
            core.DB_FILE,

        signal_id=
            signal_id,

        event_type=
            "LIVE_SIGNAL_QUEUED",

        source=
            "live_api",

        message=(
            "LIVE signal inserted after "
            "arming, kill-switch, safety, "
            "active-one-shot and quantity "
            "cap checks"
        ),

        payload={
            "execution_mode":
                "LIVE",

            "original_quantity":
                sizing[
                    "original_quantity"
                ],

            "final_quantity":
                sizing[
                    "quantity"
                ],

            "gateway_max_qty":
                LIVE_GATEWAY_MAX_QTY,

            "one_shot_scope":
                "ACTIVE_OR_UNCERTAIN_ONLY",

            "live_armed":
                live_state[
                    "worker"
                ][
                    "live_armed"
                ],

            "kill_switch":
                live_state[
                    "kill_switch"
                ],
        },

        event_key=(
            "live-signal-queued:"
            +
            signal_id
        ),
    )


    return {
        "accepted":
            True,

        "mode":
            "LIVE",

        "execution_mode":
            "LIVE",

        "test_mode":
            False,

        "signal_id":
            signal_id,

        "symbol":
            signal.symbol
            .strip()
            .upper(),

        "action":
            signal.action
            .strip()
            .upper(),

        "original_quantity":
            sizing[
                "original_quantity"
            ],

        "quantity":
            sizing[
                "quantity"
            ],

        "gateway_max_qty":
            LIVE_GATEWAY_MAX_QTY,

        "one_shot_scope":
            "ACTIVE_OR_UNCERTAIN_ONLY",

        "queued_at":
            timestamp,
    }
