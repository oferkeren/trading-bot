import base64
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(
    __file__
).resolve().parent


load_dotenv(
    BASE_DIR
    /
    ".env"
)


import ai_gate
import early_momentum
import signal_bridge


# ============================================================
# CONFIG
# ============================================================

TRADINGMAX_API_URL = (
    os.getenv(
        "TRADINGMAX_API_URL",
        "http://127.0.0.1:8000",
    )
    .strip()
    .rstrip("/")
)


DASHBOARD_USER = (
    os.getenv(
        "DASHBOARD_USER",
        "",
    )
    .strip()
)


DASHBOARD_PASSWORD = (
    os.getenv(
        "DASHBOARD_PASSWORD",
        "",
    )
)


AI_BRIDGE_ENABLED = (
    os.getenv(
        "AI_BRIDGE_ENABLED",
        "true",
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


AI_BRIDGE_FAIL_CLOSED = (
    os.getenv(
        "AI_BRIDGE_FAIL_CLOSED",
        "true",
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


AI_BRIDGE_HTTP_TIMEOUT = float(
    os.getenv(
        "AI_BRIDGE_HTTP_TIMEOUT",
        "8",
    )
)


EARLY_MOMENTUM_ENABLED = (
    os.getenv(
        "EARLY_MOMENTUM_ENABLED",
        "true",
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


# ============================================================
# ORIGINAL FUNCTIONS
# ============================================================

_original_process_candidate = (
    signal_bridge.process_candidate
)


_original_build_payload = (
    signal_bridge.build_payload
)


_original_strategy_analyze = (
    signal_bridge
    .strategy_engine
    .analyze
)


# ============================================================
# HTTP BASIC AUTH
# ============================================================

def basic_auth_header():
    if (
        not DASHBOARD_USER
        or
        not DASHBOARD_PASSWORD
    ):
        raise RuntimeError(
            "DASHBOARD_USER / DASHBOARD_PASSWORD "
            "not configured"
        )


    raw = (
        f"{DASHBOARD_USER}:"
        f"{DASHBOARD_PASSWORD}"
    )


    encoded = (
        base64.b64encode(
            raw.encode(
                "utf-8"
            )
        )
        .decode(
            "ascii"
        )
    )


    return (
        "Basic "
        +
        encoded
    )


# ============================================================
# EARLY MOMENTUM OBSERVER
# ============================================================

def analyze_with_early_momentum(
    candidate,
    bars,
    quote,
):
    result = (
        _original_strategy_analyze(
            candidate,
            bars,
            quote,
        )
    )


    if result is None:
        return None


    if not EARLY_MOMENTUM_ENABLED:
        return result


    symbol = (
        str(
            result.get(
                "symbol",
                "",
            )
        )
        .strip()
        .upper()
    )


    try:
        momentum = (
            early_momentum
            .observe_result(
                result
            )
        )


        #
        # IMPORTANT:
        #
        # Observational only.
        #
        # We attach the score to the candidate result,
        # but DO NOT modify:
        #
        #   qualified
        #   hard_pass
        #   support
        #   entry
        #   stop
        #   target
        #
        # The existing strategy remains authoritative.
        #
        enriched = dict(
            result
        )


        enriched[
            "early_score"
        ] = (
            momentum[
                "early_score"
            ]
        )


        enriched[
            "early_state"
        ] = (
            momentum[
                "early_state"
            ]
        )


        enriched[
            "early_history_windows"
        ] = (
            momentum[
                "history_windows"
            ]
        )


        enriched[
            "early_components"
        ] = (
            momentum[
                "components"
            ]
        )


        return enriched


    except Exception as exc:
        #
        # This layer is observational.
        #
        # Failure to calculate early momentum must NOT
        # interfere with the existing deterministic
        # strategy.
        #
        print(
            "EARLY MOMENTUM ERROR | "
            f"{symbol} | "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )


        return result


# ============================================================
# RISK PREVIEW
# ============================================================

def get_risk_preview(
    candidate,
):
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


    query = (
        urllib.parse.urlencode(
            {
                "entry":
                    entry,

                "stop":
                    stop,
            }
        )
    )


    url = (
        TRADINGMAX_API_URL
        +
        "/risk-preview?"
        +
        query
    )


    request = urllib.request.Request(
        url=
            url,

        method=
            "GET",

        headers={
            "Accept":
                "application/json",

            "Authorization":
                basic_auth_header(),
        },
    )


    try:
        with urllib.request.urlopen(
            request,
            timeout=
                AI_BRIDGE_HTTP_TIMEOUT,
        ) as response:

            raw = (
                response
                .read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )


            if response.status != 200:
                raise RuntimeError(
                    f"Risk preview HTTP "
                    f"{response.status}: "
                    f"{raw}"
                )


            data = json.loads(
                raw
            )


    except urllib.error.HTTPError as exc:
        raw = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        raise RuntimeError(
            f"Risk preview HTTP "
            f"{exc.code}: "
            f"{raw}"
        ) from exc


    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Risk preview unavailable: "
            f"{exc}"
        ) from exc


    if not isinstance(
        data,
        dict,
    ):
        raise RuntimeError(
            "Risk preview response is not "
            "a JSON object"
        )


    quantity = (
        data.get(
            "quantity"
        )
    )


    if quantity is None:
        raise RuntimeError(
            "Risk preview returned no quantity"
        )


    quantity = int(
        quantity
    )


    if quantity <= 0:
        raise RuntimeError(
            "Risk preview returned invalid quantity"
        )


    print(
        "AI RISK PREVIEW | "
        f"{candidate.get('symbol')} | "
        f"auto_quantity={quantity} | "
        f"sizing_mode="
        f"{data.get('sizing_mode')}",
        flush=True,
    )


    return data


# ============================================================
# REDUCED QUANTITY
# ============================================================

def calculate_reduced_quantity(
    base_quantity,
    multiplier,
):
    base_quantity = int(
        base_quantity
    )

    multiplier = float(
        multiplier
    )


    if base_quantity <= 0:
        raise ValueError(
            "base_quantity must be positive"
        )


    if multiplier not in {
        0.25,
        0.50,
        0.75,
    }:
        raise ValueError(
            "Invalid AI REDUCE multiplier: "
            f"{multiplier}"
        )


    final_quantity = math.floor(
        base_quantity
        *
        multiplier
    )


    if final_quantity < 1:
        return 0


    if final_quantity > base_quantity:
        raise RuntimeError(
            "AI sizing attempted to increase "
            "position size"
        )


    return final_quantity


# ============================================================
# PAYLOAD WRAPPER
# ============================================================

def build_payload(
    candidate,
    secret,
):
    payload = (
        _original_build_payload(
            candidate,
            secret,
        )
    )


    ai_quantity = (
        candidate.get(
            "_ai_final_quantity"
        )
    )


    if ai_quantity is not None:
        ai_quantity = int(
            ai_quantity
        )


        if ai_quantity <= 0:
            raise RuntimeError(
                "Invalid AI final quantity"
            )


        payload[
            "quantity"
        ] = ai_quantity


    return payload


# ============================================================
# AI PROCESS CANDIDATE
# ============================================================

def process_candidate(
    candidate,
    secret,
):
    signal_bridge.validate_candidate(
        candidate
    )


    symbol = (
        str(
            candidate.get(
                "symbol",
                "",
            )
        )
        .strip()
        .upper()
    )


    action = (
        str(
            candidate.get(
                "action",
                "",
            )
        )
        .strip()
        .upper()
    )


    print()
    print(
        "=============================================================="
    )

    print(
        "AI SIGNAL GATE"
    )

    print(
        "=============================================================="
    )

    print(
        f"Symbol : {symbol}"
    )

    print(
        f"Action : {action}"
    )


    if candidate.get(
        "early_score"
    ) is not None:

        print(
            "Early  : "
            f"{candidate.get('early_score'):+.2f} | "
            f"{candidate.get('early_state')}",
            flush=True,
        )


    # ========================================================
    # LONG ONLY FOR NOW
    # ========================================================

    if action != "BUY":
        print(
            "AI BRIDGE BLOCK | "
            f"{symbol} | "
            "Only BUY is currently enabled",
            flush=True,
        )


        return {
            "status":
                "AI_BLOCKED",

            "symbol":
                symbol,

            "reason":
                "UNSUPPORTED_ACTION",

            "ai_decision":
                "BLOCK",
        }


    # ========================================================
    # AI DISABLED
    # ========================================================

    if not AI_BRIDGE_ENABLED:
        if AI_BRIDGE_FAIL_CLOSED:
            print(
                "AI BRIDGE BLOCK | "
                "AI_BRIDGE_ENABLED=false",
                flush=True,
            )


            return {
                "status":
                    "AI_BLOCKED",

                "symbol":
                    symbol,

                "reason":
                    "AI_BRIDGE_DISABLED",

                "ai_decision":
                    "BLOCK",
            }


        return (
            _original_process_candidate(
                candidate,
                secret,
            )
        )


    # ========================================================
    # AI GATE
    # ========================================================

    try:
        ai_result = (
            ai_gate
            .evaluate_trade_candidate(
                candidate
            )
        )


    except Exception as exc:
        print(
            "AI BRIDGE ERROR | "
            f"{symbol} | "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )


        if AI_BRIDGE_FAIL_CLOSED:
            return {
                "status":
                    "AI_BLOCKED",

                "symbol":
                    symbol,

                "reason":
                    "AI_GATE_EXCEPTION",

                "error_type":
                    type(
                        exc
                    ).__name__,

                "ai_decision":
                    "BLOCK",
            }


        return (
            _original_process_candidate(
                candidate,
                secret,
            )
        )


    decision = (
        str(
            ai_result.get(
                "status",
                "",
            )
        )
        .strip()
        .upper()
    )


    multiplier = float(
        ai_result.get(
            "position_multiplier",
            0.0,
        )
    )


    print(
        "AI BRIDGE DECISION | "
        f"{symbol} | "
        f"decision={decision} | "
        f"multiplier={multiplier:.2f}",
        flush=True,
    )


    # ========================================================
    # BLOCK
    # ========================================================

    if decision == "BLOCK":
        return {
            "status":
                "AI_BLOCKED",

            "symbol":
                symbol,

            "reason":
                (
                    ai_result.get(
                        "reason"
                    )
                    or
                    "AI_DECISION_BLOCK"
                ),

            "ai_decision":
                decision,

            "ai_multiplier":
                multiplier,

            "ai_result":
                ai_result,
        }


    # ========================================================
    # PASS
    # ========================================================

    if decision == "PASS":
        if multiplier != 1.0:
            print(
                "AI BRIDGE BLOCK | "
                f"{symbol} | "
                "PASS returned multiplier != 1.0",
                flush=True,
            )


            return {
                "status":
                    "AI_BLOCKED",

                "symbol":
                    symbol,

                "reason":
                    "INVALID_PASS_MULTIPLIER",

                "ai_decision":
                    decision,

                "ai_multiplier":
                    multiplier,
            }


        outcome = (
            _original_process_candidate(
                candidate,
                secret,
            )
        )


        outcome[
            "ai_decision"
        ] = decision

        outcome[
            "ai_multiplier"
        ] = 1.0

        outcome[
            "ai_result"
        ] = ai_result


        if candidate.get(
            "early_score"
        ) is not None:

            outcome[
                "early_score"
            ] = candidate.get(
                "early_score"
            )

            outcome[
                "early_state"
            ] = candidate.get(
                "early_state"
            )


        return outcome


    # ========================================================
    # REDUCE
    # ========================================================

    if decision == "REDUCE":
        if multiplier not in {
            0.25,
            0.50,
            0.75,
        }:
            return {
                "status":
                    "AI_BLOCKED",

                "symbol":
                    symbol,

                "reason":
                    "INVALID_REDUCE_MULTIPLIER",

                "ai_decision":
                    decision,

                "ai_multiplier":
                    multiplier,
            }


        try:
            preview = (
                get_risk_preview(
                    candidate
                )
            )


            base_quantity = int(
                preview[
                    "quantity"
                ]
            )


            final_quantity = (
                calculate_reduced_quantity(
                    base_quantity,
                    multiplier,
                )
            )


        except Exception as exc:
            print(
                "AI REDUCE ERROR | "
                f"{symbol} | "
                f"{type(exc).__name__}: "
                f"{exc}",
                flush=True,
            )


            return {
                "status":
                    "AI_BLOCKED",

                "symbol":
                    symbol,

                "reason":
                    "AI_REDUCE_SIZING_ERROR",

                "error_type":
                    type(
                        exc
                    ).__name__,

                "ai_decision":
                    decision,

                "ai_multiplier":
                    multiplier,
            }


        print(
            "AI POSITION REDUCTION | "
            f"{symbol} | "
            f"base={base_quantity} | "
            f"multiplier={multiplier:.2f} | "
            f"final={final_quantity}",
            flush=True,
        )


        if final_quantity < 1:
            print(
                "AI BRIDGE BLOCK | "
                f"{symbol} | "
                "reduced quantity below 1",
                flush=True,
            )


            return {
                "status":
                    "AI_BLOCKED",

                "symbol":
                    symbol,

                "reason":
                    "AI_REDUCED_QUANTITY_ZERO",

                "ai_decision":
                    decision,

                "ai_multiplier":
                    multiplier,

                "base_quantity":
                    base_quantity,

                "final_quantity":
                    0,
            }


        candidate = dict(
            candidate
        )


        candidate[
            "_ai_final_quantity"
        ] = (
            final_quantity
        )


        candidate[
            "_ai_base_quantity"
        ] = (
            base_quantity
        )


        candidate[
            "_ai_multiplier"
        ] = (
            multiplier
        )


        outcome = (
            _original_process_candidate(
                candidate,
                secret,
            )
        )


        outcome[
            "ai_decision"
        ] = decision

        outcome[
            "ai_multiplier"
        ] = multiplier

        outcome[
            "base_quantity"
        ] = base_quantity

        outcome[
            "final_quantity"
        ] = final_quantity

        outcome[
            "ai_result"
        ] = ai_result


        if candidate.get(
            "early_score"
        ) is not None:

            outcome[
                "early_score"
            ] = candidate.get(
                "early_score"
            )

            outcome[
                "early_state"
            ] = candidate.get(
                "early_state"
            )


        return outcome


    # ========================================================
    # UNKNOWN
    # ========================================================

    return {
        "status":
            "AI_BLOCKED",

        "symbol":
            symbol,

        "reason":
            "UNKNOWN_AI_DECISION",

        "ai_decision":
            decision,

        "ai_multiplier":
            multiplier,
    }


# ============================================================
# INSTALL WRAPPER
# ============================================================

def install_ai_bridge():
    signal_bridge.build_payload = (
        build_payload
    )


    signal_bridge.process_candidate = (
        process_candidate
    )


    #
    # Observe every real strategy analysis.
    #
    # This does NOT change qualification.
    #
    signal_bridge.strategy_engine.analyze = (
        analyze_with_early_momentum
    )


# ============================================================
# SYNTHETIC SELF TEST
# ============================================================

def run_ai_bridge_self_test():
    print(
        "=============================================================="
    )

    print(
        "AI SIGNAL BRIDGE SYNTHETIC SELF TEST"
    )

    print(
        "NO ORDER / NO LIVE POST"
    )

    print(
        "=============================================================="
    )


    cases = [
        {
            "name":
                "PASS",

            "decision":
                {
                    "status":
                        "PASS",

                    "position_multiplier":
                        1.0,
                },
        },

        {
            "name":
                "BLOCK",

            "decision":
                {
                    "status":
                        "BLOCK",

                    "position_multiplier":
                        0.0,

                    "reason":
                        "SELF_TEST_BLOCK",
                },
        },

        {
            "name":
                "REDUCE",

            "decision":
                {
                    "status":
                        "REDUCE",

                    "position_multiplier":
                        0.50,
                },
        },
    ]


    candidate = {
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

        "early_score":
            22.5,

        "early_state":
            "BUILDING",
    }


    original_evaluator = (
        ai_gate.evaluate_trade_candidate
    )


    original_preview = (
        globals()[
            "get_risk_preview"
        ]
    )


    try:
        for case in cases:
            print()
            print(
                "===== CASE "
                f"{case['name']} ====="
            )


            def fake_evaluator(
                unused_candidate,
                result=
                    case[
                        "decision"
                    ],
            ):
                return dict(
                    result
                )


            ai_gate.evaluate_trade_candidate = (
                fake_evaluator
            )


            if (
                case[
                    "name"
                ]
                ==
                "REDUCE"
            ):
                globals()[
                    "get_risk_preview"
                ] = (
                    lambda unused_candidate: {
                        "quantity":
                            20,

                        "sizing_mode":
                            "AUTO_RISK",
                    }
                )

            else:
                globals()[
                    "get_risk_preview"
                ] = (
                    original_preview
                )


            outcome = (
                process_candidate(
                    dict(
                        candidate
                    ),
                    "SELF_TEST_SECRET",
                )
            )


            print(
                "OUTCOME="
                +
                json.dumps(
                    outcome,
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            )


            if (
                case[
                    "name"
                ]
                ==
                "PASS"
                and
                outcome.get(
                    "ai_decision"
                )
                !=
                "PASS"
            ):
                raise RuntimeError(
                    "PASS self-test failed"
                )


            if (
                case[
                    "name"
                ]
                ==
                "BLOCK"
                and
                outcome.get(
                    "status"
                )
                !=
                "AI_BLOCKED"
            ):
                raise RuntimeError(
                    "BLOCK self-test failed"
                )


            if (
                case[
                    "name"
                ]
                ==
                "REDUCE"
            ):
                if (
                    outcome.get(
                        "final_quantity"
                    )
                    !=
                    10
                ):
                    raise RuntimeError(
                        "REDUCE quantity self-test failed"
                    )


        print()
        print(
            "AI_SIGNAL_BRIDGE_SELF_TEST=PASS"
        )


    finally:
        ai_gate.evaluate_trade_candidate = (
            original_evaluator
        )

        globals()[
            "get_risk_preview"
        ] = (
            original_preview
        )


# ============================================================
# MAIN
# ============================================================

def main():
    install_ai_bridge()


    if (
        "--ai-self-test"
        in
        sys.argv
    ):
        run_ai_bridge_self_test()

        return


    signal_bridge.main()


if __name__ == "__main__":
    main()
