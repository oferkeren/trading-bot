from protection_identity import validate_protection_identity
from protection_freshness import validate_fill_freshness
from protection_policy import evaluate_stop_protection


def evaluate_live_protection(
    position,
    entry_action,
    stop_order,
    stop_fill_state,
    snapshot_started_at,
    snapshot_completed_at,
):
    identity = validate_protection_identity(
        position,
        stop_order,
    )

    if not identity["valid"]:
        return {
            "protected": False,
            "stage": "IDENTITY",
            "reason": identity["reason"],
        }

    freshness = validate_fill_freshness(
        stop_order,
        stop_fill_state,
        snapshot_started_at,
        snapshot_completed_at,
    )

    if not freshness["valid"]:
        return {
            "protected": False,
            "stage": "FRESHNESS",
            "reason": freshness["reason"],
        }

    coverage = evaluate_stop_protection(
        position["quantity"],
        entry_action,
        stop_order,
        stop_fill_state,
    )

    if not coverage["protected"]:
        return {
            "protected": False,
            "stage": "COVERAGE",
            "reason": coverage["reason"],
        }

    return {
        "protected": True,
        "stage": "OK",
        "reason": "PROTECTION_VERIFIED",
        "account": identity["account"],
        "con_id": identity["con_id"],
        "exposure": coverage["exposure"],
        "remaining": coverage["remaining"],
    }
