import json
import os
import sqlite3

from trading_clock import trading_day_bounds_utc


BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DEFAULT_DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db"
)


MANAGED_SIGNAL_STATUSES = {
    "SUBMITTED",
    "ACCEPTED_WAITING_MARKET",
    "FILLED",
    "OPEN_POSITION",
    "CANCEL_REQUESTED",
    "CANCELLING",
    "CANCEL_PENDING",
    "CANCEL_UNKNOWN",
    "UNKNOWN",
    "ERROR"
}


class PositionPolicyError(Exception):
    pass


def db_connect(
    db_file=DEFAULT_DB_FILE
):
    conn = sqlite3.connect(
        db_file,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    return conn


def parse_positions_json(
    value
):
    if value is None:
        raise PositionPolicyError(
            "positions_json missing"
        )

    try:
        result = json.loads(
            value
        )

    except Exception as exc:
        raise PositionPolicyError(
            "positions_json contains invalid JSON"
        ) from exc


    if not isinstance(
        result,
        list
    ):
        raise PositionPolicyError(
            "positions_json must be a list"
        )


    for item in result:

        if not isinstance(
            item,
            dict
        ):
            raise PositionPolicyError(
                "positions_json contains invalid position"
            )

        symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).strip()

        if not symbol:
            raise PositionPolicyError(
                "Broker position without symbol"
            )


    return result


def load_runtime_positions(
    db_file=DEFAULT_DB_FILE
):
    conn = db_connect(
        db_file
    )

    try:
        row = conn.execute(
            """
            SELECT positions_json
            FROM runtime_status
            WHERE id=1
            """
        ).fetchone()

    finally:
        conn.close()


    if row is None:
        raise PositionPolicyError(
            "runtime_status missing"
        )


    return parse_positions_json(
        row[
            "positions_json"
        ]
    )


def load_managed_signals(
    db_file=DEFAULT_DB_FILE
):
    conn = db_connect(
        db_file
    )

    try:
        placeholders = ",".join(
            "?"
            for _ in MANAGED_SIGNAL_STATUSES
        )


        rows = conn.execute(
            f"""
            SELECT
                signal_id,
                symbol,
                status,
                parent_order_id,
                parent_perm_id,
                filled_quantity,
                entry_fill_price

            FROM signals

            WHERE
                test_mode=0

                AND status IN (
                    {placeholders}
                )
            """,
            tuple(
                MANAGED_SIGNAL_STATUSES
            )
        ).fetchall()

    finally:
        conn.close()


    result = {}


    for row in rows:

        item = dict(
            row
        )


        symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).upper().strip()


        if not symbol:
            continue


        #
        # A signal cannot own a broker position unless
        # it reached the broker or is in an uncertain
        # post-submission state.
        #
        has_broker_identity = (
            item.get(
                "parent_order_id"
            )
            is not None

            or

            item.get(
                "parent_perm_id"
            )
            is not None
        )


        if not has_broker_identity:
            continue


        result.setdefault(
            symbol,
            []
        ).append(
            item
        )


    return result


def classify_positions(
    *,
    db_file=DEFAULT_DB_FILE,
    broker_positions=None
):
    if broker_positions is None:

        broker_positions = (
            load_runtime_positions(
                db_file
            )
        )


    managed_signals = (
        load_managed_signals(
            db_file
        )
    )


    managed = []

    legacy = []


    for raw_position in broker_positions:

        if not isinstance(
            raw_position,
            dict
        ):
            raise PositionPolicyError(
                "Invalid broker position entry"
            )


        position = dict(
            raw_position
        )


        symbol = str(
            position.get(
                "symbol",
                ""
            )
        ).upper().strip()


        if not symbol:
            raise PositionPolicyError(
                "Broker position without symbol"
            )


        position[
            "symbol"
        ] = symbol


        signals = (
            managed_signals.get(
                symbol,
                []
            )
        )


        position[
            "managed_signals"
        ] = signals


        if signals:

            position[
                "ownership"
            ] = "MANAGED"

            managed.append(
                position
            )

        else:

            position[
                "ownership"
            ] = "LEGACY"

            legacy.append(
                position
            )


    return {
        "broker_position_count":
            len(
                broker_positions
            ),

        "managed_position_count":
            len(
                managed
            ),

        "legacy_position_count":
            len(
                legacy
            ),

        "managed_positions":
            managed,

        "legacy_positions":
            legacy
    }


def count_trades_today(
    db_file=DEFAULT_DB_FILE
):
    bounds = (
        trading_day_bounds_utc()
    )


    conn = db_connect(
        db_file
    )

    try:
        row = conn.execute(
            """
            SELECT COUNT(*)

            FROM signals

            WHERE
                test_mode=0

                AND parent_order_id
                    IS NOT NULL

                AND julianday(created_at)
                    >= julianday(?)

                AND julianday(created_at)
                    < julianday(?)
            """,
            (
                bounds[
                    "start_utc"
                ],
                bounds[
                    "end_utc"
                ]
            )
        ).fetchone()

    finally:
        conn.close()


    return {
        "count":
            int(
                row[0]
            ),

        **bounds
    }


def evaluate_position_limits(
    *,
    db_file=DEFAULT_DB_FILE,
    broker_positions=None,
    max_managed_positions,
    max_total_broker_positions
):
    classification = (
        classify_positions(
            db_file=db_file,
            broker_positions=
                broker_positions
        )
    )


    blockers = []


    managed_count = (
        classification[
            "managed_position_count"
        ]
    )

    total_count = (
        classification[
            "broker_position_count"
        ]
    )


    if (
        managed_count
        >= max_managed_positions
    ):
        blockers.append(
            (
                "Managed position limit reached "
                f"({managed_count}/"
                f"{max_managed_positions})"
            )
        )


    if (
        total_count
        >= max_total_broker_positions
    ):
        blockers.append(
            (
                "Total broker position emergency "
                f"limit reached ({total_count}/"
                f"{max_total_broker_positions})"
            )
        )


    return {
        **classification,

        "max_managed_positions":
            max_managed_positions,

        "max_total_broker_positions":
            max_total_broker_positions,

        "blockers":
            blockers
    }


def main():
    max_managed = int(
        os.getenv(
            "MAX_MANAGED_POSITIONS",
            "3"
        )
    )

    max_total = int(
        os.getenv(
            "MAX_TOTAL_BROKER_POSITIONS",
            "10"
        )
    )


    result = evaluate_position_limits(
        max_managed_positions=
            max_managed,

        max_total_broker_positions=
            max_total
    )


    trades = count_trades_today()


    print(
        "========================================"
    )

    print(
        "TRADINGMAX POSITION POLICY"
    )

    print(
        "========================================"
    )


    print(
        f"Broker positions  : "
        f"{result['broker_position_count']}"
    )

    print(
        f"Managed positions : "
        f"{result['managed_position_count']}"
        f"/{max_managed}"
    )

    print(
        f"Legacy positions  : "
        f"{result['legacy_position_count']}"
    )

    print(
        f"Total emergency   : "
        f"{result['broker_position_count']}"
        f"/{max_total}"
    )


    print()

    print(
        f"Trading date      : "
        f"{trades['trading_date']}"
    )

    print(
        f"Trading timezone  : "
        f"{trades['timezone']}"
    )

    print(
        f"Trading day UTC   : "
        f"{trades['start_utc']} -> "
        f"{trades['end_utc']}"
    )

    print(
        f"Trades today      : "
        f"{trades['count']}"
    )


    print()
    print(
        "MANAGED"
    )

    print(
        "----------------------------------------"
    )


    if not result[
        "managed_positions"
    ]:

        print(
            "None"
        )


    else:

        for position in result[
            "managed_positions"
        ]:

            print(
                f"{position['symbol']:8} "
                f"qty="
                f"{position.get('quantity')} "
                f"avg="
                f"{position.get('avg_cost')}"
            )


    print()
    print(
        "LEGACY"
    )

    print(
        "----------------------------------------"
    )


    if not result[
        "legacy_positions"
    ]:

        print(
            "None"
        )


    else:

        for position in result[
            "legacy_positions"
        ]:

            print(
                f"{position['symbol']:8} "
                f"qty="
                f"{position.get('quantity')} "
                f"avg="
                f"{position.get('avg_cost')}"
            )


    print()
    print(
        "BLOCKERS"
    )

    print(
        "----------------------------------------"
    )


    if not result[
        "blockers"
    ]:

        print(
            "None"
        )


    else:

        for blocker in result[
            "blockers"
        ]:

            print(
                f"- {blocker}"
            )


if __name__ == "__main__":
    main()
