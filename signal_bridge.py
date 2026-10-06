import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from datetime import datetime, timezone
from pathlib import Path

import strategy_engine
import scalp_strategy

from strategy_status import (
    publish_scanner_universe,
    mark_phase,
    record_analysis,
    record_bridge_mode,
    record_error,
)


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent

ENV_FILE = (
    BASE_DIR
    /
    ".env"
)

STATE_DB = (
    BASE_DIR
    /
    "signal_bridge_state.db"
)


# ============================================================
# ENV HELPERS
# ============================================================

def parse_bool(
    value,
    default=False,
):
    if value is None:
        return default

    return (
        str(
            value
        )
        .strip()
        .lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )


def load_dotenv_file():
    if not ENV_FILE.exists():
        return

    try:
        lines = (
            ENV_FILE
            .read_text(
                encoding="utf-8"
            )
            .splitlines()
        )

    except Exception as exc:
        print(
            "ENV WARNING | "
            f"could not read {ENV_FILE}: "
            f"{type(exc).__name__}: {exc}"
        )

        return

    for raw_line in lines:
        line = (
            raw_line
            .strip()
        )

        if not line:
            continue

        if line.startswith(
            "#"
        ):
            continue

        if "=" not in line:
            continue

        key, value = (
            line.split(
                "=",
                1,
            )
        )

        key = (
            key.strip()
        )

        value = (
            value.strip()
        )

        if not key:
            continue

        if (
            len(value) >= 2
            and
            (
                (
                    value.startswith(
                        '"'
                    )
                    and
                    value.endswith(
                        '"'
                    )
                )
                or
                (
                    value.startswith(
                        "'"
                    )
                    and
                    value.endswith(
                        "'"
                    )
                )
            )
        ):
            value = (
                value[
                    1:-1
                ]
            )

        #
        # Systemd Environment wins.
        #
        os.environ.setdefault(
            key,
            value,
        )


load_dotenv_file()


# ============================================================
# CONFIG
# ============================================================

STRATEGY_NAME = (
    "tradingmax_multi_v1"
)

TIMEFRAME = "5m"


WEBHOOK_PATH = (
    "/webhook/tradingview"
)

HEALTH_PATH = (
    "/health"
)


COOLDOWN_SECONDS = int(
    os.getenv(
        "BRIDGE_COOLDOWN_SECONDS",
        "900",
    )
)


HTTP_TIMEOUT_SECONDS = int(
    os.getenv(
        "BRIDGE_HTTP_TIMEOUT_SECONDS",
        "10",
    )
)


BRIDGE_DRY_RUN = parse_bool(
    os.getenv(
        "BRIDGE_DRY_RUN"
    ),
    True,
)


BRIDGE_ALLOW_POST = parse_bool(
    os.getenv(
        "BRIDGE_ALLOW_POST"
    ),
    False,
)


RAW_BRIDGE_MODE = (
    os.getenv(
        "BRIDGE_MODE",
        ""
    )
    .strip()
    .upper()
)


def resolve_bridge_mode():
    if RAW_BRIDGE_MODE:
        if RAW_BRIDGE_MODE not in {
            "DRY_RUN",
            "TEST",
            "PRELIVE",
            "LIVE",
        }:
            raise RuntimeError(
                "Invalid BRIDGE_MODE="
                f"{RAW_BRIDGE_MODE}; "
                "expected DRY_RUN, TEST, "
                "PRELIVE or LIVE"
            )

        return RAW_BRIDGE_MODE

    #
    # Backward compatibility with existing setup.
    #
    if BRIDGE_DRY_RUN:
        return "DRY_RUN"

    return "TEST"


BRIDGE_MODE = (
    resolve_bridge_mode()
)


TEST_API_BASE_URL = (
    os.getenv(
        "TRADINGMAX_TEST_API_URL",
        os.getenv(
            "TRADINGMAX_API_URL",
            "http://127.0.0.1:8000",
        ),
    )
    .rstrip("/")
)


PRELIVE_API_BASE_URL = (
    os.getenv(
        "TRADINGMAX_PRELIVE_API_URL",
        "http://127.0.0.1:8001",
    )
    .rstrip("/")
)


if BRIDGE_MODE == "PRELIVE":
    API_BASE_URL = (
        PRELIVE_API_BASE_URL
    )

else:
    API_BASE_URL = (
        TEST_API_BASE_URL
    )


# ============================================================
# STATE DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        STATE_DB,
        timeout=10,
    )

    conn.row_factory = (
        sqlite3.Row
    )

    return conn


def init_state_db():
    conn = db_connect()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_signals (
                signal_id TEXT PRIMARY KEY,

                symbol TEXT NOT NULL,
                action TEXT NOT NULL,

                signal_time TEXT NOT NULL,

                entry REAL NOT NULL,
                stop REAL NOT NULL,
                target REAL NOT NULL,

                sent_at REAL NOT NULL,

                http_status INTEGER,
                response_body TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_sent_signals_symbol_action_time
            ON sent_signals (
                symbol,
                action,
                sent_at
            )
            """
        )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# TIME
# ============================================================

def now_utc():
    return (
        datetime.now(
            timezone.utc
        )
    )


def now_iso():
    return (
        now_utc()
        .isoformat()
    )


# ============================================================
# SIGNAL ID
# ============================================================


def build_signal_id(candidate):
    symbol = str(
        candidate[
            "symbol"
        ]
    ).strip().upper()

    action = str(
        candidate[
            "action"
        ]
    ).strip().upper()

    entry = float(
        candidate[
            "entry"
        ]
    )

    stop = float(
        candidate[
            "stop"
        ]
    )

    target = float(
        candidate[
            "target"
        ]
    )

    strategy = str(
        candidate.get(
            "strategy",
            STRATEGY_NAME,
        )
    ).strip()

    timeframe = str(
        candidate.get(
            "timeframe",
            TIMEFRAME,
        )
    ).strip()

    bucket_seconds = (
        60
        if (
            timeframe == "1m"
            or
            strategy.startswith(
                "scalp_"
            )
        )
        else
        300
    )

    now_ts = int(
        time.time()
    )

    bucket = (
        now_ts
        //
        bucket_seconds
        *
        bucket_seconds
    )

    raw = (
        f"{strategy}|"
        f"{timeframe}|"
        f"{symbol}|"
        f"{action}|"
        f"{bucket}|"
        f"{entry:.4f}|"
        f"{stop:.4f}|"
        f"{target:.4f}"
    )

    digest = (
        hashlib.sha256(
            raw.encode(
                "utf-8"
            )
        )
        .hexdigest()[
            :12
        ]
    )

    timestamp = (
        datetime.fromtimestamp(
            bucket,
            tz=timezone.utc,
        )
        .strftime(
            "%Y%m%dT%H%M%S"
        )
    )

    prefix = (
        "scalp"
        if strategy.startswith(
            "scalp_"
        )
        else
        "tmx"
    )

    return (
        f"{prefix}-"
        f"{timestamp}-"
        f"{symbol}-"
        f"{action}-"
        f"{digest}"
    )



# ============================================================
# PAYLOAD
# ============================================================


def build_payload(
    candidate,
    secret,
):
    signal_time = (
        now_iso()
    )

    signal_id = (
        build_signal_id(
            candidate
        )
    )

    strategy = str(
        candidate.get(
            "strategy",
            STRATEGY_NAME,
        )
    ).strip()

    timeframe = str(
        candidate.get(
            "timeframe",
            TIMEFRAME,
        )
    ).strip()

    return {
        "secret":
            secret,

        "signal_id":
            signal_id,

        "strategy":
            strategy,

        "timeframe":
            timeframe,

        "signal_time":
            signal_time,

        "symbol":
            candidate[
                "symbol"
            ]
            .strip()
            .upper(),

        "action":
            candidate[
                "action"
            ]
            .strip()
            .upper(),

        "entry":
            round(
                float(
                    candidate[
                        "entry"
                    ]
                ),
                4,
            ),

        "target":
            round(
                float(
                    candidate[
                        "target"
                    ]
                ),
                4,
            ),

        "stop":
            round(
                float(
                    candidate[
                        "stop"
                    ]
                ),
                4,
            ),
    }



# ============================================================
# VALIDATION
# ============================================================

def validate_candidate(
    candidate,
):
    required = {
        "symbol",
        "action",
        "entry",
        "stop",
        "target",
        "qualified",
    }

    missing = (
        required
        -
        set(
            candidate.keys()
        )
    )

    if missing:
        raise ValueError(
            "Candidate missing fields: "
            +
            ", ".join(
                sorted(
                    missing
                )
            )
        )

    if not candidate[
        "qualified"
    ]:
        raise ValueError(
            "Candidate is not qualified"
        )

    action = (
        str(
            candidate[
                "action"
            ]
        )
        .strip()
        .upper()
    )

    if action not in {
        "BUY",
        "SELL",
    }:
        raise ValueError(
            f"Unsupported action: {action}"
        )

    entry = float(
        candidate[
            "entry"
        ]
    )

    stop = float(
        candidate[
            "stop"
        ]
    )

    target = float(
        candidate[
            "target"
        ]
    )

    if (
        entry <= 0
        or
        stop <= 0
        or
        target <= 0
    ):
        raise ValueError(
            "Entry/stop/target "
            "must be positive"
        )

    if action == "BUY":
        if not (
            stop
            <
            entry
            <
            target
        ):
            raise ValueError(
                "Invalid BUY structure: "
                "stop < entry < target "
                "required"
            )

    else:
        if not (
            target
            <
            entry
            <
            stop
        ):
            raise ValueError(
                "Invalid SELL structure: "
                "target < entry < stop "
                "required"
            )


def downstream_action_supported(action):
    return str(action).strip().upper() in {"BUY", "SELL"}


# ============================================================
# COOLDOWN / DEDUPE
# ============================================================

def signal_already_sent(
    signal_id,
):
    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT signal_id
            FROM sent_signals
            WHERE signal_id = ?
            """,
            (
                signal_id,
            ),
        ).fetchone()

        return (
            row
            is not None
        )

    finally:
        conn.close()


def symbol_action_in_cooldown(
    symbol,
    action,
    cooldown_seconds=None,
):
    cutoff = (
        time.time()
        -
        (
            COOLDOWN_SECONDS
            if cooldown_seconds is None
            else int(
                cooldown_seconds
            )
        )
    )

    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT
                signal_id,
                sent_at

            FROM sent_signals

            WHERE
                symbol = ?
                AND action = ?
                AND sent_at >= ?

            ORDER BY sent_at DESC

            LIMIT 1
            """,
            (
                symbol.upper(),
                action.upper(),
                cutoff,
            ),
        ).fetchone()

        if row is None:
            return (
                False,
                None,
            )

        return (
            True,
            dict(
                row
            ),
        )

    finally:
        conn.close()


def record_sent_signal(
    payload,
    http_status,
    response_body,
):
    conn = db_connect()

    try:
        conn.execute(
            """
            INSERT OR REPLACE
            INTO sent_signals (
                signal_id,
                symbol,
                action,
                signal_time,
                entry,
                stop,
                target,
                sent_at,
                http_status,
                response_body
            )

            VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?
            )
            """,
            (
                payload[
                    "signal_id"
                ],

                payload[
                    "symbol"
                ],

                payload[
                    "action"
                ],

                payload[
                    "signal_time"
                ],

                payload[
                    "entry"
                ],

                payload[
                    "stop"
                ],

                payload[
                    "target"
                ],

                time.time(),

                http_status,

                response_body,
            ),
        )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# SAFE DISPLAY
# ============================================================

def safe_payload(
    payload,
):
    result = dict(
        payload
    )

    if "secret" in result:
        result[
            "secret"
        ] = "***REDACTED***"

    return result


def print_payload(
    payload,
):
    print(
        json.dumps(
            safe_payload(
                payload
            ),
            indent=2,
            sort_keys=True,
        )
    )


# ============================================================
# HTTP
# ============================================================

def http_json_request(
    method,
    url,
    payload=None,
):
    body = None

    headers = {
        "Accept":
            "application/json",
    }

    if payload is not None:
        body = json.dumps(
            payload
        ).encode(
            "utf-8"
        )

        headers[
            "Content-Type"
        ] = (
            "application/json"
        )

    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=
                HTTP_TIMEOUT_SECONDS,
        ) as response:
            response_body = (
                response
                .read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

            try:
                parsed = json.loads(
                    response_body
                )

            except Exception:
                parsed = (
                    response_body
                )

            return (
                response.status,
                parsed,
                response_body,
            )

    except urllib.error.HTTPError as exc:
        response_body = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        try:
            parsed = json.loads(
                response_body
            )

        except Exception:
            parsed = (
                response_body
            )

        return (
            exc.code,
            parsed,
            response_body,
        )


# ============================================================
# API HEALTH
# ============================================================

def get_api_health():
    url = (
        API_BASE_URL
        +
        HEALTH_PATH
    )

    status, parsed, raw = (
        http_json_request(
            "GET",
            url,
        )
    )

    if status != 200:
        raise RuntimeError(
            "TradingMax health check failed: "
            f"HTTP {status} | "
            f"{raw}"
        )

    if not isinstance(
        parsed,
        dict,
    ):
        raise RuntimeError(
            "TradingMax health response "
            "is not JSON object"
        )

    return parsed


def enforce_test_api():
    health = (
        get_api_health()
    )

    test_mode = (
        health.get(
            "test_mode"
        )
    )

    print(
        "API HEALTH | "
        f"mode=TEST | "
        f"status={health.get('status')} | "
        f"live_trading="
        f"{health.get('live_trading')} | "
        f"test_mode={test_mode}"
    )

    if test_mode is not True:
        raise RuntimeError(
            "Bridge TEST mode refuses POST "
            "because API is not in "
            "WEBHOOK_TEST_MODE"
        )

    return health


def enforce_prelive_api():
    health = (
        get_api_health()
    )

    mode = (
        str(
            health.get(
                "mode"
            )
            or
            ""
        )
        .strip()
        .upper()
    )

    prelive = (
        health.get(
            "prelive"
        )
    )

    test_mode = (
        health.get(
            "test_mode"
        )
    )

    worker = (
        health.get(
            "worker"
        )
        or
        {}
    )

    worker_active = (
        worker.get(
            "active"
        )
        is True
    )

    worker_dry_run = (
        worker.get(
            "prelive_dry_run"
        )
        is True
    )

    worker_safe = (
        worker.get(
            "safe"
        )
        is True
    )

    live_ready = (
        health.get(
            "live_ready"
        )
        is True
    )

    blockers = (
        health.get(
            "blockers"
        )
        or
        []
    )

    print(
        "API HEALTH | "
        f"mode={mode} | "
        f"prelive={prelive} | "
        f"test_mode={test_mode} | "
        f"worker_active={worker_active} | "
        f"worker_prelive_dry_run="
        f"{worker_dry_run} | "
        f"worker_safe={worker_safe} | "
        f"live_ready={live_ready}"
    )

    if mode != "PRELIVE":
        raise RuntimeError(
            "Bridge PRELIVE refuses POST: "
            "gateway mode is not PRELIVE"
        )

    if prelive is not True:
        raise RuntimeError(
            "Bridge PRELIVE refuses POST: "
            "prelive flag is not true"
        )

    if test_mode is not False:
        raise RuntimeError(
            "Bridge PRELIVE refuses POST: "
            "gateway test_mode must be false"
        )

    if not worker_active:
        raise RuntimeError(
            "Bridge PRELIVE refuses POST: "
            "worker is not active"
        )

    if not worker_dry_run:
        raise RuntimeError(
            "Bridge PRELIVE refuses POST: "
            "worker PRELIVE_DRY_RUN "
            "is not true"
        )

    if not worker_safe:
        raise RuntimeError(
            "Bridge PRELIVE refuses POST: "
            "gateway does not consider "
            "worker safe"
        )

    if not live_ready:
        raise RuntimeError(
            "Bridge PRELIVE refuses POST: "
            "LIVE safety is not ready | "
            f"blockers={blockers}"
        )

    return health


def enforce_live_api():
    health = (
        get_api_health()
    )

    mode = (
        str(
            health.get(
                "mode"
            )
            or
            ""
        )
        .strip()
        .upper()
    )

    live_trading = (
        health.get(
            "live_trading"
        )
        is True
    )

    test_mode = (
        health.get(
            "test_mode"
        )
    )

    live_ready = (
        health.get(
            "live_ready"
        )
        is True
    )

    kill_switch = (
        health.get(
            "kill_switch"
        )
        is True
    )

    blockers = (
        health.get(
            "blockers"
        )
        or
        []
    )

    print(
        "API HEALTH | "
        f"mode={mode} | "
        f"live_trading={live_trading} | "
        f"test_mode={test_mode} | "
        f"live_ready={live_ready} | "
        f"kill_switch={kill_switch}"
    )

    if mode != "LIVE":
        raise RuntimeError(
            "Bridge LIVE refuses POST: "
            "gateway mode is not LIVE"
        )

    if not live_trading:
        raise RuntimeError(
            "Bridge LIVE refuses POST: "
            "LIVE_TRADING is not enabled"
        )

    if test_mode is not False:
        raise RuntimeError(
            "Bridge LIVE refuses POST: "
            "gateway test_mode must be false"
        )

    if kill_switch:
        raise RuntimeError(
            "Bridge LIVE refuses POST: "
            "kill switch is enabled"
        )

    if not live_ready:
        raise RuntimeError(
            "Bridge LIVE refuses POST: "
            "LIVE safety is not ready | "
            f"blockers={blockers}"
        )

    return health


def enforce_mode_api():
    if BRIDGE_MODE == "TEST":
        return (
            enforce_test_api()
        )

    if BRIDGE_MODE == "PRELIVE":
        return (
            enforce_prelive_api()
        )

    if BRIDGE_MODE == "LIVE":
        return (
            enforce_live_api()
        )

    raise RuntimeError(
        "Network API gate called while "
        f"BRIDGE_MODE={BRIDGE_MODE}"
    )


# ============================================================
# POST
# ============================================================

def send_payload(
    payload,
):
    url = (
        API_BASE_URL
        +
        WEBHOOK_PATH
    )

    status, parsed, raw = (
        http_json_request(
            "POST",
            url,
            payload,
        )
    )

    return (
        status,
        parsed,
        raw,
    )


# ============================================================
# BRIDGE ONE CANDIDATE
# ============================================================

def process_candidate(
    candidate,
    secret,
):
    validate_candidate(
        candidate
    )

    payload = (
        build_payload(
            candidate,
            secret,
        )
    )

    signal_id = (
        payload[
            "signal_id"
        ]
    )

    symbol = (
        payload[
            "symbol"
        ]
    )

    action = (
        payload[
            "action"
        ]
    )

    print()
    print(
        "=============================================================="
    )

    print(
        "QUALIFIED CANDIDATE"
    )

    print(
        "=============================================================="
    )

    print(
        f"Mode         : "
        f"{BRIDGE_MODE}"
    )

    print(
        f"Symbol       : "
        f"{symbol}"
    )

    print(
        f"Action       : "
        f"{action}"
    )

    print(
        f"Signal ID    : "
        f"{signal_id}"
    )

    print(
        f"Instrument   : "
        f"{candidate.get('instrument_type')}"
    )

    print(
        f"Scanner score: "
        f"{candidate.get('scanner_score')}"
    )

    print(
        f"Support      : "
        f"{candidate.get('support')}/"
        f"{candidate.get('support_total')}"
    )

    print()

    print(
        "SIGNAL PAYLOAD"
    )

    print_payload(
        payload
    )

    # --------------------------------------------------------
    # DRY RUN
    # --------------------------------------------------------

    if (
        BRIDGE_MODE
        ==
        "DRY_RUN"
    ):
        print()
        print(
            "BRIDGE DRY RUN | "
            "payload NOT sent"
        )

        return {
            "status":
                "DRY_RUN",

            "signal_id":
                signal_id,
        }

    # --------------------------------------------------------
    # COMPATIBILITY SAFETY
    # --------------------------------------------------------

    if BRIDGE_DRY_RUN:
        print()
        print(
            "BRIDGE BLOCKED | "
            "BRIDGE_DRY_RUN=true while "
            f"BRIDGE_MODE={BRIDGE_MODE}"
        )

        return {
            "status":
                "BLOCKED_DRY_RUN_FLAG",

            "signal_id":
                signal_id,
        }

    if not BRIDGE_ALLOW_POST:
        print()
        print(
            "BRIDGE BLOCKED | "
            "BRIDGE_ALLOW_POST=false"
        )

        return {
            "status":
                "BLOCKED",

            "signal_id":
                signal_id,
        }

    # --------------------------------------------------------
    # DOWNSTREAM DIRECTION SUPPORT
    # --------------------------------------------------------

    if not downstream_action_supported(
        action
    ):
        print()
        print(
            "BRIDGE BLOCKED | "
            f"{action} is not yet supported "
            "by downstream API/worker"
        )

        return {
            "status":
                "UNSUPPORTED_DOWNSTREAM",

            "signal_id":
                signal_id,

            "action":
                action,
        }

    # --------------------------------------------------------
    # SECRET
    # --------------------------------------------------------

    if not secret:
        raise RuntimeError(
            "WEBHOOK_SECRET is not configured"
        )

    # --------------------------------------------------------
    # DEDUPE
    # --------------------------------------------------------

    if signal_already_sent(
        signal_id
    ):
        print(
            "BRIDGE SKIP | "
            f"duplicate signal_id="
            f"{signal_id}"
        )

        return {
            "status":
                "DUPLICATE",

            "signal_id":
                signal_id,
        }

    # --------------------------------------------------------
    # COOLDOWN
    # --------------------------------------------------------

    (
        cooldown_active,
        cooldown_record,
    ) = symbol_action_in_cooldown(
        symbol,
        action,
        (
            75
            if str(
                candidate.get(
                    "strategy",
                    "",
                )
            ).startswith(
                "scalp_"
            )
            else
            COOLDOWN_SECONDS
        ),
    )

    if cooldown_active:
        print(
            "BRIDGE SKIP | "
            f"{symbol} {action} "
            "is inside cooldown | "
            f"previous_signal="
            f"{cooldown_record['signal_id']}"
        )

        return {
            "status":
                "COOLDOWN",

            "signal_id":
                signal_id,
        }

    # --------------------------------------------------------
    # MODE-SPECIFIC ABSOLUTE SAFETY
    # --------------------------------------------------------

    health = (
        enforce_mode_api()
    )

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    status, parsed, raw = (
        send_payload(
            payload
        )
    )

    print()
    print(
        "API RESPONSE | "
        f"HTTP {status}"
    )

    print(
        json.dumps(
            parsed,
            indent=2,
            sort_keys=True,
        )
        if isinstance(
            parsed,
            (
                dict,
                list,
            ),
        )
        else str(
            parsed
        )
    )

    if (
        status
        >= 200
        and
        status
        <
        300
    ):
        #
        # PRELIVE gateway must explicitly echo
        # its safety mode. Do not trust a random
        # 2xx response on port 8001.
        #
        if BRIDGE_MODE == "PRELIVE":
            if not isinstance(
                parsed,
                dict,
            ):
                raise RuntimeError(
                    "PRELIVE API returned "
                    "non-object response"
                )

            if (
                parsed.get(
                    "prelive"
                )
                is not True
            ):
                raise RuntimeError(
                    "PRELIVE API response "
                    "missing prelive=true"
                )

            if (
                parsed.get(
                    "test_mode"
                )
                is not False
            ):
                raise RuntimeError(
                    "PRELIVE API response "
                    "must report test_mode=false"
                )

            if (
                parsed.get(
                    "worker_prelive_dry_run"
                )
                is not True
            ):
                raise RuntimeError(
                    "PRELIVE API response "
                    "does not confirm worker "
                    "PRELIVE_DRY_RUN=true"
                )

        record_sent_signal(
            payload,
            status,
            raw,
        )

        return {
            "status":
                (
                    "PRELIVE_SENT"
                    if BRIDGE_MODE
                    ==
                    "PRELIVE"
                    else
                    "TEST_SENT"
                ),

            "signal_id":
                signal_id,

            "http_status":
                status,

            "api_health":
                {
                    "mode":
                        health.get(
                            "mode"
                        ),

                    "test_mode":
                        health.get(
                            "test_mode"
                        ),

                    "live_ready":
                        health.get(
                            "live_ready"
                        ),
                },
        }

    return {
        "status":
            "API_REJECTED",

        "signal_id":
            signal_id,

        "http_status":
            status,
    }


# ============================================================
# RUN STRATEGY
# ============================================================


def collect_strategy_results():
    mode_file = (
        Path.home()
        /
        ".cache"
        /
        "tradingmax"
        /
        "strategy_mode.txt"
    )

    mode = (
        os.getenv(
            "STRATEGY_MODE",
            "AUTO",
        )
        .strip()
        .upper()
    )

    if mode_file.exists():
        try:
            configured = (
                mode_file
                .read_text(
                    encoding="utf-8"
                )
                .strip()
                .upper()
            )

            if configured:
                mode = configured

        except Exception as exc:
            print(
                "STRATEGY MODE WARNING | "
                f"{type(exc).__name__}: "
                f"{exc}",
                flush=True,
            )

    valid_modes = {
        "MOMENTUM",
        "SCALP",
        "AUTO",
        "BOTH",
    }

    if mode not in valid_modes:
        print(
            "STRATEGY MODE INVALID | "
            f"{mode} -> AUTO",
            flush=True,
        )

        mode = "AUTO"

    print()
    print(
        "=============================================================="
    )
    print(
        "TRADINGMAX MULTI-STRATEGY BRIDGE"
    )
    print(
        "=============================================================="
    )
    print(
        f"STRATEGY_MODE     = {mode}"
    )
    print(
        f"BRIDGE_DRY_RUN    = "
        f"{BRIDGE_DRY_RUN}"
    )
    print(
        f"BRIDGE_ALLOW_POST = "
        f"{BRIDGE_ALLOW_POST}"
    )
    print(
        f"MOMENTUM_COOLDOWN = "
        f"{COOLDOWN_SECONDS}s"
    )
    print(
        "SCALP_COOLDOWN    = 75s"
    )
    print()

    try:
        record_bridge_mode(
            BRIDGE_DRY_RUN,
            BRIDGE_ALLOW_POST,
        )
    except Exception:
        pass

    try:
        mark_phase(
            "SCANNING",
            analyzed=0,
            qualified=0,
            qualified_candidates=[],
            last_error=None,
        )
    except Exception:
        pass

    candidates = (
        strategy_engine
        .get_candidates()
    )

    print(
        "MULTI STRATEGY | "
        f"candidates={len(candidates)}"
    )

    if not candidates:
        try:
            record_analysis(
                []
            )
        except Exception:
            pass

        return []

    app = (
        strategy_engine
        .TradingMaxStrategy()
    )

    results = []
    mapping = None

    try:
        strategy_engine.connect_strategy(
            app
        )

        mapping = (
            strategy_engine.start_live(
                app,
                candidates,
            )
        )

        for index, candidate in enumerate(
            candidates
        ):
            quote_req_id = (
                mapping[
                    candidate[
                        "symbol"
                    ]
                ]
            )

            quote = (
                app.market_data.get(
                    quote_req_id,
                    {},
                )
            )

            momentum_result = None
            scalp_result = None

            if mode in {
                "MOMENTUM",
                "AUTO",
                "BOTH",
            }:
                momentum_bars = (
                    strategy_engine
                    .request_history(
                        app,
                        candidate,
                        20000
                        +
                        index,
                    )
                )

                momentum_result = (
                    strategy_engine
                    .analyze(
                        candidate,
                        momentum_bars,
                        quote,
                    )
                )

                if (
                    momentum_result
                    is not None
                ):
                    momentum_result = dict(
                        momentum_result
                    )

                    momentum_result.setdefault(
                        "strategy",
                        "tradingmax_multi_v1",
                    )

                    momentum_result.setdefault(
                        "timeframe",
                        "5m",
                    )

            if mode in {
                "SCALP",
                "AUTO",
                "BOTH",
            }:
                scalp_bars = (
                    scalp_strategy
                    .request_history(
                        app,
                        candidate,
                        40000
                        +
                        index,
                    )
                )

                scalp_result = (
                    scalp_strategy
                    .analyze(
                        candidate,
                        scalp_bars,
                        quote,
                    )
                )

            if mode == "MOMENTUM":
                if momentum_result is not None:
                    results.append(
                        momentum_result
                    )

            elif mode == "SCALP":
                if scalp_result is not None:
                    results.append(
                        scalp_result
                    )

            elif mode == "BOTH":
                if momentum_result is not None:
                    results.append(
                        momentum_result
                    )

                if scalp_result is not None:
                    results.append(
                        scalp_result
                    )

            else:
                #
                # AUTO:
                # Prefer a real qualified scalp setup.
                # Otherwise let the existing momentum
                # strategy remain authoritative.
                #
                if (
                    scalp_result is not None
                    and
                    scalp_result.get(
                        "qualified"
                    )
                ):
                    print(
                        "AUTO SELECT | "
                        f"{candidate['symbol']} | "
                        "SCALP | "
                        f"score="
                        f"{scalp_result.get('scalp_score')}",
                        flush=True,
                    )

                    results.append(
                        scalp_result
                    )

                elif (
                    momentum_result
                    is not None
                ):
                    print(
                        "AUTO SELECT | "
                        f"{candidate['symbol']} | "
                        "MOMENTUM",
                        flush=True,
                    )

                    results.append(
                        momentum_result
                    )

                elif (
                    scalp_result
                    is not None
                ):
                    results.append(
                        scalp_result
                    )

            time.sleep(
                0.15
            )

    finally:
        if mapping is not None:
            strategy_engine.stop_live(
                app,
                mapping,
            )

        if app.isConnected():
            app.disconnect()

            time.sleep(
                1
            )

    try:
        record_analysis(
            results
        )
    except Exception:
        pass

    return results



# ============================================================
# SELF TEST
# ============================================================

def build_self_test_candidate():
    return {
        "symbol":
            "TMXTEST",

        "instrument_type":
            "COMMON",

        "action":
            "BUY",

        "qualified":
            True,

        "hard_pass":
            True,

        "hard_failures":
            [],

        "support":
            4,

        "support_total":
            4,

        "entry":
            10.00,

        "stop":
            9.50,

        "target":
            11.00,

        "scanner_score":
            99.0,
    }


def run_self_test():
    print(
        "=============================================================="
    )

    print(
        "SIGNAL BRIDGE SELF TEST"
    )

    print(
        "NO NETWORK POST"
    )

    print(
        "=============================================================="
    )

    print(
        f"BRIDGE_MODE       = "
        f"{BRIDGE_MODE}"
    )

    print(
        f"BRIDGE_DRY_RUN    = "
        f"{BRIDGE_DRY_RUN}"
    )

    print(
        f"BRIDGE_ALLOW_POST = "
        f"{BRIDGE_ALLOW_POST}"
    )

    print(
        f"API               = "
        f"{API_BASE_URL}"
    )

    candidate = (
        build_self_test_candidate()
    )

    validate_candidate(
        candidate
    )

    payload = (
        build_payload(
            candidate,
            "SELF_TEST_SECRET",
        )
    )

    print_payload(
        payload
    )

    print()
    print(
        "SELF TEST PASS"
    )


# ============================================================
# HEALTH TEST
# ============================================================

def run_health_test():
    print(
        "=============================================================="
    )

    print(
        "SIGNAL BRIDGE API HEALTH TEST"
    )

    print(
        "=============================================================="
    )

    print(
        f"BRIDGE_MODE = "
        f"{BRIDGE_MODE}"
    )

    print(
        f"API         = "
        f"{API_BASE_URL}"
    )

    if BRIDGE_MODE == "DRY_RUN":
        raise RuntimeError(
            "Health test requires "
            "BRIDGE_MODE=TEST or PRELIVE"
        )

    health = (
        enforce_mode_api()
    )

    print()

    print(
        json.dumps(
            health,
            indent=2,
            sort_keys=True,
        )
    )

    print()
    print(
        "HEALTH TEST PASS"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    parser = (
        argparse.ArgumentParser(
            description=(
                "TradingMax strategy "
                "to API signal bridge"
            )
        )
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "Validate signal contract "
            "generation without scanning "
            "or posting"
        ),
    )

    parser.add_argument(
        "--health-test",
        action="store_true",
        help=(
            "Validate configured API mode "
            "without scanning or posting"
        ),
    )

    args = (
        parser.parse_args()
    )

    init_state_db()

    if args.self_test:
        run_self_test()
        return

    if args.health_test:
        run_health_test()
        return

    secret = (
        os.getenv(
            "WEBHOOK_SECRET",
            ""
        )
    )

    results = (
        collect_strategy_results()
    )

    for result in results:
        support_conditions = (
            result.get(
                "support_conditions",
                [],
            )
            or
            []
        )

        passed_support = [
            name
            for name, passed
            in support_conditions
            if passed
        ]

        failed_support = [
            name
            for name, passed
            in support_conditions
            if not passed
        ]

        print(
            "QUAL CHECK | "
            f"{result.get('symbol', '?'):<8} | "
            f"action={result.get('action')} | "
            f"qualified={result.get('qualified')} | "
            f"hard={result.get('hard_failures', [])} | "
            f"support={result.get('support')}/"
            f"{result.get('support_total')} | "
            f"passed={passed_support} | "
            f"failed={failed_support} | "
            f"entry={result.get('entry')} | "
            f"prev_high={result.get('previous_high')} | "
            f"prev_low={result.get('previous_low')} | "
            f"spread={result.get('spread_pct')} | "
            f"distance={result.get('trigger_distance_pct')}",
            flush=True,
        )

    qualified = [
        result
        for result
        in results
        if result.get(
            "qualified"
        )
    ]

    near_trigger = []

    for result in results:
        try:
            distance = float(
                result.get(
                    "trigger_distance_pct"
                )
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        hard_failures = set(
            result.get(
                "hard_failures",
                [],
            )
            or
            []
        )

        structural_failures = (
            hard_failures
            -
            {
                "BREAKOUT",
                "BREAKDOWN",
            }
        )

        if (
            0.0
            <
            distance
            <=
            1.0
            and
            not structural_failures
            and
            int(
                result.get(
                    "support",
                    0,
                )
                or
                0
            )
            >=
            3
        ):
            near_trigger.append(
                result
            )

    near_trigger.sort(
        key=lambda item: float(
            item.get(
                "trigger_distance_pct",
                999.0,
            )
        )
    )

    print()
    print(
        "NEAR TRIGGER : "
        f"{len(near_trigger)}"
    )

    for result in near_trigger:
        print(
            "NEAR TRIGGER | "
            f"{result.get('symbol', '?'):<8} | "
            f"action={result.get('action')} | "
            f"support={result.get('support')}/"
            f"{result.get('support_total')} | "
            f"distance="
            f"{float(result.get('trigger_distance_pct')):.3f}% | "
            f"entry={result.get('entry')} | "
            f"trigger="
            f"{result.get('previous_high') if result.get('action') == 'BUY' else result.get('previous_low')}",
            flush=True,
        )

    print()
    print(
        "=============================================================="
    )

    print(
        "BRIDGE SUMMARY"
    )

    print(
        "=============================================================="
    )

    print(
        f"Mode      : "
        f"{BRIDGE_MODE}"
    )

    print(
        f"Analyzed  : "
        f"{len(results)}"
    )

    print(
        f"Qualified : "
        f"{len(qualified)}"
    )

    if not qualified:
        print(
            "No qualified signals. "
            "Nothing to send."
        )

        return

    outcomes = []

    for candidate in qualified:
        try:
            outcome = (
                process_candidate(
                    candidate,
                    secret,
                )
            )

            outcomes.append(
                outcome
            )

        except Exception as exc:
            print(
                "BRIDGE ERROR | "
                f"{candidate.get('symbol')} | "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

    print()
    print(
        "=============================================================="
    )

    print(
        "BRIDGE OUTCOMES"
    )

    print(
        "=============================================================="
    )

    for outcome in outcomes:
        print(
            f"{outcome.get('signal_id')} | "
            f"{outcome.get('status')}"
        )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "\nInterrupted by user"
        )

        sys.exit(
            130
        )

    except Exception as exc:
        try:
            record_error(
                exc
            )
        except Exception as status_exc:
            print(
                "STRATEGY STATUS ERROR | "
                f"{type(status_exc).__name__}: "
                f"{status_exc}"
            )

        print(
            "SIGNAL BRIDGE FAILED | "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        sys.exit(
            1
        )
