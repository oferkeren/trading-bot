import sqlite3
from datetime import datetime, timezone


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def get_control_state(
    db_file
):
    conn = sqlite3.connect(
        db_file,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS system_control (
            id INTEGER PRIMARY KEY CHECK(id=1),
            kill_switch INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            updated_at TEXT
        )
        """
    )

    cur.execute(
        """
        INSERT OR IGNORE INTO system_control (
            id,
            kill_switch,
            reason,
            updated_at
        )
        VALUES (
            1,
            0,
            NULL,
            ?
        )
        """,
        (
            now_iso(),
        )
    )

    conn.commit()

    cur.execute(
        """
        SELECT *
        FROM system_control
        WHERE id=1
        """
    )

    row = cur.fetchone()

    conn.close()

    return {
        "kill_switch":
            bool(
                row[
                    "kill_switch"
                ]
            ),

        "reason":
            row[
                "reason"
            ],

        "updated_at":
            row[
                "updated_at"
            ]
    }


def set_kill_switch(
    db_file,
    enabled,
    reason=None
):
    conn = sqlite3.connect(
        db_file,
        timeout=10
    )

    cur = conn.cursor()

    cur.execute(
        """
        UPDATE system_control
        SET
            kill_switch = ?,
            reason = ?,
            updated_at = ?
        WHERE id=1
        """,
        (
            1
            if enabled
            else 0,

            reason,

            now_iso()
        )
    )

    conn.commit()
    conn.close()

    return get_control_state(
        db_file
    )


def trades_today(
    db_file
):
    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    conn = sqlite3.connect(
        db_file,
        timeout=10
    )

    cur = conn.cursor()

    cur.execute(
        """
        SELECT COUNT(*)
        FROM signals
        WHERE
            test_mode = 0
            AND parent_order_id IS NOT NULL
            AND substr(created_at, 1, 10) = ?
        """,
        (
            today,
        )
    )

    value = cur.fetchone()[0]

    conn.close()

    return value
