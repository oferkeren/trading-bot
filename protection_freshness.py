from math import isfinite


def validate_fill_freshness(
    stop_order,
    fill_state,
    snapshot_started_at,
    snapshot_completed_at,
):
    def reject(reason):
        return {
            "valid": False,
            "reason": reason,
        }

    if not stop_order or not fill_state:
        return reject("MISSING_FILL_DATA")

    try:
        order_id = int(stop_order["order_id"])
        fill_order_id = int(fill_state["order_id"])

        order_perm_id = int(stop_order["perm_id"])
        fill_perm_id = int(fill_state["perm_id"])

        received_at = float(fill_state["received_at"])
        started = float(snapshot_started_at)
        completed = float(snapshot_completed_at)

        remaining = float(fill_state["remaining"])

    except (KeyError, TypeError, ValueError, OverflowError):
        return reject("INVALID_FILL_DATA")

    if not all(map(isfinite, (
        received_at,
        started,
        completed,
        remaining,
    ))):
        return reject("INVALID_FILL_DATA")

    if (
        order_id < 0
        or order_perm_id <= 0
        or fill_order_id != order_id
        or fill_perm_id != order_perm_id
    ):
        return reject("ORDER_IDENTITY_MISMATCH")

    if (
        completed < started
        or not started <= received_at <= completed
    ):
        return reject("STALE_FILL_STATE")

    if fill_state.get("status") != stop_order.get("status"):
        return reject("ORDER_STATUS_CONFLICT")

    if remaining < 0:
        return reject("INVALID_REMAINING")

    return {
        "valid": True,
        "reason": "FILL_SNAPSHOT_CONSISTENT",
        "remaining": remaining,
    }
