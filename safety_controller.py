from datetime import datetime, timezone


def snapshot_age_seconds(
    updated_at
):
    if not updated_at:
        return None

    try:
        value = datetime.fromisoformat(
            updated_at
        )

        if value.tzinfo is None:
            value = value.replace(
                tzinfo=timezone.utc
            )

        return (
            datetime.now(
                timezone.utc
            )
            - value
        ).total_seconds()

    except Exception:
        return None


def evaluate_live_readiness(
    *,
    live_trading,
    test_mode,
    tws_connected,
    snapshot_updated_at,
    snapshot_max_age_seconds,
    positions,
    open_orders,
    max_open_positions,
    trades_today,
    max_trades_per_day,
    daily_pnl,
    max_daily_loss_usd,
    net_liquidation,
    minimum_account_equity,
    available_funds,
    block_live_on_pending_cancel
):
    blockers = []

    warnings = []


    # ========================================================
    # MODE
    # ========================================================

    if not live_trading:
        blockers.append(
            "LIVE_TRADING is disabled"
        )

    if test_mode:
        blockers.append(
            "System is in TEST mode"
        )


    # ========================================================
    # TWS
    # ========================================================

    if not tws_connected:
        blockers.append(
            "TWS is offline"
        )


    # ========================================================
    # SNAPSHOT
    # ========================================================

    age = snapshot_age_seconds(
        snapshot_updated_at
    )


    if age is None:
        blockers.append(
            "Broker snapshot timestamp is unavailable"
        )

    elif (
        age
        > snapshot_max_age_seconds
    ):
        blockers.append(
            (
                "Broker snapshot is stale "
                f"({age:.1f}s)"
            )
        )


    # ========================================================
    # POSITIONS
    # ========================================================

    position_count = len(
        positions or []
    )


    if (
        position_count
        >= max_open_positions
    ):
        blockers.append(
            (
                "Open position limit reached "
                f"({position_count}/"
                f"{max_open_positions})"
            )
        )


    # ========================================================
    # PENDING CANCEL
    # ========================================================

    pending_cancel_orders = [
        order
        for order
        in (
            open_orders or []
        )
        if (
            order.get(
                "status"
            )
            == "PendingCancel"
        )
    ]


    if (
        block_live_on_pending_cancel
        and
        pending_cancel_orders
    ):
        blockers.append(
            (
                f"{len(pending_cancel_orders)} "
                "broker order(s) are PendingCancel"
            )
        )


    # ========================================================
    # DAILY TRADE COUNT
    # ========================================================

    if (
        trades_today
        >= max_trades_per_day
    ):
        blockers.append(
            (
                "Daily trade limit reached "
                f"({trades_today}/"
                f"{max_trades_per_day})"
            )
        )


    # ========================================================
    # DAILY P/L
    # ========================================================

    if daily_pnl is None:
        warnings.append(
            "Daily P/L is unavailable"
        )

    elif (
        daily_pnl
        <= -abs(
            max_daily_loss_usd
        )
    ):
        blockers.append(
            (
                "Daily loss limit reached "
                f"(${daily_pnl:.2f})"
            )
        )


    # ========================================================
    # ACCOUNT EQUITY
    # ========================================================

    if net_liquidation is None:
        blockers.append(
            "Net liquidation value unavailable"
        )

    elif (
        net_liquidation
        < minimum_account_equity
    ):
        blockers.append(
            (
                "Account equity below minimum "
                f"(${net_liquidation:.2f} < "
                f"${minimum_account_equity:.2f})"
            )
        )


    # ========================================================
    # AVAILABLE FUNDS
    # ========================================================

    if available_funds is None:
        blockers.append(
            "Available funds unavailable"
        )

    elif available_funds <= 0:
        blockers.append(
            "No available funds"
        )


    return {
        "live_ready":
            len(blockers) == 0,

        "blockers":
            blockers,

        "warnings":
            warnings,

        "snapshot_age_seconds":
            age,

        "position_count":
            position_count,

        "pending_cancel_count":
            len(
                pending_cancel_orders
            ),

        "trades_today":
            trades_today,

        "daily_pnl":
            daily_pnl
    }
