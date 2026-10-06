from protection_guard import evaluate_live_protection


def base():
    position = {
        "account": "TEST_ACCOUNT",
        "con_id": 111,
        "quantity": 10,
    }

    stop_order = {
        "account": "TEST_ACCOUNT",
        "con_id": 111,
        "order_id": 500,
        "perm_id": 9001,
        "status": "Submitted",
        "action": "SELL",
        "order_type": "STP",
    }

    fill_state = {
        "order_id": 500,
        "perm_id": 9001,
        "status": "Submitted",
        "remaining": 10,
        "received_at": 150,
    }

    return position, stop_order, fill_state


tests = []

# valid
p, s, f = base()
tests.append((
    "VALID",
    p, "BUY", s, f,
    True, "PROTECTION_VERIFIED",
))

# pending cancel
p, s, f = base()
s["status"] = "PendingCancel"
f["status"] = "PendingCancel"
tests.append((
    "PENDING CANCEL",
    p, "BUY", s, f,
    False, "STOP_PENDING_CANCEL",
))

# undercovered
p, s, f = base()
f["remaining"] = 5
tests.append((
    "UNDERCOVERED",
    p, "BUY", s, f,
    False, "STOP_UNDERCOVERED",
))

# wrong stop direction
p, s, f = base()
s["action"] = "BUY"
tests.append((
    "WRONG DIRECTION",
    p, "BUY", s, f,
    False, "STOP_DIRECTION_MISMATCH",
))

# wrong contract
p, s, f = base()
s["con_id"] = 222
tests.append((
    "WRONG CONTRACT",
    p, "BUY", s, f,
    False, "CONTRACT_MISMATCH",
))

# stale fill
p, s, f = base()
f["received_at"] = 50
tests.append((
    "STALE FILL",
    p, "BUY", s, f,
    False, "STALE_FILL_STATE",
))

# short protected
p, s, f = base()
p["quantity"] = -10
s["action"] = "BUY"
tests.append((
    "VALID SHORT",
    p, "SELL", s, f,
    True, "PROTECTION_VERIFIED",
))


for (
    name,
    position,
    action,
    stop_order,
    fill_state,
    expected_protected,
    expected_reason,
) in tests:

    result = evaluate_live_protection(
        position,
        action,
        stop_order,
        fill_state,
        snapshot_started_at=100,
        snapshot_completed_at=200,
    )

    assert result["protected"] == expected_protected, (
        name,
        result,
    )

    assert result["reason"] == expected_reason, (
        name,
        result,
    )

    print(
        f"PASS | {name:<20} | "
        f"{result['reason']}"
    )

print("ALL 7 INTEGRATED PROTECTION TESTS PASSED")
