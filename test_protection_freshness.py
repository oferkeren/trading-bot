from protection_freshness import validate_fill_freshness

order = {
    "order_id": 101,
    "perm_id": 9001,
    "status": "Submitted",
}

fill = {
    "order_id": 101,
    "perm_id": 9001,
    "status": "Submitted",
    "remaining": 10,
    "received_at": 150,
}

tests = [
    ("VALID", {}, "FILL_SNAPSHOT_CONSISTENT"),
    ("STALE", {"received_at": 50}, "STALE_FILL_STATE"),
    ("WRONG ORDER", {"order_id": 102}, "ORDER_IDENTITY_MISMATCH"),
    ("WRONG PERM ID", {"perm_id": 9002}, "ORDER_IDENTITY_MISMATCH"),
    ("STATUS CONFLICT", {"status": "PendingCancel"}, "ORDER_STATUS_CONFLICT"),
    ("NEGATIVE REMAINING", {"remaining": -1}, "INVALID_REMAINING"),
    ("MISSING TIMESTAMP", {"received_at": None}, "INVALID_FILL_DATA"),
]

for name, changes, expected in tests:
    test_fill = {**fill, **changes}

    result = validate_fill_freshness(
        order,
        test_fill,
        snapshot_started_at=100,
        snapshot_completed_at=200,
    )

    assert result["reason"] == expected, (name, result)
    print(f"PASS | {name} | {result['reason']}")

print("ALL 7 FRESHNESS TESTS PASSED")
