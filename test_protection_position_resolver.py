from protection_position_resolver import (
    resolve_recovery_position,
)


def check(
    name,
    legacy,
    contracts,
    errors,
    expected,
    con_id=111,
):
    result = resolve_recovery_position(
        account="TEST_ACCOUNT",
        symbol="TEST",
        entry_con_id=con_id,
        legacy_positions=legacy,
        positions_by_contract=contracts,
        identity_errors=errors,
    )

    assert result["status"] == expected, (
        name,
        result,
    )

    print(
        f"PASS | {name:<27} | "
        f"{result['reason']}"
    )


normal_legacy = {
    "TEST": {"quantity": 10},
}

normal_contracts = {
    ("TEST_ACCOUNT", 111): {
        "account": "TEST_ACCOUNT",
        "con_id": 111,
        "symbol": "TEST",
        "quantity": 10,
    },
}


check(
    "NORMAL POSITION",
    normal_legacy,
    normal_contracts,
    [],
    "VERIFIED",
)

check(
    "TWO CONTRACTS",
    normal_legacy,
    {
        **normal_contracts,
        ("TEST_ACCOUNT", 222): {
            "account": "TEST_ACCOUNT",
            "con_id": 222,
            "symbol": "TEST",
            "quantity": 5,
        },
    },
    [],
    "UNKNOWN",
)

check(
    "QUANTITY MISMATCH",
    {"TEST": {"quantity": 5}},
    normal_contracts,
    [],
    "UNKNOWN",
)

check(
    "MISSING CONTRACT",
    normal_legacy,
    {},
    [],
    "UNKNOWN",
)

check(
    "MISSING CONID",
    normal_legacy,
    normal_contracts,
    [],
    "UNKNOWN",
    con_id=0,
)

check(
    "IDENTITY ERROR",
    normal_legacy,
    normal_contracts,
    [{
        "account": "TEST_ACCOUNT",
        "symbol": "TEST",
    }],
    "UNKNOWN",
)

check(
    "NO POSITION",
    {},
    {},
    [],
    "FLAT",
)

print(
    "ALL POSITION RESOLUTION TESTS PASSED"
)
