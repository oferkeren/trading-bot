import os
import sqlite3
import subprocess
import time

from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends
from fastapi.responses import FileResponse

from microcap_readiness_store import read_readiness
from signal_server_core import *
from strategy_status import read_snapshot


DASHBOARD_SERVICES = [
    "trading-bot.service",
    "trading-worker.service",
    "trading-monitor.service",
    "trading-status.service",
    "trading-watchdog.service",
    "trading-signal-runner.service",
    "cloudflared.service",
]


ACTIVE_SIGNAL_STATUSES = {
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
    "ERROR",
}


SIGNAL_DETAIL_COLUMNS = [
    "signal_id",
    "symbol",
    "action",
    "status",
    "quantity",

    "entry",
    "target",
    "stop",

    "entry_fill_price",
    "filled_quantity",

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

    "entry_time",
    "updated_at",
]


def service_state(
    service_name,
):
    try:
        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                service_name,
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )

        state = (
            result.stdout.strip()
            or
            result.stderr.strip()
            or
            "unknown"
        )

        return {
            "name":
                service_name,

            "active":
                state == "active",

            "state":
                state,
        }

    except subprocess.TimeoutExpired:
        return {
            "name":
                service_name,

            "active":
                False,

            "state":
                "timeout",
        }

    except Exception as exc:
        return {
            "name":
                service_name,

            "active":
                False,

            "state":
                (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
        }


def strategy_age_seconds(
    snapshot,
):
    heartbeat = (
        snapshot.get(
            "heartbeat_epoch"
        )
    )

    if heartbeat is None:
        return None

    try:
        return max(
            0.0,
            time.time()
            -
            float(
                heartbeat
            ),
        )

    except Exception:
        return None


def age_seconds_from_iso(
    value,
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
                +
                "+00:00"
            )

        parsed = (
            datetime.fromisoformat(
                text
            )
        )

        if parsed.tzinfo is None:
            parsed = (
                parsed.replace(
                    tzinfo=
                        timezone.utc
                )
            )

        return max(
            0.0,
            (
                datetime.now(
                    timezone.utc
                )
                -
                parsed.astimezone(
                    timezone.utc
                )
            ).total_seconds(),
        )

    except Exception:
        return None


def safe_number(
    value,
):
    if value is None:
        return None

    try:
        value = float(
            value
        )

        if abs(
            value
        ) > 1e100:
            return None

        return value

    except Exception:
        return None


def valid_order_id(
    value,
):
    try:
        value = int(
            value
        )

    except Exception:
        return None

    if value < 0:
        return None

    return value


def valid_perm_id(
    value,
):
    try:
        value = int(
            value
        )

    except Exception:
        return None

    if value <= 0:
        return None

    return value


def clean_ref(
    value,
):
    if value is None:
        return None

    value = str(
        value
    ).strip()

    if not value:
        return None

    return value


def get_position_classification(
    runtime,
):
    positions = (
        runtime.get(
            "positions"
        )
        or
        []
    )

    try:
        return (
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

    except Exception as exc:
        return {
            "broker_position_count":
                len(
                    positions
                ),

            "managed_position_count":
                None,

            "legacy_position_count":
                None,

            "managed_positions":
                [],

            "legacy_positions":
                [],

            "max_managed_positions":
                MAX_MANAGED_POSITIONS,

            "max_total_broker_positions":
                MAX_TOTAL_BROKER_POSITIONS,

            "blockers": [
                (
                    "Position classification "
                    f"failed: {exc}"
                )
            ],
        }


def table_columns(
    conn,
    table,
):
    rows = conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return {
        row[
            1
        ]
        for row
        in rows
    }


def load_signal_details():
    conn = db_connect()

    try:
        available = (
            table_columns(
                conn,
                "signals"
            )
        )

        selected_columns = [
            column
            for column
            in SIGNAL_DETAIL_COLUMNS
            if column in available
        ]

        if (
            "signal_id"
            not in selected_columns
            or
            "symbol"
            not in selected_columns
        ):
            return {}

        placeholders = ",".join(
            "?"
            for _
            in ACTIVE_SIGNAL_STATUSES
        )

        columns_sql = ", ".join(
            selected_columns
        )

        query = (
            f"""
            SELECT
                {columns_sql}

            FROM signals

            WHERE
                test_mode = 0

                AND status IN (
                    {placeholders}
                )

            ORDER BY updated_at DESC
            """
        )

        rows = (
            conn.execute(
                query,
                tuple(
                    ACTIVE_SIGNAL_STATUSES
                )
            )
            .fetchall()
        )

        result = {}

        for row in rows:
            item = dict(
                row
            )

            signal_id = clean_ref(
                item.get(
                    "signal_id"
                )
            )

            if not signal_id:
                continue

            result[
                signal_id
            ] = item

        return result

    except sqlite3.Error:
        return {}

    finally:
        conn.close()


def normalize_order(
    order,
):
    return {
        "order_id":
            valid_order_id(
                order.get(
                    "order_id"
                )
            ),

        "perm_id":
            valid_perm_id(
                order.get(
                    "perm_id"
                )
            ),

        "symbol":
            (
                str(
                    order.get(
                        "symbol"
                    )
                    or
                    ""
                )
                .upper()
                .strip()
            ),

        "action":
            (
                str(
                    order.get(
                        "action"
                    )
                    or
                    ""
                )
                .upper()
                .strip()
            ),

        "order_type":
            (
                str(
                    order.get(
                        "order_type"
                    )
                    or
                    ""
                )
                .upper()
                .strip()
            ),

        "quantity":
            safe_number(
                order.get(
                    "quantity"
                )
            ),

        "status":
            order.get(
                "status"
            ),

        "parent_id":
            valid_order_id(
                order.get(
                    "parent_id"
                )
            ),

        "order_ref":
            clean_ref(
                order.get(
                    "order_ref"
                )
            ),

        "limit_price":
            safe_number(
                order.get(
                    "limit_price"
                )
            ),

        "stop_price":
            safe_number(
                order.get(
                    "stop_price"
                )
            ),

        "price":
            safe_number(
                order.get(
                    "price"
                )
            ),

        "tif":
            order.get(
                "tif"
            ),

        "outside_rth":
            order.get(
                "outside_rth"
            ),

        "transmit":
            order.get(
                "transmit"
            ),
    }


def order_price(
    order,
):
    if not order:
        return None

    price = safe_number(
        order.get(
            "price"
        )
    )

    if price is not None:
        return price

    order_type = (
        str(
            order.get(
                "order_type"
            )
            or
            ""
        )
        .upper()
        .strip()
    )

    if order_type in {
        "STP",
        "STOP",
        "STP LMT",
    }:
        return safe_number(
            order.get(
                "stop_price"
            )
        )

    return safe_number(
        order.get(
            "limit_price"
        )
    )


def find_order_by_ref(
    open_orders,
    order_ref,
):
    wanted = clean_ref(
        order_ref
    )

    if wanted is None:
        return None

    for order in open_orders:
        if (
            clean_ref(
                order.get(
                    "order_ref"
                )
            )
            ==
            wanted
        ):
            return order

    return None


def find_order_by_id(
    open_orders,
    order_id,
):
    wanted = valid_order_id(
        order_id
    )

    if wanted is None:
        return None

    matches = [
        order
        for order
        in open_orders
        if (
            valid_order_id(
                order.get(
                    "order_id"
                )
            )
            ==
            wanted
        )
    ]

    if not matches:
        return None

    if (
        wanted == 0
        and
        len(
            matches
        )
        > 1
    ):
        return None

    return matches[
        0
    ]


def find_order_by_perm_id(
    open_orders,
    perm_id,
):
    wanted = valid_perm_id(
        perm_id
    )

    if wanted is None:
        return None

    for order in open_orders:
        if (
            valid_perm_id(
                order.get(
                    "perm_id"
                )
            )
            ==
            wanted
        ):
            return order

    return None


def find_child_by_parent(
    open_orders,
    *,
    parent_id,
    role,
):
    wanted_parent = (
        valid_order_id(
            parent_id
        )
    )

    if wanted_parent is None:
        return None

    candidates = [
        order
        for order
        in open_orders
        if (
            valid_order_id(
                order.get(
                    "parent_id"
                )
            )
            ==
            wanted_parent
        )
    ]

    if role == "TARGET":
        candidates = [
            order
            for order
            in candidates
            if (
                str(
                    order.get(
                        "order_type"
                    )
                    or
                    ""
                )
                .upper()
                .strip()
                in {
                    "LMT",
                    "LOC",
                }
            )
        ]

    elif role == "STOP":
        candidates = [
            order
            for order
            in candidates
            if (
                str(
                    order.get(
                        "order_type"
                    )
                    or
                    ""
                )
                .upper()
                .strip()
                in {
                    "STP",
                    "STOP",
                    "STP LMT",
                }
            )
        ]

    if len(
        candidates
    ) != 1:
        return None

    return candidates[
        0
    ]


def resolve_signal_order(
    signal,
    open_orders,
    role,
):
    if role == "ENTRY":
        ref_field = (
            "entry_order_ref"
        )

        id_field = (
            "entry_order_id"
        )

        perm_field = (
            "parent_perm_id"
        )

    elif role == "TARGET":
        ref_field = (
            "target_order_ref"
        )

        id_field = (
            "target_order_id"
        )

        perm_field = (
            "target_perm_id"
        )

    elif role == "STOP":
        ref_field = (
            "stop_order_ref"
        )

        id_field = (
            "stop_order_id"
        )

        perm_field = (
            "stop_perm_id"
        )

    else:
        return None

    result = (
        find_order_by_ref(
            open_orders,
            signal.get(
                ref_field
            )
        )
    )

    if result:
        return result

    result = (
        find_order_by_perm_id(
            open_orders,
            signal.get(
                perm_field
            )
        )
    )

    if result:
        return result

    result = (
        find_order_by_id(
            open_orders,
            signal.get(
                id_field
            )
        )
    )

    if result:
        return result

    if role == "ENTRY":
        return (
            find_order_by_id(
                open_orders,
                signal.get(
                    "parent_order_id"
                )
            )
        )

    return (
        find_child_by_parent(
            open_orders,
            parent_id=
                signal.get(
                    "parent_order_id"
                ),
            role=
                role,
        )
    )


def enrich_signal(
    base_signal,
    signal_details,
    open_orders,
):
    signal_id = clean_ref(
        base_signal.get(
            "signal_id"
        )
    )

    full = {}

    if signal_id:
        full.update(
            signal_details.get(
                signal_id,
                {}
            )
        )

    full.update(
        base_signal
    )

    entry_order = (
        resolve_signal_order(
            full,
            open_orders,
            "ENTRY"
        )
    )

    target_order = (
        resolve_signal_order(
            full,
            open_orders,
            "TARGET"
        )
    )

    stop_order = (
        resolve_signal_order(
            full,
            open_orders,
            "STOP"
        )
    )

    entry_fill_price = (
        safe_number(
            full.get(
                "entry_fill_price"
            )
        )
    )

    planned_entry = (
        safe_number(
            full.get(
                "entry"
            )
        )
    )

    planned_target = (
        safe_number(
            full.get(
                "target"
            )
        )
    )

    planned_stop = (
        safe_number(
            full.get(
                "stop"
            )
        )
    )

    actual_entry = (
        entry_fill_price
        if entry_fill_price is not None
        else planned_entry
    )

    broker_target = (
        order_price(
            target_order
        )
    )

    broker_stop = (
        order_price(
            stop_order
        )
    )

    actual_target = (
        broker_target
        if broker_target is not None
        else planned_target
    )

    actual_stop = (
        broker_stop
        if broker_stop is not None
        else planned_stop
    )

    target_status = (
        target_order.get(
            "status"
        )
        if target_order
        else None
    )

    stop_status = (
        stop_order.get(
            "status"
        )
        if stop_order
        else None
    )

    protection_complete = (
        target_order is not None
        and
        stop_order is not None
    )

    return {
        "signal_id":
            signal_id,

        "symbol":
            full.get(
                "symbol"
            ),

        "action":
            full.get(
                "action"
            ),

        "status":
            full.get(
                "status"
            ),

        "quantity":
            safe_number(
                full.get(
                    "quantity"
                )
            ),

        "filled_quantity":
            safe_number(
                full.get(
                    "filled_quantity"
                )
            ),

        "planned_entry":
            planned_entry,

        "entry_fill_price":
            entry_fill_price,

        "entry_price":
            actual_entry,

        "planned_target":
            planned_target,

        "target_price":
            actual_target,

        "target_price_source":
            (
                "BROKER"
                if broker_target is not None
                else
                (
                    "SIGNAL"
                    if planned_target is not None
                    else
                    None
                )
            ),

        "planned_stop":
            planned_stop,

        "stop_price":
            actual_stop,

        "stop_price_source":
            (
                "BROKER"
                if broker_stop is not None
                else
                (
                    "SIGNAL"
                    if planned_stop is not None
                    else
                    None
                )
            ),

        "parent_order_id":
            valid_order_id(
                full.get(
                    "parent_order_id"
                )
            ),

        "entry_order_id":
            (
                entry_order.get(
                    "order_id"
                )
                if entry_order
                else
                valid_order_id(
                    full.get(
                        "entry_order_id"
                    )
                )
            ),

        "target_order_id":
            (
                target_order.get(
                    "order_id"
                )
                if target_order
                else
                valid_order_id(
                    full.get(
                        "target_order_id"
                    )
                )
            ),

        "stop_order_id":
            (
                stop_order.get(
                    "order_id"
                )
                if stop_order
                else
                valid_order_id(
                    full.get(
                        "stop_order_id"
                    )
                )
            ),

        "entry_order_ref":
            (
                entry_order.get(
                    "order_ref"
                )
                if entry_order
                else
                clean_ref(
                    full.get(
                        "entry_order_ref"
                    )
                )
            ),

        "target_order_ref":
            (
                target_order.get(
                    "order_ref"
                )
                if target_order
                else
                clean_ref(
                    full.get(
                        "target_order_ref"
                    )
                )
            ),

        "stop_order_ref":
            (
                stop_order.get(
                    "order_ref"
                )
                if stop_order
                else
                clean_ref(
                    full.get(
                        "stop_order_ref"
                    )
                )
            ),

        "entry_order_status":
            (
                entry_order.get(
                    "status"
                )
                if entry_order
                else None
            ),

        "target_order_status":
            target_status,

        "stop_order_status":
            stop_status,

        "target_order_open":
            target_order
            is not None,

        "stop_order_open":
            stop_order
            is not None,

        "protection_complete":
            protection_complete,

        "updated_at":
            full.get(
                "updated_at"
            ),
    }


def normalize_position(
    position,
    *,
    signal_details,
    open_orders,
):
    quantity = (
        safe_number(
            position.get(
                "quantity"
            )
        )
    )

    if quantity is None:
        side = "UNKNOWN"

    elif quantity > 0:
        side = "LONG"

    elif quantity < 0:
        side = "SHORT"

    else:
        side = "FLAT"

    avg_cost = (
        safe_number(
            position.get(
                "avg_cost"
            )
        )
    )

    market_price = (
        safe_number(
            position.get(
                "market_price"
            )
        )
    )

    market_value = (
        safe_number(
            position.get(
                "market_value"
            )
        )
    )

    daily_pnl = (
        safe_number(
            position.get(
                "daily_pnl"
            )
        )
    )

    unrealized_pnl = (
        safe_number(
            position.get(
                "unrealized_pnl"
            )
        )
    )

    realized_pnl = (
        safe_number(
            position.get(
                "realized_pnl"
            )
        )
    )

    if (
        market_value is None
        and
        market_price is not None
        and
        quantity is not None
    ):
        market_value = (
            market_price
            *
            quantity
        )

    if (
        market_price is None
        and
        market_value is not None
        and
        quantity is not None
        and
        quantity != 0
    ):
        market_price = abs(
            market_value
            /
            quantity
        )

    managed_signals_raw = (
        position.get(
            "managed_signals"
        )
        or
        []
    )

    managed_signals = [
        enrich_signal(
            signal,
            signal_details,
            open_orders,
        )

        for signal
        in managed_signals_raw
    ]

    protection_complete = (
        bool(
            managed_signals
        )
        and
        all(
            signal.get(
                "protection_complete",
                False
            )
            for signal
            in managed_signals
        )
    )

    return {
        "symbol":
            position.get(
                "symbol"
            ),

        "con_id":
            position.get(
                "con_id"
            ),

        "currency":
            position.get(
                "currency"
            ),

        "quantity":
            quantity,

        "side":
            side,

        "avg_cost":
            avg_cost,

        "market_price":
            market_price,

        "market_value":
            market_value,

        "daily_pnl":
            daily_pnl,

        "unrealized_pnl":
            unrealized_pnl,

        "realized_pnl":
            realized_pnl,

        "position_pnl_available":
            bool(
                position.get(
                    "position_pnl_available",
                    (
                        market_price
                        is not None
                        or
                        unrealized_pnl
                        is not None
                    ),
                )
            ),

        "ownership":
            position.get(
                "ownership",
                "UNKNOWN",
            ),

        "managed_signals":
            managed_signals,

        "protection_complete":
            (
                protection_complete
                if managed_signals
                else None
            ),
    }


def read_runtime_components():
    conn = db_connect()

    try:
        cur = conn.cursor()

        cur.execute(
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
        )

        rows = [
            dict(
                row
            )

            for row
            in cur.fetchall()
        ]

    except sqlite3.Error:
        rows = []

    finally:
        conn.close()

    result = []

    for item in rows:
        age = (
            age_seconds_from_iso(
                item.get(
                    "checked_at"
                )
            )
        )

        healthy = bool(
            item.get(
                "healthy"
            )
        )

        critical = bool(
            item.get(
                "critical"
            )
        )

        result.append({
            "component":
                item.get(
                    "component"
                ),

            "healthy":
                healthy,

            "critical":
                critical,

            "detail":
                item.get(
                    "detail"
                ),

            "checked_at":
                item.get(
                    "checked_at"
                ),

            "age_seconds":
                age,

            "fresh":
                (
                    age is not None
                    and
                    age <= 30
                ),

            "effective_healthy":
                (
                    healthy
                    and
                    age is not None
                    and
                    age <= 30
                ),
        })

    return result


@app.get("/microcap-research-panel.js")
def microcap_research_panel_script(user=Depends(dashboard_auth)):
    return FileResponse(
        Path(__file__).with_name("microcap_research_panel.js"),
        media_type="application/javascript",
    )


@app.get("/microcap-research-status")
def microcap_research_status(user=Depends(dashboard_auth)):
    return read_readiness(
        os.environ.get("MICROCAP_READINESS_PATH"),
        now=datetime.now(timezone.utc),
    )


@app.get(
    "/strategy-status"
)
def strategy_status_endpoint(
    user=
        Depends(
            dashboard_auth
        ),
):
    snapshot = (
        read_snapshot()
    )

    age = (
        strategy_age_seconds(
            snapshot
        )
    )

    snapshot[
        "age_seconds"
    ] = age

    snapshot[
        "fresh"
    ] = (
        age is not None
        and
        age <= 120
    )

    return snapshot


@app.get(
    "/dashboard-services"
)
def dashboard_services(
    user=
        Depends(
            dashboard_auth
        ),
):
    services = [
        service_state(
            service
        )

        for service
        in DASHBOARD_SERVICES
    ]

    active_count = sum(
        1

        for item
        in services

        if item[
            "active"
        ]
    )

    return {
        "total":
            len(
                services
            ),

        "active":
            active_count,

        "all_active":
            active_count
            ==
            len(
                services
            ),

        "services":
            services,
    }


@app.get(
    "/dashboard-components"
)
def dashboard_components(
    user=
        Depends(
            dashboard_auth
        ),
):
    components = (
        read_runtime_components()
    )

    critical_failures = [
        item

        for item
        in components

        if (
            item[
                "critical"
            ]
            and
            not item[
                "effective_healthy"
            ]
        )
    ]

    unhealthy = [
        item

        for item
        in components

        if not item[
            "effective_healthy"
        ]
    ]

    return {
        "total":
            len(
                components
            ),

        "healthy":
            (
                len(
                    components
                )
                -
                len(
                    unhealthy
                )
            ),

        "unhealthy":
            len(
                unhealthy
            ),

        "critical_failures":
            len(
                critical_failures
            ),

        "all_critical_healthy":
            len(
                critical_failures
            )
            ==
            0,

        "components":
            components,
    }


@app.get(
    "/dashboard-portfolio"
)
def dashboard_portfolio(
    user=
        Depends(
            dashboard_auth
        ),
):
    runtime = (
        get_runtime_snapshot()
        or
        {}
    )

    raw_open_orders = (
        runtime.get(
            "open_orders"
        )
        or
        []
    )

    open_orders = [
        normalize_order(
            item
        )

        for item
        in raw_open_orders

        if isinstance(
            item,
            dict,
        )
    ]

    signal_details = (
        load_signal_details()
    )

    classification = (
        get_position_classification(
            runtime
        )
    )

    managed = [
        normalize_position(
            item,
            signal_details=
                signal_details,
            open_orders=
                open_orders,
        )

        for item
        in (
            classification.get(
                "managed_positions"
            )
            or
            []
        )
    ]

    legacy = [
        normalize_position(
            item,
            signal_details=
                signal_details,
            open_orders=
                open_orders,
        )

        for item
        in (
            classification.get(
                "legacy_positions"
            )
            or
            []
        )
    ]

    all_positions = (
        managed
        +
        legacy
    )

    total_market_value = sum(
        abs(
            item[
                "market_value"
            ]
        )

        for item
        in all_positions

        if item[
            "market_value"
        ]
        is not None
    )

    total_position_unrealized = sum(
        item[
            "unrealized_pnl"
        ]

        for item
        in all_positions

        if item[
            "unrealized_pnl"
        ]
        is not None
    )

    pnl_available_count = sum(
        1

        for item
        in all_positions

        if item[
            "position_pnl_available"
        ]
    )

    managed_protected_count = sum(
        1

        for item
        in managed

        if item.get(
            "protection_complete"
        )
        is True
    )

    managed_unprotected = [
        {
            "symbol":
                item.get(
                    "symbol"
                ),

            "signal_ids": [
                signal.get(
                    "signal_id"
                )
                for signal
                in (
                    item.get(
                        "managed_signals"
                    )
                    or
                    []
                )
            ],
        }

        for item
        in managed

        if item.get(
            "protection_complete"
        )
        is False
    ]

    return {
        "account":
            runtime.get(
                "account"
            ),

        "snapshot_updated_at":
            runtime.get(
                "updated_at"
            ),

        "managed_positions":
            managed,

        "legacy_positions":
            legacy,

        "open_orders":
            open_orders,

        "managed_count":
            classification.get(
                "managed_position_count"
            ),

        "legacy_count":
            classification.get(
                "legacy_position_count"
            ),

        "total_count":
            classification.get(
                "broker_position_count"
            ),

        "max_managed":
            classification.get(
                "max_managed_positions",
                MAX_MANAGED_POSITIONS,
            ),

        "max_total":
            classification.get(
                "max_total_broker_positions",
                MAX_TOTAL_BROKER_POSITIONS,
            ),

        "position_blockers":
            classification.get(
                "blockers",
                [],
            ),

        "total_market_value":
            total_market_value,

        "total_position_unrealized_pnl":
            total_position_unrealized,

        "pnl_available_count":
            pnl_available_count,

        "managed_protected_count":
            managed_protected_count,

        "managed_unprotected_count":
            len(
                managed_unprotected
            ),

        "managed_unprotected":
            managed_unprotected,
    }


@app.get(
    "/dashboard-summary"
)
def dashboard_summary(
    user=
        Depends(
            dashboard_auth
        ),
):
    runtime = (
        get_runtime_snapshot()
        or
        {}
    )

    safety = (
        build_safety_state()
    )

    control_state = (
        get_control_state(
            DB_FILE
        )
    )

    strategy = (
        read_snapshot()
    )

    strategy_age = (
        strategy_age_seconds(
            strategy
        )
    )

    services = [
        service_state(
            service
        )

        for service
        in DASHBOARD_SERVICES
    ]

    service_active_count = sum(
        1

        for item
        in services

        if item[
            "active"
        ]
    )

    classification = (
        get_position_classification(
            runtime
        )
    )

    components = (
        read_runtime_components()
    )

    critical_component_failures = sum(
        1

        for item
        in components

        if (
            item[
                "critical"
            ]
            and
            not item[
                "effective_healthy"
            ]
        )
    )

    return {
        "system": {
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
                control_state.get(
                    "kill_switch",
                    False,
                ),

            "kill_switch_reason":
                control_state.get(
                    "reason"
                ),
        },


        "broker": {
            "tws_connected":
                runtime.get(
                    "tws_connected",
                    False,
                ),

            "account":
                runtime.get(
                    "account"
                ),

            "snapshot_updated_at":
                runtime.get(
                    "updated_at"
                ),

            "snapshot_age_seconds":
                safety.get(
                    "snapshot_age_seconds"
                ),

            "net_liquidation":
                runtime.get(
                    "net_liquidation"
                ),

            "available_funds":
                runtime.get(
                    "available_funds"
                ),

            "daily_pnl":
                runtime.get(
                    "daily_pnl"
                ),

            "unrealized_pnl":
                runtime.get(
                    "unrealized_pnl"
                ),

            "realized_pnl":
                runtime.get(
                    "realized_pnl"
                ),
        },


        "positions": {
            "managed":
                classification.get(
                    "managed_position_count"
                ),

            "legacy":
                classification.get(
                    "legacy_position_count"
                ),

            "total":
                classification.get(
                    "broker_position_count"
                ),

            "max_managed":
                MAX_MANAGED_POSITIONS,

            "max_total":
                MAX_TOTAL_BROKER_POSITIONS,
        },


        "trading_day":
            safety.get(
                "trading_day"
            ),


        "safety": {
            "blockers":
                safety.get(
                    "blockers",
                    [],
                ),

            "pending_cancel_count":
                safety.get(
                    "pending_cancel_count"
                ),
        },


        "strategy": {
            "phase":
                strategy.get(
                    "phase"
                ),

            "session":
                strategy.get(
                    "session"
                ),

            "cycle":
                strategy.get(
                    "cycle"
                ),

            "directional":
                strategy.get(
                    "directional",
                    0,
                ),

            "universe":
                strategy.get(
                    "universe",
                    0,
                ),

            "eligible":
                strategy.get(
                    "eligible",
                    0,
                ),

            "hot_pool":
                strategy.get(
                    "hot_pool",
                    0,
                ),

            "hot_core":
                strategy.get(
                    "hot_core",
                    0,
                ),

            "hot_rotate":
                strategy.get(
                    "hot_rotate",
                    0,
                ),

            "analyzed":
                strategy.get(
                    "analyzed",
                    0,
                ),

            "qualified":
                strategy.get(
                    "qualified",
                    0,
                ),

            "bridge_dry_run":
                strategy.get(
                    "bridge_dry_run"
                ),

            "bridge_allow_post":
                strategy.get(
                    "bridge_allow_post"
                ),

            "updated_at":
                strategy.get(
                    "updated_at"
                ),

            "age_seconds":
                strategy_age,

            "fresh":
                (
                    strategy_age
                    is not None
                    and
                    strategy_age <= 120
                ),
        },


        "services": {
            "active":
                service_active_count,

            "total":
                len(
                    services
                ),

            "all_active":
                service_active_count
                ==
                len(
                    services
                ),

            "items":
                services,
        },


        "components": {
            "total":
                len(
                    components
                ),

            "critical_failures":
                critical_component_failures,

            "all_critical_healthy":
                critical_component_failures
                ==
                0,
        },
    }
