import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db"
)

WATCHDOG_INTERVAL_SECONDS = int(
    os.getenv(
        "WATCHDOG_INTERVAL_SECONDS",
        "5"
    )
)

STATUS_MAX_AGE_SECONDS = int(
    os.getenv(
        "STATUS_MAX_AGE_SECONDS",
        "30"
    )
)

API_URL = os.getenv(
    "WATCHDOG_API_URL",
    "http://127.0.0.1:8000/health"
)

API_TIMEOUT_SECONDS = float(
    os.getenv(
        "WATCHDOG_API_TIMEOUT_SECONDS",
        "2"
    )
)


CRITICAL_SERVICES = {
    "api":
        "trading-bot.service",

    "worker":
        "trading-worker.service",

    "monitor":
        "trading-monitor.service",

    "status":
        "trading-status.service"
}


OPTIONAL_SERVICES = {
    "cloudflared":
        "cloudflared.service"
}


# ============================================================
# HELPERS
# ============================================================

def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    return conn


def parse_iso(
    value
):
    if not value:
        return None

    try:
        text = str(
            value
        )

        if text.endswith(
            "Z"
        ):
            text = (
                text[:-1]
                + "+00:00"
            )

        result = datetime.fromisoformat(
            text
        )

        if result.tzinfo is None:
            result = result.replace(
                tzinfo=timezone.utc
            )

        return result.astimezone(
            timezone.utc
        )

    except Exception:
        return None


def age_seconds(
    value
):
    timestamp = parse_iso(
        value
    )

    if timestamp is None:
        return None

    return max(
        0.0,
        (
            datetime.now(
                timezone.utc
            )
            - timestamp
        ).total_seconds()
    )


# ============================================================
# DB INIT
# ============================================================

def init_db():
    conn = db_connect()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_components (
            component TEXT PRIMARY KEY,

            healthy INTEGER NOT NULL,

            critical INTEGER NOT NULL,

            detail TEXT,

            metadata_json TEXT,

            checked_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_runtime_components_health
        ON runtime_components(
            healthy,
            critical
        )
        """
    )

    conn.commit()
    conn.close()


# ============================================================
# SYSTEMD
# ============================================================

def service_state(
    service_name
):
    try:
        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                service_name
            ],
            capture_output=True,
            text=True,
            timeout=2
        )

        state = (
            result.stdout
            or result.stderr
            or ""
        ).strip()

        healthy = (
            result.returncode == 0
            and
            state == "active"
        )

        return {
            "healthy":
                healthy,

            "state":
                state
                or "unknown"
        }

    except Exception as exc:
        return {
            "healthy":
                False,

            "state":
                "error",

            "error":
                str(
                    exc
                )
        }


# ============================================================
# API CHECK
# ============================================================

def api_health():
    started = time.monotonic()

    try:
        request = urllib.request.Request(
            API_URL,
            method="GET"
        )

        with urllib.request.urlopen(
            request,
            timeout=
                API_TIMEOUT_SECONDS
        ) as response:

            body = response.read().decode(
                "utf-8"
            )

            data = json.loads(
                body
            )


        latency_ms = (
            time.monotonic()
            - started
        ) * 1000


        healthy = (
            response.status == 200
            and
            data.get(
                "status"
            )
            == "ok"
        )


        return {
            "healthy":
                healthy,

            "detail":
                (
                    "API healthy"
                    if healthy
                    else
                    "API returned unhealthy response"
                ),

            "metadata": {
                "http_status":
                    response.status,

                "latency_ms":
                    round(
                        latency_ms,
                        2
                    ),

                "response":
                    data
            }
        }


    except Exception as exc:

        return {
            "healthy":
                False,

            "detail":
                "API health check failed",

            "metadata": {
                "error":
                    str(
                        exc
                    )
            }
        }


# ============================================================
# STATUS COLLECTOR / TWS
# ============================================================

def broker_snapshot_health():
    conn = db_connect()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT
                updated_at,
                tws_connected,
                account,
                last_error
            FROM runtime_status
            WHERE id = 1
            """
        )

        row = cur.fetchone()

    finally:
        conn.close()


    if row is None:
        return {
            "status_snapshot": {
                "healthy":
                    False,

                "detail":
                    "runtime_status missing",

                "metadata":
                    {}
            },

            "tws": {
                "healthy":
                    False,

                "detail":
                    "No broker snapshot",

                "metadata":
                    {}
            }
        }


    age = age_seconds(
        row[
            "updated_at"
        ]
    )


    snapshot_healthy = (
        age is not None
        and
        age
        <= STATUS_MAX_AGE_SECONDS
    )


    tws_connected = bool(
        row[
            "tws_connected"
        ]
    )


    snapshot = {
        "healthy":
            snapshot_healthy,

        "detail":
            (
                f"Snapshot age "
                f"{age:.1f}s"
                if age is not None
                else
                "Invalid snapshot timestamp"
            ),

        "metadata": {
            "age_seconds":
                age,

            "updated_at":
                row[
                    "updated_at"
                ],

            "account":
                row[
                    "account"
                ],

            "last_error":
                row[
                    "last_error"
                ]
        }
    }


    tws = {
        "healthy":
            (
                snapshot_healthy
                and
                tws_connected
            ),

        "detail":
            (
                "TWS connected"
                if (
                    snapshot_healthy
                    and
                    tws_connected
                )
                else
                "TWS unavailable or snapshot stale"
            ),

        "metadata": {
            "tws_connected":
                tws_connected,

            "snapshot_age_seconds":
                age,

            "account":
                row[
                    "account"
                ],

            "last_error":
                row[
                    "last_error"
                ]
        }
    }


    return {
        "status_snapshot":
            snapshot,

        "tws":
            tws
    }


# ============================================================
# STORE
# ============================================================

def store_component(
    *,
    component,
    healthy,
    critical,
    detail,
    metadata
):
    conn = db_connect()

    try:
        conn.execute(
            """
            INSERT INTO runtime_components (
                component,
                healthy,
                critical,
                detail,
                metadata_json,
                checked_at
            )

            VALUES (
                ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT(component)
            DO UPDATE SET

                healthy =
                    excluded.healthy,

                critical =
                    excluded.critical,

                detail =
                    excluded.detail,

                metadata_json =
                    excluded.metadata_json,

                checked_at =
                    excluded.checked_at
            """,
            (
                component,

                1
                if healthy
                else 0,

                1
                if critical
                else 0,

                detail,

                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    sort_keys=True
                ),

                now_iso()
            )
        )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# CHECK CYCLE
# ============================================================

def check_once():
    results = {}


    # --------------------------------------------------------
    # SYSTEMD SERVICES
    # --------------------------------------------------------

    for component, service in (
        CRITICAL_SERVICES.items()
    ):
        state = service_state(
            service
        )

        results[
            component
        ] = {
            "healthy":
                state[
                    "healthy"
                ],

            "critical":
                True,

            "detail":
                (
                    f"{service}: "
                    f"{state.get('state')}"
                ),

            "metadata":
                state
        }


    for component, service in (
        OPTIONAL_SERVICES.items()
    ):
        state = service_state(
            service
        )

        results[
            component
        ] = {
            "healthy":
                state[
                    "healthy"
                ],

            "critical":
                False,

            "detail":
                (
                    f"{service}: "
                    f"{state.get('state')}"
                ),

            "metadata":
                state
        }


    # --------------------------------------------------------
    # API REQUEST
    # --------------------------------------------------------

    api = api_health()

    #
    # Require BOTH:
    # systemd active + HTTP health success.
    #
    results[
        "api"
    ][
        "healthy"
    ] = (
        results[
            "api"
        ][
            "healthy"
        ]
        and
        api[
            "healthy"
        ]
    )

    results[
        "api"
    ][
        "detail"
    ] = api[
        "detail"
    ]

    results[
        "api"
    ][
        "metadata"
    ][
        "http"
    ] = api[
        "metadata"
    ]


    # --------------------------------------------------------
    # BROKER SNAPSHOT
    # --------------------------------------------------------

    broker = broker_snapshot_health()


    snapshot = broker[
        "status_snapshot"
    ]


    #
    # Status collector must both:
    # 1. have an active systemd service
    # 2. actually update runtime_status
    #
    results[
        "status"
    ][
        "healthy"
    ] = (
        results[
            "status"
        ][
            "healthy"
        ]
        and
        snapshot[
            "healthy"
        ]
    )

    results[
        "status"
    ][
        "detail"
    ] = snapshot[
        "detail"
    ]

    results[
        "status"
    ][
        "metadata"
    ][
        "snapshot"
    ] = snapshot[
        "metadata"
    ]


    tws = broker[
        "tws"
    ]


    results[
        "tws"
    ] = {
        "healthy":
            tws[
                "healthy"
            ],

        "critical":
            True,

        "detail":
            tws[
                "detail"
            ],

        "metadata":
            tws[
                "metadata"
            ]
    }


    # --------------------------------------------------------
    # WATCHDOG ITSELF
    # --------------------------------------------------------

    results[
        "watchdog"
    ] = {
        "healthy":
            True,

        "critical":
            True,

        "detail":
            "Watchdog running",

        "metadata": {
            "interval_seconds":
                WATCHDOG_INTERVAL_SECONDS
        }
    }


    # --------------------------------------------------------
    # STORE EVERYTHING
    # --------------------------------------------------------

    for component, result in (
        results.items()
    ):
        store_component(
            component=
                component,

            healthy=
                result[
                    "healthy"
                ],

            critical=
                result[
                    "critical"
                ],

            detail=
                result[
                    "detail"
                ],

            metadata=
                result[
                    "metadata"
                ]
        )


    unhealthy = [
        name
        for name, state
        in results.items()
        if (
            state[
                "critical"
            ]
            and
            not state[
                "healthy"
            ]
        )
    ]


    summary = (
        "HEALTHY"
        if not unhealthy
        else
        "BLOCKED: "
        + ", ".join(
            unhealthy
        )
    )


    print(
        f"WATCHDOG | {summary}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    init_db()

    print(
        "TradingMax watchdog started"
    )

    print(
        f"interval="
        f"{WATCHDOG_INTERVAL_SECONDS}s"
    )


    while True:
        try:
            check_once()

        except Exception as exc:
            print(
                f"WATCHDOG ERROR | "
                f"{exc}"
            )


        time.sleep(
            WATCHDOG_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
