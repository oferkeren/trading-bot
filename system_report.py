import json
import math
import os
import shlex
import sqlite3
import subprocess

from datetime import datetime, timezone

from dotenv import load_dotenv

from position_policy import (
    evaluate_position_limits,
    count_trades_today,
    PositionPolicyError,
)


# ============================================================
# PATHS / ENVIRONMENT
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db"
)


load_dotenv(
    os.path.join(
        BASE_DIR,
        ".env"
    )
)


# ============================================================
# CONFIG
# ============================================================

EXPECTED_ACCOUNT = os.getenv(
    "IB_ACCOUNT",
    ""
).strip()


MAX_MANAGED_POSITIONS = int(
    os.getenv(
        "MAX_MANAGED_POSITIONS",
        os.getenv(
            "MAX_OPEN_POSITIONS",
            "3"
        )
    )
)


MAX_TOTAL_BROKER_POSITIONS = int(
    os.getenv(
        "MAX_TOTAL_BROKER_POSITIONS",
        "10"
    )
)


MAX_TRADES_PER_DAY = int(
    os.getenv(
        "MAX_TRADES_PER_DAY",
        "5"
    )
)


MAX_DAILY_LOSS_USD = float(
    os.getenv(
        "MAX_DAILY_LOSS_USD",
        "100"
    )
)


MIN_ACCOUNT_EQUITY_USD = float(
    os.getenv(
        "MIN_ACCOUNT_EQUITY_USD",
        "100"
    )
)


STATUS_MAX_AGE_SECONDS = int(
    os.getenv(
        "STATUS_MAX_AGE_SECONDS",
        "30"
    )
)


WEBHOOK_TEST_MODE = (
    os.getenv(
        "WEBHOOK_TEST_MODE",
        "true"
    ).lower()
    == "true"
)


LIVE_TRADING = (
    os.getenv(
        "LIVE_TRADING",
        "false"
    ).lower()
    == "true"
)


SERVICES = [
    "trading-bot",
    "trading-worker",
    "trading-monitor",
    "trading-status",
    "trading-watchdog",
    "cloudflared",
]


WORKER_SERVICE = (
    "trading-worker"
)


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    return conn


# ============================================================
# GENERIC HELPERS
# ============================================================

def parse_bool(
    value,
    default=None
):
    if value is None:
        return default


    text = str(
        value
    ).strip().lower()


    if text in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True


    if text in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False


    return default


def parse_time(
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
    timestamp = parse_time(
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
            -
            timestamp
        ).total_seconds()
    )


def safe_number(
    value
):
    try:
        number = float(
            value
        )


        if not math.isfinite(
            number
        ):
            return None


        if abs(
            number
        ) > 1e100:
            return None


        return number


    except Exception:
        return None


def strict_json_list(
    value
):
    if value is None:
        return None


    try:
        result = json.loads(
            value
        )


    except Exception:
        return None


    if not isinstance(
        result,
        list
    ):
        return None


    return result


def print_title(
    title
):
    print()

    print(
        "=" * 78
    )

    print(
        title
    )

    print(
        "=" * 78
    )


# ============================================================
# SYSTEMD HELPERS
# ============================================================

def service_status():
    result = {}


    for service in SERVICES:
        try:
            proc = subprocess.run(
                [
                    "systemctl",
                    "is-active",
                    service,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )


            state = (
                proc.stdout
                or
                proc.stderr
                or ""
            ).strip()


            if not state:
                state = "unknown"


        except Exception as exc:
            state = (
                f"error:{exc}"
            )


        result[
            service
        ] = state


    return result


def read_service_environment(
    service
):
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                service,
                "-p",
                "Environment",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )


        if proc.returncode != 0:
            message = (
                proc.stderr
                or proc.stdout
                or "systemctl failed"
            ).strip()


            return {
                "ok":
                    False,

                "values":
                    {},

                "error":
                    message,
            }


        raw = (
            proc.stdout
            or ""
        ).strip()


        values = {}


        if raw:
            try:
                parts = shlex.split(
                    raw
                )


            except Exception:
                parts = raw.split()


            for part in parts:
                if "=" not in part:
                    continue


                key, value = part.split(
                    "=",
                    1
                )


                key = key.strip()


                if not key:
                    continue


                values[
                    key
                ] = value


        return {
            "ok":
                True,

            "values":
                values,

            "error":
                None,
        }


    except Exception as exc:
        return {
            "ok":
                False,

            "values":
                {},

            "error":
                str(
                    exc
                ),
        }


def resolve_worker_prelive_mode():
    service_env = read_service_environment(
        WORKER_SERVICE
    )


    if service_env[
        "ok"
    ]:

        raw = service_env[
            "values"
        ].get(
            "PRELIVE_DRY_RUN"
        )


        if raw is not None:
            parsed = parse_bool(
                raw,
                default=None
            )


            if parsed is not None:
                return {
                    "value":
                        parsed,

                    "source":
                        "systemd",

                    "raw":
                        raw,

                    "error":
                        None,
                }


            return {
                "value":
                    True,

                "source":
                    "systemd-invalid",

                "raw":
                    raw,

                "error":
                    (
                        "Invalid PRELIVE_DRY_RUN "
                        f"value in systemd: {raw}"
                    ),
            }


    env_raw = os.getenv(
        "PRELIVE_DRY_RUN"
    )


    if env_raw is not None:
        parsed = parse_bool(
            env_raw,
            default=None
        )


        if parsed is not None:
            return {
                "value":
                    parsed,

                "source":
                    ".env/process",

                "raw":
                    env_raw,

                "error":
                    (
                        None
                        if service_env["ok"]
                        else service_env["error"]
                    ),
            }


        return {
            "value":
                True,

            "source":
                ".env-invalid",

            "raw":
                env_raw,

            "error":
                (
                    "Invalid PRELIVE_DRY_RUN "
                    f"value: {env_raw}"
                ),
        }


    #
    # worker.py itself defaults PRELIVE_DRY_RUN to true.
    #
    # The report must fail closed as well.
    #
    return {
        "value":
            True,

        "source":
            "safe-default",

        "raw":
            None,

        "error":
            (
                None
                if service_env["ok"]
                else service_env["error"]
            ),
    }


# ============================================================
# RUNTIME DATABASE
# ============================================================

def read_runtime():
    conn = db_connect()


    try:
        row = conn.execute(
            """
            SELECT *
            FROM runtime_status
            WHERE id=1
            """
        ).fetchone()


    finally:
        conn.close()


    if row is None:
        return None


    return dict(
        row
    )


def read_components():
    conn = db_connect()


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


        return [
            dict(
                row
            )
            for row
            in rows
        ]


    except sqlite3.Error:
        return []


    finally:
        conn.close()


def read_control():
    conn = db_connect()


    try:
        row = conn.execute(
            """
            SELECT
                kill_switch,
                reason,
                updated_at
            FROM system_control
            WHERE id=1
            """
        ).fetchone()


    finally:
        conn.close()


    if row is None:
        return None


    return dict(
        row
    )


def read_signal_summary():
    conn = db_connect()


    try:
        status_rows = conn.execute(
            """
            SELECT
                status,
                COUNT(*) AS count
            FROM signals
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()


        status_counts = {
            row[
                "status"
            ]:
            row[
                "count"
            ]
            for row
            in status_rows
        }


        active_rows = conn.execute(
            """
            SELECT
                signal_id,
                symbol,
                status,
                parent_order_id,
                parent_perm_id,
                entry_order_ref,
                target_order_ref,
                stop_order_ref,
                updated_at,
                monitor_message,
                error_message
            FROM signals
            WHERE
                status IN (
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
            ORDER BY updated_at DESC
            """
        ).fetchall()


        active = [
            dict(
                row
            )
            for row
            in active_rows
        ]


        event_rows = conn.execute(
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
            LIMIT 20
            """
        ).fetchall()


        events = [
            dict(
                row
            )
            for row
            in event_rows
        ]


        return (
            status_counts,
            active,
            events,
        )


    finally:
        conn.close()


# ============================================================
# MAIN REPORT
# ============================================================

def main():
    blockers = []


    prelive = (
        resolve_worker_prelive_mode()
    )

    PRELIVE_DRY_RUN = bool(
        prelive[
            "value"
        ]
    )


    # ========================================================
    # HEADER
    # ========================================================

    print_title(
        "TRADINGMAX SYSTEM REPORT"
    )


    print(
        f"Generated UTC     : "
        f"{datetime.now(timezone.utc).isoformat()}"
    )

    print(
        f"LIVE_TRADING      : "
        f"{LIVE_TRADING}"
    )

    print(
        f"TEST_MODE         : "
        f"{WEBHOOK_TEST_MODE}"
    )

    print(
        f"PRELIVE_DRY_RUN   : "
        f"{PRELIVE_DRY_RUN}"
    )

    print(
        f"PRELIVE source    : "
        f"{prelive['source']}"
    )

    print(
        f"IB_ACCOUNT        : "
        f"{EXPECTED_ACCOUNT or 'NOT CONFIGURED'}"
    )


    if prelive[
        "error"
    ]:

        print(
            f"PRELIVE warning   : "
            f"{prelive['error']}"
        )


    # ========================================================
    # SERVICES
    # ========================================================

    print_title(
        "SERVICES"
    )


    services = (
        service_status()
    )


    for (
        service,
        state
    ) in services.items():

        marker = (
            "OK"
            if state == "active"
            else "FAIL"
        )


        print(
            f"{marker:4}  "
            f"{service:20} "
            f"{state}"
        )


        if (
            service
            != "cloudflared"
            and
            state
            != "active"
        ):

            blockers.append(
                (
                    "Service unhealthy: "
                    f"{service}={state}"
                )
            )


    # ========================================================
    # WATCHDOG
    # ========================================================

    print_title(
        "WATCHDOG COMPONENTS"
    )


    components = (
        read_components()
    )


    if not components:

        blockers.append(
            "runtime_components unavailable"
        )

        print(
            "FAIL  no watchdog data"
        )


    else:

        for item in components:

            age = age_seconds(
                item[
                    "checked_at"
                ]
            )


            effective = (
                bool(
                    item[
                        "healthy"
                    ]
                )
                and
                age is not None
                and
                age
                <= STATUS_MAX_AGE_SECONDS
            )


            marker = (
                "OK"
                if effective
                else "FAIL"
            )


            age_text = (
                f"{age:.1f}s"
                if age is not None
                else "INVALID"
            )


            print(
                f"{marker:4}  "
                f"{item['component']:12} "
                f"critical="
                f"{item['critical']} "
                f"age={age_text:8} "
                f"{item['detail']}"
            )


            if (
                bool(
                    item[
                        "critical"
                    ]
                )
                and
                not effective
            ):

                blockers.append(
                    (
                        "Critical runtime component "
                        f"unhealthy: "
                        f"{item['component']}"
                    )
                )


    # ========================================================
    # CONTROL
    # ========================================================

    print_title(
        "SYSTEM CONTROL"
    )


    control = (
        read_control()
    )


    if control is None:

        blockers.append(
            "system_control missing"
        )

        print(
            "FAIL  system_control missing"
        )


    else:

        kill_switch = bool(
            control[
                "kill_switch"
            ]
        )


        print(
            f"Kill switch : "
            f"{'ON' if kill_switch else 'OFF'}"
        )

        print(
            f"Reason      : "
            f"{control['reason'] or '-'}"
        )


        if kill_switch:

            blockers.append(
                "Kill switch enabled"
            )


    # ========================================================
    # BROKER SNAPSHOT
    # ========================================================

    print_title(
        "BROKER SNAPSHOT"
    )


    runtime = (
        read_runtime()
    )

    positions = None

    open_orders = None

    position_state = None


    if runtime is None:

        blockers.append(
            "runtime_status missing"
        )

        print(
            "FAIL runtime_status missing"
        )


    else:

        snapshot_age = age_seconds(
            runtime.get(
                "updated_at"
            )
        )


        print(
            f"TWS connected   : "
            f"{bool(runtime.get('tws_connected'))}"
        )

        print(
            f"Account         : "
            f"{runtime.get('account')}"
        )

        print(
            (
                f"Snapshot age    : "
                f"{snapshot_age:.1f}s"
            )
            if snapshot_age is not None
            else
            "Snapshot age    : INVALID"
        )


        if not bool(
            runtime.get(
                "tws_connected"
            )
        ):

            blockers.append(
                "TWS disconnected"
            )


        if (
            snapshot_age is None
            or
            snapshot_age
            > STATUS_MAX_AGE_SECONDS
        ):

            blockers.append(
                "Broker snapshot stale"
            )


        if (
            EXPECTED_ACCOUNT
            and
            runtime.get(
                "account"
            )
            != EXPECTED_ACCOUNT
        ):

            blockers.append(
                (
                    "IBKR account mismatch: "
                    f"{runtime.get('account')} "
                    f"!= "
                    f"{EXPECTED_ACCOUNT}"
                )
            )


        positions = strict_json_list(
            runtime.get(
                "positions_json"
            )
        )


        open_orders = strict_json_list(
            runtime.get(
                "open_orders_json"
            )
        )


        if positions is None:

            blockers.append(
                "positions_json invalid"
            )

            positions = []


        if open_orders is None:

            blockers.append(
                "open_orders_json invalid"
            )

            open_orders = []


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


            blockers.extend(
                position_state[
                    "blockers"
                ]
            )


        except PositionPolicyError as exc:

            blockers.append(
                (
                    "Position policy failed: "
                    f"{exc}"
                )
            )


        daily_pnl = safe_number(
            runtime.get(
                "daily_pnl"
            )
        )


        equity = safe_number(
            runtime.get(
                "net_liquidation"
            )
        )


        available = safe_number(
            runtime.get(
                "available_funds"
            )
        )


        print(
            f"Broker positions : "
            f"{len(positions)}"
        )


        if position_state:

            print(
                f"Managed positions: "
                f"{position_state['managed_position_count']}"
                f"/{MAX_MANAGED_POSITIONS}"
            )

            print(
                f"Legacy positions : "
                f"{position_state['legacy_position_count']}"
            )

            print(
                f"Emergency total  : "
                f"{position_state['broker_position_count']}"
                f"/{MAX_TOTAL_BROKER_POSITIONS}"
            )


        print(
            f"Open orders      : "
            f"{len(open_orders)}"
        )

        print(
            f"Daily P/L        : "
            f"{daily_pnl}"
        )

        print(
            f"Net liquidation  : "
            f"{equity}"
        )

        print(
            f"Available funds  : "
            f"{available}"
        )


        pending_cancel = [
            order
            for order
            in open_orders
            if (
                isinstance(
                    order,
                    dict
                )
                and
                str(
                    order.get(
                        "status",
                        ""
                    )
                ).strip()
                == "PendingCancel"
            )
        ]


        if pending_cancel:

            blockers.append(
                (
                    f"{len(pending_cancel)} "
                    "broker order(s) PendingCancel"
                )
            )


        if daily_pnl is None:

            blockers.append(
                "Daily P/L unavailable"
            )


        elif (
            daily_pnl
            <=
            -abs(
                MAX_DAILY_LOSS_USD
            )
        ):

            blockers.append(
                (
                    "Daily loss limit reached "
                    f"({daily_pnl:.2f})"
                )
            )


        if equity is None:

            blockers.append(
                "Net liquidation unavailable"
            )


        elif (
            equity
            < MIN_ACCOUNT_EQUITY_USD
        ):

            blockers.append(
                (
                    "Account equity below minimum "
                    f"({equity:.2f} < "
                    f"{MIN_ACCOUNT_EQUITY_USD:.2f})"
                )
            )


        if (
            available is None
            or
            available <= 0
        ):

            blockers.append(
                "Available funds invalid"
            )


    # ========================================================
    # POSITIONS
    # ========================================================

    print_title(
        "POSITIONS"
    )


    if position_state:

        for position in (
            position_state[
                "managed_positions"
            ]
        ):

            print(
                f"MANAGED  "
                f"{position['symbol']:8} "
                f"qty="
                f"{position.get('quantity')} "
                f"avg="
                f"{position.get('avg_cost')}"
            )


        for position in (
            position_state[
                "legacy_positions"
            ]
        ):

            print(
                f"LEGACY   "
                f"{position['symbol']:8} "
                f"qty="
                f"{position.get('quantity')} "
                f"avg="
                f"{position.get('avg_cost')}"
            )


    elif positions:

        for position in positions:

            print(
                f"UNKNOWN  "
                f"{position.get('symbol', '?'):8} "
                f"qty="
                f"{position.get('quantity')} "
                f"avg="
                f"{position.get('avg_cost')}"
            )


    else:

        print(
            "No positions"
        )


    # ========================================================
    # OPEN ORDERS
    # ========================================================

    print_title(
        "OPEN ORDERS"
    )


    if not open_orders:

        print(
            "No open orders"
        )


    else:

        for order in open_orders:

            print(
                f"id="
                f"{order.get('order_id')} "
                f"symbol="
                f"{order.get('symbol')} "
                f"status="
                f"{order.get('status')} "
                f"permId="
                f"{order.get('perm_id')} "
                f"ref="
                f"{order.get('order_ref') or '-'}"
            )


    # ========================================================
    # TRADING DAY
    # ========================================================

    print_title(
        "TRADING DAY LIMIT"
    )


    try:

        trading_day = (
            count_trades_today(
                DB_FILE
            )
        )


        today_count = (
            trading_day[
                "count"
            ]
        )


        print(
            f"Trading date : "
            f"{trading_day['trading_date']}"
        )

        print(
            f"Timezone     : "
            f"{trading_day['timezone']}"
        )

        print(
            f"UTC window   : "
            f"{trading_day['start_utc']} -> "
            f"{trading_day['end_utc']}"
        )

        print(
            f"Trades       : "
            f"{today_count}/"
            f"{MAX_TRADES_PER_DAY}"
        )


        if (
            today_count
            >= MAX_TRADES_PER_DAY
        ):

            blockers.append(
                (
                    "Trading-day limit reached "
                    f"({today_count}/"
                    f"{MAX_TRADES_PER_DAY})"
                )
            )


    except Exception as exc:

        blockers.append(
            (
                "Trading-day calculation "
                f"failed: {exc}"
            )
        )

        print(
            f"FAIL: {exc}"
        )


    # ========================================================
    # SIGNALS / EVENTS
    # ========================================================

    (
        status_counts,
        active,
        events,
    ) = read_signal_summary()


    print_title(
        "SIGNAL STATES"
    )


    for (
        status,
        count
    ) in status_counts.items():

        print(
            f"{status:28} "
            f"{count}"
        )


    print_title(
        "ACTIVE / UNRESOLVED SIGNALS"
    )


    if not active:

        print(
            "None"
        )


    else:

        for signal in active:

            print(
                f"{signal['signal_id']:24} "
                f"{signal['symbol']:8} "
                f"{signal['status']:24} "
                f"order="
                f"{signal['parent_order_id']} "
                f"permId="
                f"{signal['parent_perm_id']} "
                f"ref="
                f"{signal['entry_order_ref'] or '-'}"
            )


            if signal.get(
                "monitor_message"
            ):

                print(
                    f"    monitor: "
                    f"{signal['monitor_message']}"
                )


            if signal.get(
                "error_message"
            ):

                print(
                    f"    error  : "
                    f"{signal['error_message']}"
                )


    print_title(
        "LAST 20 EVENTS"
    )


    if not events:

        print(
            "No events"
        )


    else:

        for event in reversed(
            events
        ):

            state = ""


            if (
                event[
                    "old_status"
                ]
                or
                event[
                    "new_status"
                ]
            ):

                state = (
                    " "
                    f"{event['old_status'] or '-'}"
                    " -> "
                    f"{event['new_status'] or '-'}"
                )


            print(
                f"#{event['event_id']} "
                f"{event['created_at']} "
                f"{event['source']:8} "
                f"{event['event_type']:30}"
                f"{state}"
            )


    # ========================================================
    # LIVE READINESS
    # ========================================================

    #
    # WEBHOOK_TEST_MODE=true means signals are still running
    # through TEST mode.
    #
    if WEBHOOK_TEST_MODE:

        blockers.append(
            "WEBHOOK_TEST_MODE=true"
        )


    #
    # LIVE_TRADING must be enabled for the real execution
    # pipeline to be considered live-capable.
    #
    if not LIVE_TRADING:

        blockers.append(
            "LIVE_TRADING=false"
        )


    #
    # PRELIVE_DRY_RUN is a deliberately separate execution
    # barrier inside trading-worker.
    #
    # The value must be read from the worker's systemd
    # environment because it does not necessarily exist in
    # .env.
    #
    if PRELIVE_DRY_RUN:

        blockers.append(
            "PRELIVE_DRY_RUN=true"
        )


    #
    # If systemd explicitly contained a malformed value,
    # fail closed.
    #
    if (
        prelive[
            "source"
        ]
        in {
            "systemd-invalid",
            ".env-invalid",
        }
    ):

        blockers.append(
            "PRELIVE_DRY_RUN configuration invalid"
        )


    unique_blockers = []


    for blocker in blockers:

        if blocker not in (
            unique_blockers
        ):

            unique_blockers.append(
                blocker
            )


    print_title(
        "LIVE READINESS"
    )


    if unique_blockers:

        print(
            "RESULT: BLOCKED"
        )

        print()


        for blocker in (
            unique_blockers
        ):

            print(
                f" - {blocker}"
            )


    else:

        print(
            "RESULT: READY"
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
