import math

from signal_contract import (
    normalize_action,
    trade_direction,
)


class RiskError(Exception):
    pass


def safe_float(
    value,
    name
):
    try:
        result = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise RiskError(
            f"{name} must be numeric"
        ) from exc


    if not math.isfinite(
        result
    ):

        raise RiskError(
            f"{name} must be finite"
        )


    return result


def calculate_position_size(
    entry,
    stop,
    net_liquidation,
    available_funds,
    risk_mode,
    risk_per_trade_pct,
    max_capital_per_trade_pct,
    max_risk_usd,
    max_position_usd,
    minimum_equity_usd,
    action="BUY",
):
    try:
        action = normalize_action(
            action
        )

    except Exception as exc:

        raise RiskError(
            str(
                exc
            )
        ) from exc


    direction = trade_direction(
        action
    )


    entry = safe_float(
        entry,
        "Entry"
    )

    stop = safe_float(
        stop,
        "Stop"
    )

    net_liquidation = safe_float(
        net_liquidation,
        "Net liquidation"
    )

    available_funds = safe_float(
        available_funds,
        "Available funds"
    )

    risk_per_trade_pct = safe_float(
        risk_per_trade_pct,
        "Risk per trade percentage"
    )

    max_capital_per_trade_pct = safe_float(
        max_capital_per_trade_pct,
        "Maximum capital per trade percentage"
    )

    max_risk_usd = safe_float(
        max_risk_usd,
        "Maximum risk"
    )

    max_position_usd = safe_float(
        max_position_usd,
        "Maximum position value"
    )

    minimum_equity_usd = safe_float(
        minimum_equity_usd,
        "Minimum account equity"
    )


    # ========================================================
    # BASIC VALIDATION
    # ========================================================

    if entry <= 0:

        raise RiskError(
            "Entry must be greater than zero"
        )


    if stop <= 0:

        raise RiskError(
            "Stop must be greater than zero"
        )


    action = str(
        action
    ).strip().upper()

    if action not in {
        "BUY",
        "SELL",
    }:
        raise RiskError(
            f"Unsupported action: {action!r}"
        )


    if action == "BUY":

        if stop >= entry:

            raise RiskError(
                (
                    "For a LONG trade, "
                    "stop must be below entry"
                )
            )


    else:

        if stop <= entry:

            raise RiskError(
                (
                    "For a SHORT trade, "
                    "stop must be above entry"
                )
            )


    if (
        net_liquidation
        <
        minimum_equity_usd
    ):

        raise RiskError(
            "Account equity below configured minimum"
        )


    if available_funds <= 0:

        raise RiskError(
            "No available funds"
        )


    if risk_per_trade_pct <= 0:

        raise RiskError(
            "Risk per trade percentage must be positive"
        )


    if max_capital_per_trade_pct <= 0:

        raise RiskError(
            (
                "Maximum capital per trade "
                "percentage must be positive"
            )
        )


    if max_risk_usd <= 0:

        raise RiskError(
            "Maximum risk must be positive"
        )


    if max_position_usd <= 0:

        raise RiskError(
            "Maximum position value must be positive"
        )


    risk_per_share = abs(
        entry
        -
        stop
    )


    if risk_per_share <= 0:

        raise RiskError(
            "Risk per share must be greater than zero"
        )


    # ========================================================
    # RISK BUDGET
    # ========================================================

    risk_mode = str(
        risk_mode
        or ""
    ).strip().upper()


    if (
        risk_mode
        == "PERCENT_EQUITY"
    ):

        equity_risk_budget = (
            net_liquidation
            *
            (
                risk_per_trade_pct
                /
                100.0
            )
        )


        risk_budget = min(
            equity_risk_budget,
            max_risk_usd
        )


    elif (
        risk_mode
        == "FIXED_USD"
    ):

        equity_risk_budget = None


        risk_budget = (
            max_risk_usd
        )


    else:

        raise RiskError(
            (
                "Unknown risk mode: "
                f"{risk_mode}"
            )
        )


    if risk_budget <= 0:

        raise RiskError(
            "Risk budget is zero"
        )


    # ========================================================
    # CAPITAL BUDGET
    # ========================================================

    equity_capital_budget = (
        net_liquidation
        *
        (
            max_capital_per_trade_pct
            /
            100.0
        )
    )


    capital_budget = min(
        equity_capital_budget,
        max_position_usd,
        available_funds
    )


    if capital_budget <= 0:

        raise RiskError(
            "Capital budget is zero"
        )


    # ========================================================
    # POSITION SIZE
    # ========================================================

    quantity_by_risk = math.floor(
        risk_budget
        /
        risk_per_share
    )


    quantity_by_capital = math.floor(
        capital_budget
        /
        entry
    )


    quantity = min(
        quantity_by_risk,
        quantity_by_capital
    )


    if quantity < 1:

        raise RiskError(
            (
                "Trade cannot be sized "
                "inside configured risk limits"
            )
        )


    # ========================================================
    # FINAL VALUES
    # ========================================================

    position_value = (
        entry
        *
        quantity
    )


    planned_risk = (
        risk_per_share
        *
        quantity
    )


    return {
        "action":
            action,

        "direction":
            direction,

        "quantity":
            quantity,

        "risk_per_share":
            risk_per_share,

        "risk_budget":
            risk_budget,

        "equity_risk_budget":
            equity_risk_budget,

        "capital_budget":
            capital_budget,

        "equity_capital_budget":
            equity_capital_budget,

        "position_value":
            position_value,

        "planned_risk":
            planned_risk,

        "net_liquidation":
            net_liquidation,

        "available_funds":
            available_funds,

        "risk_mode":
            risk_mode,
    }
