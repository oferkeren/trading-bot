import math


class RiskError(Exception):
    pass


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
    minimum_equity_usd
):
    entry = float(entry)
    stop = float(stop)

    net_liquidation = float(
        net_liquidation
    )

    available_funds = float(
        available_funds
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


    if stop >= entry:
        raise RiskError(
            "Stop must be below entry"
        )


    if (
        net_liquidation
        < minimum_equity_usd
    ):
        raise RiskError(
            "Account equity below configured minimum"
        )


    if available_funds <= 0:
        raise RiskError(
            "No available funds"
        )


    risk_per_share = (
        entry - stop
    )


    # ========================================================
    # RISK BUDGET
    # ========================================================

    if (
        risk_mode
        == "PERCENT_EQUITY"
    ):
        equity_risk_budget = (
            net_liquidation
            * (
                risk_per_trade_pct
                / 100.0
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
            f"Unknown risk mode: "
            f"{risk_mode}"
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
        * (
            max_capital_per_trade_pct
            / 100.0
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

    quantity_by_risk = (
        math.floor(
            risk_budget
            / risk_per_share
        )
    )


    quantity_by_capital = (
        math.floor(
            capital_budget
            / entry
        )
    )


    quantity = min(
        quantity_by_risk,
        quantity_by_capital
    )


    if quantity < 1:
        raise RiskError(
            "Trade cannot be sized "
            "inside configured risk limits"
        )


    # ========================================================
    # FINAL VALUES
    # ========================================================

    position_value = (
        entry * quantity
    )


    planned_risk = (
        risk_per_share
        * quantity
    )


    return {
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
            risk_mode
    }
