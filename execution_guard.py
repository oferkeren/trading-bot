import json
import math
import os
import sqlite3

from datetime import (
    datetime,
    timezone
)

from dotenv import load_dotenv

from position_policy import (
    evaluate_position_limits,
    count_trades_today,
    PositionPolicyError
)


# ============================================================
# ENV
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

ENV_FILE = os.path.join(
    BASE_DIR,
    ".env"
)

load_dotenv(
    ENV_FILE
)


class ExecutionBlocked(Exception):
    pass


# ============================================================
# DATABASE
# ============================================================

def db_connect(
    db_file
):
    conn = sqlite3.connect(
        db_file,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    return conn


# ============================================================
# TIME
# ============================================================

def parse_datetime(
    value
):
    if not value:
        return None

    try:
        text = str(
            value
        ).strip()

        if text.endswith(
            "Z"
        ):
            text = (
                text[:-1]
                + "+00:00"
            )

        result = datetime.fromisoformat(
            text
        )

        if result.tzinfo is None:
            result = result.replace(
                tzinfo=timezone.utc
            )

        return result.astimezone(
            timezone.utc
        )

    except Exception:
        return None


def age_seconds(
    value
):
    timestamp = parse_datetime(
        value
    )

    if timestamp is None:
        return None

    return max(
        0.0,
        (
            datetime.now(
                timezone.utc
            )
            - timestamp
        ).total_seconds()
    )


# ============================================================
# STRICT VALUES
# ============================================================

def strict_json_list(
    value,
    field_name
):
    if value is None:
        raise ExecutionBlocked(
            f"{field_name} missing"
        )

    try:
        parsed = json.loads(
            value
        )

    except Exception as exc:
        raise ExecutionBlocked(
            f"{field_name} contains invalid JSON"
        ) from exc

    if not isinstance(
        parsed,
        list
    ):
        raise ExecutionBlocked(
            f"{field_name} must contain a JSON list"
        )

    return parsed


def strict_number(
    value,
    field_name,
    *,
    positive=False
):
    if value is None:
        raise ExecutionBlocked(
            f"{field_name} unavailable"
        )

    try:
        number = float(
            value
        )

    except Exception as exc:
        raise ExecutionBlocked(
            f"{field_name} invalid"
        ) from exc

    if not math.isfinite(
        number
    ):
        raise ExecutionBlocked(
            f"{field_name} is not finite"
        )

    if abs(
        number
    ) > 1e100:
        raise ExecutionBlocked(
            (
                f"{field_name} contains "
                "IBKR sentinel value"
            )
        )

    if (
        positive
        and
        number <= 0
    ):
        raise ExecutionBlocked(
            f"{field_name} must be positive"
        )

    return number


# ============================================================
# CONTROL
# ============================================================

def get_kill_switch(
    db_file
):
    conn = db_connect(
        db_file
    )

    try:
        row = conn.execute(
            """
            SELECT
                kill_switch,
                reason
            FROM system_control
            WHERE id=1
            """
        ).fetchone()

    except sqlite3.Error as exc:
        raise ExecutionBlocked(
            f"system_control unavailable: {exc}"
        ) from exc

    finally:
        conn.close()

    if row is None:
        raise ExecutionBlocked(
            "system_control row missing"
        )

    return {
        "enabled":
            bool(
                row[
                    "kill_switch"
                ]
            ),

        "reason":
            row[
                "reason"
            ]
    }


# ============================================================
# RUNTIME
# ============================================================

def get_runtime_status(
    db_file
):
    conn = db_connect(
        db_file
    )

    try:
        row = conn.execute(
            """
            SELECT *
            FROM runtime_status
            WHERE id=1
            """
        ).fetchone()

    except sqlite3.Error as exc:
        raise ExecutionBlocked(
            f"runtime_status unavailable: {exc}"
        ) from exc

    finally:
        conn.close()

    if row is None:
        raise ExecutionBlocked(
            "runtime_status row missing"
        )

    return dict(
        row
    )


# ============================================================
# WATCHDOG
# ============================================================

def get_component_health(
    db_file,
    max_age_seconds
):
    conn = db_connect(
        db_file
    )

    try:
        rows = conn.execute(
            """
            SELECT
                component,
                healthy,
                critical,
                detail,
                checked_at
            FROM runtime_components
            ORDER BY component
            """
        ).fetchall()

    except sqlite3.Error as exc:
        raise ExecutionBlocked(
            f"runtime watchdog unavailable: {exc}"
        ) from exc

    finally:
        conn.close()

    if not rows:
        raise ExecutionBlocked(
            "runtime watchdog contains no data"
        )

    required = {
        "api",
        "worker",
        "monitor",
        "status",
        "tws",
        "watchdog"
    }

    seen = set()
    blockers = []
    components = []

    for row in rows:
        component = row[
            "component"
        ]

        seen.add(
            component
        )

        heartbeat_age = age_seconds(
            row[
                "checked_at"
            ]
        )

        fresh = (
            heartbeat_age
            is not None
            and
            heartbeat_age
            <= max_age_seconds
        )

        reported_healthy = bool(
            row[
                "healthy"
            ]
        )

        critical = bool(
            row[
                "critical"
            ]
        )

        effective = (
            reported_healthy
            and
            fresh
        )

        components.append({
            "component":
                component,

            "healthy":
                effective,

            "age_seconds":
                heartbeat_age,

            "detail":
                row[
                    "detail"
                ]
        })

        if (
            critical
            and
            not effective
        ):
            if not fresh:
                blockers.append(
                    (
                        "Component heartbeat stale: "
                        f"{component}"
                    )
                )
            else:
                blockers.append(
                    (
                        "Critical component unhealthy: "
                        f"{component}"
                    )
                )

    for component in sorted(
        required
        - seen
    ):
        blockers.append(
            (
                "Critical component missing: "
                f"{component}"
            )
        )

    return {
        "components":
            components,

        "blockers":
            blockers
    }


# ============================================================
# PRE EXECUTION
# ============================================================

def check_pre_execution(
    *,
    db_file,
    max_snapshot_age_seconds,
    max_open_positions,
    max_trades_per_day,
    max_daily_loss_usd,
    minimum_account_equity,
    block_on_pending_cancel
):
    blockers = []

    expected_account = (
        os.getenv(
            "IB_ACCOUNT",
            ""
        ).strip()
    )

    max_managed_positions = int(
        os.getenv(
            "MAX_MANAGED_POSITIONS",
            str(
                max_open_positions
            )
        )
    )

    max_total_positions = int(
        os.getenv(
            "MAX_TOTAL_BROKER_POSITIONS",
            "10"
        )
    )

    if not expected_account:
        blockers.append(
            "IB_ACCOUNT is not configured"
        )

    # --------------------------------------------------------
    # KILL SWITCH
    # --------------------------------------------------------

    try:
        kill = get_kill_switch(
            db_file
        )

        if kill[
            "enabled"
        ]:
            message = (
                "Kill switch enabled"
            )

            if kill[
                "reason"
            ]:
                message += (
                    ": "
                    + str(
                        kill[
                            "reason"
                        ]
                    )
                )

            blockers.append(
                message
            )

    except ExecutionBlocked as exc:
        blockers.append(
            str(
                exc
            )
        )

    # --------------------------------------------------------
    # WATCHDOG
    # --------------------------------------------------------

    try:
        component_state = (
            get_component_health(
                db_file,
                max_snapshot_age_seconds
            )
        )

        blockers.extend(
            component_state[
                "blockers"
            ]
        )

    except ExecutionBlocked as exc:
        blockers.append(
            str(
                exc
            )
        )

        component_state = {
            "components":
                []
        }

    # --------------------------------------------------------
    # RUNTIME SNAPSHOT
    # --------------------------------------------------------

    try:
        snapshot = get_runtime_status(
            db_file
        )

    except ExecutionBlocked as exc:
        blockers.append(
            str(
                exc
            )
        )

        raise ExecutionBlocked(
            "; ".join(
                blockers
            )
        )

    snapshot_account = str(
        snapshot.get(
            "account"
        )
        or ""
    ).strip()

    if not snapshot_account:
        blockers.append(
            "Broker snapshot account missing"
        )

    elif (
        expected_account
        and
        snapshot_account
        != expected_account
    ):
        blockers.append(
            (
                "Broker account mismatch "
                f"({snapshot_account} != "
                f"{expected_account})"
            )
        )

    if not bool(
        snapshot.get(
            "tws_connected"
        )
    ):
        blockers.append(
            "TWS snapshot reports disconnected"
        )

    snapshot_age = age_seconds(
        snapshot.get(
            "updated_at"
        )
    )

    if snapshot_age is None:
        blockers.append(
            "Broker snapshot timestamp invalid"
        )

    elif (
        snapshot_age
        >
        max_snapshot_age_seconds
    ):
        blockers.append(
            (
                "Broker snapshot stale "
                f"({snapshot_age:.1f}s)"
            )
        )

    # --------------------------------------------------------
    # STRICT BROKER DATA
    # --------------------------------------------------------

    try:
        positions = strict_json_list(
            snapshot.get(
                "positions_json"
            ),
            "positions_json"
        )

    except ExecutionBlocked as exc:
        blockers.append(
            str(
                exc
            )
        )

        positions = None

    try:
        open_orders = strict_json_list(
            snapshot.get(
                "open_orders_json"
            ),
            "open_orders_json"
        )

    except ExecutionBlocked as exc:
        blockers.append(
            str(
                exc
            )
        )

        open_orders = None

    # --------------------------------------------------------
    # POSITION POLICY
    # --------------------------------------------------------

    position_state = None

    if positions is not None:
        try:
            position_state = (
                evaluate_position_limits(
                    db_file=db_file,
                    broker_positions=
                        positions,
                    max_managed_positions=
                        max_managed_positions,
                    max_total_broker_positions=
                        max_total_positions
                )
            )

            blockers.extend(
                position_state[
                    "blockers"
                ]
            )

        except PositionPolicyError as exc:
            blockers.append(
                (
                    "Position ownership unavailable: "
                    f"{exc}"
                )
            )

        except Exception as exc:
            blockers.append(
                (
                    "Position ownership check failed: "
                    f"{exc}"
                )
            )

    # --------------------------------------------------------
    # PENDING CANCEL
    # --------------------------------------------------------

    pending_cancel_count = 0

    if open_orders is not None:
        for order in open_orders:

            if not isinstance(
                order,
                dict
            ):
                blockers.append(
                    (
                        "open_orders_json contains "
                        "invalid order entry"
                    )
                )

                continue

            status = str(
                order.get(
                    "status",
                    ""
                )
            ).strip()

            if status == "PendingCancel":
                pending_cancel_count += 1

    if (
        block_on_pending_cancel
        and
        pending_cancel_count > 0
    ):
        blockers.append(
            (
                f"{pending_cancel_count} "
                "broker order(s) PendingCancel"
            )
        )

    # --------------------------------------------------------
    # TRADING-DAY LIMIT
    # --------------------------------------------------------

    try:
        trades = count_trades_today(
            db_file
        )

        trade_count = (
            trades[
                "count"
            ]
        )

        if (
            trade_count
            >= max_trades_per_day
        ):
            blockers.append(
                (
                    "Trading-day limit reached "
                    f"({trade_count}/"
                    f"{max_trades_per_day})"
                )
            )

    except Exception as exc:
        trade_count = None
        trades = None

        blockers.append(
            (
                "Unable to calculate "
                f"trading-day count: {exc}"
            )
        )

    # --------------------------------------------------------
    # DAILY P/L
    # --------------------------------------------------------

    try:
        daily_pnl = strict_number(
            snapshot.get(
                "daily_pnl"
            ),
            "Daily P/L"
        )

        if (
            daily_pnl
            <=
            -abs(
                max_daily_loss_usd
            )
        ):
            blockers.append(
                (
                    "Daily loss limit reached "
                    f"({daily_pnl:.2f})"
                )
            )

    except ExecutionBlocked as exc:
        daily_pnl = None

        blockers.append(
            str(
                exc
            )
        )

    # --------------------------------------------------------
    # EQUITY
    # --------------------------------------------------------

    try:
        equity = strict_number(
            snapshot.get(
                "net_liquidation"
            ),
            "Net liquidation",
            positive=True
        )

        if (
            equity
            < minimum_account_equity
        ):
            blockers.append(
                (
                    "Account equity below minimum "
                    f"({equity:.2f} < "
                    f"{minimum_account_equity:.2f})"
                )
            )

    except ExecutionBlocked as exc:
        equity = None

        blockers.append(
            str(
                exc
            )
        )

    # --------------------------------------------------------
    # AVAILABLE FUNDS
    # --------------------------------------------------------

    try:
        available_funds = strict_number(
            snapshot.get(
                "available_funds"
            ),
            "Available funds",
            positive=True
        )

    except ExecutionBlocked as exc:
        available_funds = None

        blockers.append(
            str(
                exc
            )
        )

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    if blockers:
        raise ExecutionBlocked(
            "; ".join(
                blockers
            )
        )

    if position_state is None:
        raise ExecutionBlocked(
            "Position ownership state unavailable"
        )

    if trades is None:
        raise ExecutionBlocked(
            "Trading-day state unavailable"
        )

    return {
        "passed":
            True,

        "account":
            snapshot_account,

        "snapshot_age_seconds":
            snapshot_age,

        "managed_positions":
            position_state[
                "managed_position_count"
            ],

        "legacy_positions":
            position_state[
                "legacy_position_count"
            ],

        "total_broker_positions":
            position_state[
                "broker_position_count"
            ],

        "max_managed_positions":
            max_managed_positions,

        "max_total_broker_positions":
            max_total_positions,

        "pending_cancel_count":
            pending_cancel_count,

        "trades_today":
            trade_count,

        "trading_day":
            trades,

        "daily_pnl":
            daily_pnl,

        "equity":
            equity,

        "available_funds":
            available_funds,

        "components":
            component_state[
                "components"
            ]
    }
