from math import isfinite


def evaluate_stop_protection(
    position_qty,
    entry_action,
    stop_order,
    stop_fill_state,
):
    """
    Pure validation function.

    Does not connect to IBKR.
    Does not place or cancel orders.
    """

    def fail(reason):
        return {
            "protected": False,
            "reason": reason,
        }

    try:
        position_qty = float(position_qty)

        if not isfinite(position_qty):
            return fail("INVALID_POSITION")

        if position_qty == 0:
            return fail("NO_OPEN_POSITION")

        if entry_action not in {"BUY", "SELL"}:
            return fail("INVALID_ENTRY_ACTION")

        expected_sign = (
            1 if entry_action == "BUY" else -1
        )

        if position_qty * expected_sign <= 0:
            return fail("POSITION_DIRECTION_MISMATCH")

        if not stop_order:
            return fail("STOP_NOT_FOUND")

        if stop_order.get("status") == "PendingCancel":
            return fail("STOP_PENDING_CANCEL")

        if stop_order.get("status") not in {
            "Submitted",
            "PreSubmitted",
        }:
            return fail("STOP_STATUS_NOT_CONFIRMED")

        expected_stop_action = (
            "SELL" if position_qty > 0 else "BUY"
        )

        if stop_order.get("action") != expected_stop_action:
            return fail("STOP_DIRECTION_MISMATCH")

        if stop_order.get("order_type") not in {
            "STP",
            "STP LMT",
        }:
            return fail("INVALID_STOP_TYPE")

        if not stop_fill_state:
            return fail("STOP_REMAINING_UNKNOWN")

        if stop_fill_state.get("status") != (
            stop_order.get("status")
        ):
            return fail("STOP_STATUS_CONFLICT")

        remaining = float(
            stop_fill_state["remaining"]
        )

        if not isfinite(remaining):
            return fail("INVALID_STOP_REMAINING")

        exposure = abs(position_qty)

        if remaining < exposure - 1e-9:
            return fail("STOP_UNDERCOVERED")

        if remaining > exposure + 1e-9:
            return fail("STOP_OVERSIZED")

        return {
            "protected": True,
            "reason": "STOP_COVERAGE_VALIDATED",
            "exposure": exposure,
            "remaining": remaining,
        }

    except (TypeError, ValueError, KeyError):
        return fail("INVALID_PROTECTION_DATA")
