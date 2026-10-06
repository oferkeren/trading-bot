import json
import sqlite3

from datetime import datetime, timezone


# ============================================================
# STATES
# ============================================================

TERMINAL_STATES = {
    "TESTED",
    "REJECTED",
    "BLOCKED",
    "CANCELLED",
    "CLOSED_TP",
    "CLOSED_SL"
}


ACTIVE_STATES = {
    "QUEUED",
    "PROCESSING",

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


ALLOWED_TRANSITIONS = {
    "QUEUED": {
        "PROCESSING",
        "TESTED",
        "BLOCKED",
        "REJECTED"
    },

    "PROCESSING": {
        "SUBMITTED",
        "ACCEPTED_WAITING_MARKET",
        "OPEN_POSITION",
        "BLOCKED",
        "ERROR",
        "UNKNOWN",
        "TESTED"
    },

    "SUBMITTED": {
        "ACCEPTED_WAITING_MARKET",
        "FILLED",
        "OPEN_POSITION",
        "CANCEL_REQUESTED",
        "CANCEL_PENDING",
        "CANCELLED",
        "ERROR",
        "UNKNOWN"
    },

    "ACCEPTED_WAITING_MARKET": {
        "SUBMITTED",
        "FILLED",
        "OPEN_POSITION",
        "CANCEL_REQUESTED",
        "CANCEL_PENDING",
        "CANCELLED",
        "ERROR",
        "UNKNOWN"
    },

    "FILLED": {
        "OPEN_POSITION",
        "CLOSED_TP",
        "CLOSED_SL",
        "ERROR",
        "UNKNOWN"
    },

    "OPEN_POSITION": {
        "CLOSED_TP",
        "CLOSED_SL",
        "ERROR",
        "UNKNOWN"
    },

    "CANCEL_REQUESTED": {
        "CANCELLING",
        "CANCEL_PENDING",
        "CANCELLED",
        "CANCEL_UNKNOWN",
        "ERROR",
        "UNKNOWN"
    },

    "CANCELLING": {
        "CANCEL_PENDING",
        "CANCELLED",
        "CANCEL_UNKNOWN",
        "ERROR",
        "UNKNOWN"
    },

    "CANCEL_PENDING": {
        "CANCELLED",
        "CANCEL_UNKNOWN",
        "OPEN_POSITION",
        "ERROR",
        "UNKNOWN"
    },

    "CANCEL_UNKNOWN": {
        "CANCEL_REQUESTED",
        "CANCEL_PENDING",
        "CANCELLED",
        "OPEN_POSITION",
        "ERROR",
        "UNKNOWN"
    },

    "UNKNOWN": {
        "SUBMITTED",
        "ACCEPTED_WAITING_MARKET",
        "OPEN_POSITION",
        "CANCEL_REQUESTED",
        "CANCEL_PENDING",
        "CANCELLED",
        "CLOSED_TP",
        "CLOSED_SL",
        "ERROR"
    },

    "ERROR": {
        "SUBMITTED",
        "OPEN_POSITION",
        "CANCEL_REQUESTED",
        "CANCEL_PENDING",
        "CANCELLED",
        "CLOSED_TP",
        "CLOSED_SL",
        "UNKNOWN"
    }
}


class InvalidStateTransition(Exception):
    pass


class SignalNotFound(Exception):
    pass


# ============================================================
# HELPERS
# ============================================================

def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def db_connect(
    db_file
):
    conn = sqlite3.connect(
        db_file,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    return conn


def json_payload(
    payload
):
    if payload is None:
        return None

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str
    )


# ============================================================
# INIT
# ============================================================

def init_trade_state(
    db_file
):
    conn = db_connect(
        db_file
    )

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_events (
                event_id INTEGER
                    PRIMARY KEY AUTOINCREMENT,

                event_key TEXT UNIQUE,

                signal_id TEXT,

                event_type TEXT NOT NULL,

                source TEXT NOT NULL,

                old_status TEXT,

                new_status TEXT,

                message TEXT,

                payload_json TEXT,

                created_at TEXT NOT NULL
            )
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_trade_events_signal
            ON trade_events(signal_id)
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_trade_events_created
            ON trade_events(created_at)
            """
        )


        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_trade_events_type
            ON trade_events(event_type)
            """
        )


        conn.commit()

    finally:
        conn.close()


# ============================================================
# INTERNAL EVENT INSERT
# ============================================================

def _event_exists(
    conn,
    event_key
):
    if not event_key:
        return False


    row = conn.execute(
        """
        SELECT event_id
        FROM trade_events
        WHERE event_key = ?
        """,
        (
            event_key,
        )
    ).fetchone()


    return row is not None


def _insert_event(
    conn,
    *,
    event_key,
    signal_id,
    event_type,
    source,
    old_status,
    new_status,
    message,
    payload
):
    conn.execute(
        """
        INSERT INTO trade_events (
            event_key,
            signal_id,
            event_type,
            source,
            old_status,
            new_status,
            message,
            payload_json,
            created_at
        )

        VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?
        )
        """,
        (
            event_key,
            signal_id,
            event_type,
            source,
            old_status,
            new_status,
            message,
            json_payload(
                payload
            ),
            now_iso()
        )
    )


# ============================================================
# RECORD EVENT
# ============================================================

def record_event(
    *,
    db_file,
    signal_id,
    event_type,
    source,
    message=None,
    payload=None,
    event_key=None,
    old_status=None,
    new_status=None
):
    conn = db_connect(
        db_file
    )

    try:
        conn.execute(
            "BEGIN IMMEDIATE"
        )


        if (
            event_key
            and
            _event_exists(
                conn,
                event_key
            )
        ):
            conn.commit()

            return {
                "inserted":
                    False,

                "duplicate":
                    True
            }


        _insert_event(
            conn,
            event_key=
                event_key,

            signal_id=
                signal_id,

            event_type=
                event_type,

            source=
                source,

            old_status=
                old_status,

            new_status=
                new_status,

            message=
                message,

            payload=
                payload
        )


        conn.commit()


        return {
            "inserted":
                True,

            "duplicate":
                False
        }


    except Exception:
        conn.rollback()
        raise


    finally:
        conn.close()


# ============================================================
# TRANSITION
# ============================================================

def transition_signal(
    *,
    db_file,
    signal_id,
    new_status,
    event_type,
    source,
    message=None,
    payload=None,
    extra_fields=None,
    event_key=None,
    allow_same_state=False,
    force=False
):
    conn = db_connect(
        db_file
    )

    try:
        conn.execute(
            "BEGIN IMMEDIATE"
        )


        #
        # IMPORTANT:
        # event_key idempotency is checked BEFORE any mutation.
        #
        if (
            event_key
            and
            _event_exists(
                conn,
                event_key
            )
        ):
            row = conn.execute(
                """
                SELECT status
                FROM signals
                WHERE signal_id = ?
                """,
                (
                    signal_id,
                )
            ).fetchone()


            conn.commit()


            if row is None:
                raise SignalNotFound(
                    signal_id
                )


            return {
                "changed":
                    False,

                "duplicate":
                    True,

                "old_status":
                    row[
                        "status"
                    ],

                "new_status":
                    row[
                        "status"
                    ]
            }


        row = conn.execute(
            """
            SELECT status
            FROM signals
            WHERE signal_id = ?
            """,
            (
                signal_id,
            )
        ).fetchone()


        if row is None:
            raise SignalNotFound(
                signal_id
            )


        old_status = row[
            "status"
        ]


        # ----------------------------------------------------
        # SAME STATE
        # ----------------------------------------------------

        if (
            old_status
            == new_status
        ):
            if not allow_same_state:
                conn.commit()

                return {
                    "changed":
                        False,

                    "duplicate":
                        False,

                    "old_status":
                        old_status,

                    "new_status":
                        new_status
                }


            #
            # Same-state events without an event_key are
            # suppressed. This prevents reconciliation spam.
            #
            if not event_key:
                conn.commit()

                return {
                    "changed":
                        False,

                    "duplicate":
                        False,

                    "old_status":
                        old_status,

                    "new_status":
                        new_status
                }


        # ----------------------------------------------------
        # VALIDATE TRANSITION
        # ----------------------------------------------------

        elif not force:

            allowed = (
                ALLOWED_TRANSITIONS.get(
                    old_status,
                    set()
                )
            )


            if (
                new_status
                not in allowed
            ):
                raise InvalidStateTransition(
                    (
                        f"Invalid transition "
                        f"{old_status} -> "
                        f"{new_status}"
                    )
                )


        # ----------------------------------------------------
        # UPDATE SIGNAL
        # ----------------------------------------------------

        fields = dict(
            extra_fields
            or {}
        )


        fields[
            "status"
        ] = new_status

        fields[
            "updated_at"
        ] = now_iso()


        assignments = []

        values = []


        for field, value in (
            fields.items()
        ):
            assignments.append(
                f"{field} = ?"
            )

            values.append(
                value
            )


        values.append(
            signal_id
        )


        conn.execute(
            f"""
            UPDATE signals
            SET {", ".join(assignments)}
            WHERE signal_id = ?
            """,
            values
        )


        # ----------------------------------------------------
        # EVENT
        # ----------------------------------------------------

        _insert_event(
            conn,
            event_key=
                event_key,

            signal_id=
                signal_id,

            event_type=
                event_type,

            source=
                source,

            old_status=
                old_status,

            new_status=
                new_status,

            message=
                message,

            payload=
                payload
        )


        conn.commit()


        return {
            "changed":
                (
                    old_status
                    != new_status
                ),

            "duplicate":
                False,

            "old_status":
                old_status,

            "new_status":
                new_status
        }


    except Exception:
        conn.rollback()
        raise


    finally:
        conn.close()


# ============================================================
# METADATA UPDATE + EVENT
# ============================================================

def update_signal_metadata(
    *,
    db_file,
    signal_id,
    source,
    event_type,
    fields,
    message=None,
    payload=None,
    event_key=None
):
    conn = db_connect(
        db_file
    )

    try:
        conn.execute(
            "BEGIN IMMEDIATE"
        )


        if (
            event_key
            and
            _event_exists(
                conn,
                event_key
            )
        ):
            conn.commit()

            return {
                "changed":
                    False,

                "duplicate":
                    True
            }


        row = conn.execute(
            """
            SELECT status
            FROM signals
            WHERE signal_id = ?
            """,
            (
                signal_id,
            )
        ).fetchone()


        if row is None:
            raise SignalNotFound(
                signal_id
            )


        current_status = row[
            "status"
        ]


        update_fields = dict(
            fields
            or {}
        )


        update_fields[
            "updated_at"
        ] = now_iso()


        assignments = []

        values = []


        for field, value in (
            update_fields.items()
        ):
            assignments.append(
                f"{field} = ?"
            )

            values.append(
                value
            )


        values.append(
            signal_id
        )


        conn.execute(
            f"""
            UPDATE signals
            SET {", ".join(assignments)}
            WHERE signal_id = ?
            """,
            values
        )


        _insert_event(
            conn,
            event_key=
                event_key,

            signal_id=
                signal_id,

            event_type=
                event_type,

            source=
                source,

            old_status=
                current_status,

            new_status=
                current_status,

            message=
                message,

            payload=
                payload
        )


        conn.commit()


        return {
            "changed":
                True,

            "duplicate":
                False
        }


    except Exception:
        conn.rollback()
        raise


    finally:
        conn.close()


# ============================================================
# ATOMIC CLAIM
# ============================================================



def claim_signal(
    *,
    db_file,
    from_status,
    to_status,
    event_type,
    source,
    order_by="created_at",
    increment_attempts=False,
    broker_account=None,
    broker_port=None
):
    if order_by not in {
        "created_at",
        "updated_at"
    }:
        raise ValueError(
            "Invalid order_by"
        )

    if (
        (broker_account is None)
        !=
        (broker_port is None)
    ):
        raise ValueError(
            "broker_account and broker_port "
            "must be supplied together"
        )

    conn = db_connect(
        db_file
    )

    try:
        conn.execute(
            "BEGIN IMMEDIATE"
        )

        sql = """
            SELECT *
            FROM signals
            WHERE status = ?
        """

        params = [
            from_status
        ]

        if broker_account is not None:
            sql += """
                AND (
                    execution_mode IS NULL
                    OR execution_mode <> 'LIVE'
                    OR (
                        execution_mode = 'LIVE'
                        AND broker_account = ?
                        AND broker_port = ?
                    )
                )
            """

            params.extend([
                broker_account,
                int(broker_port),
            ])

        sql += f"""
            ORDER BY {order_by} ASC
            LIMIT 1
        """

        row = conn.execute(
            sql,
            params,
        ).fetchone()

        if row is None:
            conn.commit()
            return None

        signal = dict(row)

        assignments = [
            "status = ?",
            "updated_at = ?"
        ]

        values = [
            to_status,
            now_iso()
        ]

        if increment_attempts:
            assignments.append(
                "attempts = attempts + 1"
            )

        values.extend([
            signal["signal_id"],
            from_status
        ])

        cursor = conn.execute(
            f"""
            UPDATE signals
            SET {", ".join(assignments)}
            WHERE
                signal_id = ?
                AND status = ?
            """,
            values
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return None

        _insert_event(
            conn,
            event_key=None,
            signal_id=signal["signal_id"],
            event_type=event_type,
            source=source,
            old_status=from_status,
            new_status=to_status,
            message=(
                f"Atomic claim "
                f"{from_status} -> {to_status}"
            ),
            payload={
                "execution_mode":
                    signal.get("execution_mode"),
                "broker_account":
                    signal.get("broker_account"),
                "broker_port":
                    signal.get("broker_port"),
            }
        )

        conn.commit()

        signal["status"] = to_status

        if increment_attempts:
            signal["attempts"] = (
                int(
                    signal.get("attempts")
                    or 0
                )
                + 1
            )

        return signal

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()




# ============================================================
# EVENTS
# ============================================================

def get_signal_events(
    *,
    db_file,
    signal_id
):
    conn = db_connect(
        db_file
    )

    try:
        rows = conn.execute(
            """
            SELECT
                event_id,
                event_key,
                signal_id,
                event_type,
                source,
                old_status,
                new_status,
                message,
                payload_json,
                created_at

            FROM trade_events

            WHERE signal_id = ?

            ORDER BY event_id ASC
            """,
            (
                signal_id,
            )
        ).fetchall()


        result = []


        for row in rows:
            item = dict(
                row
            )


            raw = item.pop(
                "payload_json",
                None
            )


            if raw:
                try:
                    item[
                        "payload"
                    ] = json.loads(
                        raw
                    )

                except Exception:
                    item[
                        "payload"
                    ] = None

            else:
                item[
                    "payload"
                ] = None


            result.append(
                item
            )


        return result


    finally:
        conn.close()
