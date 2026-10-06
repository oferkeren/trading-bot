import os
import json
import secrets
import sqlite3

from datetime import datetime, timezone
from typing import Optional, Union

from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    HTTPException,
    Depends,
)

from fastapi.responses import HTMLResponse

from fastapi.security import (
    HTTPBasic,
    HTTPBasicCredentials,
)

from pydantic import BaseModel

from risk_engine import (
    calculate_position_size,
    RiskError,
)

from safety_controller import (
    evaluate_live_readiness,
)

from system_state import (
    get_control_state,
    set_kill_switch,
)

from execution_guard import (
    check_pre_execution,
    ExecutionBlocked,
)

from position_policy import (
    evaluate_position_limits,
    count_trades_today,
    PositionPolicyError,
)

from signal_contract import (
    validate_signal_metadata,
    validate_price_structure,
    SignalContractError,
)

from trade_state import (
    init_trade_state,
    record_event,
    transition_signal,
    InvalidStateTransition,
)


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db",
)

DASHBOARD_FILE = os.path.join(
    BASE_DIR,
    "dashboard.html",
)


WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "",
)

DASHBOARD_USER = os.getenv(
    "DASHBOARD_USER",
    "",
)

DASHBOARD_PASSWORD = os.getenv(
    "DASHBOARD_PASSWORD",
    "",
)


LIVE_TRADING = (
    os.getenv(
        "LIVE_TRADING",
        "false",
    ).lower()
    == "true"
)

WEBHOOK_TEST_MODE = (
    os.getenv(
        "WEBHOOK_TEST_MODE",
        "true",
    ).lower()
    == "true"
)


MAX_POSITION_USD = float(
    os.getenv(
        "MAX_POSITION_USD",
        "500",
    )
)

MAX_RISK_PER_TRADE_USD = float(
    os.getenv(
        "MAX_RISK_PER_TRADE_USD",
        "25",
    )
)

MAX_TRADES_PER_DAY = int(
    os.getenv(
        "MAX_TRADES_PER_DAY",
        "5",
    )
)


# Kept as a backward-compatible fallback.
MAX_OPEN_POSITIONS = int(
    os.getenv(
        "MAX_OPEN_POSITIONS",
        "3",
    )
)


MAX_MANAGED_POSITIONS = int(
    os.getenv(
        "MAX_MANAGED_POSITIONS",
        str(
            MAX_OPEN_POSITIONS
        ),
    )
)

MAX_TOTAL_BROKER_POSITIONS = int(
    os.getenv(
        "MAX_TOTAL_BROKER_POSITIONS",
        "10",
    )
)


MAX_DAILY_LOSS_USD = float(
    os.getenv(
        "MAX_DAILY_LOSS_USD",
        "100",
    )
)


RISK_MODE = os.getenv(
    "RISK_MODE",
    "PERCENT_EQUITY",
).upper()

RISK_PER_TRADE_PCT = float(
    os.getenv(
        "RISK_PER_TRADE_PCT",
        "0.50",
    )
)

MAX_CAPITAL_PER_TRADE_PCT = float(
    os.getenv(
        "MAX_CAPITAL_PER_TRADE_PCT",
        "5.0",
    )
)

MIN_ACCOUNT_EQUITY_USD = float(
    os.getenv(
        "MIN_ACCOUNT_EQUITY_USD",
        "100",
    )
)


STATUS_MAX_AGE_SECONDS = int(
    os.getenv(
        "STATUS_MAX_AGE_SECONDS",
        "30",
    )
)

BLOCK_LIVE_ON_PENDING_CANCEL = (
    os.getenv(
        "BLOCK_LIVE_ON_PENDING_CANCEL",
        "true",
    ).lower()
    == "true"
)


MAX_SIGNAL_AGE_SECONDS = int(
    os.getenv(
        "MAX_SIGNAL_AGE_SECONDS",
        "60",
    )
)

MAX_SIGNAL_FUTURE_SKEW_SECONDS = int(
    os.getenv(
        "MAX_SIGNAL_FUTURE_SKEW_SECONDS",
        "10",
    )
)


app = FastAPI(
    title="TradingMax",
)

security = HTTPBasic()


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


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def column_exists(
    conn,
    table,
    column,
):
    cur = conn.cursor()

    cur.execute(
        f"PRAGMA table_info({table})"
    )

    return column in {
        row[1]
        for row
        in cur.fetchall()
    }


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

    signal_columns = {
        "strategy":
            "TEXT",

        "timeframe":
            "TEXT",

        "signal_time":
            "TEXT",

        "signal_age_seconds":
            "REAL",

        "entry_order_ref":
            "TEXT",

        "target_order_ref":
            "TEXT",

        "stop_order_ref":
            "TEXT",
    }


    for (
        column,
        definition,
    ) in signal_columns.items():

        ensure_column(
            conn,
            "signals",
            column,
            definition,
        )


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_control (
            id INTEGER PRIMARY KEY CHECK(id=1),
            kill_switch INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            updated_at TEXT
        )
        """
    )


    conn.execute(
        """
        INSERT OR IGNORE INTO system_control (
            id,
            kill_switch,
            reason,
            updated_at
        )
        VALUES (
            1,
            0,
            NULL,
            ?
        )
        """,
        (
            now_iso(),
        ),
    )


    conn.commit()
    conn.close()


    init_trade_state(
        DB_FILE
    )


init_db()


# ============================================================
# MODELS
# ============================================================

class TradeSignal(
    BaseModel
):
    secret: str

    signal_id: str

    strategy: str

    timeframe: str

    signal_time: Union[
        str,
        int,
        float,
    ]

    symbol: str

    action: str = "BUY"

    entry: float

    target: float

    stop: float

    quantity: Optional[int] = None


class CancelRequest(
    BaseModel
):
    secret: str


class KillSwitchRequest(
    BaseModel
):
    enabled: bool

    reason: Optional[str] = None


# ============================================================
# AUTH
# ============================================================

def dashboard_auth(
    credentials:
        HTTPBasicCredentials
        =
        Depends(
            security
        ),
):
    if (
        not DASHBOARD_USER
        or
        not DASHBOARD_PASSWORD
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Dashboard authentication "
                "not configured"
            ),
        )


    username_ok = secrets.compare_digest(
        credentials.username,
        DASHBOARD_USER,
    )

    password_ok = secrets.compare_digest(
        credentials.password,
        DASHBOARD_PASSWORD,
    )


    if (
        not username_ok
        or
        not password_ok
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={
                "WWW-Authenticate":
                    "Basic",
            },
        )


    return credentials.username


def validate_secret(
    value,
):
    if (
        not WEBHOOK_SECRET
        or
        not secrets.compare_digest(
            value,
            WEBHOOK_SECRET,
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid secret",
        )


# ============================================================
# STRICT SNAPSHOT PARSING
# ============================================================

def strict_json_list(
    value,
):
    if value is None:
        return None


    try:
        parsed = json.loads(
            value
        )

    except Exception:
        return None


    if not isinstance(
        parsed,
        list,
    ):
        return None


    return parsed


def get_runtime_snapshot():
    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
        """
        SELECT *
        FROM runtime_status
        WHERE id=1
        """
    )


    row = cur.fetchone()

    conn.close()


    if row is None:
        return None


    data = dict(
        row
    )


    positions = strict_json_list(
        data.get(
            "positions_json"
        )
    )


    open_orders = strict_json_list(
        data.get(
            "open_orders_json"
        )
    )


    data[
        "positions_valid"
    ] = (
        positions
        is not None
    )

    data[
        "open_orders_valid"
    ] = (
        open_orders
        is not None
    )


    #
    # IMPORTANT:
    #
    # Invalid JSON stays None.
    #
    # We do NOT silently turn malformed broker data
    # into an empty account.
    #
    data[
        "positions"
    ] = positions

    data[
        "open_orders"
    ] = open_orders


    data[
        "tws_connected"
    ] = bool(
        data.get(
            "tws_connected"
        )
    )


    return data


# ============================================================
# SAFETY
# ============================================================

def build_safety_state():
    snapshot = (
        get_runtime_snapshot()
        or {}
    )


    control = get_control_state(
        DB_FILE
    )


    extra_blockers = []


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
        else []
    )

    open_orders = (
        snapshot.get(
            "open_orders"
        )
        if open_orders_valid
        else []
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
    # NEW YORK TRADING DAY
    # --------------------------------------------------------

    try:
        trading_day = (
            count_trades_today(
                DB_FILE
            )
        )

        today_count = trading_day[
            "count"
        ]


    except Exception as exc:
        #
        # Fail closed.
        #
        trading_day = None

        today_count = (
            MAX_TRADES_PER_DAY
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
            evaluate_position_limits(
                db_file=
                    DB_FILE,

                broker_positions=
                    positions,

                max_managed_positions=
                    MAX_MANAGED_POSITIONS,

                max_total_broker_positions=
                    MAX_TOTAL_BROKER_POSITIONS,
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


    except PositionPolicyError as exc:
        #
        # Fail closed.
        #
        position_state = None

        managed_positions = []

        extra_blockers.append(
            (
                "Position policy failed: "
                f"{exc}"
            )
        )


    # --------------------------------------------------------
    # EXISTING SAFETY CONTROLLER
    # --------------------------------------------------------
    #
    # Important:
    # positions=managed_positions
    #
    # Legacy holdings do NOT consume the bot's managed limit.
    #
    result = evaluate_live_readiness(
        live_trading=
            LIVE_TRADING,

        test_mode=
            WEBHOOK_TEST_MODE,

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
            STATUS_MAX_AGE_SECONDS,

        positions=
            managed_positions,

        open_orders=
            open_orders,

        max_open_positions=
            MAX_MANAGED_POSITIONS,

        trades_today=
            today_count,

        max_trades_per_day=
            MAX_TRADES_PER_DAY,

        daily_pnl=
            snapshot.get(
                "daily_pnl"
            ),

        max_daily_loss_usd=
            MAX_DAILY_LOSS_USD,

        net_liquidation=
            snapshot.get(
                "net_liquidation"
            ),

        minimum_account_equity=
            MIN_ACCOUNT_EQUITY_USD,

        available_funds=
            snapshot.get(
                "available_funds"
            ),

        block_live_on_pending_cancel=
            BLOCK_LIVE_ON_PENDING_CANCEL,
    )


    # --------------------------------------------------------
    # EXTRA FAIL-CLOSED BLOCKERS
    # --------------------------------------------------------

    for blocker in extra_blockers:

        if blocker not in result[
            "blockers"
        ]:
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


        if message not in result[
            "blockers"
        ]:

            result[
                "blockers"
            ].insert(
                0,
                message,
            )


    #
    # Recalculate readiness after adding our own blockers.
    #
    result[
        "live_ready"
    ] = (
        len(
            result[
                "blockers"
            ]
        )
        == 0
    )


    result[
        "kill_switch"
    ] = control[
        "kill_switch"
    ]

    result[
        "kill_switch_reason"
    ] = control[
        "reason"
    ]

    result[
        "kill_switch_updated_at"
    ] = control[
        "updated_at"
    ]


    result[
        "positions_json_valid"
    ] = positions_valid

    result[
        "open_orders_json_valid"
    ] = open_orders_valid


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
        MAX_MANAGED_POSITIONS
    )

    result[
        "max_total_broker_positions"
    ] = (
        MAX_TOTAL_BROKER_POSITIONS
    )


    result[
        "trading_day"
    ] = trading_day


    return result


def enforce_live_safety():
    #
    # Test-mode webhook never creates a live signal.
    #
    if WEBHOOK_TEST_MODE:
        return


    #
    # Authoritative LIVE gate.
    #
    # This is intentionally the SAME execution guard
    # used by worker.py.
    #
    try:
        check_pre_execution(
            db_file=
                DB_FILE,

            max_snapshot_age_seconds=
                STATUS_MAX_AGE_SECONDS,

            max_open_positions=
                MAX_MANAGED_POSITIONS,

            max_trades_per_day=
                MAX_TRADES_PER_DAY,

            max_daily_loss_usd=
                MAX_DAILY_LOSS_USD,

            minimum_account_equity=
                MIN_ACCOUNT_EQUITY_USD,

            block_on_pending_cancel=
                BLOCK_LIVE_ON_PENDING_CANCEL,
        )


    except ExecutionBlocked as exc:

        raise HTTPException(
            status_code=409,

            detail={
                "message":
                    "LIVE trading blocked",

                "blockers": [
                    str(
                        exc
                    )
                ],
            },
        )


# ============================================================
# SIGNAL VALIDATION
# ============================================================

def validate_prices(
    signal,
):
    symbol = (
        signal.symbol
        or ""
    ).strip().upper()


    if not symbol:
        raise HTTPException(
            status_code=400,
            detail="symbol is required",
        )


    if len(
        symbol
    ) > 16:
        raise HTTPException(
            status_code=400,
            detail="Invalid symbol",
        )


    try:
        return validate_price_structure(
            action=
                signal.action,

            entry=
                signal.entry,

            stop=
                signal.stop,

            target=
                signal.target,
        )


    except SignalContractError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(
                exc
            ),
        )


def validate_signal_contract(
    signal,
):
    try:
        return validate_signal_metadata(
            strategy=
                signal.strategy,

            timeframe=
                signal.timeframe,

            signal_time=
                signal.signal_time,

            max_age_seconds=
                MAX_SIGNAL_AGE_SECONDS,

            max_future_skew_seconds=
                MAX_SIGNAL_FUTURE_SKEW_SECONDS,

            action=
                signal.action,
        )


    except SignalContractError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(
                exc
            ),
        )


# ============================================================
# POSITION SIZE
# ============================================================

def calculate_dynamic_budget(
    signal,
    snapshot,
):
    try:
        return calculate_position_size(
            action=
                signal.action,

            entry=
                signal.entry,

            stop=
                signal.stop,

            net_liquidation=
                snapshot.get(
                    "net_liquidation"
                ),

            available_funds=
                snapshot.get(
                    "available_funds"
                ),

            risk_mode=
                RISK_MODE,

            risk_per_trade_pct=
                RISK_PER_TRADE_PCT,

            max_capital_per_trade_pct=
                MAX_CAPITAL_PER_TRADE_PCT,

            max_risk_usd=
                MAX_RISK_PER_TRADE_USD,

            max_position_usd=
                MAX_POSITION_USD,

            minimum_equity_usd=
                MIN_ACCOUNT_EQUITY_USD,
        )


    except (
        RiskError,
        TypeError,
        ValueError,
    ) as exc:

        raise HTTPException(
            status_code=400,
            detail=str(
                exc
            ),
        )


def resolve_position_size(
    signal,
):
    snapshot = (
        get_runtime_snapshot()
    )


    if snapshot is None:

        raise HTTPException(
            status_code=503,
            detail=(
                "Broker snapshot unavailable"
            ),
        )


    dynamic = calculate_dynamic_budget(
        signal,
        snapshot,
    )


    if signal.quantity is not None:

        quantity = int(
            signal.quantity
        )


        if quantity <= 0:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Quantity must be positive"
                ),
            )


        risk_per_share = abs(
            signal.entry
            -
            signal.stop
        )


        position_value = (
            signal.entry
            *
            quantity
        )


        planned_risk = (
            risk_per_share
            *
            quantity
        )


        if (
            position_value
            > MAX_POSITION_USD
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "Manual quantity exceeds "
                    "MAX_POSITION_USD"
                ),
            )


        if (
            planned_risk
            > MAX_RISK_PER_TRADE_USD
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "Manual quantity exceeds "
                    "MAX_RISK_PER_TRADE_USD"
                ),
            )


        if (
            position_value
            >
            dynamic[
                "capital_budget"
            ]
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "Manual quantity exceeds "
                    "current capital budget "
                    f"(${dynamic['capital_budget']:.2f})"
                ),
            )


        if (
            planned_risk
            >
            dynamic[
                "risk_budget"
            ]
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "Manual quantity exceeds "
                    "current risk budget "
                    f"(${dynamic['risk_budget']:.2f})"
                ),
            )


        return {
            "quantity":
                quantity,

            "sizing_mode":
                "MANUAL",

            "risk_per_share":
                risk_per_share,

            "planned_risk":
                planned_risk,

            "position_value":
                position_value,

            "risk_budget":
                dynamic[
                    "risk_budget"
                ],

            "capital_budget":
                dynamic[
                    "capital_budget"
                ],

            "account_equity":
                dynamic[
                    "net_liquidation"
                ],

            "available_funds":
                dynamic[
                    "available_funds"
                ],

            "equity_risk_budget":
                dynamic.get(
                    "equity_risk_budget"
                ),

            "equity_capital_budget":
                dynamic.get(
                    "equity_capital_budget"
                ),
        }


    return {
        **dynamic,

        "sizing_mode":
            "AUTO_RISK",

        "account_equity":
            dynamic[
                "net_liquidation"
            ],
    }


# ============================================================
# HEALTH
# ============================================================

@app.get(
    "/health"
)
def health():
    safety = (
        build_safety_state()
    )

    return {
        "status":
            "ok",

        "mode":
            (
                "TEST"
                if WEBHOOK_TEST_MODE
                else "LIVE"
            ),

        "live_trading":
            LIVE_TRADING,

        "test_mode":
            WEBHOOK_TEST_MODE,

        "live_ready":
            safety.get(
                "live_ready",
                False,
            ),

        "kill_switch":
            safety.get(
                "kill_switch",
                True,
            ),

        "blockers":
            safety.get(
                "blockers",
                [],
            ),

        "signal_max_age_seconds":
            MAX_SIGNAL_AGE_SECONDS,

        "max_managed_positions":
            MAX_MANAGED_POSITIONS,

        "max_total_broker_positions":
            MAX_TOTAL_BROKER_POSITIONS,

        "event_store":
            True,
    }


# ============================================================
# DASHBOARD
# ============================================================

@app.get(
    "/dashboard",
    response_class=HTMLResponse,
)
def dashboard(
    user=Depends(
        dashboard_auth
    ),
):
    with open(
        DASHBOARD_FILE,
        "r",
        encoding="utf-8",
    ) as handle:

        return handle.read()


# ============================================================
# STATUS
# ============================================================

@app.get(
    "/status"
)
def status(
    user=Depends(
        dashboard_auth
    ),
):
    snapshot = (
        get_runtime_snapshot()
        or {}
    )


    safety = (
        build_safety_state()
    )


    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
        """
        SELECT
            status,
            COUNT(*) AS count
        FROM signals
        GROUP BY status
        """
    )


    signal_counts = {
        row[
            "status"
        ]:
            row[
                "count"
            ]

        for row
        in cur.fetchall()
    }


    cur.execute(
        """
        SELECT
            signal_id,

            strategy,
            timeframe,
            signal_time,
            signal_age_seconds,

            symbol,
            action,
            quantity,
            sizing_mode,

            position_value,
            planned_risk,

            status,

            parent_order_id,

            entry_order_ref,
            target_order_ref,
            stop_order_ref,

            updated_at,
            error_message,
            monitor_message,

            entry_fill_price,
            exit_fill_price,
            exit_reason,

            realized_pnl,
            gross_realized_pnl,
            net_realized_pnl

        FROM signals

        ORDER BY updated_at DESC

        LIMIT 20
        """
    )


    recent_signals = [
        dict(
            row
        )

        for row
        in cur.fetchall()
    ]


    cur.execute(
        """
        SELECT
            event_id,
            signal_id,
            event_type,
            source,
            old_status,
            new_status,
            message,
            created_at

        FROM trade_events

        ORDER BY event_id DESC

        LIMIT 30
        """
    )


    recent_events = [
        dict(
            row
        )

        for row
        in cur.fetchall()
    ]


    conn.close()


    return {
        "system": {
            "live_trading":
                LIVE_TRADING,

            "test_mode":
                WEBHOOK_TEST_MODE,

            "live_ready":
                safety[
                    "live_ready"
                ],

            "event_store":
                True,
        },


        "signal_policy": {
            "max_signal_age_seconds":
                MAX_SIGNAL_AGE_SECONDS,

            "max_future_skew_seconds":
                MAX_SIGNAL_FUTURE_SKEW_SECONDS,
        },


        "safety":
            safety,


        "account": {
            "net_liquidation":
                snapshot.get(
                    "net_liquidation"
                ),

            "available_funds":
                snapshot.get(
                    "available_funds"
                ),

            "buying_power":
                snapshot.get(
                    "buying_power"
                ),

            "total_cash_value":
                snapshot.get(
                    "total_cash_value"
                ),

            "excess_liquidity":
                snapshot.get(
                    "excess_liquidity"
                ),

            "daily_pnl":
                snapshot.get(
                    "daily_pnl"
                ),

            "unrealized_pnl":
                snapshot.get(
                    "unrealized_pnl"
                ),

            "realized_pnl":
                snapshot.get(
                    "realized_pnl"
                ),
        },


        "broker": {
            "updated_at":
                snapshot.get(
                    "updated_at"
                ),

            "snapshot_age_seconds":
                safety[
                    "snapshot_age_seconds"
                ],

            "tws_connected":
                snapshot.get(
                    "tws_connected",
                    False,
                ),

            "account":
                snapshot.get(
                    "account"
                ),

            "positions":
                (
                    snapshot.get(
                        "positions"
                    )
                    or []
                ),

            "open_orders":
                (
                    snapshot.get(
                        "open_orders"
                    )
                    or []
                ),

            "last_error":
                snapshot.get(
                    "last_error"
                ),
        },


        "risk": {
            "risk_mode":
                RISK_MODE,

            "risk_per_trade_pct":
                RISK_PER_TRADE_PCT,

            "max_capital_per_trade_pct":
                MAX_CAPITAL_PER_TRADE_PCT,

            "max_risk_per_trade_usd":
                MAX_RISK_PER_TRADE_USD,

            "max_position_usd":
                MAX_POSITION_USD,
        },


        "limits": {
            "max_managed_positions":
                MAX_MANAGED_POSITIONS,

            "max_total_broker_positions":
                MAX_TOTAL_BROKER_POSITIONS,

            "max_open_positions_legacy_fallback":
                MAX_OPEN_POSITIONS,

            "max_trades_per_day":
                MAX_TRADES_PER_DAY,

            "max_daily_loss_usd":
                MAX_DAILY_LOSS_USD,

            "minimum_account_equity":
                MIN_ACCOUNT_EQUITY_USD,
        },


        "summary": {
            "managed_position_count":
                safety.get(
                    "managed_position_count"
                ),

            "legacy_position_count":
                safety.get(
                    "legacy_position_count"
                ),

            "total_broker_position_count":
                safety.get(
                    "total_broker_position_count"
                ),

            #
            # Compatibility field for the existing dashboard.
            #
            "position_count":
                safety.get(
                    "managed_position_count"
                ),

            "open_order_count":
                len(
                    snapshot.get(
                        "open_orders"
                    )
                    or []
                ),

            "pending_cancel_count":
                safety[
                    "pending_cancel_count"
                ],

            "trades_today":
                safety[
                    "trades_today"
                ],

            "signal_counts":
                signal_counts,
        },


        "recent_signals":
            recent_signals,

        "recent_events":
            recent_events,
    }



# ============================================================
# STRATEGY_MODE_API_V2
# ============================================================

STRATEGY_MODE_FILE = os.path.expanduser(
    "~/.cache/tradingmax/strategy_mode.txt"
)

VALID_STRATEGY_MODES = {
    "AUTO",
    "SCALP",
    "MOMENTUM",
    "REBOUND",
    "BOTH",
}


def read_strategy_mode():
    try:
        if os.path.exists(
            STRATEGY_MODE_FILE
        ):
            with open(
                STRATEGY_MODE_FILE,
                "r",
                encoding="utf-8",
            ) as handle:

                mode = (
                    handle
                    .read()
                    .strip()
                    .upper()
                )

                if (
                    mode
                    in
                    VALID_STRATEGY_MODES
                ):
                    return mode

    except Exception as exc:
        print(
            "STRATEGY MODE READ ERROR | "
            f"{type(exc).__name__}: {exc}"
        )

    return "AUTO"


@app.get("/strategy-mode")
def get_strategy_mode(
    user=Depends(
        dashboard_auth
    ),
):
    return {
        "mode":
            read_strategy_mode(),

        "valid_modes":
            [
                "AUTO",
                "SCALP",
                "MOMENTUM",
                "REBOUND",
                "BOTH",
            ],
    }


@app.post("/strategy-mode")
def set_strategy_mode(
    payload: dict,
    user=Depends(
        dashboard_auth
    ),
):
    mode = (
        str(
            payload.get(
                "mode",
                "",
            )
        )
        .strip()
        .upper()
    )

    if (
        mode
        not in
        VALID_STRATEGY_MODES
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid strategy mode",
        )

    directory = os.path.dirname(
        STRATEGY_MODE_FILE
    )

    os.makedirs(
        directory,
        exist_ok=True,
    )

    with open(
        STRATEGY_MODE_FILE,
        "w",
        encoding="utf-8",
    ) as handle:

        handle.write(
            mode
            +
            "\n"
        )

    print(
        "STRATEGY MODE CHANGED | "
        f"{mode}",
        flush=True,
    )

    return {
        "ok":
            True,

        "mode":
            mode,
    }


# ============================================================
# EVENTS
# ============================================================

@app.get(
    "/events/{signal_id}"
)
def signal_events(
    signal_id: str,

    user=Depends(
        dashboard_auth
    ),
):
    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
        """
        SELECT
            event_id,
            event_key,
            signal_id,
            event_type,
            source,
            old_status,
            new_status,
            message,
            payload_json,
            created_at

        FROM trade_events

        WHERE signal_id = ?

        ORDER BY event_id ASC

        LIMIT 500
        """,
        (
            signal_id,
        ),
    )


    result = []


    for row in cur.fetchall():

        item = dict(
            row
        )


        raw_payload = item.pop(
            "payload_json",
            None,
        )


        if raw_payload:

            try:
                item[
                    "payload"
                ] = json.loads(
                    raw_payload
                )

            except Exception:
                item[
                    "payload"
                ] = None

        else:
            item[
                "payload"
            ] = None


        result.append(
            item
        )


    conn.close()


    return result


# ============================================================
# RISK PREVIEW
# ============================================================

@app.get(
    "/risk-preview"
)
def risk_preview(
    entry: float,
    stop: float,
    action: str = "BUY",

    user=Depends(
        dashboard_auth
    ),
):
    class PreviewSignal:
        pass


    signal = PreviewSignal()

    signal.action = action

    signal.entry = entry

    signal.stop = stop

    signal.quantity = None


    return resolve_position_size(
        signal
    )


# ============================================================
# CONTROL
# ============================================================

@app.get(
    "/control"
)
def control(
    user=Depends(
        dashboard_auth
    ),
):
    return get_control_state(
        DB_FILE
    )


@app.post(
    "/control/kill-switch"
)
def control_kill_switch(
    request:
        KillSwitchRequest,

    user=Depends(
        dashboard_auth
    ),
):
    state = set_kill_switch(
        DB_FILE,
        request.enabled,
        request.reason,
    )


    record_event(
        db_file=
            DB_FILE,

        signal_id=
            None,

        event_type=(
            "KILL_SWITCH_ENABLED"
            if request.enabled
            else
            "KILL_SWITCH_DISABLED"
        ),

        source=
            "api",

        message=
            request.reason,

        payload={
            "enabled":
                request.enabled,

            "user":
                user,
        },
    )


    print(
        f"KILL SWITCH | "
        f"enabled="
        f"{state['kill_switch']} | "
        f"reason="
        f"{state['reason']}"
    )


    return state


# ============================================================
# TRADES
# ============================================================

@app.get(
    "/trades"
)
def trades(
    user=Depends(
        dashboard_auth
    ),
):
    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
        """
        SELECT
            signal_id,
            strategy,
            timeframe,

            symbol,
            action,
            quantity,

            entry,
            stop,
            target,

            sizing_mode,

            position_value,
            planned_risk,

            status,

            entry_fill_price,
            exit_fill_price,

            realized_pnl,

            exit_reason,

            updated_at

        FROM signals

        WHERE
            test_mode = 0

            AND parent_order_id
                IS NOT NULL

        ORDER BY updated_at DESC

        LIMIT 100
        """
    )


    rows = [
        dict(
            row
        )

        for row
        in cur.fetchall()
    ]


    conn.close()


    return rows


# ============================================================
# QUEUE
# ============================================================

@app.get(
    "/queue"
)
def queue(
    user=Depends(
        dashboard_auth
    ),
):
    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
        """
        SELECT *
        FROM signals
        ORDER BY created_at DESC
        LIMIT 100
        """
    )


    rows = [
        dict(
            row
        )

        for row
        in cur.fetchall()
    ]


    conn.close()


    return rows


# ============================================================
# WEBHOOK
# ============================================================

@app.post(
    "/webhook/tradingview"
)
def webhook(
    signal:
        TradeSignal,
):
    # --------------------------------------------------------
    # SECRET
    # --------------------------------------------------------

    validate_secret(
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


    # --------------------------------------------------------
    # SIGNAL VALIDATION
    # --------------------------------------------------------

    validate_prices(
        signal
    )


    metadata = (
        validate_signal_contract(
            signal
        )
    )


    # --------------------------------------------------------
    # DUPLICATE CHECK
    # --------------------------------------------------------

    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
        """
        SELECT status
        FROM signals
        WHERE signal_id = ?
        """,
        (
            signal_id,
        ),
    )


    existing = (
        cur.fetchone()
    )


    conn.close()


    if existing is not None:

        raise HTTPException(
            status_code=409,
            detail=(
                "Duplicate signal_id"
            ),
        )


    # --------------------------------------------------------
    # KILL SWITCH
    # --------------------------------------------------------
    #
    # Test-mode signals remain useful for system tests.
    # Kill switch prevents creation of LIVE signals.
    #

    control = get_control_state(
        DB_FILE
    )


    if (
        not WEBHOOK_TEST_MODE
        and
        control[
            "kill_switch"
        ]
    ):

        raise HTTPException(
            status_code=409,
            detail=(
                "LIVE blocked: "
                "kill switch enabled"
            ),
        )


    # --------------------------------------------------------
    # POSITION SIZE
    # --------------------------------------------------------

    sizing = resolve_position_size(
        signal
    )


    # --------------------------------------------------------
    # LIVE INGEST QUANTITY HARD CAP
    # --------------------------------------------------------

    live_ingest_max_qty = int(
        os.getenv(
            "LIVE_INGEST_MAX_QTY",
            "1",
        )
    )

    if (
        not WEBHOOK_TEST_MODE
        and
        int(
            sizing[
                "quantity"
            ]
        )
        >
        live_ingest_max_qty
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"LIVE quantity "
                f"{sizing['quantity']} "
                f"exceeds "
                f"LIVE_INGEST_MAX_QTY="
                f"{live_ingest_max_qty}"
            ),
        )


    # --------------------------------------------------------
    # AUTHORITATIVE LIVE SAFETY GATE
    # --------------------------------------------------------

    enforce_live_safety()


    broker_account = (
        os.getenv(
            "IB_ACCOUNT",
            ""
        )
        .strip()
    )

    try:
        broker_port = int(
            os.getenv(
                "IB_PORT",
                "0"
            )
        )
    except ValueError:
        broker_port = 0

    if (
        not WEBHOOK_TEST_MODE
        and (
            not broker_account
            or broker_port <= 0
        )
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Broker ownership is not configured: "
                "IB_ACCOUNT/IB_PORT"
            ),
        )


    timestamp = now_iso()


    # --------------------------------------------------------
    # INSERT SIGNAL
    # --------------------------------------------------------

    conn = db_connect()

    cur = conn.cursor()


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
                attempts,

                broker_account,
                broker_port,

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

                metadata[
                    "action"
                ],

                sizing[
                    "quantity"
                ],

                signal.entry,

                signal.target,

                signal.stop,

                "QUEUED",

                timestamp,

                timestamp,

                1
                if WEBHOOK_TEST_MODE
                else 0,

                0,

                (
                    broker_account
                    if not WEBHOOK_TEST_MODE
                    else None
                ),

                (
                    broker_port
                    if not WEBHOOK_TEST_MODE
                    else None
                ),

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


    except sqlite3.IntegrityError:

        conn.rollback()


        raise HTTPException(
            status_code=409,
            detail=(
                "Duplicate signal_id"
            ),
        )


    except Exception:

        conn.rollback()

        raise


    finally:

        conn.close()


    # --------------------------------------------------------
    # AUDIT EVENTS
    # --------------------------------------------------------

    record_event(
        db_file=
            DB_FILE,

        signal_id=
            signal_id,

        event_type=
            "SIGNAL_RECEIVED",

        source=
            "api",

        message=(
            "TradingView signal accepted"
        ),

        payload={
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
                metadata[
                    "action"
                ],

            "direction":
                metadata[
                    "direction"
                ],

            "entry":
                signal.entry,

            "target":
                signal.target,

            "stop":
                signal.stop,

            "test_mode":
                WEBHOOK_TEST_MODE,

            "broker_account":
                (
                    broker_account
                    if not WEBHOOK_TEST_MODE
                    else None
                ),

            "broker_port":
                (
                    broker_port
                    if not WEBHOOK_TEST_MODE
                    else None
                ),
        },

        event_key=(
            f"signal:"
            f"{signal_id}:received"
        ),
    )


    record_event(
        db_file=
            DB_FILE,

        signal_id=
            signal_id,

        event_type=
            "RISK_APPROVED",

        source=
            "api",

        message=(
            "Position sizing approved"
        ),

        payload={
            "action":
                metadata[
                    "action"
                ],

            "direction":
                metadata[
                    "direction"
                ],

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
        },

        event_key=(
            f"signal:"
            f"{signal_id}:risk"
        ),
    )


    record_event(
        db_file=
            DB_FILE,

        signal_id=
            signal_id,

        event_type=
            "SIGNAL_QUEUED",

        source=
            "api",

        message=(
            "Signal inserted into queue"
        ),

        payload={
            "status":
                "QUEUED",
        },

        event_key=(
            f"signal:"
            f"{signal_id}:queued"
        ),
    )


    return {
        "accepted":
            True,

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
            metadata[
                "action"
            ],

        "direction":
            metadata[
                "direction"
            ],

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

        "test_mode":
            WEBHOOK_TEST_MODE,
    }


# ============================================================
# CANCEL
# ============================================================

@app.post(
    "/cancel/{signal_id}"
)
def cancel(
    signal_id: str,

    request:
        CancelRequest,
):
    validate_secret(
        request.secret
    )


    conn = db_connect()

    cur = conn.cursor()


    cur.execute(
        """
        SELECT *
        FROM signals
        WHERE signal_id = ?
        """,
        (
            signal_id,
        ),
    )


    row = cur.fetchone()


    conn.close()


    if row is None:

        raise HTTPException(
            status_code=404,
            detail=(
                "Signal not found"
            ),
        )


    signal = dict(
        row
    )


    if (
        signal[
            "parent_order_id"
        ]
        is None
    ):

        raise HTTPException(
            status_code=409,
            detail=(
                "Signal has no "
                "IBKR parent order"
            ),
        )


    current_status = (
        signal[
            "status"
        ]
    )


    if (
        current_status
        == "CANCELLED"
    ):

        return {
            "accepted":
                True,

            "signal_id":
                signal_id,

            "status":
                "CANCELLED",
        }


    if current_status in {
        "CANCEL_REQUESTED",
        "CANCELLING",
        "CANCEL_PENDING",
    }:

        return {
            "accepted":
                True,

            "signal_id":
                signal_id,

            "status":
                current_status,
        }


    allowed = {
        "SUBMITTED",
        "ACCEPTED_WAITING_MARKET",
        "ERROR",
        "UNKNOWN",
        "CANCEL_UNKNOWN",
    }


    if (
        current_status
        not in allowed
    ):

        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot cancel from "
                + current_status
            ),
        )


    try:
        result = transition_signal(
            db_file=
                DB_FILE,

            signal_id=
                signal_id,

            new_status=
                "CANCEL_REQUESTED",

            event_type=
                "CANCEL_REQUESTED",

            source=
                "api",

            message=(
                "Cancellation requested "
                "through API"
            ),

            payload={
                "parent_order_id":
                    signal[
                        "parent_order_id"
                    ],
            },

            force=(
                current_status
                == "CANCEL_UNKNOWN"
            ),
        )


    except InvalidStateTransition as exc:

        raise HTTPException(
            status_code=409,
            detail=str(
                exc
            ),
        )


    return {
        "accepted":
            True,

        "signal_id":
            signal_id,

        "status":
            result[
                "new_status"
            ],
    }
