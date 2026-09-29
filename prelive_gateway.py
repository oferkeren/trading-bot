import os
import sqlite3
import subprocess

from fastapi import (
    FastAPI,
    HTTPException,
)

import signal_server_core as core

from execution_mode_guard import (
    ensure_execution_mode_guard,
    assert_execution_mode_guard,
    get_execution_mode_guard_state,
    ExecutionModeGuardError,
)


app = FastAPI(
    title=
        "TradingMax PRELIVE Gateway"
)


PRELIVE_WORKER_SERVICE = os.getenv(
    "PRELIVE_WORKER_SERVICE",
    "trading-worker.service",
)


# ============================================================
# EXECUTION MODE GUARD
# ============================================================

def initialize_execution_guard():
    return (
        ensure_execution_mode_guard(
            core.DB_FILE
        )
    )


EXECUTION_GUARD_STARTUP_STATE = (
    initialize_execution_guard()
)


def enforce_execution_guard():
    try:
        return (
            assert_execution_mode_guard(
                core.DB_FILE
            )
        )

    except ExecutionModeGuardError as exc:
        raise HTTPException(
            status_code=503,

            detail={
                "message":
                    "PRELIVE execution-mode guard unavailable",

                "reason":
                    str(
                        exc
                    ),
            },
        )


# ============================================================
# WORKER HARD GATE
# ============================================================

def worker_service_active():
    try:
        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                PRELIVE_WORKER_SERVICE,
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )

        return (
            result.stdout
            .strip()
            ==
            "active"
        )

    except Exception:
        return False


def worker_service_environment():
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                PRELIVE_WORKER_SERVICE,
                "-p",
                "Environment",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )

        if (
            result.returncode
            !=
            0
        ):
            return ""

        return (
            result.stdout
            .strip()
        )

    except Exception:
        return ""


def parse_systemd_environment(
    raw,
):
    result = {}

    for token in (
        raw.split()
    ):
        if "=" not in token:
            continue

        key, value = (
            token.split(
                "=",
                1,
            )
        )

        key = (
            key.strip()
        )

        value = (
            value.strip()
        )

        if not key:
            continue

        result[
            key
        ] = value

    return result


def worker_prelive_state():
    active = (
        worker_service_active()
    )

    raw_environment = (
        worker_service_environment()
    )

    environment = (
        parse_systemd_environment(
            raw_environment
        )
    )

    dry_run_value = (
        environment.get(
            "PRELIVE_DRY_RUN"
        )
    )

    dry_run_enabled = (
        str(
            dry_run_value
            or
            ""
        )
        .strip()
        .lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )

    return {
        "service":
            PRELIVE_WORKER_SERVICE,

        "active":
            active,

        "prelive_dry_run":
            dry_run_enabled,

        "safe":
            (
                active
                and
                dry_run_enabled
            ),
    }


def enforce_worker_prelive():
    state = (
        worker_prelive_state()
    )

    if not state[
        "active"
    ]:
        raise HTTPException(
            status_code=503,

            detail={
                "message":
                    "PRELIVE disabled",

                "reason":
                    (
                        "trading worker "
                        "is not active"
                    ),

                "worker":
                    state,
            },
        )

    if not state[
        "prelive_dry_run"
    ]:
        raise HTTPException(
            status_code=503,

            detail={
                "message":
                    "PRELIVE disabled",

                "reason":
                    (
                        "worker is not explicitly "
                        "configured with "
                        "PRELIVE_DRY_RUN=true"
                    ),

                "worker":
                    state,
            },
        )

    return state


# ============================================================
# PRELIVE SAFETY
# ============================================================

def build_prelive_safety():
    snapshot = (
        core.get_runtime_snapshot()
        or
        {}
    )

    control = (
        core.get_control_state(
            core.DB_FILE
        )
    )

    extra_blockers = []


    # --------------------------------------------------------
    # SNAPSHOT JSON VALIDITY
    # --------------------------------------------------------

    positions_valid = bool(
        snapshot.get(
            "positions_valid",
            False,
        )
    )

    open_orders_valid = bool(
        snapshot.get(
            "open_orders_valid",
            False,
        )
    )

    positions = (
        snapshot.get(
            "positions"
        )
        if positions_valid
        else
        []
    )

    open_orders = (
        snapshot.get(
            "open_orders"
        )
        if open_orders_valid
        else
        []
    )

    if not positions_valid:
        extra_blockers.append(
            "positions_json invalid"
        )

    if not open_orders_valid:
        extra_blockers.append(
            "open_orders_json invalid"
        )


    # --------------------------------------------------------
    # TRADING DAY
    # --------------------------------------------------------

    try:
        trading_day = (
            core.count_trades_today(
                core.DB_FILE
            )
        )

        today_count = (
            trading_day[
                "count"
            ]
        )

    except Exception as exc:
        trading_day = None

        today_count = (
            core.MAX_TRADES_PER_DAY
        )

        extra_blockers.append(
            (
                "Trading-day calculation "
                f"failed: {exc}"
            )
        )


    # --------------------------------------------------------
    # POSITION OWNERSHIP POLICY
    # --------------------------------------------------------

    try:
        position_state = (
            core.evaluate_position_limits(
                db_file=
                    core.DB_FILE,

                broker_positions=
                    positions,

                max_managed_positions=
                    core.MAX_MANAGED_POSITIONS,

                max_total_broker_positions=
                    core.MAX_TOTAL_BROKER_POSITIONS,
            )
        )

        managed_positions = (
            position_state[
                "managed_positions"
            ]
        )

        extra_blockers.extend(
            position_state[
                "blockers"
            ]
        )

    except core.PositionPolicyError as exc:
        position_state = None

        managed_positions = []

        extra_blockers.append(
            (
                "Position policy failed: "
                f"{exc}"
            )
        )


    # --------------------------------------------------------
    # LIVE SEMANTICS
    #
    # PRELIVE deliberately evaluates as test_mode=False.
    # Public :8000 may remain WEBHOOK_TEST_MODE=true.
    # --------------------------------------------------------

    result = (
        core.evaluate_live_readiness(
            live_trading=
                core.LIVE_TRADING,

            test_mode=
                False,

            tws_connected=
                snapshot.get(
                    "tws_connected",
                    False,
                ),

            snapshot_updated_at=
                snapshot.get(
                    "updated_at"
                ),

            snapshot_max_age_seconds=
                core.STATUS_MAX_AGE_SECONDS,

            positions=
                managed_positions,

            open_orders=
                open_orders,

            max_open_positions=
                core.MAX_MANAGED_POSITIONS,

            trades_today=
                today_count,

            max_trades_per_day=
                core.MAX_TRADES_PER_DAY,

            daily_pnl=
                snapshot.get(
                    "daily_pnl"
                ),

            max_daily_loss_usd=
                core.MAX_DAILY_LOSS_USD,

            net_liquidation=
                snapshot.get(
                    "net_liquidation"
                ),

            minimum_account_equity=
                core.MIN_ACCOUNT_EQUITY_USD,

            available_funds=
                snapshot.get(
                    "available_funds"
                ),

            block_live_on_pending_cancel=
                core.BLOCK_LIVE_ON_PENDING_CANCEL,
        )
    )


    # --------------------------------------------------------
    # EXTRA BLOCKERS
    # --------------------------------------------------------

    for blocker in (
        extra_blockers
    ):
        if (
            blocker
            not in
            result[
                "blockers"
            ]
        ):
            result[
                "blockers"
            ].append(
                blocker
            )


    # --------------------------------------------------------
    # KILL SWITCH
    # --------------------------------------------------------

    if control[
        "kill_switch"
    ]:
        message = (
            "Kill switch enabled"
            +
            (
                f": {control['reason']}"
                if control[
                    "reason"
                ]
                else ""
            )
        )

        if (
            message
            not in
            result[
                "blockers"
            ]
        ):
            result[
                "blockers"
            ].insert(
                0,
                message,
            )


    result[
        "live_ready"
    ] = (
        len(
            result[
                "blockers"
            ]
        )
        ==
        0
    )

    result[
        "kill_switch"
    ] = (
        control[
            "kill_switch"
        ]
    )

    result[
        "kill_switch_reason"
    ] = (
        control[
            "reason"
        ]
    )

    result[
        "kill_switch_updated_at"
    ] = (
        control[
            "updated_at"
        ]
    )

    result[
        "positions_json_valid"
    ] = (
        positions_valid
    )

    result[
        "open_orders_json_valid"
    ] = (
        open_orders_valid
    )

    result[
        "managed_position_count"
    ] = (
        position_state[
            "managed_position_count"
        ]
        if position_state
        else None
    )

    result[
        "legacy_position_count"
    ] = (
        position_state[
            "legacy_position_count"
        ]
        if position_state
        else None
    )

    result[
        "total_broker_position_count"
    ] = (
        position_state[
            "broker_position_count"
        ]
        if position_state
        else None
    )

    result[
        "max_managed_positions"
    ] = (
        core.MAX_MANAGED_POSITIONS
    )

    result[
        "max_total_broker_positions"
    ] = (
        core.MAX_TOTAL_BROKER_POSITIONS
    )

    result[
        "trading_day"
    ] = (
        trading_day
    )

    result[
        "prelive"
    ] = True

    result[
        "evaluated_test_mode"
    ] = False

    return result


def enforce_prelive_safety():
    safety = (
        build_prelive_safety()
    )

    if not safety.get(
        "live_ready",
        False,
    ):
        raise HTTPException(
            status_code=409,

            detail={
                "message":
                    (
                        "PRELIVE signal blocked "
                        "by LIVE safety"
                    ),

                "blockers":
                    safety.get(
                        "blockers",
                        [],
                    ),
            },
        )

    return safety


# ============================================================
# DUPLICATE
# ============================================================

def enforce_not_duplicate(
    signal_id,
):
    conn = (
        core.db_connect()
    )

    try:
        row = (
            conn.execute(
                """
                SELECT
                    signal_id,
                    status,
                    test_mode,
                    execution_mode

                FROM signals

                WHERE signal_id = ?
                """,
                (
                    signal_id,
                ),
            )
            .fetchone()
        )

    finally:
        conn.close()

    if row is None:
        return

    raise HTTPException(
        status_code=409,

        detail={
            "message":
                "Duplicate signal_id",

            "signal_id":
                signal_id,

            "existing_status":
                row[
                    "status"
                ],

            "existing_test_mode":
                row[
                    "test_mode"
                ],

            "existing_execution_mode":
                row[
                    "execution_mode"
                ],
        },
    )


# ============================================================
# INSERT
# ============================================================

def insert_prelive_signal(
    *,
    signal,
    signal_id,
    metadata,
    sizing,
):
    timestamp = (
        core.now_iso()
    )

    conn = (
        core.db_connect()
    )

    cur = (
        conn.cursor()
    )

    try:
        cur.execute(
            "BEGIN IMMEDIATE"
        )

        cur.execute(
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
                execution_mode,

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
                equity_capital_budget
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

                ?,

                ?, ?, ?,

                ?, ?,

                ?, ?,

                ?, ?
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

                signal.symbol.upper(),

                signal.action.upper(),

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

                "PRELIVE",

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
            ),
        )

        conn.commit()

    except sqlite3.IntegrityError as exc:
        conn.rollback()

        raise HTTPException(
            status_code=409,

            detail=(
                f"PRELIVE insert blocked: "
                f"{exc}"
            ),
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    return timestamp


# ============================================================
# AUDIT
# ============================================================

def record_prelive_events(
    *,
    signal,
    signal_id,
    metadata,
    sizing,
    worker_state,
    safety,
):
    core.record_event(
        db_file=
            core.DB_FILE,

        signal_id=
            signal_id,

        event_type=
            "PRELIVE_SIGNAL_RECEIVED",

        source=
            "prelive_api",

        message=(
            "PRELIVE signal accepted "
            "with LIVE safety semantics"
        ),

        payload={
            "execution_mode":
                "PRELIVE",

            "strategy":
                metadata[
                    "strategy"
                ],

            "timeframe":
                metadata[
                    "timeframe"
                ],

            "signal_time":
                metadata[
                    "signal_time"
                ],

            "signal_age_seconds":
                metadata[
                    "signal_age_seconds"
                ],

            "symbol":
                signal.symbol.upper(),

            "action":
                signal.action.upper(),

            "entry":
                signal.entry,

            "target":
                signal.target,

            "stop":
                signal.stop,

            "test_mode":
                False,

            "prelive":
                True,

            "worker_prelive_dry_run":
                worker_state[
                    "prelive_dry_run"
                ],
        },

        event_key=(
            f"prelive:"
            f"{signal_id}:received"
        ),
    )

    core.record_event(
        db_file=
            core.DB_FILE,

        signal_id=
            signal_id,

        event_type=
            "PRELIVE_RISK_APPROVED",

        source=
            "prelive_api",

        message=(
            "PRELIVE position sizing "
            "and LIVE safety approved"
        ),

        payload={
            "execution_mode":
                "PRELIVE",

            "quantity":
                sizing[
                    "quantity"
                ],

            "sizing_mode":
                sizing[
                    "sizing_mode"
                ],

            "position_value":
                sizing[
                    "position_value"
                ],

            "planned_risk":
                sizing[
                    "planned_risk"
                ],

            "risk_budget":
                sizing.get(
                    "risk_budget"
                ),

            "capital_budget":
                sizing.get(
                    "capital_budget"
                ),

            "account_equity":
                sizing.get(
                    "account_equity"
                ),

            "available_funds":
                sizing.get(
                    "available_funds"
                ),

            "live_ready":
                safety.get(
                    "live_ready"
                ),
        },

        event_key=(
            f"prelive:"
            f"{signal_id}:risk"
        ),
    )

    core.record_event(
        db_file=
            core.DB_FILE,

        signal_id=
            signal_id,

        event_type=
            "PRELIVE_SIGNAL_QUEUED",

        source=
            "prelive_api",

        message=(
            "PRELIVE signal inserted "
            "into execution queue"
        ),

        payload={
            "status":
                "QUEUED",

            "test_mode":
                False,

            "execution_mode":
                "PRELIVE",

            "prelive":
                True,
        },

        event_key=(
            f"prelive:"
            f"{signal_id}:queued"
        ),
    )


# ============================================================
# HEALTH
# ============================================================

@app.get(
    "/health"
)
def health():
    worker = (
        worker_prelive_state()
    )

    guard_state = (
        get_execution_mode_guard_state(
            core.DB_FILE
        )
    )

    try:
        safety = (
            build_prelive_safety()
        )

        live_ready = (
            safety.get(
                "live_ready",
                False,
            )
        )

        blockers = list(
            safety.get(
                "blockers",
                [],
            )
        )

    except Exception as exc:
        live_ready = False

        blockers = [
            (
                f"{type(exc).__name__}: "
                f"{exc}"
            )
        ]

        safety = {}

    if not guard_state.get(
        "ready",
        False,
    ):
        live_ready = False

        blockers.insert(
            0,
            (
                "Execution-mode guard "
                "is not ready"
            ),
        )

    overall_ready = (
        worker[
            "safe"
        ]

        and
        guard_state.get(
            "ready",
            False,
        )

        and
        live_ready
    )

    return {
        "status":
            (
                "ok"
                if overall_ready
                else
                "blocked"
            ),

        "mode":
            "PRELIVE",

        "test_mode":
            False,

        "prelive":
            True,

        "execution_mode":
            "PRELIVE",

        "worker":
            worker,

        "execution_mode_guard":
            guard_state,

        "live_ready":
            live_ready,

        "blockers":
            blockers,

        "broker": {
            "tws_connected":
                safety.get(
                    "tws_connected"
                ),

            "snapshot_age_seconds":
                safety.get(
                    "snapshot_age_seconds"
                ),

            "net_liquidation":
                safety.get(
                    "net_liquidation"
                ),

            "available_funds":
                safety.get(
                    "available_funds"
                ),

            "daily_pnl":
                safety.get(
                    "daily_pnl"
                ),
        },

        "positions": {
            "managed":
                safety.get(
                    "managed_position_count"
                ),

            "legacy":
                safety.get(
                    "legacy_position_count"
                ),

            "total":
                safety.get(
                    "total_broker_position_count"
                ),
        },

        "trading_day":
            safety.get(
                "trading_day"
            ),

        "evaluated_test_mode":
            safety.get(
                "evaluated_test_mode"
            ),
    }


# ============================================================
# PRELIVE WEBHOOK
# ============================================================

@app.post(
    "/webhook/tradingview"
)
def prelive_webhook(
    signal:
        core.TradeSignal,
):
    # --------------------------------------------------------
    # PERSISTENT DB GUARD
    # --------------------------------------------------------

    guard_state = (
        enforce_execution_guard()
    )


    # --------------------------------------------------------
    # WORKER HARD GATE
    # --------------------------------------------------------

    worker_state = (
        enforce_worker_prelive()
    )


    # --------------------------------------------------------
    # SECRET
    # --------------------------------------------------------

    core.validate_secret(
        signal.secret
    )

    signal_id = (
        signal.signal_id
        or
        ""
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


    # --------------------------------------------------------
    # CORE VALIDATION
    # --------------------------------------------------------

    core.validate_prices(
        signal
    )

    metadata = (
        core.validate_signal_contract(
            signal
        )
    )


    # --------------------------------------------------------
    # DUPLICATE
    # --------------------------------------------------------

    enforce_not_duplicate(
        signal_id
    )


    # --------------------------------------------------------
    # KILL SWITCH
    # --------------------------------------------------------

    control = (
        core.get_control_state(
            core.DB_FILE
        )
    )

    if control[
        "kill_switch"
    ]:
        raise HTTPException(
            status_code=409,

            detail={
                "message":
                    "PRELIVE blocked",

                "reason":
                    "kill switch enabled",

                "kill_switch_reason":
                    control.get(
                        "reason"
                    ),
            },
        )


    # --------------------------------------------------------
    # POSITION SIZING
    # --------------------------------------------------------

    sizing = (
        core.resolve_position_size(
            signal
        )
    )


    # --------------------------------------------------------
    # LIVE SAFETY
    # --------------------------------------------------------

    safety = (
        enforce_prelive_safety()
    )


    # --------------------------------------------------------
    # RECHECK PERSISTENT GUARD + WORKER JUST BEFORE INSERT
    # --------------------------------------------------------

    guard_state = (
        enforce_execution_guard()
    )

    worker_state = (
        enforce_worker_prelive()
    )


    # --------------------------------------------------------
    # INSERT PRELIVE
    # --------------------------------------------------------

    timestamp = (
        insert_prelive_signal(
            signal=
                signal,

            signal_id=
                signal_id,

            metadata=
                metadata,

            sizing=
                sizing,
        )
    )


    # --------------------------------------------------------
    # AUDIT
    # --------------------------------------------------------

    record_prelive_events(
        signal=
            signal,

        signal_id=
            signal_id,

        metadata=
            metadata,

        sizing=
            sizing,

        worker_state=
            worker_state,

        safety=
            safety,
    )


    return {
        "accepted":
            True,

        "mode":
            "PRELIVE",

        "execution_mode":
            "PRELIVE",

        "execution_mode_guard_ready":
            guard_state.get(
                "ready"
            ),

        "prelive":
            True,

        "test_mode":
            False,

        "signal_id":
            signal_id,

        "strategy":
            metadata[
                "strategy"
            ],

        "timeframe":
            metadata[
                "timeframe"
            ],

        "signal_time":
            metadata[
                "signal_time"
            ],

        "signal_age_seconds":
            metadata[
                "signal_age_seconds"
            ],

        "symbol":
            signal.symbol.upper(),

        "action":
            signal.action.upper(),

        "quantity":
            sizing[
                "quantity"
            ],

        "sizing_mode":
            sizing[
                "sizing_mode"
            ],

        "planned_risk":
            sizing[
                "planned_risk"
            ],

        "position_value":
            sizing[
                "position_value"
            ],

        "worker_prelive_dry_run":
            worker_state[
                "prelive_dry_run"
            ],

        "live_ready":
            safety.get(
                "live_ready"
            ),

        "queued_at":
            timestamp,
    }
