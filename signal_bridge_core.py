import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from datetime import (
    datetime,
    timezone,
)

from pathlib import Path

import strategy_engine


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
            f"could not read "
            f"{ENV_FILE}: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        return

    for raw_line in lines:
        line = (
            raw_line.strip()
        )

        if (
            not line
            or
            line.startswith(
                "#"
            )
            or
            "=" not in line
        ):
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
            len(
                value
            ) >= 2
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

        os.environ.setdefault(
            key,
            value,
        )


load_dotenv_file()


API_BASE_URL = (
    os.getenv(
        "TRADINGMAX_API_URL",
        "http://127.0.0.1:8000",
    )
    .rstrip(
        "/"
    )
)

WEBHOOK_PATH = (
    "/webhook/tradingview"
)

HEALTH_PATH = (
    "/health"
)

STRATEGY_NAME = (
    "tradingmax_multi_v1"
)

TIMEFRAME = "5m"

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

BRIDGE_DRY_RUN = (
    parse_bool(
        os.getenv(
            "BRIDGE_DRY_RUN"
        ),
        True,
    )
)

BRIDGE_ALLOW_POST = (
    parse_bool(
        os.getenv(
            "BRIDGE_ALLOW_POST"
        ),
        False,
    )
)


def db_connect():
    conn = (
        sqlite3.connect(
            STATE_DB,
            timeout=10,
        )
    )

    conn.row_factory = (
        sqlite3.Row
    )

    return conn


def init_state_db():
    conn = (
        db_connect()
    )

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


def now_iso():
    return (
        datetime.now(
            timezone.utc
        )
        .isoformat()
    )


def build_signal_id(
    candidate,
):
    symbol = (
        candidate[
            "symbol"
        ]
        .strip()
        .upper()
    )

    action = (
        candidate[
            "action"
        ]
        .strip()
        .upper()
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

    bucket = (
        int(
            time.time()
        )
        //
        300
        *
        300
    )

    raw = (
        f"{STRATEGY_NAME}|"
        f"{TIMEFRAME}|"
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
            "%Y%m%dT%H%M"
        )
    )

    return (
        f"tmx-"
        f"{timestamp}-"
        f"{symbol}-"
        f"{action}-"
        f"{digest}"
    )


def build_payload(
    candidate,
    secret,
):
    return {
        "secret":
            secret,

        "signal_id":
            build_signal_id(
                candidate
            ),

        "strategy":
            STRATEGY_NAME,

        "timeframe":
            TIMEFRAME,

        "signal_time":
            now_iso(),

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
            "Candidate "
            "missing fields: "
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
            "Candidate is not "
            "qualified"
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
            f"Unsupported action: "
            f"{action}"
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
                "Invalid BUY "
                "structure: "
                "stop < entry < "
                "target required"
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
                "Invalid SELL "
                "structure: "
                "target < entry < "
                "stop required"
            )


def signal_already_sent(
    signal_id,
):
    conn = (
        db_connect()
    )

    try:
        row = (
            conn.execute(
                """
                SELECT signal_id
                FROM sent_signals
                WHERE signal_id = ?
                """,
                (
                    signal_id,
                ),
            )
            .fetchone()
        )

        return (
            row is not None
        )

    finally:
        conn.close()


def symbol_action_in_cooldown(
    symbol,
    action,
):
    cutoff = (
        time.time()
        -
        COOLDOWN_SECONDS
    )

    conn = (
        db_connect()
    )

    try:
        row = (
            conn.execute(
                """
                SELECT
                    signal_id,
                    sent_at
                FROM sent_signals
                WHERE symbol = ?
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
            )
            .fetchone()
        )

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
    conn = (
        db_connect()
    )

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


def safe_payload(
    payload,
):
    result = dict(
        payload
    )

    if "secret" in result:
        result[
            "secret"
        ] = (
            "***REDACTED***"
        )

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
        body = (
            json.dumps(
                payload
            )
            .encode(
                "utf-8"
            )
        )

        headers[
            "Content-Type"
        ] = (
            "application/json"
        )

    request = (
        urllib.request.Request(
            url=url,
            data=body,
            headers=headers,
            method=method,
        )
    )

    try:
        with (
            urllib.request.urlopen(
                request,
                timeout=
                    HTTP_TIMEOUT_SECONDS,
            )
        ) as response:

            raw = (
                response
                .read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

            try:
                parsed = (
                    json.loads(
                        raw
                    )
                )

            except Exception:
                parsed = raw

            return (
                response.status,
                parsed,
                raw,
            )

    except urllib.error.HTTPError as exc:
        raw = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        try:
            parsed = (
                json.loads(
                    raw
                )
            )

        except Exception:
            parsed = raw

        return (
            exc.code,
            parsed,
            raw,
        )


def get_api_health():
    (
        status,
        parsed,
        raw,
    ) = (
        http_json_request(
            "GET",
            API_BASE_URL
            +
            HEALTH_PATH,
        )
    )

    if status != 200:
        raise RuntimeError(
            "TradingMax health "
            "check failed: "
            f"HTTP {status} | "
            f"{raw}"
        )

    if not isinstance(
        parsed,
        dict,
    ):
        raise RuntimeError(
            "TradingMax health "
            "response is not "
            "JSON object"
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
        f"status="
        f"{health.get('status')} | "
        f"live_trading="
        f"{health.get('live_trading')} | "
        f"test_mode="
        f"{test_mode}"
    )

    if test_mode is not True:
        raise RuntimeError(
            "Bridge refuses POST "
            "because TradingMax API "
            "is not in "
            "WEBHOOK_TEST_MODE"
        )

    return health


def send_payload(
    payload,
):
    return (
        http_json_request(
            "POST",
            API_BASE_URL
            +
            WEBHOOK_PATH,
            payload,
        )
    )


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
        "=" * 62
    )

    print(
        "QUALIFIED CANDIDATE"
    )

    print(
        "=" * 62
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
        f"Hot score    : "
        f"{candidate.get('hot_score')}"
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

    if BRIDGE_DRY_RUN:
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

    if not secret:
        raise RuntimeError(
            "WEBHOOK_SECRET "
            "is not configured"
        )

    if signal_already_sent(
        signal_id
    ):
        print(
            "BRIDGE SKIP | "
            f"duplicate "
            f"signal_id="
            f"{signal_id}"
        )

        return {
            "status":
                "DUPLICATE",
            "signal_id":
                signal_id,
        }

    (
        cooldown_active,
        cooldown_record,
    ) = (
        symbol_action_in_cooldown(
            symbol,
            action,
        )
    )

    if cooldown_active:
        print(
            "BRIDGE SKIP | "
            f"{symbol} "
            f"{action} "
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

    enforce_test_api()

    (
        status,
        parsed,
        raw,
    ) = (
        send_payload(
            payload
        )
    )

    print()

    print(
        f"API RESPONSE | "
        f"HTTP {status}"
    )

    if isinstance(
        parsed,
        (
            dict,
            list,
        ),
    ):
        print(
            json.dumps(
                parsed,
                indent=2,
                sort_keys=True,
            )
        )

    else:
        print(
            str(
                parsed
            )
        )

    if (
        200
        <=
        status
        <
        300
    ):
        record_sent_signal(
            payload,
            status,
            raw,
        )

        return {
            "status":
                "SENT",
            "signal_id":
                signal_id,
            "http_status":
                status,
        }

    return {
        "status":
            "API_REJECTED",
        "signal_id":
            signal_id,
        "http_status":
            status,
    }


def collect_strategy_results():
    print()

    print(
        "=" * 62
    )

    print(
        "TRADINGMAX SIGNAL BRIDGE"
    )

    print(
        "=" * 62
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
        f"COOLDOWN_SECONDS  = "
        f"{COOLDOWN_SECONDS}"
    )

    print(
        f"API               = "
        f"{API_BASE_URL}"
    )

    print()

    candidates = (
        strategy_engine
        .get_candidates()
    )

    print()

    print(
        f"Universe candidates: "
        f"{len(candidates)}"
    )

    if not candidates:
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

        hot_candidates = (
            strategy_engine
            .select_hot_pool(
                candidates,
                app,
                mapping,
            )
        )

        print()

        print(
            f"Hot candidates: "
            f"{len(hot_candidates)}"
        )

        if not hot_candidates:
            return []

        for index, candidate in enumerate(
            hot_candidates
        ):
            history_req_id = (
                20000
                +
                index
            )

            bars = (
                strategy_engine
                .request_history(
                    app,
                    candidate,
                    history_req_id,
                )
            )

            quote = (
                app.market_data.get(
                    mapping[
                        candidate[
                            "symbol"
                        ]
                    ],
                    {},
                )
            )

            print(
                "BRIDGE ANALYZE | "
                f"{candidate['symbol']} | "
                f"type="
                f"{candidate.get('instrument_type')} | "
                f"bucket="
                f"{candidate.get('selection_bucket')} | "
                f"hot="
                f"{strategy_engine.fmt(candidate.get('hot_score'), 2)} | "
                f"proxyDist="
                f"{strategy_engine.fmt(candidate.get('hot_proxy_distance_pct'), 3)}% | "
                f"bars={len(bars)} | "
                f"bid="
                f"{quote.get('bid')} | "
                f"ask="
                f"{quote.get('ask')} | "
                f"last="
                f"{quote.get('last')} | "
                f"dataType="
                f"{quote.get('market_data_type')}"
            )

            result = (
                strategy_engine.analyze(
                    candidate,
                    bars,
                    quote,
                )
            )

            if result is not None:
                results.append(
                    result
                )

            time.sleep(
                0.20
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

    return results


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
        "hot_score":
            99.0,
    }


def run_self_test():
    print(
        "=" * 62
    )

    print(
        "SIGNAL BRIDGE "
        "SELF TEST"
    )

    print(
        "NO NETWORK POST"
    )

    print(
        "=" * 62
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
        action=
            "store_true",
        help=(
            "Validate signal "
            "contract generation "
            "without scanning "
            "or posting"
        ),
    )

    args = (
        parser.parse_args()
    )

    init_state_db()

    if args.self_test:
        run_self_test()
        return

    secret = (
        os.getenv(
            "WEBHOOK_SECRET",
            "",
        )
    )

    results = (
        collect_strategy_results()
    )

    qualified = [
        result
        for result in results
        if result.get(
            "qualified"
        )
    ]

    print()

    print(
        "=" * 62
    )

    print(
        "BRIDGE SUMMARY"
    )

    print(
        "=" * 62
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
            "No qualified "
            "signals. "
            "Nothing to send."
        )

        return

    outcomes = []

    for candidate in qualified:
        try:
            outcomes.append(
                process_candidate(
                    candidate,
                    secret,
                )
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
        "=" * 62
    )

    print(
        "BRIDGE OUTCOMES"
    )

    print(
        "=" * 62
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
        print(
            "SIGNAL BRIDGE FAILED | "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        sys.exit(
            1
        )
