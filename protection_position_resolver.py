from math import isfinite


def resolve_recovery_position(
    account,
    symbol,
    entry_con_id,
    legacy_positions,
    positions_by_contract,
    identity_errors,
):
    def result(status, reason, quantity=None):
        return {
            "status": status,
            "reason": reason,
            "quantity": quantity,
        }

    if not account or not symbol:
        return result("UNKNOWN", "MISSING_IDENTITY")

    symbol = str(symbol).upper()

    try:
        con_id = int(entry_con_id)
    except (TypeError, ValueError, OverflowError):
        return result("UNKNOWN", "INVALID_CONTRACT")

    if con_id <= 0:
        return result("UNKNOWN", "INVALID_CONTRACT")

    if any(
        e.get("symbol") == symbol
        and e.get("account") == account
        for e in identity_errors
    ):
        return result(
            "UNKNOWN",
            "POSITION_IDENTITY_ERROR",
        )

    matches = [
        p for (acc, _), p in positions_by_contract.items()
        if acc == account
        and p.get("symbol") == symbol
    ]

    if len(matches) > 1:
        return result(
            "UNKNOWN",
            "AMBIGUOUS_SYMBOL",
        )

    legacy = legacy_positions.get(symbol)

    contract_position = positions_by_contract.get(
        (account, con_id)
    )

    if contract_position is None:
        if legacy is not None or matches:
            return result(
                "UNKNOWN",
                "POSITION_CONTRACT_MISMATCH",
            )

        return result(
            "FLAT",
            "NO_MATCHING_POSITION",
            0.0,
        )

    if (
        contract_position.get("account") != account
        or contract_position.get("con_id") != con_id
        or contract_position.get("symbol") != symbol
    ):
        return result(
            "UNKNOWN",
            "POSITION_IDENTITY_MISMATCH",
        )

    if legacy is None:
        return result(
            "UNKNOWN",
            "LEGACY_POSITION_MISSING",
        )

    try:
        quantity = float(
            contract_position["quantity"]
        )

        legacy_quantity = float(
            legacy["quantity"]
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return result(
            "UNKNOWN",
            "INVALID_POSITION_QUANTITY",
        )

    if not (
        isfinite(quantity)
        and isfinite(legacy_quantity)
    ):
        return result(
            "UNKNOWN",
            "INVALID_POSITION_QUANTITY",
        )

    if abs(quantity - legacy_quantity) > 1e-9:
        return result(
            "UNKNOWN",
            "POSITION_QUANTITY_MISMATCH",
        )

    if quantity == 0:
        return result(
            "UNKNOWN",
            "UNEXPECTED_ZERO_POSITION",
        )

    return result(
        "VERIFIED",
        "POSITION_IDENTITY_VERIFIED",
        quantity,
    )
