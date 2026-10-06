from protection_identity import validate_protection_identity


position = {
    "account": "TEST_ACCOUNT",
    "con_id": 12345,
}

tests = [
    (
        "VALID",
        {"account": "TEST_ACCOUNT", "con_id": 12345},
        "IDENTITY_VERIFIED",
    ),
    (
        "WRONG ACCOUNT",
        {"account": "OTHER_ACCOUNT", "con_id": 12345},
        "ACCOUNT_MISMATCH",
    ),
    (
        "WRONG CONTRACT",
        {"account": "TEST_ACCOUNT", "con_id": 99999},
        "CONTRACT_MISMATCH",
    ),
    (
        "MISSING ACCOUNT",
        {"con_id": 12345},
        "ACCOUNT_NOT_VERIFIED",
    ),
    (
        "MISSING CONTRACT",
        {"account": "TEST_ACCOUNT"},
        "CONTRACT_NOT_VERIFIED",
    ),
    (
        "INVALID CONTRACT",
        {"account": "TEST_ACCOUNT", "con_id": 0},
        "CONTRACT_NOT_VERIFIED",
    ),
]

for name, stop_order, expected in tests:
    result = validate_protection_identity(
        position,
        stop_order,
    )

    assert result["reason"] == expected, (
        name,
        result,
    )

    print(f"PASS | {name} | {result['reason']}")

print("ALL 6 IDENTITY TESTS PASSED")
