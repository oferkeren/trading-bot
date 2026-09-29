import sqlite3


VALID_EXECUTION_MODES = {
    "LIVE",
    "PRELIVE",
}


PRELIVE_ALLOWED_STATUSES = {
    "QUEUED",
    "PROCESSING",
    "BLOCKED",
    "REJECTED",
    "TESTED",
    "ERROR",
}


REQUIRED_TRIGGERS = {
    "trg_execution_mode_insert_valid",
    "trg_execution_mode_immutable",
    "trg_prelive_status_guard",
    "trg_prelive_order_identity_guard",
    "trg_prelive_insert_identity_guard",
}


BROKER_IDENTITY_COLUMNS = [
    "parent_order_id",
    "entry_order_id",
    "target_order_id",
    "stop_order_id",

    "parent_perm_id",
    "target_perm_id",
    "stop_perm_id",

    "entry_order_ref",
    "target_order_ref",
    "stop_order_ref",
]


class ExecutionModeGuardError(
    RuntimeError
):
    pass


def db_connect(
    db_file
):
    conn = sqlite3.connect(
        db_file,
        timeout=10,
    )

    conn.row_factory = (
        sqlite3.Row
    )

    return conn


def table_exists(
    conn,
    table_name,
):
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE
            type = 'table'
            AND name = ?
        """,
        (
            table_name,
        ),
    ).fetchone()

    return (
        row
        is not None
    )


def get_columns(
    conn,
    table_name,
):
    rows = conn.execute(
        f"""
        PRAGMA table_info(
            {table_name}
        )
        """
    ).fetchall()

    return {
        row[
            "name"
        ]
        for row
        in rows
    }


def get_triggers(
    conn,
):
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'trigger'
        """
    ).fetchall()

    return {
        row[
            "name"
        ]
        for row
        in rows
    }


def ensure_execution_mode_column(
    conn,
):
    columns = (
        get_columns(
            conn,
            "signals",
        )
    )

    if (
        "execution_mode"
        not in columns
    ):
        conn.execute(
            """
            ALTER TABLE signals
            ADD COLUMN execution_mode
                TEXT
                NOT NULL
                DEFAULT 'LIVE'
            """
        )

    #
    # Only repair legacy NULL/empty values.
    #
    # Unknown non-empty modes are intentionally NOT silently
    # converted. Guard health will report them and PRELIVE
    # will fail closed.
    #
    conn.execute(
        """
        UPDATE signals
        SET execution_mode = 'LIVE'
        WHERE
            execution_mode IS NULL
            OR TRIM(execution_mode) = ''
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_signals_execution_mode
        ON signals(execution_mode)
        """
    )


def create_mode_validation_trigger(
    conn,
):
    conn.execute(
        """
        DROP TRIGGER IF EXISTS
        trg_execution_mode_insert_valid
        """
    )

    conn.execute(
        """
        CREATE TRIGGER
        trg_execution_mode_insert_valid

        BEFORE INSERT ON signals

        WHEN
            NEW.execution_mode
            NOT IN (
                'LIVE',
                'PRELIVE'
            )

        BEGIN
            SELECT RAISE(
                ABORT,
                'INVALID_EXECUTION_MODE'
            );
        END
        """
    )


def create_mode_immutability_trigger(
    conn,
):
    conn.execute(
        """
        DROP TRIGGER IF EXISTS
        trg_execution_mode_immutable
        """
    )

    conn.execute(
        """
        CREATE TRIGGER
        trg_execution_mode_immutable

        BEFORE UPDATE OF execution_mode
        ON signals

        WHEN
            NEW.execution_mode
            IS NOT
            OLD.execution_mode

        BEGIN
            SELECT RAISE(
                ABORT,
                'EXECUTION_MODE_IMMUTABLE'
            );
        END
        """
    )


def create_prelive_status_trigger(
    conn,
):
    conn.execute(
        """
        DROP TRIGGER IF EXISTS
        trg_prelive_status_guard
        """
    )

    conn.execute(
        """
        CREATE TRIGGER
        trg_prelive_status_guard

        BEFORE UPDATE OF status
        ON signals

        WHEN
            OLD.execution_mode = 'PRELIVE'

            AND

            NEW.status NOT IN (
                'QUEUED',
                'PROCESSING',
                'BLOCKED',
                'REJECTED',
                'TESTED',
                'ERROR'
            )

        BEGIN
            SELECT RAISE(
                ABORT,
                'PRELIVE_FORBIDDEN_STATUS'
            );
        END
        """
    )


def create_prelive_identity_update_trigger(
    conn,
):
    columns = (
        get_columns(
            conn,
            "signals",
        )
    )

    available = [
        column
        for column
        in BROKER_IDENTITY_COLUMNS
        if column
        in columns
    ]

    conn.execute(
        """
        DROP TRIGGER IF EXISTS
        trg_prelive_order_identity_guard
        """
    )

    if not available:
        raise ExecutionModeGuardError(
            (
                "signals table has no "
                "broker identity columns"
            )
        )

    update_columns = (
        ", ".join(
            available
        )
    )

    conn.execute(
        f"""
        CREATE TRIGGER
        trg_prelive_order_identity_guard

        BEFORE UPDATE OF
            {update_columns}
        ON signals

        WHEN
            OLD.execution_mode = 'PRELIVE'

        BEGIN
            SELECT RAISE(
                ABORT,
                'PRELIVE_BROKER_IDENTITY_BLOCK'
            );
        END
        """
    )


def create_prelive_identity_insert_trigger(
    conn,
):
    columns = (
        get_columns(
            conn,
            "signals",
        )
    )

    available = [
        column
        for column
        in BROKER_IDENTITY_COLUMNS
        if column
        in columns
    ]

    conn.execute(
        """
        DROP TRIGGER IF EXISTS
        trg_prelive_insert_identity_guard
        """
    )

    conditions = []

    for column in available:
        conditions.append(
            (
                f"NEW.{column} "
                "IS NOT NULL"
            )
        )

    if not conditions:
        raise ExecutionModeGuardError(
            (
                "signals table has no "
                "broker identity columns"
            )
        )

    identity_condition = (
        "\n                OR\n                "
        .join(
            conditions
        )
    )

    conn.execute(
        f"""
        CREATE TRIGGER
        trg_prelive_insert_identity_guard

        BEFORE INSERT
        ON signals

        WHEN
            NEW.execution_mode = 'PRELIVE'

            AND

            (
                {identity_condition}
            )

        BEGIN
            SELECT RAISE(
                ABORT,
                'PRELIVE_INSERT_IDENTITY_BLOCK'
            );
        END
        """
    )


def ensure_execution_mode_guard(
    db_file,
):
    conn = (
        db_connect(
            db_file
        )
    )

    try:
        conn.execute(
            "BEGIN IMMEDIATE"
        )

        if not table_exists(
            conn,
            "signals",
        ):
            raise ExecutionModeGuardError(
                (
                    "signals table "
                    "does not exist"
                )
            )

        ensure_execution_mode_column(
            conn
        )

        create_mode_validation_trigger(
            conn
        )

        create_mode_immutability_trigger(
            conn
        )

        create_prelive_status_trigger(
            conn
        )

        create_prelive_identity_update_trigger(
            conn
        )

        create_prelive_identity_insert_trigger(
            conn
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    return (
        get_execution_mode_guard_state(
            db_file
        )
    )


def get_execution_mode_guard_state(
    db_file,
):
    conn = (
        db_connect(
            db_file
        )
    )

    try:
        if not table_exists(
            conn,
            "signals",
        ):
            return {
                "ready":
                    False,

                "signals_table":
                    False,

                "execution_mode_column":
                    False,

                "missing_triggers":
                    sorted(
                        REQUIRED_TRIGGERS
                    ),

                "invalid_mode_count":
                    None,

                "prelive_count":
                    None,

                "live_count":
                    None,
            }

        columns = (
            get_columns(
                conn,
                "signals",
            )
        )

        triggers = (
            get_triggers(
                conn
            )
        )

        column_ready = (
            "execution_mode"
            in columns
        )

        missing_triggers = (
            REQUIRED_TRIGGERS
            -
            triggers
        )

        if column_ready:
            invalid_mode_count = (
                conn.execute(
                    """
                    SELECT COUNT(*)
                    AS count

                    FROM signals

                    WHERE
                        execution_mode
                        NOT IN (
                            'LIVE',
                            'PRELIVE'
                        )

                        OR execution_mode
                        IS NULL
                    """
                )
                .fetchone()[
                    "count"
                ]
            )

            prelive_count = (
                conn.execute(
                    """
                    SELECT COUNT(*)
                    AS count

                    FROM signals

                    WHERE
                        execution_mode
                        =
                        'PRELIVE'
                    """
                )
                .fetchone()[
                    "count"
                ]
            )

            live_count = (
                conn.execute(
                    """
                    SELECT COUNT(*)
                    AS count

                    FROM signals

                    WHERE
                        execution_mode
                        =
                        'LIVE'
                    """
                )
                .fetchone()[
                    "count"
                ]
            )

        else:
            invalid_mode_count = None
            prelive_count = None
            live_count = None

        ready = (
            column_ready

            and
            not missing_triggers

            and
            invalid_mode_count
            ==
            0
        )

        return {
            "ready":
                ready,

            "signals_table":
                True,

            "execution_mode_column":
                column_ready,

            "required_triggers":
                sorted(
                    REQUIRED_TRIGGERS
                ),

            "installed_triggers":
                sorted(
                    REQUIRED_TRIGGERS
                    &
                    triggers
                ),

            "missing_triggers":
                sorted(
                    missing_triggers
                ),

            "invalid_mode_count":
                invalid_mode_count,

            "prelive_count":
                prelive_count,

            "live_count":
                live_count,
        }

    finally:
        conn.close()


def assert_execution_mode_guard(
    db_file,
):
    state = (
        get_execution_mode_guard_state(
            db_file
        )
    )

    if not state.get(
        "ready",
        False,
    ):
        raise ExecutionModeGuardError(
            (
                "Execution-mode guard "
                f"is not ready: {state}"
            )
        )

    return state


def get_signal_execution_mode(
    db_file,
    signal_id,
):
    conn = (
        db_connect(
            db_file
        )
    )

    try:
        row = (
            conn.execute(
                """
                SELECT execution_mode
                FROM signals
                WHERE signal_id = ?
                """,
                (
                    signal_id,
                ),
            )
            .fetchone()
        )

        if row is None:
            raise ExecutionModeGuardError(
                (
                    "Signal not found: "
                    f"{signal_id}"
                )
            )

        mode = (
            str(
                row[
                    "execution_mode"
                ]
                or
                ""
            )
            .strip()
            .upper()
        )

        if (
            mode
            not in
            VALID_EXECUTION_MODES
        ):
            raise ExecutionModeGuardError(
                (
                    "Invalid execution mode "
                    f"for {signal_id}: "
                    f"{mode!r}"
                )
            )

        return mode

    finally:
        conn.close()
