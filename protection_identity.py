def validate_protection_identity(position, stop_order):
    """
    Pure identity validation.
    No broker connections or order operations.
    """

    def reject(reason):
        return {
            "valid": False,
            "reason": reason,
        }

    if not position or not stop_order:
        return reject("MISSING_BROKER_DATA")

    position_account = position.get("account")
    stop_account = stop_order.get("account")

    if not position_account or not stop_account:
        return reject("ACCOUNT_NOT_VERIFIED")

    if position_account != stop_account:
        return reject("ACCOUNT_MISMATCH")

    try:
        position_conid = int(position.get("con_id"))
        stop_conid = int(stop_order.get("con_id"))
    except (TypeError, ValueError):
        return reject("CONTRACT_NOT_VERIFIED")

    if position_conid <= 0 or stop_conid <= 0:
        return reject("CONTRACT_NOT_VERIFIED")

    if position_conid != stop_conid:
        return reject("CONTRACT_MISMATCH")

    return {
        "valid": True,
        "reason": "IDENTITY_VERIFIED",
        "account": position_account,
        "con_id": position_conid,
    }
